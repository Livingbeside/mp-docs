"""Wildberries: 13 разделов, в каждом своя спека OpenAPI.

Qrator отдаёт 498 всему, что не похоже на браузер, независимо от IP — берём
браузерной ступенью proxy-web. Отдельного JSON со спекой нет: Redoc получает её
инлайном, в теге <script> лежит `const __redoc_state = {...}`, где spec.data —
полная спека раздела. Её и достаём, HTML не парсим.
"""

from __future__ import annotations

import json
import re

from common import MIRROR, json_stable, log, now_iso, write_raw
from render_openapi import render_spec

HOME = "https://dev.wildberries.ru/"
SECTION_URL = "https://dev.wildberries.ru/docs/openapi/{}"
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

    urls = [SECTION_URL.format(s) for s in sections]
    # Один браузер на все разделы: челлендж Qrator проходится один раз.
    results = browser.browse(urls, proxy, wait_ms=40_000)

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

    log(f"WB: разделов взято {ok_sections}/{len(sections)}, методов {total}, "
        f"обновлено файлов {changed}")
    (MIRROR / "wb").mkdir(parents=True, exist_ok=True)
    (MIRROR / "wb" / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "wb", "total": total, "changed": changed,
            "sections": ok_sections}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
