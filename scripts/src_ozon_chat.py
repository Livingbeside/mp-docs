"""Обновления Seller API — из служебных чатов кабинета продавца.

Ozon не отдаёт анонсы отдельным методом: он пишет их в служебные чаты, которые
читаются обычными методами чатов. Каналов три, и это не дубль журнала из спеки:
чаты приходят по конкретному кабинету и включают то, чего в документации нет
(например, изменения атрибутно-категорийной модели).

Seller API вызывается СТРОГО напрямую, без прокси проекта.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from common import MIRROR, load_cache, log, now_iso, save_cache, slug, write_doc

API = "https://api-seller.ozon.ru"
BASE = "ozon/api/updates"
CACHE = "ozon-chat.json"
CONFIG = Path.home() / ".config" / "mp-docs" / "config.json"
TIMEOUT = 30

# Служебные чаты, которые несут информацию об API. Чаты с покупателями и
# поддержкой сюда не попадают — там переписка по заказам, зеркалу она не нужна.
WANTED = {
    "SELLER_API_UPDATES": ("Обновления Seller API", "обновления самого API"),
    "SELLER_API_NOTIFICATIONS": ("Уведомления Seller API", "уведомления по API"),
    "SELLER_NOTIFICATION_UPDATE_CONTENT": (
        "Изменения атрибутно-категорийной модели",
        "правки категорий и характеристик — ломают загрузку товаров"),
}


def credentials() -> tuple[str, str] | None:
    """Ключи Seller API: переменные окружения, затем ~/.config/mp-docs/config.json.

    Намеренно не лезем в базы боевых сервисов — источник кредов должен быть явным.
    """
    cid = os.environ.get("MP_DOCS_OZON_CLIENT_ID")
    key = os.environ.get("MP_DOCS_OZON_API_KEY")
    if cid and key:
        return cid, key
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except ValueError:
            log(f"chat: {CONFIG} не читается как JSON")
            return None
        oz = cfg.get("ozon_seller") or {}
        if oz.get("client_id") and oz.get("api_key"):
            return str(oz["client_id"]), str(oz["api_key"])
    return None


def _post(path: str, creds: tuple[str, str], payload: dict) -> dict | None:
    try:
        r = requests.post(
            f"{API}{path}",
            headers={"Client-Id": creds[0], "Api-Key": creds[1],
                     "Content-Type": "application/json"},
            json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        log(f"chat: {path} — сеть недоступна ({type(exc).__name__})")
        return None
    if r.status_code == 403:
        log(f"chat: {path} — 403, у ключа нет доступа к методам чатов")
        return None
    if r.status_code != 200:
        # Тело ответа может содержать эхо заголовков — печатаем только код.
        log(f"chat: {path} — HTTP {r.status_code}")
        return None
    try:
        return r.json()
    except ValueError:
        log(f"chat: {path} — ответ не JSON")
        return None


def list_chats(creds) -> list[dict]:
    out, cursor = [], None
    for _ in range(20):                       # страховка от бесконечной пагинации
        payload: dict = {"limit": 100, "filter": {"chat_status": "ALL"}}
        if cursor:
            payload["cursor"] = cursor
        data = _post("/v3/chat/list", creds, payload)
        if not data:
            break
        out.extend(data.get("chats") or [])
        if not data.get("has_next"):
            break
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def chat_history(creds, chat_id: str, limit_total: int = 3000) -> list[dict]:
    """Всю историю чата, от новых к старым, страницами по 1000."""
    messages: list[dict] = []
    from_id = None
    for _ in range(limit_total // 1000 + 1):
        payload: dict = {"chat_id": chat_id, "direction": "Backward", "limit": 1000}
        if from_id is not None:
            payload["from_message_id"] = from_id
        data = _post("/v3/chat/history", creds, payload)
        if not data:
            break
        batch = data.get("messages") or []
        if not batch:
            break
        messages.extend(batch)
        if not data.get("has_next"):
            break
        from_id = batch[-1].get("message_id")
        if from_id is None:
            break
    return messages


def _render(title: str, note: str, chat: dict, messages: list[dict]) -> str:
    lines = [f"# {title}", "",
             f"_{note}_", "",
             f"Служебный чат кабинета продавца, `chat_type: {chat.get('chat_type')}`. "
             f"Сообщений: {len(messages)}.", ""]
    # Ozon отдаёт от новых к старым — разворачиваем, чтобы дифф рос сверху вниз.
    for msg in sorted(messages, key=lambda m: m.get("message_id") or 0, reverse=True):
        when = (msg.get("created_at") or "")[:19].replace("T", " ")
        body = "\n".join(msg.get("data") or []).strip()
        if not body:
            continue
        lines += [f"## {when}", "", body, ""]
    return "\n".join(lines)


def run(**_kw) -> dict:
    creds = credentials()
    if not creds:
        log("chat: ключи Seller API не заданы — источник пропущен. "
            "Задать: MP_DOCS_OZON_CLIENT_ID / MP_DOCS_OZON_API_KEY "
            f"или {CONFIG}")
        return {"source": "ozon-chat", "total": 0, "changed": 0, "skipped": True}

    chats = list_chats(creds)
    log(f"chat: всего чатов в кабинете — {len(chats)}")
    cache = load_cache(CACHE)
    fresh: dict = {}
    total = changed = 0

    for entry in chats:
        chat = (entry or {}).get("chat") or {}
        ctype = chat.get("chat_type")
        if ctype not in WANTED:
            continue
        title, note = WANTED[ctype]
        chat_id = chat.get("chat_id")
        last_id = entry.get("last_message_id")
        known = (cache.get(ctype) or {}).get("last_message_id")
        rel = f"{BASE}/{slug(ctype)}.md"

        if known and known == last_id and (MIRROR / rel).exists():
            log(f"chat: {title} — новых сообщений нет")
            fresh[ctype] = {"last_message_id": last_id, "rel": rel}
            total += 1
            continue

        messages = chat_history(creds, str(chat_id))
        log(f"chat: {title} — сообщений {len(messages)}")
        total += 1
        fresh[ctype] = {"last_message_id": last_id, "rel": rel}
        if write_doc(rel,
                     {"title": title, "marketplace": "ozon", "kind": "changelog",
                      "chat_type": ctype, "messages": len(messages),
                      "source": "https://api-seller.ozon.ru/v3/chat/history",
                      "fetched_at": now_iso()},
                     _render(title, note, chat, messages)):
            changed += 1

    if not total:
        log("chat: служебных чатов с обновлениями API в кабинете не нашлось")
    save_cache(CACHE, fresh)
    (MIRROR / BASE).mkdir(parents=True, exist_ok=True)
    (MIRROR / BASE / ".fetched").write_text(now_iso(), encoding="utf-8")
    return {"source": "ozon-chat", "total": total, "changed": changed}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
