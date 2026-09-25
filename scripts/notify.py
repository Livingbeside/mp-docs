"""Сводка в Telegram: что нового принесло обновление зеркала.

Правда — в git-истории зеркала, а не в счётчиках источников: те врут (ozon-api
каждую ночь рапортует о двух обновлённых файлах, хотя меняются только метки
.fetched). Сводка строится по диапазону «последний отправленный коммит → HEAD»,
поэтому неудачная отправка находок не теряет — они уедут следующим прогоном.

Шум отсекается по смыслу, а не по списку файлов:
- .fetched, fetched_at, content_sha — служебные метки;
- ЯМ пишет в каждую страницу версию своего генератора (Diplodoc Platform vX)
  и при её смене перевыпускает все ~240 страниц — это не правка документации;
  то же с путями сборочной песочницы в схемах (build_root/<каталог сборки>/…);
- WB рисует над журналом календарь текущей недели, числа меняются каждый день.
  Поэтому журналы сравниваются по записям, а не построчно.

Методы сравниваются по спекам, а не по файлам: источники не удаляют файлы
исчезнувших методов, а при переименовании раздела WB файлы переезжают в новый
каталог — по файлам первое не видно вовсе, второе выглядит как «новые методы».

Включается, только если заданы бот и чат (MP_DOCS_TG_BOT_TOKEN и
MP_DOCS_TG_CHAT_ID или секция telegram в ~/.config/mp-docs/config.json):
у чужих копий зеркала слать некуда.
"""

from __future__ import annotations

import difflib
import html
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common import MIRROR, git, load_cache, log, now_iso, save_cache

CONFIG = Path.home() / ".config" / "mp-docs" / "config.json"
STATE = "notify.json"
TZ = ZoneInfo("Europe/Moscow")

TG_LIMIT = 3900        # у Telegram 4096 символов текста на сообщение — берём с запасом
MAX_MESSAGES = 4       # дальше это уже не сводка: остальное по ссылке на дифф
ANNOUNCE_DAYS = 45     # правка метода «объявлена», если он упомянут в журнале за это окно
LIST_OPS = 12          # новых/удалённых методов по одной строке, дальше — «и ещё N»
LIST_CHANGED = 6       # изменённых методов поимённо; больше — сводкой по разделам
LIST_ENTRIES = 12      # записей журнала на площадку
DOCS_DETAILED = 8      # страниц справки с фрагментами правки; остальные — одними названиями
SNIPPETS = 2           # фрагментов правки на страницу
SNIP = 190             # длина фрагмента, символов

HTTP = ("get", "post", "put", "patch", "delete")
MARKET = {"ozon": "Ozon", "wb": "WB", "ym": "ЯМ", "uzum": "Uzum"}
CHANGELOGS = {
    "ozon/api/seller/changelog.md": "Ozon · журнал Seller API",
    "wb/api/changelog.md": "WB · журнал API",
    "ym/doc/changelog/all.md": "ЯМ · журнал Partner API",
    "ym/doc/changelog/main.md": "ЯМ · журнал Partner API",
    "ym/doc/changelog/deprecated.md": "ЯМ · журнал Partner API",
}


# ─── git ─────────────────────────────────────────────────────────────────────

class Blobs:
    """Файлы на любой ревизии одним процессом `git cat-file --batch`: за ночь
    меняются сотни файлов, запускать git на каждый — секунды впустую."""

    def __init__(self):
        self._p = subprocess.Popen(["git", "cat-file", "--batch"], cwd=str(MIRROR),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def get(self, rev: str, path: str) -> str | None:
        self._p.stdin.write(f"{rev}:{path}\n".encode())
        self._p.stdin.flush()
        header = self._p.stdout.readline().decode().split()
        if len(header) != 3 or header[1] != "blob":
            return None                  # «… missing»: файла на этой ревизии нет
        data = self._p.stdout.read(int(header[2]))
        self._p.stdout.read(1)           # перевод строки после содержимого
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        self._p.stdin.close()
        self._p.wait()


def rev(ref: str) -> str:
    return git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip()


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                          cwd=str(MIRROR), capture_output=True).returncode == 0


def changed_files(base: str, head: str) -> list[tuple[str, str, str]]:
    """(статус, путь до, путь после). Источники файлов не удаляют, поэтому переезд
    (раздел WB переименовали, статью Ozon перенесли) выглядит как новый файл при
    живом старом — его ловит только поиск копий среди неизменённых файлов (-C -C)."""
    parts = git("diff", "--name-status", "-z", "-C", "-C", base, head).split("\0")
    out, i = [], 0
    while i < len(parts) - 1:
        if parts[i][:1] in "CR":         # «C089»: копия со сходством 89 %
            out.append((parts[i], parts[i + 1], parts[i + 2]))
            i += 3
        else:
            out.append((parts[i], parts[i + 1], parts[i + 1]))
            i += 2
    return out


