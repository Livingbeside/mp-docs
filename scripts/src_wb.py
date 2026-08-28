"""Wildberries: 13 разделов, в каждом своя спека OpenAPI.

Qrator отдаёт 498 всему, что не похоже на браузер, независимо от IP — берём
браузерной ступенью proxy-web. Отдельного JSON со спекой нет: Redoc получает её
инлайном, в теге <script> лежит `const __redoc_state = {...}`, где spec.data —
полная спека раздела. Её и достаём, HTML не парсим.
"""

from __future__ import annotations

import json
import re

from common import MIRROR, json_stable, log, now_iso, write_doc, write_raw
from render_openapi import render_spec

HOME = "https://dev.wildberries.ru/"
SECTION_URL = "https://dev.wildberries.ru/docs/openapi/{}"
RELEASE_NOTES = "https://dev.wildberries.ru/release-notes"
BASE = "wb/api"

# Запасной список: если главная не отдалась, разделы всё равно известны.
FALLBACK_SECTIONS = [
    "analytics", "api-information", "financial-reports-and-accounting",
    "in-store-pickup", "orders-dbs", "orders-dbw", "orders-fbs", "orders-fbw",
    "promotion", "reports", "user-communication", "wb-tariffs",
    "work-with-products",
]

MARKER = 'const __redoc_state = '


def _balanced_json(text: str, start: int) -> str | None:
    """Вырезает JSON-объект от `{` до парной закрывающей скобки."""
    if start >= len(text) or text[start] != "{":
        return None
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def spec_from_html(html: str) -> dict | None:
    """В HTML две копии состояния: RSC-экранированная и настоящая. Берём настоящую."""
    for m in re.finditer(re.escape(MARKER) + r'\{"', html):
        raw = _balanced_json(html, m.end() - 2)
        if not raw:
            continue
        try:
            state = json.loads(raw)
        except ValueError:
            continue
        spec = (state.get("spec") or {}).get("data")
        if isinstance(spec, dict) and spec.get("paths"):
            return spec
    return None


DATE_LINE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})$")


def _structure_notes(md: str) -> str:
    """Даты в журнале WB — обычные строки. Делаем из них заголовки,
    иначе записи не отделить друг от друга ни глазом, ни командой changelog.
    Заодно схлопываем дубли ярлыков («Новое / Новое»), которые даёт вёрстка.
    """
    out: list[str] = []
    prev = ""
    for line in md.split("\n"):
        stripped = line.strip()
        if stripped and stripped == prev:
            continue
        m = DATE_LINE.match(stripped)
        out.append(f"## {m.group(1)}" if m else line)
        if stripped:
            prev = stripped
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _notes_markdown(html: str) -> str:
    """Журнал WB — ~400 мелких записей, ни одна не «главный контейнер».

    Readability-скоринг proxy-web на такой странице выбирает пустой блок и отдаёт
    25 символов, поэтому берём тело целиком поблочно, убрав служебную обвязку.
    """
    from common import proxyweb_scripts
    proxyweb_scripts()
    import extract

    try:
        from lxml import html as LH
        doc = LH.fromstring(html)
        for bad in doc.xpath("//script|//style|//nav|//footer|//noscript|//head"):
            parent = bad.getparent()
            if parent is not None:
                parent.remove(bad)
        body = doc.find("body")
        if body is not None:
            blocks = extract._blocks(body)
            md = "\n\n".join(b for b in blocks if b.strip())
            if len(md) > 2000:
                return _structure_notes(md)
    except Exception as exc:
        log(f"WB: поблочный разбор журнала не удался ({type(exc).__name__}), беру скоринг")
    md, _title = extract.html_to_markdown(html, RELEASE_NOTES)
    return md


def discover_sections(html: str) -> list[str]:
    found = re.findall(r'href="/docs/openapi/([a-z0-9-]+)"', html)
    return sorted(dict.fromkeys(found))


def run(channel: str = "optic", **_kw) -> dict:
    from common import proxyweb_scripts
    proxyweb_scripts()
    import browser, channels  # noqa: E402

    ch = channels.get_channel(channel)
    channels.require_tunnel(ch)
    proxy = channels.proxy_for(ch)

    log("WB: открываю главную, чтобы узнать список разделов")
    home = browser.browse([HOME], proxy, wait_ms=40_000)
    sections = discover_sections(home[0].html) if home and home[0].html else []
    if not sections:
        log("WB: список разделов не прочитался, беру зашитый")
        sections = FALLBACK_SECTIONS
    else:
        new = set(sections) - set(FALLBACK_SECTIONS)
        if new:
            log(f"WB: появились новые разделы — {', '.join(sorted(new))}")
    log(f"WB: разделов {len(sections)}")

    urls = [SECTION_URL.format(s) for s in sections] + [RELEASE_NOTES]
    # Один браузер на все страницы: челлендж Qrator проходится один раз.
    # scroll — ради журнала изменений: он подгружает записи по мере прокрутки,
    # без неё в DOM попадают только последние ~20.
    results = browser.browse(urls, proxy, wait_ms=40_000, scroll=8)
    notes_result = results[-1] if len(results) > len(sections) else None
    results = results[:len(sections)]

    total = changed = 0
    ok_sections = 0
    for section, res in zip(sections, results):
        if not res.html:
            log(f"WB: {section} — пусто (status={res.status}, {res.error})")
            continue
        spec = spec_from_html(res.html)
        if not spec:
            log(f"WB: {section} — спека не найдена в странице ({len(res.html)} байт)")
            continue
        ok_sections += 1
        if write_raw(f"{BASE}/{section}/spec.json", json_stable(spec)):
            changed += 1
        ops, ops_changed = render_spec(
            spec, api=f"wb-{section}", base=f"{BASE}/{section}",
            source_url=SECTION_URL.format(section))
        total += ops
        changed += ops_changed
        log(f"WB: {section} — методов {ops}, обновлено {ops_changed}")

    # Журнал изменений у WB, в отличие от Ozon, лежит не в спеке, а отдельной страницей.
    if notes_result is not None and notes_result.html:
        md = _notes_markdown(notes_result.html)
        if len(md) > 2000:
            if write_doc(f"{BASE}/changelog.md",
                         {"title": "Журнал изменений WB API", "api": "wildberries",
                          "kind": "changelog", "source": RELEASE_NOTES,
                          "window": "последние записи, страница отдаёт не всю историю",
                          "fetched_at": now_iso()},
                         "# Журнал изменений WB API\n\n"
                         "> Страница WB отдаёт в DOM только последние записи "
                         "(прокрутка остальные не подгружает), поэтому здесь "
                         "скользящее окно, а не вся история. Полная история "
                         "накапливается в git: `mpdocs changes`.\n\n" + md):
                changed += 1
            log(f"WB: журнал изменений — {len(md) // 1024} КБ")
        else:
            log(f"WB: журнал изменений пуст ({len(md)} символов), пропускаю")

    log(f"WB: разделов взято {ok_sections}/{len(sections)}, методов {total}, "
        f"обновлено файлов {changed}")
    (MIRROR / "wb").mkdir(parents=True, exist_ok=True)
    (MIRROR / "wb" / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "wb", "total": total, "changed": changed,
            "sections": ok_sections}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
