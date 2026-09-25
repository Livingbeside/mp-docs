"""Инструкция для продавцов Uzum Market — seller.uzum.uz/manual, сайт на VuePress 1.

Страницы закрыты «проверкой браузера» Яндекса: requests вместо статьи получает
редирект на /tmgrdfrend/showcaptchafast и заглушку «Верификация». Camoufox со
своего IP проходит её за ~13 с, дальше страницы открываются меньше чем за секунду —
поэтому браузер один на весь обход. Резидентный канал не нужен (проверено 2026-09-25).

Список страниц берём не из меню, а из siteData VuePress в app.*.js: там все
страницы с заголовками разделов, включая скрытые (инструкция по брифу ЦПТ лежит
в /uz/, хотя написана по-русски). Скрипты антибот не закрывает, но имя app.*.js
с хешем сборки видно только в HTML страницы — отсюда порядок: корень, скрипт, статьи.

Во время выкатки узлы за балансировщиком отдают разные сборки (видели 2026-09-25:
старая и новая вперемешку полчаса), а скрипт чужой сборки узел не знает — 404.
Поэтому скрипты берём с повтором, а если в обходе встретилось две сборки —
дожимаем все страницы до самой свежей (по Last-Modified её app.*.js). Иначе
зеркало собралось бы из двух версий и назавтра «поменялось» бы обратно.

Узбекская версия (/uz/) — перевод тех же страниц. В зеркало её не берём, иначе
каждая правка приходила бы дважды.
"""

from __future__ import annotations

import asyncio
import json
import re
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin

from common import MIRROR, log, now_iso, slug, write_doc

ROOT = "https://seller.uzum.uz/manual/"
BASE = "uzum/kb"
CONTENT = "div.theme-default-content"
APP_JS = re.compile(r'src="(/manual/assets/js/app\.[0-9a-f]+\.js)"')
# Страница в siteData: {title:"…",frontmatter:{…},regularPath:"…",…,headers:[…]}
PAGE_RE = re.compile(
    r'\{(?:title:"(?P<title>(?:[^"\\]|\\.)*)",)?frontmatter:\{.*?\},'
    r'regularPath:"(?P<path>[^"]*)"[^{}]*?(?:headers:\[(?P<headers>.*?)\])?\}(?=[,\]])')
HEADER_RE = re.compile(r'level:(\d),title:"((?:[^"\\]|\\.)*)"')
LOCALE_RE = re.compile(r'"(/[^"/]+/)":\{selectText:')
CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def _js_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def site_pages(app_js: str) -> list[dict]:
    """Все страницы сборки: путь, заголовок и заголовки разделов [(уровень, текст)]."""
    pages = []
    for m in PAGE_RE.finditer(app_js):
        heads = [(int(lvl), _js_str(t)) for lvl, t in HEADER_RE.findall(m.group("headers") or "")]
        pages.append({"path": m.group("path"), "title": _js_str(m.group("title") or ""),
                      "headers": heads})
    return pages


def _russian(text: str) -> bool:
    letters = [c for c in text.lower() if c.isalpha()]
    return bool(letters) and sum(c in CYRILLIC for c in letters) / len(letters) > 0.5


def wanted(page: dict, locales: tuple[str, ...]) -> bool:
    text = " ".join([page["title"], *(t for _lvl, t in page["headers"])])
    if not text.strip():
        return False                  # ни заголовка, ни разделов — пустая заглушка (/0/)
    return not page["path"].startswith(locales) or _russian(text)


def _rel_for(path: str) -> str:
    parts = [slug(p) for p in path.strip("/").split("/") if p]
    return f"{BASE}/{'/'.join(parts) or 'intro'}.md"