def web_link(base: str, head: str, single: bool) -> str | None:
    """Ссылка на дифф в публичном репозитории зеркала — если коммит уже туда уехал."""
    url = git("remote", "get-url", "origin", check=False).strip()
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    if not m or not git("branch", "-r", "--contains", head, check=False).strip():
        return None
    repo = f"https://github.com/{m.group(1)}"
    return f"{repo}/commit/{head}" if single else f"{repo}/compare/{base[:12]}...{head[:12]}"


# ─── разбор документов ───────────────────────────────────────────────────────

_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# ЯМ: в теле свой YAML-блок генератора (версия Diplodoc, альтернативные ссылки)
_DIPLODOC = re.compile(r"\A\s*---\n[A-Za-z_]+:.*?\n---\n", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
# ЯМ: в схемах торчат пути сборочной песочницы, каталог сборки новый на каждую
# сборку (build_root/jc95/… → build_root/pyur/…) — десятки страниц «меняются» зря.
_BUILD_PATH = re.compile(r"/home/sandbox/\S*?/build_root/[^/\s]+/[^/\s]+/")
_LIST = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")


def split_doc(text: str | None) -> tuple[dict, str]:
    text = text or ""
    m = _FM.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(": ")
        if sep and not line.startswith(" "):
            val = val.strip()
            if val.startswith('"'):
                try:
                    val = json.loads(val)
                except ValueError:
                    pass
            meta[key] = val
    return meta, _DIPLODOC.sub("", text[m.end():], count=1)


def plain(s: str) -> str:
    """Markdown → текст для сообщения: ссылки → их текст, без разметки."""
    s = _LINK.sub(r"\1", s)
    s = re.sub(r"<br\s*/?>", ", ", s, flags=re.I)
    s = re.sub(r"</li>", "; ", s, flags=re.I)                      # списки в ячейках Ozon
    s = re.sub(r"</?(?:ul|ol|li|p|div)\b[^>]*>", " ", s, flags=re.I)
    # html-теги, YFM-директивы {% … %}, якоря {#…}, атрибуты {.класс}, шаблоны WB {{ … }}
    s = re.sub(r"<!--.*?-->|</?[a-zA-Z][^>]*>|\{%.*?%\}|\{#[^}]*\}|\{\.[^}]*\}|\{\{.*?\}\}", "", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"(?<![\w*])([*_])(\S(?:.*?\S)?)\1(?![\w*])", r"\2", s)    # _курсив_
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>])", r"\1", s)
    return " ".join(s.split())


def norm(s: str) -> str:
    return plain(s).lower()


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip(" ,.;:—-") + "…"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# ─── журналы изменений ───────────────────────────────────────────────────────

@dataclass
class Entry:
    market: str
    when: str             # дата, как её пишет журнал
    day: date | None
    head: str             # методы (Ozon, ЯМ) или заголовок записи (WB)
    text: str
    key: str
    tag: str = ""         # WB: Новое / Изменения; ЯМ: устарело
    area: str = ""        # WB: раздел API
    critical: bool = False


_GEN_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря")
_NOM_MONTHS = {"январь", "февраль", "март", "апрель", "май", "июнь", "июль",
               "август", "сентябрь", "октябрь", "ноябрь", "декабрь"}
WB_TAGS = {"Новое", "Изменения", "Устарело", "Удалено"}


def ru_date(label: str) -> date | None:
    m = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", label.lower())
    try:
        if m and m.group(2) in _GEN_MONTHS:
            return date(int(m.group(3)), _GEN_MONTHS.index(m.group(2)) + 1, int(m.group(1)))
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", label)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        pass
    return None


def sections(body: str, marker: str) -> list[tuple[str, str]]:
    parts = re.split(rf"^{re.escape(marker)}(.+)$", body, flags=re.M)
    return list(zip(parts[1::2], parts[2::2]))


def names(cell: str) -> str:
    """Ячейка «Метод»: ссылки или код через запятую, без адресов."""
    found = re.findall(r"\[([^\]]+)\]\(", cell) or re.findall(r"`([^`]+)`", cell)
    return ", ".join(plain(x) for x in found) if found else plain(cell)


def md_rows(block: str) -> list[list[str]]:
    lines = [ln.strip() for ln in block.splitlines()]
    rows = []
    for i, ln in enumerate(lines):
        if not ln.startswith("|") or _SEP.match(ln):
            continue
        if i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            continue                                   # шапка таблицы
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    return rows


def ozon_entries(body: str) -> list[Entry]:
    out = []
    for when, block in sections(body, "## "):
        when, day = plain(when), ru_date(when)
        rows = [r for r in md_rows(block) if len(r) >= 2]
        items = [(names(r[0]), plain(" ".join(r[1:]))) for r in rows]
        if not rows:                                   # записи без таблицы — абзацами
            items = [("", plain(p)) for p in re.split(r"\n\s*\n", block) if plain(p)]
        for head, text in items:
            head = "" if head in ("—", "–", "-") else head   # правка раздела, а не метода
            out.append(Entry("Ozon", when, day, head, text,
                             key=f"ozon|{when}|{norm(head)}|{norm(text)}"))
    return out


def _looks_like_body(line: str) -> bool:
    return (len(line) > 90 or "](" in line or "`" in line or "**" in line
            or line.startswith(("|", ">")) or bool(_LIST.match(line))
            or line.endswith((".", ":", ";", "!", "?")))


