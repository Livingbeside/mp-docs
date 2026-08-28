"""Сборка многофайловой спеки OpenAPI в одну.

Яндекс Маркет публикует спеку россыпью: openapi.yaml + paths/*.yaml +
components/**. Рендер по методам умеет разворачивать только внутренние ссылки
`#/…`, поэтому файловые `$ref` сначала подтягиваем внутрь одного документа.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import yaml


def _load(path: Path, cache: dict) -> object:
    key = str(path.resolve())
    if key not in cache:
        cache[key] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return cache[key]


def _pointer(doc, frag: str):
    cur = doc
    for part in frag.lstrip("#/").split("/"):
        if not part:
            continue
        part = unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def bundle(root_file: Path, *, max_depth: int = 40) -> dict:
    cache: dict = {}
    root = _load(root_file, cache)

    def walk(node, base: Path, depth: int, stack: frozenset):
        if depth > max_depth:
            return {"type": "object", "description": "(слишком глубокая вложенность)"}
        if isinstance(node, list):
            return [walk(x, base, depth + 1, stack) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            file_part, _, frag = ref.partition("#")
            target = (base.parent / file_part).resolve()
            marker = f"{target}#{frag}"
            if marker in stack or not target.exists():
                # Циклы между файлами реальны (DTO ссылаются друг на друга).
                return {"type": "object", "description": f"(циклическая ссылка {ref})"}
            doc = _load(target, cache)
            sub = _pointer(doc, frag) if frag else doc
            if sub is None:
                return {"type": "object", "description": f"(не найдено {ref})"}
            merged = walk(sub, target, depth + 1, stack | {marker})
            rest = {k: v for k, v in node.items() if k != "$ref"}
            if rest and isinstance(merged, dict):
                merged = {**merged, **walk(rest, base, depth + 1, stack)}
            return merged
        return {k: walk(v, base, depth + 1, stack) for k, v in node.items()}

    return walk(root, root_file, 0, frozenset())
