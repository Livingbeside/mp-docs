"""OpenAPI → markdown: по файлу на метод.

Почему по файлу на метод, а не одной простынёй: `rg "v3/posting/fbs/get"` должен
приводить ровно к нужному методу, а `git log` — показывать, что изменилось именно
в нём. Сырая спека рядом (spec.json) хранит полную правду, markdown — читаемый срез.
"""

from __future__ import annotations

import re

from common import slug, write_doc

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_DEPTH = 6


def _deref(node, spec, seen: tuple = ()):
    """Разворачивает $ref внутри той же спеки. Циклы обрываем, а не падаем."""
    for _ in range(20):
        if not isinstance(node, dict) or "$ref" not in node:
            return node
        ref = node["$ref"]
        if not ref.startswith("#/") or ref in seen:
            return {"type": "object", "description": f"(ссылка {ref})"}
        seen = seen + (ref,)
        cur = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(cur, dict) or part not in cur:
                return {"type": "object", "description": f"(не найдено {ref})"}
            cur = cur[part]
        node = cur
    return node


def _type_of(sch: dict, spec) -> str:
    if not isinstance(sch, dict):
        return "?"
    for key in ("oneOf", "anyOf", "allOf"):
        if key in sch and isinstance(sch[key], list):
            parts = [_type_of(_deref(s, spec), spec) for s in sch[key][:4]]
            return " | ".join(dict.fromkeys(p for p in parts if p != "?")) or "object"
    t = sch.get("type")
    if t == "array":
        return f"array[{_type_of(_deref(sch.get('items') or {}, spec), spec)}]"
    if sch.get("enum"):
        vals = ", ".join(str(v) for v in sch["enum"][:12])
        extra = "…" if len(sch["enum"]) > 12 else ""
        return f"{t or 'enum'} ({vals}{extra})"
    if sch.get("format"):
        return f"{t}<{sch['format']}>"
    return t or ("object" if sch.get("properties") else "?")


def _clean(text) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _fields(sch, spec, depth: int = 0, seen: tuple = ()) -> list[str]:
    """Схема → вложенный список полей. Глубина ограничена: спеки бывают рекурсивные."""
    sch = _deref(sch, spec)
    if not isinstance(sch, dict) or depth > MAX_DEPTH:
        return []
    for key in ("allOf", "oneOf", "anyOf"):
        if key in sch and isinstance(sch[key], list):
            out = []
            for sub in sch[key]:
                out.extend(_fields(sub, spec, depth, seen))
            return out
    if sch.get("type") == "array" or "items" in sch:
        return _fields(sch.get("items") or {}, spec, depth, seen)
    props = sch.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(sch.get("required") or [])
    pad = "  " * depth
    out = []
    for name, raw in props.items():
        p = _deref(raw, spec)
        mark = " **обязательный**" if name in required else ""
        desc = _clean(p.get("description"))
        line = f"{pad}- `{name}` — {_type_of(p, spec)}{mark}"
        if desc:
            line += f". {desc}"
        if p.get("default") is not None:
            line += f" По умолчанию: `{p['default']}`."
        out.append(line)
        ref = raw.get("$ref") if isinstance(raw, dict) else None
        if ref and ref in seen:
            out.append(f"{pad}  - _(рекурсия, см. выше)_")
            continue
        out.extend(_fields(raw, spec, depth + 1, seen + ((ref,) if ref else ())))
    return out


def _params(op, spec, path_item) -> list[str]:
    rows = []
    for raw in (path_item.get("parameters") or []) + (op.get("parameters") or []):
        p = _deref(raw, spec)
        if not isinstance(p, dict) or "name" not in p:
            continue
        sch = _deref(p.get("schema") or {}, spec)
        rows.append("| `{}` | {} | {} | {} | {} |".format(
            p["name"], p.get("in", ""), _type_of(sch, spec),
            "да" if p.get("required") else "нет",
            _clean(p.get("description")) or "—"))
    return rows


