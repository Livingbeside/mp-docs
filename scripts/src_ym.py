"""Яндекс Маркет: самый дешёвый источник из трёх.

Антибота нет — с серверного IP всё отдаётся напрямую. Больше того, у Diplodoc
каждая страница имеет markdown-двойник (`.md`), а список всех страниц лежит в
`llms.txt`. То есть парсить HTML не нужно вообще: качаем исходники как есть.
Спека OpenAPI живёт в официальном репозитории на GitHub — там просто git pull.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import requests

from common import (MIRROR, STATE, json_stable, log, now_iso, slug, write_doc,
                    write_raw)

LLMS = "https://yandex.ru/dev/market/partner-api/doc/ru/llms.txt"
DOC_BASE = "ozon"  # не используется, оставлено для симметрии
BASE = "ym/doc"
API_BASE = "ym/api"
REPO = "https://github.com/yandex-market/yandex-market-partner-api.git"
CHECKOUT = STATE / "ym-openapi"
TIMEOUT = 30
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mp-docs/1.0"}


def _get(url: str) -> str | None:
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as e:
        log(f"ЯМ: {url} — {e}")
        return None
    if r.status_code != 200:
        log(f"ЯМ: {url} — HTTP {r.status_code}")
        return None
    r.encoding = r.encoding or "utf-8"
    return r.text


def doc_pages() -> list[tuple[str, str]]:
    """(заголовок, url) всех страниц справки — из llms.txt."""
    txt = _get(LLMS) or ""
    pairs = re.findall(r"^\s*[-*]\s*\[([^\]]+)\]\((https://[^)]+\.md)\)", txt, re.M)
    seen, out = set(), []
    for title, url in pairs:
        if url in seen:
            continue
        seen.add(url)
        out.append((title.strip(), url))
    return out


def _rel_for(url: str) -> str:
    tail = url.split("/doc/ru/", 1)[-1]
    tail = tail[:-3] if tail.endswith(".md") else tail
    parts = [slug(p) for p in tail.split("/") if p]
    return f"{BASE}/{'/'.join(parts) or 'index'}.md"


def fetch_docs() -> tuple[int, int]:
    pages = doc_pages()
    log(f"ЯМ: страниц в llms.txt — {len(pages)}")
    total = changed = 0
    for title, url in pages:
        body = _get(url)
        if body is None:
            continue
        total += 1
        if write_doc(_rel_for(url),
                     {"title": title, "marketplace": "yandex-market",
                      "source": url, "fetched_at": now_iso()},
                     body):
            changed += 1
    return total, changed


def fetch_openapi() -> tuple[int, int]:
    """Спека — из официального репозитория: обновление это git pull, а не скрейп."""
    if CHECKOUT.exists():
        subprocess.run(["git", "-C", str(CHECKOUT), "pull", "-q", "--ff-only"],
                       capture_output=True, text=True, timeout=180)
    else:
        r = subprocess.run(["git", "clone", "-q", "--depth", "1", REPO, str(CHECKOUT)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log(f"ЯМ: клон спеки не удался — {r.stderr.strip()[:200]}")
            return 0, 0

    specs = sorted(p for p in (CHECKOUT / "openapi").rglob("*")
                   if p.suffix in (".json", ".yaml", ".yml"))
    if not specs:
        log("ЯМ: в репозитории нет файлов спеки")
        return 0, 0

    total = changed = 0
    for p in specs:
        rel = f"{API_BASE}/{p.relative_to(CHECKOUT / 'openapi')}"
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            try:
                text = json_stable(json.loads(text))
            except ValueError:
                pass
        total += 1
        if write_raw(rel, text):
            changed += 1
    root = CHECKOUT / "openapi" / "openapi.yaml"
    if root.exists():
        from bundle_openapi import bundle
        from render_openapi import render_spec
        spec = bundle(root)
        ops, ops_changed = render_spec(spec, api="yandex-market",
                                       base=f"{API_BASE}/methods",
                                       source_url="https://yandex.ru/dev/market/partner-api/")
        total += ops
        changed += ops_changed
        log(f"ЯМ: методов в спеке {ops}, обновлено {ops_changed}")
    log(f"ЯМ: файлов спеки {total}, обновлено {changed}")
    return total, changed


def run(**_kw) -> dict:
    d_total, d_changed = fetch_docs()
    s_total, s_changed = fetch_openapi()
    log(f"ЯМ: справка {d_total} страниц (обновлено {d_changed}), "
        f"спека {s_total} файлов (обновлено {s_changed})")
    (MIRROR / "ym").mkdir(parents=True, exist_ok=True)
    (MIRROR / "ym" / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "ym", "total": d_total + s_total, "changed": d_changed + s_changed}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