def _flow(lines: list[str]) -> str:
    """Абзацы и пункты списка в одну строку: пункты через «;», как их читают вслух."""
    out, prev_item = "", False
    for ln in lines:
        item = bool(_LIST.match(ln))
        text = plain(_LIST.sub("", ln))
        out = (out + ("; " if item and prev_item else " ") + text) if out else text
        prev_item = item
    return out


def wb_entries(body: str) -> list[Entry]:
    """Запись WB: «## ДД.ММ.ГГГГ», затем раздел, подраздел, заголовок и текст.
    Метка (Новое/Изменения) стоит строкой ПЕРЕД заголовком даты — то есть в хвосте
    предыдущей записи, а на стыке месяцев там ещё «Август / 2026»."""
    parts = re.split(r"^## (\d{2}\.\d{2}\.\d{4})\s*$", body, flags=re.M)
    pre = [ln.strip() for ln in parts[0].splitlines() if ln.strip()]
    tag = pre[-1] if pre and pre[-1] in WB_TAGS else ""
    out = []
    for when, block in zip(parts[1::2], parts[2::2]):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        next_tag = lines.pop() if lines and lines[-1] in WB_TAGS else ""
        while lines and (re.fullmatch(r"\d{4}", lines[-1]) or lines[-1].lower() in _NOM_MONTHS):
            lines.pop()
        critical = bool(lines) and lines[0].lower().startswith("критичное")
        if critical:
            lines = lines[1:]
        head_lines = []
        for ln in lines:
            if _looks_like_body(ln):
                break
            head_lines.append(ln)
        title = plain(head_lines[-1]) if head_lines else ""
        text = _flow(lines[len(head_lines):len(head_lines) + 6])
        key = f"wb|{when}|{norm(title) or norm(text)[:120]}"
        out.append(Entry("WB", when, ru_date(when), title, text, key, tag,
                         area=plain(head_lines[0]) if len(head_lines) > 1 else "",
                         critical=critical))
        tag = next_tag
    return out


def yfm_rows(block: str) -> list[list[str]]:
    """Таблицы YFM: `#| … |#`, строка — между `||`, ячейки — через `|`."""
    rows = []
    for table in re.findall(r"#\|(.*?)\|#", block, flags=re.S):
        for row in table.split("||"):
            cells = [c.strip() for c in row.split("|")]
            if len(cells) >= 2 and "описание изменений" not in norm(cells[-1]):
                rows.append(cells)
    return rows


def ym_entries(body: str, tag: str) -> list[Entry]:
    out = []
    for when, block in sections(body, "### "):
        m = re.search(r"\{#(\d{2})-(\d{2})-(\d{2})\}", when)
        day = None
        if m:
            try:
                day = date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                pass
        when = plain(when) + (f" {day.year}" if day else "")
        for cells in yfm_rows(block):
            head, text = names(cells[0]), plain(" ".join(cells[1:]))
            if head or text:
                out.append(Entry("ЯМ", when, day, head, text,
                                 key=f"ym|{when}|{norm(head)}|{norm(text)}", tag=tag))
    return out


def entries_of(path: str, text: str | None) -> list[Entry]:
    if not text:
        return []
    _meta, body = split_doc(text)
    if path.startswith("ozon/"):
        return ozon_entries(body)
    if path.startswith("wb/"):
        return wb_entries(body)
    return ym_entries(body, "устарело" if path.endswith("deprecated.md") else "")


# ─── методы по спекам ────────────────────────────────────────────────────────

def json_ops(text: str | None) -> dict[str, tuple[str, bool]]:
    try:
        spec = json.loads(text or "{}")
    except ValueError:
        return {}
    ops = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if method in HTTP and isinstance(op, dict):
                ops[f"{method.upper()} {path}"] = (plain(op.get("summary") or ""),
                                                   bool(op.get("deprecated")))
    return ops


def ym_ops(blobs: Blobs, at: str) -> dict[str, tuple[str, bool]]:
    """ЯМ: спека многофайловая — корень ссылается на файл на каждый путь."""
    root = blobs.get(at, "ym/api/openapi.yaml") or ""
    m = re.search(r"^paths:\n(.*?)(?=^\S|\Z)", root, flags=re.S | re.M)
    ops = {}
    for path, ref in re.findall(r"^  ['\"]?(/[^'\"\n]*?)['\"]?:\s*\n\s+\$ref:\s*['\"]?([^'\"\s]+)",
                                m.group(1) if m else "", flags=re.M):
        text = blobs.get(at, f"ym/api/{ref}") or ""
        for method, block in re.findall(r"^(get|post|put|patch|delete):[ \t]*\n((?:[ \t]+.*\n?|\n)*)",
                                        text, flags=re.M):
            summary = re.search(r"^  summary:\s*(.+)$", block, flags=re.M)
            ops[f"{method.upper()} {path}"] = (
                plain(summary.group(1).strip("'\" ")) if summary else "",
                bool(re.search(r"^  deprecated:\s*true", block, flags=re.M)))
    return ops


