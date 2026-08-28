"""База знаний Ozon (seller-edu.ozon.ru): регламенты, тарифы, лимиты, габариты.

Текста статьи в DOM нет — Nuxt берёт её XHR-ом у document-manager, и ответ уже
содержит и сам документ (ProseMirror), и список детей. Значит обход дерева и
получение текста — это один и тот же запрос, а HTML не нужен вовсе.

Экономика обхода: первая страница проходит антибот ~60 с, а дальше запросы идут
куками того же контекста по ~0.3 с. Поэтому браузер поднимается ОДИН раз, а все
статьи забираются через ctx.request.fetch — не по странице на статью.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from common import (MIRROR, load_cache, log, now_iso, save_cache, slug,
                    write_doc)

ROOT_URL = "https://seller-edu.ozon.ru/"
BASE = "ozon/kb"
CACHE = "ozon-kb.json"
PAGE_LINK = "https://seller-edu.ozon.ru/libra"


def _is_doc_response(body) -> bool:
    return (isinstance(body, dict) and isinstance(body.get("document"), dict)
            and ("articles" in body or "sections" in body))


def _template_from(url: str) -> str | None:
    """Из перехваченного запроса делаем шаблон: подставлять будем только path."""
    parts = urlparse(url)
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "path" not in qs:
        return None
    qs["path"] = "__PATH__"
    return urlunparse(parts._replace(query=urlencode(qs)))


def _url_for(template: str, path: str) -> str:
    from urllib.parse import quote
    return template.replace("__PATH__", quote(path, safe=""))


def _rel_for(path: str) -> str:
    parts = [slug(p) for p in path.strip("/").split("/") if p]
    return f"{BASE}/{'/'.join(parts) or 'index'}.md"


async def _article_url(page, captured: list[dict]) -> str:
    """Любая живая статья базы знаний: со ссылок страницы или из уже пойманного ответа."""
    try:
        hrefs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href^=\"/libra/\"]'))"
            ".map(a => a.getAttribute('href'))")
        for h in hrefs or []:
            if h.count("/") >= 3:            # /libra/<раздел>/… — не корень каталога
                return "https://seller-edu.ozon.ru" + h
        if hrefs:
            return "https://seller-edu.ozon.ru" + hrefs[0]
    except Exception:
        pass
    for cap in captured:
        body = cap.get("body")
        if not _is_doc_response(body):
            continue
        for key in ("articles", "sections"):
            for item in body.get(key) or []:
                url = (item or {}).get("url")
                if url:
                    return f"https://seller-edu.ozon.ru/libra{url}"
    return "https://seller-edu.ozon.ru/libra/fbo"


async def _roots(ctx, page, doc_template: str) -> list[str]:
    """Корень базы знаний по пути `/` не отдаётся (404) — разделы берём из дерева.

    Дерево ленивое (subtree пустой, hasChildren=true), поэтому глубже идём уже
    документным эндпоинтом: он и текст отдаёт, и детей.
    """
    tree_url = doc_template.replace("document/public/by-path", "tree")
    try:
        resp = await ctx.request.fetch(_url_for(tree_url, "/"), timeout=45_000)
        nodes = (json.loads(await resp.text()) or {}).get("tree")
        urls = [n.get("url") for n in (nodes or []) if isinstance(n, dict) and n.get("url")]
        if urls:
            return urls
    except Exception as exc:
        log(f"KB: дерево не отдалось ({type(exc).__name__}), беру ссылки со страницы")

    try:
        hrefs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href^=\"/libra/\"]'))"
            ".map(a => a.getAttribute('href'))")
    except Exception:
        return []
    seen, out = set(), []
    for h in hrefs or []:
        path = h[len("/libra"):] or "/"
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


async def _crawl(proxy, *, max_docs: int, delay: float, wait_ms: int) -> list[dict]:
    from common import proxyweb_scripts
    proxyweb_scripts()
    import browser as pw_browser
    import extract
    from camoufox.async_api import AsyncCamoufox

    captured: list[dict] = []
    content: list[dict] = []
    docs: list[dict] = []

    with pw_browser.browser_lock():
        async with AsyncCamoufox(headless=True, geoip=True, locale="ru-RU",
                                 **({"proxy": proxy} if proxy else {})) as br:
            page, ctx, _sess, _state = await pw_browser._open_page(
                br, session=None, with_images=False)

            async def on_response(resp):
                try:
                    if "json" not in (resp.headers or {}).get("content-type", ""):
                        return
                    body = await resp.json()
                except Exception:
                    return
                captured.append({"url": resp.url, "body": body})
                if _is_doc_response(body) or next(extract.pm_iter(body), None):
                    content.append({"url": resp.url, "body": body})

            page.on("response", on_response)

            log("KB: открываю корень базы знаний (антибот ~60 с)")
            try:
                await page.goto(ROOT_URL, wait_until="domcontentloaded", timeout=90_000)
            except Exception as exc:
                log(f"KB: навигация с ошибкой ({exc}) — смотрим, что успело прийти")
            await pw_browser._settle(page, wait_ms, content)

            # Корень дёргает только старый эндпоинт; актуальный v3 всплывает на статье.
            second = await _article_url(page, captured)
            log(f"KB: захожу на статью, чтобы увидеть актуальный API — {second}")
            try:
                await page.goto(second, wait_until="domcontentloaded", timeout=90_000)
                await pw_browser._settle(page, 25_000, [])
            except Exception as exc:
                log(f"KB: статья не открылась ({type(exc).__name__}) — работаем тем, что есть")

            # Эндпоинтов два поколения: .../v1/document-manager/section/... и .../v3/document/...
            # Нужен документный: он отдаёт и текст статьи, и список детей одним ответом.
            candidates = [_template_from(c["url"]) for c in captured
                          if _is_doc_response(c["body"])]
            candidates = [c for c in candidates if c]
            template = (next((c for c in candidates if "/api/v3/document/public/by-path" in c), None)
                        or next((c for c in candidates if "/document/public/by-path" in c), None)
                        or (candidates[0] if candidates else None))
            if not template:
                raise RuntimeError(
                    f"не нашёл эндпоинт документов среди {len(captured)} JSON-ответов; "
                    "структура сайта изменилась — посмотреть kb_probe в scratchpad")
            log(f"KB: эндпоинт — {template.split('?')[0]}")

            roots = await _roots(ctx, page, template)
            if not roots:
                raise RuntimeError("не удалось получить корневые разделы базы знаний")
            log(f"KB: корневых разделов — {len(roots)}")

            queue: list[str] = list(roots)
            seen: set[str] = set()
            while queue and len(docs) < max_docs:
                path = queue.pop(0)
                if path in seen:
                    continue
                seen.add(path)
                try:
                    resp = await ctx.request.fetch(_url_for(template, path), timeout=45_000)
                    raw = await resp.text()
                    body = json.loads(raw)
                except Exception as exc:
                    log(f"KB: {path} — ошибка запроса ({type(exc).__name__}: {exc})")
                    continue
                if not isinstance(body, dict) or not isinstance(body.get("document"), dict):
                    keys = list(body)[:8] if isinstance(body, dict) else type(body).__name__
                    log(f"KB: {path} — неожиданный ответ: status={resp.status} "
                        f"keys={keys} :: {raw[:200]}")
                    continue
                docs.append({"path": path, "body": body})
                for key in ("sections", "articles"):
                    for item in body.get(key) or []:
                        url = (item or {}).get("url")
                        if url and url not in seen:
                            queue.append(url)
                if len(docs) % 25 == 0:
                    log(f"KB: {len(docs)} документов, в очереди {len(queue)}")
                await asyncio.sleep(delay)

            if queue:
                log(f"KB: упёрся в лимит --max-docs={max_docs}, "
                    f"не обойдено {len(queue)} путей")
    return docs


def _render(entry: dict) -> tuple[str, dict, str] | None:
    from common import proxyweb_scripts
    proxyweb_scripts()
    import extract

    body = entry["body"]
    doc = body["document"]
    title = (doc.get("title") or "").strip() or entry["path"]

    parts = []
    for pm in extract.pm_iter(doc.get("contentJson") or doc.get("content")):
        text = extract.pm_render(pm).strip()
        if text:
            parts.append(text)
            break
    kids = extract.pm_children(body, base=PAGE_LINK)
    if kids:
        parts.append(kids)
    if not parts:
        return None

    crumbs = " / ".join((c.get("title") or "") for c in (body.get("breadcrumbs") or []))
    md = f"# {title}\n"
    if crumbs:
        md += f"\n_{crumbs}_\n"
    if doc.get("description"):
        md += f"\n{doc['description']}\n"
    md += "\n" + "\n\n".join(parts)

    meta = {
        "title": title,
        "marketplace": "ozon",
        "kind": "section" if doc.get("isSection") else "article",
        "path": entry["path"],
        "source": PAGE_LINK + entry["path"],
        "updated": doc.get("updated") or "",
        "doc_id": doc.get("id") or "",
    }
    return _rel_for(entry["path"]), meta, md


def run(channel: str = "optic", *, max_docs: int = 4000, delay: float = 0.35,
        wait_ms: int = 60_000, **_kw) -> dict:
    from common import proxyweb_scripts
    proxyweb_scripts()
    import channels

    ch = channels.get_channel(channel)
    channels.require_tunnel(ch)
    proxy = channels.proxy_for(ch)

    docs = asyncio.run(_crawl(proxy, max_docs=max_docs, delay=delay, wait_ms=wait_ms))
    log(f"KB: получено документов — {len(docs)}")

    cache = load_cache(CACHE)
    total = changed = skipped = 0
    fresh: dict = {}
    for entry in docs:
        rendered = _render(entry)
        if rendered is None:
            skipped += 1
            continue
        rel, meta, md = rendered
        total += 1
        fresh[entry["path"]] = {"updated": meta["updated"], "rel": rel}
        if write_doc(rel, {**meta, "fetched_at": now_iso()}, md):
            changed += 1
            was = (cache.get(entry["path"]) or {}).get("updated")
            if was and was != meta["updated"]:
                log(f"KB: изменилось — {meta['title']} ({was} → {meta['updated']})")
    save_cache(CACHE, fresh)

    log(f"KB: статей {total}, обновлено {changed}, пустых пропущено {skipped}")
    (MIRROR / BASE).mkdir(parents=True, exist_ok=True)
    (MIRROR / BASE / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "ozon-kb", "total": total, "changed": changed,
            "skipped": skipped}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-docs", type=int, default=4000)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--channel", default="optic")
    a = ap.parse_args()
    print(json.dumps(run(a.channel, max_docs=a.max_docs, delay=a.delay),
                     ensure_ascii=False))
