"""Ozon Seller API: справочник целиком приходит одной спекой OpenAPI.

docs.ozon.ru/api/seller/ — это Redoc, который тянет спеку отдельным XHR (~4 МБ,
463 метода). Скрейпить отрендеренный HTML незачем: спека и есть первоисточник.
Страница закрыта антиботом (403 с любого IP), поэтому идём браузерной ступенью
proxy-web — она уже умеет ждать контент, а не верить первому ответу.
"""

from __future__ import annotations

import json
from pathlib import Path

from common import MIRROR, json_stable, log, now_iso, write_raw
from render_openapi import render_spec

PAGE = "https://docs.ozon.ru/api/seller/"
BASE = "ozon/api/seller"
RAW = f"{BASE}/spec.json"


def _looks_like_spec(body) -> bool:
    return isinstance(body, dict) and "openapi" in body and isinstance(body.get("paths"), dict)


def fetch_spec(channel: str = "optic") -> dict:
    """Открывает страницу браузером и забирает спеку из перехваченных XHR."""
    from common import proxyweb_scripts
    proxyweb_scripts()
    import browser, channels  # noqa: E402  (модули скилла proxy-web)

    ch = channels.get_channel(channel)
    channels.require_tunnel(ch)
    proxy = channels.proxy_for(ch)

    log(f"Ozon API: открываю {PAGE} (канал {channel}, ждём антибот ~60 с)")
    results = browser.browse([PAGE], proxy, wait_ms=45_000)
    if not results:
        raise RuntimeError("браузер не вернул результата")
    res = results[0]

    best = None
    for cap in res.captured:
        if _looks_like_spec(cap.get("body")):
            body = cap["body"]
            if best is None or len(body["paths"]) > len(best[1]["paths"]):
                best = (cap.get("url", ""), body)
    if best is None:
        raise RuntimeError(
            f"спека не найдена среди {len(res.captured)} JSON-ответов "
            f"(status={res.status}, error={res.error})")
    url, spec = best
    log(f"Ozon API: спека получена, методов {len(spec['paths'])}, источник {url or '—'}")
    return spec


def run(channel: str = "optic", from_file: str | None = None) -> dict:
    if from_file:
        spec = json.loads(Path(from_file).read_text(encoding="utf-8"))
        log(f"Ozon API: спека из файла {from_file}, методов {len(spec.get('paths', {}))}")
    else:
        spec = fetch_spec(channel)

    raw_changed = write_raw(RAW, json_stable(spec))
    total, changed = render_spec(spec, api="ozon-seller", base=BASE,
                                 source_url=PAGE)
    log(f"Ozon API: методов {total}, обновлено файлов {changed}"
        f"{', спека изменилась' if raw_changed else ', спека без изменений'}")
    (MIRROR / BASE / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "ozon-api", "total": total, "changed": changed,
            "spec_changed": raw_changed}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file")
    ap.add_argument("--channel", default="optic")
    a = ap.parse_args()
    print(json.dumps(run(a.channel, a.from_file), ensure_ascii=False))
