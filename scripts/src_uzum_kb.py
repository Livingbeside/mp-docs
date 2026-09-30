"""Инструкция для продавцов Uzum Market — seller.uzum.uz/manual, сайт на VitePress.

Страницы закрыты «проверкой браузера» Яндекса: requests вместо статьи получает
редирект на /tmgrdfrend/showcaptchafast и заглушку «Верификация». Camoufox со
своего IP проходит её за ~13 с, дальше страницы открываются меньше чем за секунду —
поэтому браузер один на весь обход. Резидентный канал не нужен (проверено 2026-09-25).

До 2026-09-30 сайт был на VuePress 1, потом переехал на VitePress — обход,
завязанный на разметку VuePress, в ту ночь упал. Список страниц теперь берём
из __VP_HASH_MAP__, который VitePress кладёт в каждую страницу: там все страницы
сборки, включая скрытые из меню (инструкция по брифу ЦПТ лежит в /uz/, хотя
написана по-русски). Ключи карты — в нижнем регистре, а сервер к регистру
чувствителен (/11.Analytics/ открывается, /11.analytics/ — 404), поэтому
настоящий путь берём из ссылок меню, а у страниц вне меню он совпадает с ключом.

Во время выкатки узлы за балансировщиком отдают разные сборки (видели 2026-09-25:
старая и новая вперемешку полчаса), а скрипт чужой сборки узел не знает — 404.
Поэтому если в обходе встретилось две сборки, дожимаем все страницы до самой
свежей (по Last-Modified её app.*.js). Иначе зеркало собралось бы из двух версий
и назавтра «поменялось» бы обратно.

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
READY = "#VPContent"          # есть у любой страницы VitePress и нет у заглушки проверки
APP_JS = re.compile(r'src="(/manual/assets/app\.[\w-]+\.js)"')
HASH_MAP = re.compile(r'__VP_HASH_MAP__\s*=\s*JSON\.parse\("((?:[^"\\]|\\.)*)"\)')
SITE_DATA = re.compile(r'__VP_SITE_DATA__\s*=\s*JSON\.parse\("((?:[^"\\]|\\.)*)"\)')
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def _vp_json(rx: re.Pattern, html: str) -> dict:
    """VitePress встраивает данные как JSON.parse("…") — JSON внутри JS-строки."""
    m = rx.search(html or "")
    if not m:
        return {}
    try:
        return json.loads(json.loads(f'"{m.group(1)}"'))
    except ValueError:
        return {}


def _sidebar_links(items) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            if item.get("link"):
                out.append(item["link"])
            out += _sidebar_links(item.get("items"))
    return out


def _key_of(link: str) -> str:
    """Ссылка меню → ключ __VP_HASH_MAP__: исходник .md, «/» → «_», нижний регистр."""
    p = link.split("#")[0].lstrip("/")
    if not p or p.endswith("/"):
        p += "index.md"
    elif p.endswith(".html"):
        p = p[:-5] + ".md"
    elif not p.endswith(".md"):
        p += ".md"
    return p.replace("/", "_").lower()


def _path_of(key: str, clean_urls: bool) -> str:
    # В именах страниц Uzum подчёркиваний нет; появятся — страница уйдёт в 404, это видно в логе.
    p = key.replace("_", "/")
    if p == "index.md" or p.endswith("/index.md"):
        return p[:-len("index.md")]
    return p[:-3] + ("" if clean_urls else ".html")


def site_pages(html: str) -> list[dict]:
    """Все страницы сборки: путь от ROOT и признак чужой локали."""
    hash_map = _vp_json(HASH_MAP, html)
    site = _vp_json(SITE_DATA, html)
    links = {}
    for items in ((site.get("themeConfig") or {}).get("sidebar") or {}).values():
        for link in _sidebar_links(items):
            links[_key_of(link)] = link.split("#")[0].lstrip("/")
    foreign = tuple(f"{k}_" for k in site.get("locales") or {} if k != "root")
    return [{"path": links.get(key) or _path_of(key, bool(site.get("cleanUrls"))),
             "foreign": key.startswith(foreign)}
            for key in hash_map]


def _russian(text: str) -> bool:
    letters = [c for c in text.lower() if c.isalpha()]
    return bool(letters) and sum(c in CYRILLIC for c in letters) / len(letters) > 0.5


def _title(html: str) -> str:
    """Заголовок страницы из <title>: «Страница | Сайт»; без « | » — у страницы его нет."""
    m = TITLE.search(html or "")
    text = " ".join(m.group(1).split()) if m else ""
    return text.rsplit(" | ", 1)[0] if " | " in text else ""


def _rel_for(path: str) -> str:
    p = re.sub(r"(index)?\.html$", "", path).strip("/")
    parts = [slug(x) for x in p.split("/") if x]
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
    found = _by_class(doc, "div", "vp-doc")
    if not found:
        return ""
    root = found[0]
    # Служебное: якорь у заголовков, подпись языка и кнопка копирования у кода.
    for el in (_by_class(root, "a", "header-anchor") + _by_class(root, "span", "lang")
               + _by_class(root, "button", "copy")):
        el.drop_tree()
    # Врезки «::: tip/warning» — цитатой, иначе они сливаются с текстом вокруг.
    for el in _by_class(root, "div", "custom-block"):
        el.tag = "blockquote"
    for el, attr in [(a, "href") for a in root.iter("a")] + [(i, "src") for i in root.iter("img")]:
        if el.get(attr):
            el.set(attr, _readable(el.get(attr)))

    md = "\n\n".join(extract._blocks_children(root, 0))
    return re.sub(r"\n{3,}", "\n\n", md).replace("​", "").strip()


def _first_line(md: str) -> str:
    """Заголовок страницы без собственного title (бриф ЦПТ начинается с жирной строки)."""
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
    """Один браузер на весь обход. -> (страницы сборки, {путь: html})."""
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
                        log(f"Uzum KB: {url} — 404")
                        return None
                    await page.wait_for_selector(READY, timeout=wait)
                    return await page.content()
                except Exception as exc:
                    log(f"Uzum KB: {url} — {type(exc).__name__}: {str(exc)[:160]}")
                    return None

            async def modified(build: str) -> float:
                """Last-Modified скрипта сборки. Узел с другой сборкой его не знает
                (404) — повторяем: с новым параметром балансировщик выберет другой узел."""
                for i in range(8):
                    resp = await ctx.request.fetch(f"{urljoin(ROOT, build)}?_={i}",
                                                   method="HEAD", timeout=45_000)
                    stamp = resp.headers.get("last-modified") if resp.ok else None
                    if stamp:
                        return parsedate_to_datetime(stamp).timestamp()
                    await asyncio.sleep(0.5)
                return 0.0

            log("Uzum KB: открываю корень инструкции (проверка браузера ~15 с)")
            root_html = await open_page(ROOT, wait_ms)
            if not root_html:
                raise RuntimeError("корень инструкции не открылся — проверка браузера не пустила?")
            build = _build(root_html)
            pages = site_pages(root_html)
            if not build or not pages:
                raise RuntimeError("в странице нет app.*.js или __VP_HASH_MAP__ — "
                                   "сайт больше не на VitePress?")
            log(f"Uzum KB: страниц в сборке {len(pages)}")

            got: dict[str, tuple[str | None, str | None]] = {"": (root_html, build)}
            for p in pages:
                if p["path"] not in got:
                    html = await open_page(ROOT + p["path"])
                    got[p["path"]] = (html, _build(html))

            builds = {b for _h, b in got.values() if b}
            if len(builds) > 1:
                stamps = {b: await modified(b) for b in builds}
                target = max(builds, key=lambda b: stamps[b])
                log(f"Uzum KB: идёт выкатка — узлы отдают {len(builds)} сборки, "
                    f"беру свежую {target.rsplit('/', 1)[-1]}")
                if target != build:
                    for i in range(8):     # список страниц — из корня свежей сборки
                        html = await open_page(f"{ROOT}?_={i}", wait_ms)
                        if _build(html) == target:
                            got[""] = (html, target)
                            pages = site_pages(html)
                            break
                for p in pages:
                    html, b = got.get(p["path"], (None, None))
                    for i in range(8):
                        if b == target:
                            break
                        html = await open_page(f"{ROOT}{p['path']}?_={i}")
                        b = _build(html)
                    got[p["path"]] = (html, b)
                    if b != target:
                        log(f"Uzum KB: {p['path'] or '/'} — свежая сборка так и не попалась, "
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
        lines += [f"  - {h}" for h in p["sections"]]
    return "\n".join(lines)


def run(wait_ms: int = 60_000, **_kw) -> dict:
    pages, htmls = asyncio.run(_crawl(wait_ms))

    total = changed = skipped = translated = 0
    written = []
    for p in pages:
        html = htmls.get(p["path"])
        if not html:
            skipped += 1
            continue
        title = _title(html)
        if p["foreign"] and not _russian(title):
            translated += 1
            continue
        url = ROOT + p["path"]
        md = page_markdown(html, url)
        if len(md) < 200:
            log(f"Uzum KB: /{p['path']} — статьи в странице нет ({len(md)} символов)")
            skipped += 1
            continue
        p["title"] = title or _first_line(md) or p["path"]
        p["sections"] = [ln[3:].strip() for ln in md.splitlines() if ln.startswith("## ")]
        total += 1
        written.append(p)
        if write_doc(_rel_for(p["path"]),
                     {"title": p["title"], "marketplace": "uzum", "kind": "article",
                      "path": "/" + p["path"], "source": url, "fetched_at": now_iso()},
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

    log(f"Uzum KB: статей {total}, обновлено {changed}, переводов пропущено {translated}, "
        f"пустых и недоступных {skipped}")
    (MIRROR / BASE / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "uzum-kb", "total": total, "changed": changed, "skipped": skipped}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
