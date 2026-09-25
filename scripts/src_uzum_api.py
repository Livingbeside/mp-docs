"""Uzum Market Seller API: спека OpenAPI отдаётся напрямую, одним JSON.

У api-seller.uzum.uz антибота нет — Swagger UI берёт спеку обычным запросом,
её и качаем, браузер не нужен.

Своей справки в спеке почти нет: описания разделов однострочные, а про
авторизацию и лимиты она говорит только схемой безопасности и заголовками
ответов. Их выносим отдельной страницей — иначе поиск по markdown их не найдёт,
а «токен без префикса Bearer» — ровно то место, где спотыкаются.
"""

from __future__ import annotations

import json

import requests

from common import MIRROR, json_stable, log, now_iso, write_doc, write_raw
from render_openapi import render_spec

SPEC_URL = "https://api-seller.uzum.uz/api/seller-openapi/swagger/api-docs"
PAGE = "https://api-seller.uzum.uz/api/seller-openapi/swagger/swagger-ui/swagger-ui/index.html"
BASE = "uzum/api/seller"
TIMEOUT = 60
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mp-docs/1.0"}


def fetch_spec() -> dict:
    r = requests.get(SPEC_URL, headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"спека не отдалась: HTTP {r.status_code}")
    try:
        spec = r.json()
    except ValueError:
        raise RuntimeError(f"вместо спеки не JSON ({r.headers.get('content-type')})") from None
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise RuntimeError("в ответе нет paths — это не спека OpenAPI")
    return spec


def overview(spec: dict) -> str | None:
    """Базовый адрес, авторизация и лимиты — из servers, securitySchemes и headers."""
    comp = spec.get("components") or {}
    servers = [s["url"] for s in spec.get("servers") or [] if s.get("url")]
    schemes = comp.get("securitySchemes") or {}
    headers = comp.get("headers") or {}
    if not (servers or schemes or headers):
        return None

    lines = ["# Авторизация и лимиты запросов", ""]
    if servers:
        lines += ["Базовый адрес: " + ", ".join(f"`{u}`" for u in servers), ""]
    if schemes:
        lines += ["## Авторизация", ""]
        where = {"header": "заголовок", "query": "параметр запроса", "cookie": "cookie"}
        for name, s in schemes.items():
            what = (f"{where.get(s.get('in'), s.get('in'))} `{s['name']}`" if s.get("name")
                    else s.get("scheme") or s.get("type") or name)
            desc = (s.get("description") or "").strip()
            lines.append(f"- **{name}** — {what}" + (f". {desc}" if desc else ""))
        lines.append("")
    if headers:
        lines += ["## Лимиты запросов", "", "Текущий лимит сообщают заголовки ответа:", "",
                  "| Заголовок | Что значит |", "|---|---|"]
        for name, h in headers.items():
            desc = " ".join((h.get("description") or "—").split())
            lines.append(f"| `{name}` | {desc} |")
    return "\n".join(lines)


def run(**_kw) -> dict:
    spec = fetch_spec()
    raw_changed = write_raw(f"{BASE}/spec.json", json_stable(spec))
    total, changed = render_spec(spec, api="uzum-seller", base=BASE, source_url=PAGE)

    text = overview(spec)
    if text and write_doc(f"{BASE}/guides/avtorizaciya-i-limity.md",
                          {"title": "Авторизация и лимиты запросов", "api": "uzum-seller",
                           "kind": "guide", "source": PAGE}, text):
        changed += 1

    log(f"Uzum API: методов {total}, обновлено файлов {changed}"
        f"{', спека изменилась' if raw_changed else ', спека без изменений'}")
    (MIRROR / BASE / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "uzum-api", "total": total, "changed": changed + raw_changed,
            "spec_changed": raw_changed}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
