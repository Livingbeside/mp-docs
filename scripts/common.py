"""Общее для всех источников: пути, запись документов, git-история зеркала.

Зеркало — отдельный git-репозиторий (mirror/), чтобы `git log` показывал только
изменения документации, а не правки качалок. Главная ценность зеркала не в тексте,
а в диффе: «Ozon молча поменял лимит» видно одной командой.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "mirror"
STATE = ROOT / "state"
PROXY_WEB = Path.home() / ".claude" / "skills" / "proxy-web"

STATE.mkdir(parents=True, exist_ok=True)


def proxyweb_scripts() -> Path:
    """Модули скилла proxy-web: в них уже зашит обход антибота Ozon/WB."""
    p = PROXY_WEB / "scripts"
    if not p.is_dir():
        raise RuntimeError(f"нет скилла proxy-web: {p}")
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
    return p


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slug(text: str, limit: int = 80) -> str:
    """Латиница в имени файла: кириллические пути ломают grep-подсказки и ссылки."""
    text = (text or "").strip().lower()
    out = []
    for ch in text:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        elif unicodedata.category(ch).startswith("L") and ch.isascii():
            out.append(ch)
        else:
            out.append("-")
    s = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    if len(s) > limit:
        s = s[:limit].rstrip("-")
    return s or "untitled"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return '""'
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r'[:#\n"\']|^\s|\s$', s):
        return json.dumps(s, ensure_ascii=False)
    return s


def frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            lines.append(f"{k}:")
            lines.extend(f"  - {_yaml_scalar(x)}" for x in v)
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


def write_doc(rel: str, meta: dict, body: str) -> bool:
    """Пишет документ, если содержимое изменилось. True — файл обновлён.

    fetched_at намеренно НЕ участвует в сравнении: иначе каждый ночной прогон
    давал бы дифф по всем файлам и история стала бы бесполезной.
    """
    path = MIRROR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body.rstrip() + "\n"
    meta = {**meta, "content_sha": sha(body)}
    new = frontmatter(meta) + "\n\n" + body

    if path.exists():
        old = path.read_text(encoding="utf-8")
        m = re.search(r"^content_sha: (\S+)$", old, re.M)
        if m and m.group(1) == meta["content_sha"]:
            return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return True


def write_raw(rel: str, text: str) -> bool:
    """Сырой файл (спека OpenAPI) — ключи отсортированы, чтобы дифф был осмысленным."""
    path = MIRROR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text.rstrip() + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def json_stable(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def cache_path(name: str) -> Path:
    return STATE / name


def load_cache(name: str) -> dict:
    p = cache_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(name: str, data: dict) -> None:
    p = cache_path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def git(*args, cwd: Path = MIRROR, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def commit_mirror(message: str) -> str | None:
    """Коммитит изменения зеркала. Возвращает краткую сводку или None."""
    status = git("status", "--porcelain")
    if not status.strip():
        return None
    git("add", "-A")
    stat = git("diff", "--cached", "--shortstat").strip()
    git("-c", "user.name=mp-docs", "-c", "user.email=mp-docs@local",
        "commit", "-q", "-m", message)
    return stat


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