def _body(op, spec) -> list[str]:
    rb = _deref(op.get("requestBody") or {}, spec)
    content = (rb.get("content") or {}) if isinstance(rb, dict) else {}
    out = []
    for ctype, media in content.items():
        sch = media.get("schema") or {}
        out.append(f"**Тело запроса** (`{ctype}`):")
        out.append("")
        fields = _fields(sch, spec)
        out.extend(fields or ["- _(схема не детализирована, см. spec.json)_"])
        out.append("")
    return out


def _responses(op, spec) -> list[str]:
    out = []
    for code, raw in (op.get("responses") or {}).items():
        resp = _deref(raw, spec)
        if not isinstance(resp, dict):
            continue
        out.append(f"**{code}** — {_clean(resp.get('description')) or 'без описания'}")
        out.append("")
        for _ctype, media in (resp.get("content") or {}).items():
            fields = _fields(media.get("schema") or {}, spec)
            if fields:
                out.extend(fields[:200])
                out.append("")
            break
    return out


def render_operation(spec: dict, path: str, method: str, op: dict,
                     path_item: dict, *, api: str, base: str,
                     source_url: str, spec_version: str) -> tuple[str, dict, str]:
    tags = op.get("tags") or ["other"]
    tag = tags[0]
    op_id = op.get("operationId") or f"{method}-{slug(path)}"
    title = op.get("summary") or op_id

    lines = [f"# {title}", "", f"`{method.upper()} {path}`", ""]
    desc = op.get("description") or ""
    if desc:
        lines += [_clean(desc), ""]
    if op.get("deprecated"):
        lines.insert(4, "> ⚠️ Метод помечен как **deprecated**.")
        lines.insert(5, "")

    rows = _params(op, spec, path_item)
    if rows:
        lines += ["## Параметры", "",
                  "| Имя | Где | Тип | Обяз. | Описание |",
                  "|---|---|---|---|---|", *rows, ""]
    body = _body(op, spec)
    if body:
        lines += ["## Запрос", "", *body]
    resp = _responses(op, spec)
    if resp:
        lines += ["## Ответы", "", *resp]

    rel = f"{base}/{slug(tag)}/{method}-{slug(path.strip('/').replace('/', '-'))}.md"
    meta = {
        "title": title, "api": api, "method": method.upper(), "path": path,
        "operation_id": op_id, "tags": tags, "spec_version": spec_version,
        "source": source_url, "deprecated": bool(op.get("deprecated")),
    }
    return rel, meta, "\n".join(lines)


def render_spec(spec: dict, *, api: str, base: str, source_url: str) -> tuple[int, int]:
    """Пишет все операции спеки. Возвращает (всего, изменилось)."""
    info = spec.get("info") or {}
    version = str(info.get("version", ""))
    total = changed = 0
    index: list[str] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            rel, meta, body = render_operation(
                spec, path, method, op, path_item,
                api=api, base=base, source_url=source_url, spec_version=version)
            total += 1
            if write_doc(rel, meta, body):
                changed += 1
            index.append("| `{}` | `{}` | {} | [{}]({}) |".format(
                method.upper(), path, (op.get("tags") or ["—"])[0],
                _clean(op.get("summary")) or meta["operation_id"],
                rel.split("/", len(base.split("/")))[-1]))

    head = [f"# {info.get('title') or api}", ""]
    if info.get("description"):
        head += [_clean(info["description"])[:2000], ""]
    head += [f"Версия спеки: `{version}` · методов: **{total}**", "",
             f"Источник: {source_url}", "",
             "| Метод | Путь | Раздел | Описание |", "|---|---|---|---|"]
    if write_doc(f"{base}/index.md",
                 {"title": f"{info.get('title') or api} — все методы",
                  "api": api, "spec_version": version, "operations": total,
                  "source": source_url},
                 "\n".join(head + sorted(index))):
        changed += 1
    return total, changed