def spec_ops(blobs: Blobs, market: str, at: str) -> dict[str, tuple[str, bool]]:
    if market in ("ozon", "uzum"):
        return json_ops(blobs.get(at, f"{market}/api/seller/spec.json"))
    if market == "ym":
        return ym_ops(blobs, at)
    # WB: объединение по всем разделам. Устаревшие каталоги переименованных разделов
    # остаются в зеркале — так переезд раздела не выглядит как «все методы новые».
    ops = {}
    listing = git("ls-tree", "-r", "--name-only", at, "--", "wb/api", check=False)
    for path in listing.splitlines():
        if path.endswith("/spec.json"):
            ops.update(json_ops(blobs.get(at, path)))
    return ops


def spec_market(path: str) -> str | None:
    if path in ("ozon/api/seller/spec.json", "uzum/api/seller/spec.json"):
        return path.split("/", 1)[0]
    if path.startswith("wb/api/") and path.endswith("/spec.json"):
        return "wb"
    if path == "ym/api/openapi.yaml" or path.startswith("ym/api/paths/"):
        return "ym"
    return None


# ─── правки страниц ──────────────────────────────────────────────────────────

def doc_lines(text: str | None) -> list[str]:
    """Тело страницы построчно, без разметки: смена форматирования или адреса
    ссылки правкой не считается."""
    _meta, body = split_doc(text)
    out = []
    for raw in body.splitlines():
        ln = _BUILD_PATH.sub("", raw.strip())
        # Определения ссылок и сносок ([//]: …, [*термин]:) — служебная разметка.
        if not ln or _SEP.match(ln) or re.match(r"\[[^\]]*\]:", ln):
            continue
        if ln.startswith("|"):
            ln = " · ".join(c for c in (plain(x) for x in ln.strip("|").split("|")) if c)
        else:
            ln = plain(re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)+", "", ln))
        if re.search(r"\w", ln):         # строка из одной пунктуации (|, —, #|) — не текст
            out.append(ln)
    return out


def _meaningful(s: str) -> bool:
    return len(re.findall(r"\w", s)) >= 3


def word_diff(a: str, b: str) -> str | None:
    """Правка внутри строки: было зачёркнуто, стало жирным, вокруг — немного контекста."""
    ta, tb = a.split(), b.split()
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    if sm.ratio() < 0.5:
        return None
    ops, segs, ctx = sm.get_opcodes(), [], 4
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            w = ta[i1:i2]
            first, last = k == 0, k == len(ops) - 1
            if first and len(w) > ctx:
                w = ["…"] + w[-ctx:]
            elif last and len(w) > ctx:
                w = w[:ctx] + ["…"]
            elif not first and not last and len(w) > 2 * ctx:
                w = w[:ctx] + ["…"] + w[-ctx:]
            segs.append(("", " ".join(w)))
            continue
        if i2 > i1:
            segs.append(("s", " ".join(ta[i1:i2])))
        if i2 > i1 and j2 > j1:
            segs.append(("", "→"))       # зачёркивание видно не везде, стрелка — везде
        if j2 > j1:
            segs.append(("b", " ".join(tb[j1:j2])))
    out, used = [], 0
    for style, text in segs:
        room = SNIP - used
        if room < 12:
            out.append(("", "…"))
            break
        text = clip(text, room)
        out.append((style, text))
        used += len(text) + 1
    return " ".join(f"<{st}>{esc(t)}</{st}>" if st else esc(t) for st, t in out)


def _hunk_snippet(old: list[str], new: list[str]) -> str | None:
    old = [x for x in old if _meaningful(x)]
    new = [x for x in new if _meaningful(x)]
    if old and new:
        wd = word_diff(old[0], new[0])
        if wd:
            return wd
    # Вставка — несколько строк подряд: у таблицы первая строка часто лишь подпись.
    if new:
        return "+ " + esc(clip(" / ".join(new[:6]), SNIP))
    if old:
        return "− <s>" + esc(clip(" / ".join(old[:6]), SNIP)) + "</s>"
    return None


def _hunk_weight(old: list[str], new: list[str]) -> int:
    """Вес правки: изменённые слова, цифры втрое — ставки, сроки и лимиты важнее слов."""
    ta, tb = " ".join(old).split(), " ".join(new).split()
    changed = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag != "equal":
            changed += ta[i1:i2] + tb[j1:j2]
    return len(changed) + 2 * sum(1 for t in changed if re.search(r"\d", t))


def doc_change(old_text: str | None, new_text: str | None) -> tuple[int, list[str]] | None:
    """(вес правки, фрагменты) или None, если правка — шум."""
    a, b = doc_lines(old_text), doc_lines(new_text)
    if a == b:
        return None
    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            weight = _hunk_weight(a[i1:i2], b[j1:j2])
            snippet = _hunk_snippet(a[i1:i2], b[j1:j2])
            if weight and snippet:
                hunks.append((i1, weight, snippet))
    if not hunks:
        return None
    top, seen = [], set()
    for h in sorted(hunks, key=lambda h: -h[1]):
        if h[2] not in seen and len(top) < SNIPPETS:   # одна и та же вставка в двух местах
            top.append(h)
            seen.add(h[2])
    return sum(h[1] for h in hunks), [h[2] for h in sorted(top)]