def _readable(url: str) -> str:
    """Свои якоря и имена картинок — кириллицей, а не %D0%…. Переводы строк
    (бывают прямо в href) выкидываем, как это делает браузер, а пробелы и скобки
    кодируем в любых ссылках — иначе ломается markdown-ссылка."""
    url = re.sub(r"[\t\r\n]", "", url).strip()
    if url.startswith("https://seller.uzum.uz/"):
        url = unquote(url)
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _by_class(el, tag: str, cls: str) -> list:
    return el.xpath(f'.//{tag}[contains(concat(" ", normalize-space(@class), " "), " {cls} ")]')


def page_markdown(html: str, url: str) -> str:
    """Markdown статьи; пустая строка, если статьи в странице нет."""
    from common import proxyweb_scripts
    proxyweb_scripts()
    import extract
    from lxml import html as LH

    doc = LH.fromstring(html)
    doc.make_links_absolute(url)
    found = _by_class(doc, "div", "theme-default-content")
    if not found:
        return ""
    root = found[0]
    # Служебное VuePress: «#» у заголовков и «(opens new window)» у внешних ссылок.
    for el in _by_class(root, "a", "header-anchor") + _by_class(root, "span", "sr-only"):
        el.drop_tree()
    # Врезки «::: note/warning» — цитатой, иначе они сливаются с текстом вокруг.
    for el in _by_class(root, "div", "custom-block"):
        el.tag = "blockquote"
    for el, attr in [(a, "href") for a in root.iter("a")] + [(i, "src") for i in root.iter("img")]:
        if el.get(attr):
            el.set(attr, _readable(el.get(attr)))

    md = "\n\n".join(extract._blocks_children(root, 0))
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _first_line(md: str) -> str:
    """Заголовок страницы без h1 (бриф ЦПТ начинается с жирной строки)."""
    for line in md.splitlines():
        text = re.sub(r"[#*_>`]+", "", line).strip()
        if text:
            return text[:120]
    return ""


def _build(html: str | None) -> str | None:
    """Сборка, из которой пришла страница, — путь её app.*.js."""
    m = APP_JS.search(html or "")
    return m.group(1) if m else None


async def _crawl(wait_ms: int) -> tuple[list[dict], dict[str, str]]:
    """Один браузер на весь обход. -> (выбранные страницы, {путь: html})."""
    from common import proxyweb_scripts
    proxyweb_scripts()
    import browser as pw_browser
    from camoufox.async_api import AsyncCamoufox

    with pw_browser.browser_lock():
        async with AsyncCamoufox(headless=True, geoip=True, locale="ru-RU") as br:
            page, ctx, _sess, _state = await pw_browser._open_page(
                br, session=None, with_images=False)

            async def open_page(url: str, wait: int = 20_000) -> str | None:
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                    if resp is not None and resp.status == 404:
                        return None           # у узла со старой сборкой новой страницы нет
                    await page.wait_for_selector(CONTENT, timeout=wait)
                    return await page.content()
                except Exception as exc:
                    log(f"Uzum KB: {url} — {type(exc).__name__}: {str(exc)[:160]}")
                    return None

            async def asset(build: str, method: str = "GET"):
                """Скрипт сборки. Узел с другой сборкой его не знает (404) — повторяем:
                запрос с новым параметром балансировщик отправит на другой узел."""
                for i in range(8):
                    resp = await ctx.request.fetch(f"{urljoin(ROOT, build)}?_={i}",
                                                   method=method, timeout=45_000)
                    if resp.ok:
                        return resp
                    await asyncio.sleep(0.5)
                return None

            async def listed(build: str) -> list[dict]:
                resp = await asset(build)
                app_js = await resp.text() if resp else ""
                locales = tuple(LOCALE_RE.findall(app_js)) or ("/uz/",)
                everything = site_pages(app_js)
                pages = [p for p in everything if wanted(p, locales)]
                if not pages:
                    raise RuntimeError(f"siteData сборки {build} не разобрался: страниц "
                                       f"{len(everything)}, app.js {len(app_js)} байт")
                log(f"Uzum KB: страниц в сборке {len(everything)}, берём {len(pages)} "
                    f"(без локалей {', '.join(locales)} и пустых)")
                return pages

            async def modified(build: str) -> float:
                resp = await asset(build, "HEAD")
                stamp = resp.headers.get("last-modified") if resp else None
                return parsedate_to_datetime(stamp).timestamp() if stamp else 0.0

            log("Uzum KB: открываю корень инструкции (проверка браузера ~15 с)")
            root_html = await open_page(ROOT, wait_ms)
            if not root_html:
                raise RuntimeError("корень инструкции не открылся — проверка браузера не пустила?")
            build = _build(root_html)
            if not build:
                raise RuntimeError("в странице нет app.*.js — сайт больше не на VuePress?")
            pages = await listed(build)

            got: dict[str, tuple[str | None, str | None]] = {"/": (root_html, build)}
            for p in pages:
                if p["path"] not in got:
                    html = await open_page(ROOT + p["path"].lstrip("/"))
                    got[p["path"]] = (html, _build(html))

            builds = {b for _h, b in got.values() if b}
            if len(builds) > 1:
                stamps = {b: await modified(b) for b in builds}
                target = max(builds, key=lambda b: stamps[b])
                log(f"Uzum KB: идёт выкатка — узлы отдают {len(builds)} сборки, "
                    f"беру свежую {target.rsplit('/', 1)[-1]}")
                if target != build:
                    pages = await listed(target)
                for p in pages:
                    html, b = got.get(p["path"], (None, None))
                    for i in range(8):
                        if b == target:
                            break
                        html = await open_page(f"{ROOT}{p['path'].lstrip('/')}?_={i}")
                        b = _build(html)
                    got[p["path"]] = (html, b)
                    if b != target:
                        log(f"Uzum KB: {p['path']} — свежая сборка так и не попалась, "
                            "страницу не трогаю")
                build = target
    return pages, {path: h for path, (h, b) in got.items() if h and b == build}


