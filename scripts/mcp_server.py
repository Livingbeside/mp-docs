"""MCP-сервер поверх зеркала: тот же справочник для Cursor и других клиентов.

Сервер ничего не качает — только читает готовые файлы зеркала. Обновлением
занимается `mpdocs update` по таймеру, поэтому сервер быстрый и офлайновый.

Протокол — JSON-RPC 2.0 по stdio (базовый MCP), без внешних зависимостей:
на сервере нет пакета mcp, а тащить его ради четырёх инструментов незачем.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import MIRROR, git  # noqa: E402

PROTOCOL = "2024-11-05"
MAX_CHARS = 60_000

TOOLS = [
    {
        "name": "search_docs",
        "description": (
            "Поиск по локальному зеркалу документации маркетплейсов "
            "(Ozon Seller API и база знаний, Wildberries, Яндекс Маркет). "
            "Возвращает совпадения с путями файлов."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "что искать (регулярка ripgrep)"},
                "marketplace": {"type": "string", "enum": ["ozon", "wb", "ym", "any"],
                                "description": "ограничить площадкой"},
                "limit": {"type": "integer", "description": "сколько файлов, по умолчанию 20"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_doc",
        "description": "Прочитать документ зеркала целиком по его пути "
                       "(путь берётся из search_docs).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "find_method",
        "description": ("Найти метод API по пути или operationId "
                        "(например «v4/posting/fbs/list» или «PostingAPI»). "
                        "Возвращает готовое описание метода: параметры, тело, ответы."),
        "inputSchema": {
            "type": "object",
            "properties": {"method": {"type": "string"}},
            "required": ["method"],
        },
    },
    {
        "name": "announced_changes",
        "description": ("Официальный журнал обновлений маркетплейса — что он объявил сам, "
                        "с датами и ссылками на методы. Дополняет recent_changes, который "
                        "показывает фактические правки файлов, включая необъявленные."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "marketplace": {"type": "string", "enum": ["ozon", "wb", "ym", "any"]},
                "count": {"type": "integer", "description": "сколько последних записей, по умолчанию 5"},
            },
        },
    },
    {
        "name": "recent_changes",
        "description": ("Что менялось в документации за период: маркетплейсы правят "
                        "лимиты и отключают методы молча, история зеркала это ловит."),
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "string",
                                     "description": "например 2.weeks, 1.month"}},
        },
    },
]

SUBDIRS = {"ozon": "ozon", "wb": "wb", "ym": "ym"}


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n… обрезано, всего {len(text)} символов"


def _rg(args: list[str], subdir: str = "") -> str:
    """Ограничение площадкой — это корень поиска: глоб не совпал бы с абсолютным путём."""
    root = MIRROR / subdir if subdir else MIRROR
    if not root.exists():
        return ""
    r = subprocess.run(["rg", "--color=never", *args, str(root)],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.replace(str(MIRROR) + "/", "")


def tool_search(a: dict) -> str:
    args = ["-n", "-i", "--max-count", "3", "--max-columns", "300",
            "-g", "*.md", a["query"]]
    out = _rg(args, SUBDIRS.get(a.get("marketplace", "any"), ""))
    if not out.strip():
        return "ничего не найдено"
    lines = out.splitlines()
    limit = int(a.get("limit") or 20)
    files, kept = set(), []
    for line in lines:
        f = line.split(":", 1)[0]
        if f not in files and len(files) >= limit:
            break
        files.add(f)
        kept.append(line)
    return _clip("\n".join(kept))


def tool_read(a: dict) -> str:
    rel = a["path"].lstrip("/")
    target = (MIRROR / rel).resolve()
    if not str(target).startswith(str(MIRROR.resolve())):
        return "путь вне зеркала"
    if not target.is_file():
        return f"нет такого файла: {rel}"
    return _clip(target.read_text(encoding="utf-8"))


def tool_find_method(a: dict) -> str:
    needle = a["method"].strip().strip("/")
    esc = re.escape(needle)
    out = _rg(["-l", "-i", "-g", "*.md", f"^(path|operation_id): .*{esc}$"])
    files = [f for f in out.splitlines() if f.strip()]
    if not files:
        return (f"метод «{needle}» не найден. "
                "Попробуй search_docs — возможно, он описан под другим путём.")
    if len(files) == 1:
        return _clip((MIRROR / files[0]).read_text(encoding="utf-8"))
    head = "\n".join(f"- {f}" for f in files[:30])
    first = (MIRROR / files[0]).read_text(encoding="utf-8")
    return _clip(f"Подходит {len(files)} файлов:\n{head}\n\n---\n\n{first}")


def tool_changes(a: dict) -> str:
    since = a.get("since") or "2.weeks"
    out = git("log", f"--since={since}", "--stat", "--format=%h %ad %s",
              "--date=short", check=False)
    return _clip(out.strip() or f"за {since} документация не менялась")


def tool_announced(a: dict) -> str:
    files = sorted(set(MIRROR.rglob("changelog.md")) |
                   {f for f in MIRROR.rglob("changelog/*.md")})
    sub = SUBDIRS.get(a.get("marketplace", "any"), "")
    if sub:
        files = [f for f in files if str(f.relative_to(MIRROR)).startswith(sub)]
    if not files:
        return "официальных журналов обновлений в зеркале нет"
    count = int(a.get("count") or 5)
    out = []
    for f in files:
        body = f.read_text(encoding="utf-8").split("---", 2)[-1]
        blocks = re.split(r"^(#{2,3} )", body, flags=re.M)
        pairs = list(zip(blocks[1::2], blocks[2::2]))
        out.append(f"═══ {f.relative_to(MIRROR)} ═══")
        out += [lvl + b.rstrip() for lvl, b in pairs[:count]]
    return _clip("\n".join(out))


HANDLERS = {"search_docs": tool_search, "read_doc": tool_read,
            "find_method": tool_find_method, "recent_changes": tool_changes,
            "announced_changes": tool_announced}


def handle(req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mp-docs", "version": "1.0.0"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"нет инструмента {name}"}}
        try:
            text = fn(params.get("arguments") or {})
        except Exception as exc:
            text = f"ошибка: {type(exc).__name__}: {exc}"
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}]}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"неизвестный метод {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