def doc_group(path: str) -> str | None:
    if path.startswith("ozon/kb/"):
        return "Ozon · база знаний"
    if path.startswith("ozon/api/updates/"):
        return "Ozon · служебные чаты кабинета"
    if path.startswith("ozon/"):
        return "Ozon · справка API"
    if path.startswith("wb/"):
        return "WB · справка API"
    if path.startswith("ym/doc/"):
        return "ЯМ · справка"
    if path.startswith("ym/"):
        return "ЯМ · справка API"
    if path.startswith("uzum/kb/"):
        return "Uzum · инструкция продавца"
    if path.startswith("uzum/"):
        return "Uzum · справка API"
    return None


def doc_link(meta: dict, path: str) -> str:
    title = esc(clip(meta.get("title") or path.rsplit("/", 1)[-1], 110))
    url = meta.get("source") or ""
    if url.startswith("https://yandex.ru/") and url.endswith(".md"):
        url = url[:-3]                   # .md — сырой двойник страницы, человеку нужна сама страница
    return f'<a href="{html.escape(url)}">{title}</a>' if url.startswith("http") else title


def method_section(path: str) -> str:
    parts = path.split("/")
    return parts[2] if path.startswith("wb/") else parts[3] if len(parts) > 4 else ""


# ─── сводка ──────────────────────────────────────────────────────────────────

@dataclass
class Findings:
    entries: dict           # название журнала → [Entry]
    added: dict             # площадка → {операция: (summary, deprecated)}
    removed: dict
    deprecated: dict
    changed: dict           # площадка → [(раздел, операция, title)]
    docs: dict              # группа → [(вес, новая ли, ссылка, [фрагменты])]
    announced: str          # текст свежих записей всех журналов — для «без анонса»
    sources: dict           # новая площадка → [методов, страниц справки]

    def empty(self) -> bool:
        return not any((self.entries, self.added, self.removed, self.deprecated,
                        self.changed, self.docs, self.sources))


def collect(base: str, head: str) -> Findings:
    blobs = Blobs()
    try:
        return _collect(blobs, base, head)
    finally:
        blobs.close()


def _collect(blobs: Blobs, base: str, head: str) -> Findings:
    files = changed_files(base, head)

    # Журналы: новые записи — те, которых не было на базовой ревизии. ЯМ пишет одну
    # и ту же запись в all.md и main.md, поэтому ключ общий на все файлы площадки.
    entries: dict[str, dict[str, Entry]] = defaultdict(dict)
    for status, _old, path in files:
        if path in CHANGELOGS and status != "D":
            old_text = blobs.get(base, path)
            if old_text is None:
                continue                  # журнал только появился в зеркале — это его история, а не новости
            before = {e.key for e in entries_of(path, old_text)}
            for e in entries_of(path, blobs.get(head, path)):
                if e.key not in before:
                    known = entries[CHANGELOGS[path]].get(e.key)
                    if known and e.tag:
                        known.tag = e.tag
                    entries[CHANGELOGS[path]].setdefault(e.key, e)

    # Что считается объявленным: всё свежее из журналов на новой ревизии.
    horizon = datetime.now(TZ).date() - timedelta(days=ANNOUNCE_DAYS)
    fresh = [e for path in CHANGELOGS for e in entries_of(path, blobs.get(head, path))
             if e.day is None or e.day >= horizon]
    announced = "\n".join(f"{e.head} {e.text}" for e in fresh).lower()

    added, removed, deprecated = {}, {}, {}
    for market in sorted({m for _s, _o, p in files if (m := spec_market(p))}):
        old, new = spec_ops(blobs, market, base), spec_ops(blobs, market, head)
        if not old:
            continue                      # спеки раньше не было — это первое наполнение, а не новости
        if a := {k: new[k] for k in new.keys() - old.keys()}:
            added[market] = a
        if r := {k: old[k] for k in old.keys() - new.keys()}:
            removed[market] = r
        if d := {k: new[k] for k in new.keys() & old.keys() if new[k][1] and not old[k][1]}:
            deprecated[market] = d

    # Площадка появилась в зеркале целиком — это не десятки «новых страниц», а новый
    # источник: одной строкой. Спеки и журналы первое наполнение и так не считают.
    present = set(git("ls-tree", "--name-only", base, check=False).split())
    sources: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    changed: dict[str, list] = defaultdict(list)
    docs: dict[str, list] = defaultdict(list)
    for status, old_path, path in files:
        name = path.rsplit("/", 1)[-1]
        if (status == "D" or not path.endswith(".md") or name in ("index.md", "README.md")
                or path in CHANGELOGS or path.startswith("ym/api/components/")):
            continue
        new_text = blobs.get(head, path)
        meta, _body = split_doc(new_text)
        market = path.split("/", 1)[0]
        if market not in present:
            sources[market][0 if meta.get("method") else 1] += 1
            continue
        if status[:1] in "CR":
            # Переезд почти не меняет текст, а короткая страница с правкой ссылок
            # теряет в сходстве — поэтому переезд ещё и тот, где заголовок прежний.
            # Похожая страница с другим заголовком (у статей-архивов общий шаблон) — новая.
            old_title = split_doc(blobs.get(base, old_path))[0].get("title")
            moved = int(status[1:] or 0) >= 90 or (old_title and old_title == meta.get("title"))
            status, old_path = ("M", old_path) if moved else ("A", path)
        if meta.get("method") and meta.get("path"):
            op = f"{meta['method'].upper()} {meta['path']}"
            # Новые файлы методов — это новые методы, а их видно по спеке. Устаревание
            # тоже видно по спеке. Здесь — только правки, в том числе у переехавших.
            if (status == "M" and op not in added.get(market, {})
                    and op not in deprecated.get(market, {})
                    and doc_lines(blobs.get(base, old_path)) != doc_lines(new_text)):
                changed[market].append((method_section(path), op, meta.get("title") or ""))
            continue
        group = doc_group(path)
        if not group:
            continue
        if status == "A":
            lead = [ln for ln in doc_lines(new_text)[1:8]
                    if _meaningful(ln) and not ln.startswith("Главная /")][:4]
            docs[group].append((10 ** 6, True, doc_link(meta, path),
                                ["+ " + esc(clip(" / ".join(lead), SNIP))] if lead else []))
        elif (change := doc_change(blobs.get(base, old_path), new_text)):
            docs[group].append((change[0], False, doc_link(meta, path), change[1]))

    return Findings({k: list(v.values()) for k, v in entries.items() if v},
                    added, removed, deprecated, dict(changed), dict(docs), announced,
                    dict(sources))