def _number(page: dict) -> int:
    m = re.match(r"(\d+)\.", page["title"])
    return int(m.group(1)) if m else 10 ** 6


def _index(pages: list[dict]) -> str:
    lines = ["# Инструкция для продавцов Uzum Market", "",
             f"Источник: {ROOT}", ""]
    for p in pages:
        rel = _rel_for(p["path"])[len(BASE) + 1:]
        lines.append(f"- [{p['title']}]({rel})")
        lines += [f"  - {t}" for lvl, t in p["headers"] if lvl == 2]
    return "\n".join(lines)


def run(wait_ms: int = 60_000, **_kw) -> dict:
    pages, htmls = asyncio.run(_crawl(wait_ms))

    total = changed = skipped = 0
    written = []
    for p in pages:
        html = htmls.get(p["path"])
        if not html:
            skipped += 1
            continue
        url = ROOT + p["path"].lstrip("/")
        md = page_markdown(html, url)
        if len(md) < 200:
            log(f"Uzum KB: {p['path']} — статьи в странице нет ({len(md)} символов)")
            skipped += 1
            continue
        p["title"] = p["title"] or _first_line(md) or p["path"]
        total += 1
        written.append(p)
        if write_doc(_rel_for(p["path"]),
                     {"title": p["title"], "marketplace": "uzum", "kind": "article",
                      "path": p["path"], "source": url, "fetched_at": now_iso()},
                     md):
            changed += 1
    if not total:
        raise RuntimeError(f"ни одной статьи из {len(pages)} страниц")

    written.sort(key=lambda p: (_number(p), p["path"]))
    if write_doc(f"{BASE}/index.md",
                 {"title": "Инструкция для продавцов Uzum Market — оглавление",
                  "marketplace": "uzum", "source": ROOT},
                 _index(written)):
        changed += 1

    log(f"Uzum KB: статей {total}, обновлено {changed}, пропущено {skipped}")
    (MIRROR / BASE / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "uzum-kb", "total": total, "changed": changed, "skipped": skipped}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