def _is_announced(op: str, announced: str) -> bool:
    path = op.split(" ", 1)[-1].lstrip("/").lower()
    return bool(re.search(re.escape(path) + r"(?![\w/{-])", announced))


def _op_line(icon: str, market: str, op: str, summary: str, announced: str, note: str = "") -> str:
    line = f"{icon} {MARKET[market]} · <code>{esc(op)}</code>"
    if summary:
        line += f" — {esc(clip(summary, 110))}"
    if note:
        line += f" · {note}"
    if not _is_announced(op, announced):
        line += " · <i>без анонса</i>"
    return line


def render(f: Findings) -> list[list[str]]:
    """Сообщение как список блоков; блок — строки, которые нельзя разрывать."""
    blocks: list[list[str]] = []

    for market, (ops, pages) in f.sources.items():
        what = ", ".join(x for x in (
            f"{ops} {plural(ops, 'метод', 'метода', 'методов')} API" if ops else "",
            f"{pages} {plural(pages, 'страница', 'страницы', 'страниц')} справки" if pages else "")
            if x)
        blocks.append([f"🆕 <b>Новая площадка в зеркале: {esc(MARKET.get(market, market))}</b>"
                       + (f" — {what}" if what else "")])

    for title in dict.fromkeys(CHANGELOGS.values()):
        items = f.entries.get(title)
        if not items:
            continue
        # Заголовок журнала и дата держатся за первой записью: при разбиении на
        # сообщения они не должны остаться висеть в конце предыдущего.
        lead, last_when = [f"<b>{esc(title)}</b>"], None
        for e in items[:LIST_ENTRIES]:
            if e.when != last_when:
                lead.append(f"<u>{esc(e.when)}</u>")
                last_when = e.when
            mark = "‼️ " if e.critical else ""
            tag = f"<i>{esc(e.tag)}</i> · " if e.tag else ""
            if e.market == "WB":
                area = f" ({esc(e.area)})" if e.area else ""
                line = f"• {mark}{tag}<b>{esc(clip(e.head, 120))}</b>{area}"
            else:
                line = f"• {mark}{tag}<code>{esc(clip(e.head, 160))}</code>" if e.head else f"• {mark}{tag}"
            if e.text:
                line += (" — " if e.head else "") + esc(clip(e.text, 260))
            blocks.append(lead + [line])
            lead = []
        if len(items) > LIST_ENTRIES:
            rest = len(items) - LIST_ENTRIES
            blocks.append([f"…и ещё {rest} {plural(rest, 'запись', 'записи', 'записей')}"])

    ops_lines = []
    for icon, bucket, note in (("🆕", f.added, ""), ("🗑", f.removed, "убран из спеки"),
                               ("⚠️", f.deprecated, "помечен устаревшим")):
        for market in MARKET:
            ops = sorted(bucket.get(market, {}).items())
            for op, (summary, _dep) in ops[:LIST_OPS]:
                ops_lines.append(_op_line(icon, market, op, summary, f.announced, note))
            if len(ops) > LIST_OPS:
                rest = len(ops) - LIST_OPS
                ops_lines.append(f"{icon} {MARKET[market]} · …и ещё {rest} "
                                 f"{plural(rest, 'метод', 'метода', 'методов')}")
    for market in MARKET:
        items = sorted(f.changed.get(market, []))
        if not items:
            continue
        if len(items) <= LIST_CHANGED:
            ops_lines += [_op_line("✏️", market, op, title, f.announced) for _s, op, title in items]
            continue
        by_section = defaultdict(int)
        for section, _op, _t in items:
            by_section[section] += 1
        silent = sum(1 for _s, op, _t in items if not _is_announced(op, f.announced))
        parts = ", ".join(f"{s} {n}" for s, n in sorted(by_section.items(), key=lambda x: -x[1]))
        n = len(items)
        ops_lines.append(f"✏️ {MARKET[market]} · правки в {n} {plural(n, 'методе', 'методах', 'методах')}"
                         + (f", без анонса {silent}" if silent else "") + f": {esc(parts)}")
    if ops_lines:
        blocks.append(["<b>Методы API</b>", ops_lines[0]])
        blocks += [[ln] for ln in ops_lines[1:]]

    for group in sorted(f.docs, key=lambda g: list(MARKET.values()).index(g.split(" ")[0])):
        items = sorted(f.docs[group], key=lambda d: -d[0])
        new = sum(1 for d in items if d[1])
        counts = ", ".join(x for x in (f"новых {new}" if new else "",
                                       f"изменено {len(items) - new}" if len(items) > new else "") if x)
        head = f"<b>{esc(group)}</b> — {counts}"
        detailed, rest = items[:DOCS_DETAILED], items[DOCS_DETAILED:]
        for i, (_w, is_new, link, snippets) in enumerate(detailed):
            lines = [f"• {'🆕 ' if is_new else ''}{link}"] + [f"   {s}" for s in snippets]
            if i == len(detailed) - 1 and rest:
                # Хвост держится за последним пунктом группы, чтобы не уехать в
                # следующее сообщение без заголовка.
                more = len(rest) - 15
                lines.append("Ещё: " + ", ".join(link for _w, _n, link, _s in rest[:15])
                             + (f" и ещё {more}" if more > 0 else ""))
            blocks.append([head] + lines if i == 0 else lines)
    return blocks


def summary_line(f: Findings) -> str:
    """Вторая строка — то, что видно в превью уведомления на телефоне."""
    out = []
    for market in MARKET:
        name, parts = MARKET[market], []
        if market in f.sources:
            parts.append("новая площадка")
        journal = sum(len(v) for k, v in f.entries.items() if k.startswith(name + " "))
        if journal:
            parts.append(f"журнал {journal}")
        for label, n in (("новые методы", len(f.added.get(market, {}))),
                         ("убраны методы", len(f.removed.get(market, {}))),
                         ("устарели", len(f.deprecated.get(market, {}))),
                         ("правки методов", len(f.changed.get(market, [])))):
            if n:
                parts.append(f"{label} {n}")
        docs = sum(len(v) for k, v in f.docs.items() if k.startswith(name + " "))
        if docs:
            parts.append(f"справка {docs}")
        if parts:
            out.append(f"{name}: " + ", ".join(parts))
    return " · ".join(out)


def _visible(s: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


def pack(blocks: list[list[str]], header: str, footer: list[str]) -> list[str]:
    msgs, cur = [], header
    dropped, section = 0, ""
    # Место под подвал (ссылка на дифф, сбои) держим заранее — иначе он уезжает
    # отдельным сообщением из одной ссылки.
    limit = TG_LIMIT - _visible("\n".join(footer)) - 80
    for block in blocks:
        if m := re.match(r"<b>.*?</b>", block[0]):
            section = m.group(0)
        text = "\n".join(block)
        if dropped or _visible(cur) + _visible(text) + 2 > limit:
            if dropped or len(msgs) + 1 >= MAX_MESSAGES:
                dropped += 1             # начали отбрасывать — отбрасываем до конца, без дыр
                continue
            msgs.append(cur)
            # Группа разорвалась между сообщениями — напомним, чья это середина.
            cur = text if block[0].startswith("<b>") else f"{section} <i>(продолжение)</i>\n{text}"
        else:
            cur += ("\n\n" if block[0].startswith("<b>") else "\n") + text
    tail = list(footer)
    if dropped:
        tail.insert(0, f"…не влезло ещё {dropped} {plural(dropped, 'пункт', 'пункта', 'пунктов')} — "
                       "смотри полный дифф")
    tail_text = "\n".join(tail)
    if tail_text and _visible(cur) + _visible(tail_text) + 2 > TG_LIMIT:
        msgs.append(cur)
        cur = tail_text
    elif tail_text:
        cur += "\n\n" + tail_text
    msgs.append(cur)
    return msgs


def build(base: str, head: str, failed: list[dict] | None = None) -> list[str]:
    """Готовые сообщения (HTML) или пустой список, если сообщать нечего."""
    f = collect(base, head) if base != head else None
    failed = failed or []
    if (f is None or f.empty()) and not failed:
        return []

    commits = git("rev-list", "--count", f"{base}..{head}", check=False).strip()
    single = commits == "1"
    stamps = git("log", "--format=%ct", f"{base}..{head}", check=False).split()
    days = sorted({datetime.fromtimestamp(int(s), TZ).strftime("%d.%m") for s in stamps})
    when = days[-1] if len(days) == 1 else f"{days[0]}–{days[-1]}" if days else \
        datetime.now(TZ).strftime("%d.%m")

    if f and not f.empty():
        header = (f"<b>📚 Что нового в документации маркетплейсов · {when}</b>\n"
                  + esc(summary_line(f)))
    else:
        # Новостей нет, но источник упал: молчание тогда значило бы «ничего не
        # поменялось», а на деле мы просто не посмотрели.
        header = f"<b>📚 Зеркало документации: обновилось не всё · {when}</b>"

    footer = []
    for item in failed:
        err = re.sub(r"://[^/\s:@]+:[^/\s@]+@", "://***@", str(item.get("error", "")))
        footer.append(f"⚠️ Не обновился источник {esc(item.get('source', '?'))}: "
                      f"{esc(clip(err, 200))}")
    if f and not f.empty():
        link = web_link(base, head, single)
        footer.append(f'<a href="{link}">Полный дифф</a>' if link else
                      "Полный дифф: <code>mpdocs changes</code>")
    return pack(render(f) if f else [], header, footer)


# ─── отправка ────────────────────────────────────────────────────────────────

def telegram_config() -> tuple[str, str] | None:
    """Бот и чат: переменные окружения, затем ~/.config/mp-docs/config.json."""
    token = os.environ.get("MP_DOCS_TG_BOT_TOKEN")
    chat = os.environ.get("MP_DOCS_TG_CHAT_ID")
    if token and chat:
        return token, chat
    if CONFIG.exists():
        try:
            tg = json.loads(CONFIG.read_text(encoding="utf-8")).get("telegram") or {}
        except ValueError:
            log(f"telegram: {CONFIG} не читается как JSON")
            return None
        if tg.get("bot_token") and tg.get("chat_id"):
            return str(tg["bot_token"]), str(tg["chat_id"])
    return None


def _post(token: str, payload: dict) -> tuple[bool, str]:
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as exc:
            # Текст исключения requests содержит URL, а в нём токен — печатаем только тип.
            err = type(exc).__name__
            time.sleep(5 * (attempt + 1))
            continue
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 429:
            time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
            err = "429"
            continue
        if data.get("ok"):
            return True, ""
        return False, f"HTTP {r.status_code}: {data.get('description', '')}"
    return False, err


def send(messages: list[str], cfg: tuple[str, str]) -> bool:
    token, chat = cfg
    for i, text in enumerate(messages):
        if i:
            time.sleep(1)
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        ok, err = _post(token, payload)
        if not ok and "parse" in err.lower():
            # Разметка не разобралась — лучше простым текстом, чем никак.
            payload = {"chat_id": chat, "text": html.unescape(re.sub(r"<[^>]+>", "", text)),
                       "disable_web_page_preview": True}
            ok, err = _post(token, payload)
        if not ok:
            log(f"!! telegram: сообщение {i + 1}/{len(messages)} не ушло ({err})")
            return False
    return True


def _remember(head: str) -> None:
    save_cache(STATE, {"last_commit": head, "at": now_iso()})


def after_update(head_before: str | None, failed: list[dict]) -> bool | None:
    """Вызывается из `mpdocs update`. None — сообщать нечего или Telegram не настроен."""
    cfg = telegram_config()
    head = rev("HEAD")
    if not cfg or not head:
        return None
    base = load_cache(STATE).get("last_commit") or ""
    if not base or not rev(base) or not is_ancestor(base, head):
        base = head_before or head
    messages = build(base, head, failed)
    if not messages:
        if base != head:
            log("telegram: в изменениях только шум — сообщать нечего")
            _remember(head)
        return None
    ok = send(messages, cfg)
    log(f"telegram: сводка отправлена ({len(messages)} сообщ.)" if ok
        else "!! telegram: сводка не ушла — повторю со следующим прогоном")
    if ok:
        _remember(head)
    return ok


def cli(since: str | None, until: str | None, dry_run: bool, as_html: bool) -> int:
    head = rev(until or "HEAD")
    base = rev(since) if since else rev(load_cache(STATE).get("last_commit") or "HEAD~1")
    if not head or not base:
        print("не нашёл ревизию в зеркале")
        return 2
    messages = build(base, head)
    if not messages:
        print(f"после {head[:9]} обновлений не было" if base == head
              else f"{base[:9]}..{head[:9]}: ничего нового, только шум")
        return 0
    if dry_run:
        for i, text in enumerate(messages):
            if i:
                print("\n" + "─" * 60 + "\n")
            print(text if as_html else html.unescape(re.sub(r"<[^>]+>", "", text)))
        return 0
    cfg = telegram_config()
    if not cfg:
        print("Telegram не настроен: MP_DOCS_TG_BOT_TOKEN/MP_DOCS_TG_CHAT_ID "
              f"или секция telegram в {CONFIG}")
        return 2
    if not send(messages, cfg):
        return 1
    if head == rev("HEAD"):
        _remember(head)
    print(f"отправлено сообщений: {len(messages)}")
    return 0
