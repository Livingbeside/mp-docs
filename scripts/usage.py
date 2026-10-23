"""Касается ли нашего кода: какие методы API площадок вызывают наши проекты и что
с ними в свежей документации.

Каждую ночь после обновления зеркала (notify.after_update) скачивает рабочие
проекты Apps Script, ищет в коде проектов пути методов — все, что когда-либо были
в спеках зеркала, включая уже удалённые, — и сверяет их со спекой на HEAD:

  🔴 метода в документации больше нет, или дата отключения прошла;
  🟠 метод устарел — с датой отключения, если она объявлена;
  ✏️ в этом обновлении поменялась документация метода или о нём запись в журнале;
  ✅ метод, о котором предупреждали, из кода ушёл.

Постоянное не повторяется каждую ночь: новое — сразу, 🟠 — за 30/14/7/3/2/1/0 дней
до отключения, 🔴 — раз в неделю. Что уже сообщено — state/usage-reported.json.

Что сканировать — usage.local.json в корне репозитория (в .gitignore: пути и
скрипты у каждой машины свои). Нет файла — проверка выключена.

    {"roots": {"fbs-hub": "/root/fbs-hub"},
     "apps_script": {"EASY": "<scriptId>"},
     "ignore": ["Отгрузки Склад/make-order-list.js", "/v1/some/path"]}

`ignore` — шаблоны fnmatch по «проект/файл» или по пути метода.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import notify
from common import ROOT, STATE, git, load_cache, log, now_iso, save_cache, slug
from notify import MARKET, TZ, Blobs, clip, esc, plain, plural

CONFIG = ROOT / "usage.local.json"
GAS_DIR = STATE / "usage-gas"
KNOWN = "usage-known.json"          # все пути методов, когда-либо бывшие в спеках
REPORTED = "usage-reported.json"    # что и когда уже сообщили
WB_SECTIONS = "wb-sections.json"    # живые разделы WB, пишет src_wb

SPEC_FILES = ("ozon/api/seller/spec.json", "uzum/api/seller/spec.json", "wb/api",
              "ym/api/openapi.yaml", "ym/api/paths")
CODE_GLOB = "*.{py,js,mjs,cjs,ts,tsx,jsx,gs,php,go,rb,sh}"
SKIP_DIRS = ("node_modules", ".git", "venv", ".venv", "site-packages", "dist", "build",
             ".next", "__pycache__", "coverage", "backups")
REMIND_DAYS = (30, 14, 7, 3, 2, 1, 0)
BROKEN_EVERY = 7       # дней между напоминаниями о сломанном
LOCS = 3               # мест в коде на строку сводки
CLASP_CANDIDATES = ("/root/.hermes/node/bin/clasp", "/usr/local/bin/clasp", "/usr/bin/clasp")

# Сегмент-параметр пути в коде: ${id}, {id} (f-строки), ' + id + ', литерал, %s.
_PARAM = (r"(?:\$\{[^}\n]*\}|\{[^}\n]*\}|['\"`]\s*\+\s*[\w.\[\]()]+(?:\s*\+\s*['\"`])?"
          r"|[^/\s'\"`?#{}()+]+)")
_OFF = re.compile(r"отключ|перестан|удалим|выключ|прекрат", re.I)
_API_PATH = re.compile(r"/v\d+/[\w/{}.-]+")


def load_config() -> dict | None:
    if not CONFIG.exists():
        return None
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except ValueError:
        log(f"usage: {CONFIG.name} не читается как JSON — проверка пропущена")
        return None


# ─── Apps Script ─────────────────────────────────────────────────────────────

def refresh_gas(cfg: dict) -> list[str]:
    """Свежие копии скриптов Google. Контейнерные скрипты clasp не перечисляет —
    только по id. В копиях бывают ключи (EASY), поэтому каталог закрыт (700)."""
    scripts = cfg.get("apps_script") or {}
    if not scripts:
        return []
    clasp = shutil.which("clasp") or next((c for c in CLASP_CANDIDATES if Path(c).exists()), None)
    if not clasp:
        return ["clasp не найден — скрипты Google проверены по прошлой копии"]
    env = {**os.environ, "PATH": f"{Path(clasp).parent}:{os.environ.get('PATH', '')}"}
    GAS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(GAS_DIR, 0o700)
    errors = []
    for name, sid in scripts.items():
        tmp = Path(tempfile.mkdtemp(dir=GAS_DIR, prefix=".clone-"))
        try:
            r = subprocess.run([clasp, "clone", sid], cwd=tmp, env=env, capture_output=True,
                               text=True, timeout=180)
            ok = r.returncode == 0 and any(p.suffix in (".js", ".gs") for p in tmp.iterdir())
        except subprocess.TimeoutExpired:
            r, ok = None, False
        if not ok:
            why = "таймаут" if r is None else clip(" ".join((r.stderr or r.stdout).split()), 160)
            errors.append(f"скрипт Google «{name}» не скачался ({why}) — проверен по прошлой копии")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        dst = GAS_DIR / slug(name)
        shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
    return errors


def roots(cfg: dict) -> dict[str, Path]:
    out = {name: Path(p) for name, p in (cfg.get("roots") or {}).items()}
    for name in cfg.get("apps_script") or {}:
        d = GAS_DIR / slug(name)
        if d.exists():
            out[name] = d
    return out


# ─── пути методов ────────────────────────────────────────────────────────────

def known_paths(blobs: Blobs, head: str) -> dict[str, set[str]]:
    """Все пути, что когда-либо были в спеках зеркала, — иначе удалённый метод в
    коде не узнать. Копится в state/, за ночь добавляются только новые коммиты."""
    cache = load_cache(KNOWN)
    paths = {m: set(v) for m, v in (cache.get("paths") or {}).items()}
    upto = cache.get("upto")
    rng = f"{upto}..{head}" if upto and notify.rev(upto) and notify.is_ancestor(upto, head) else head
    commits = git("rev-list", "--reverse", rng, "--", *SPEC_FILES, check=False).split()
    for c in commits:
        files = git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", c, check=False)
        for market in {m for f in files.splitlines() if (m := notify.spec_market(f))}:
            for op in notify.spec_ops(blobs, market, c):
                paths.setdefault(market, set()).add(op.split(" ", 1)[1])
    if commits or not cache:
        save_cache(KNOWN, {"upto": head, "at": now_iso(),
                           "paths": {m: sorted(v) for m, v in paths.items()}})
    return paths


@dataclass
class Op:
    method: str
    deprecated: bool
    text: str          # summary + description, простым текстом


def _json_detail(text: str | None) -> dict[str, list[Op]]:
    try:
        spec = json.loads(text or "{}")
    except ValueError:
        return {}
    out: dict[str, list[Op]] = defaultdict(list)
    for path, item in (spec.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if method in notify.HTTP and isinstance(op, dict):
                out[path].append(Op(method.upper(), bool(op.get("deprecated")),
                                    plain(f"{op.get('summary') or ''}. {op.get('description') or ''}")[:3000]))
    return out


def _ym_detail(blobs: Blobs, at: str) -> dict[str, list[Op]]:
    root = blobs.get(at, "ym/api/openapi.yaml") or ""
    m = re.search(r"^paths:\n(.*?)(?=^\S|\Z)", root, flags=re.S | re.M)
    out: dict[str, list[Op]] = defaultdict(list)
    for path, ref in re.findall(r"^  ['\"]?(/[^'\"\n]*?)['\"]?:\s*\n\s+\$ref:\s*['\"]?([^'\"\s]+)",
                                m.group(1) if m else "", flags=re.M):
        text = blobs.get(at, f"ym/api/{ref}") or ""
        for method, block in re.findall(r"^(get|post|put|patch|delete):[ \t]*\n((?:[ \t]+.*\n?|\n)*)",
                                        text, flags=re.M):
            out[path].append(Op(method.upper(),
                                bool(re.search(r"^  deprecated:\s*true", block, flags=re.M)),
                                plain(block)[:3000]))
    return out


def head_detail(blobs: Blobs, head: str) -> dict[str, dict[str, list[Op]]]:
    out = {"ozon": _json_detail(blobs.get(head, "ozon/api/seller/spec.json")),
           "uzum": _json_detail(blobs.get(head, "uzum/api/seller/spec.json")),
           "ym": _ym_detail(blobs, head), "wb": defaultdict(list)}
    live = set(load_cache(WB_SECTIONS).get("sections") or [])
    listing = git("ls-tree", "-r", "--name-only", head, "--", "wb/api", check=False)
    for f in listing.splitlines():
        if f.endswith("/spec.json") and (not live or f.split("/")[2] in live):
            for path, ops in _json_detail(blobs.get(head, f)).items():
                out["wb"][path] += ops
    return out


_PARAM_WORDS = re.compile(r"параметр|\bпол[еяюи]\b|значени|заголов", re.I)
_DATES = re.compile(r"(\d{1,2})\s+(" + "|".join(notify._GEN_MONTHS) + r")\s+(\d{4})|(\d{2})\.(\d{2})\.(\d{4})",
                    re.I)


def _nearest_date(sent: str, at: int) -> date | None:
    """Во фразе бывает две даты («С 5 октября … новые этикетки … С 2 ноября метод
    будет отключён») — берём ближайшую к слову про отключение."""
    best = None
    for m in _DATES.finditer(sent):
        try:
            d = (date(int(m.group(3)), notify._GEN_MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))
                 if m.group(1) else date(int(m.group(6)), int(m.group(5)), int(m.group(4))))
        except ValueError:
            continue
        dist = abs(m.start() - at)
        if best is None or dist < best[0]:
            best = (dist, d)
    return best[1] if best else None


@dataclass
class OffNote:
    when: date
    text: str
    param: bool        # отключают параметр/поле, а не весь метод


def off_notes(text: str, path: str) -> list[OffNote]:
    """Фразы описания про отключение — с датой и без чужого пути в них
    («…/v3/… будет отключён» в описании v4 — это не про нас)."""
    own = path.lstrip("/")
    out = []
    for sent in re.split(r"(?<=[.!?])[\s,]+|\n", text):
        m = _OFF.search(sent)
        if not m or any(p.lstrip("/") != own for p in _API_PATH.findall(sent)):
            continue
        low = sent.lower()
        param = bool(_PARAM_WORDS.search(low))
        if not param and "метод" not in low:
            continue                # не про метод и не про параметр — непонятно что
        if d := _nearest_date(sent, m.start()):
            out.append(OffNote(d, sent.strip(), param))
    return out


def journal_dates(blobs: Blobs, head: str) -> dict[str, date]:
    """Путь → дата отключения из самой свежей записи журнала, где она названа."""
    best: dict[str, tuple[date, date]] = {}
    for path in dict.fromkeys(notify.CHANGELOGS):
        for e in notify.entries_of(path, blobs.get(head, path)):
            m = _OFF.search(e.text)
            if not m or _PARAM_WORDS.search(e.text) or not (d := _nearest_date(e.text, m.start())):
                continue            # про параметр — это не дата отключения метода
            for p in _API_PATH.findall(e.head + " " + e.text):
                key = "/" + p.lstrip("/")
                when = e.day or date.min
                if key not in best or when >= best[key][0]:
                    best[key] = (when, d)
    return {k: v[1] for k, v in best.items()}


# ─── поиск в коде ────────────────────────────────────────────────────────────

@dataclass
class Pattern:
    market: str
    path: str
    needle: str
    rx: re.Pattern


def compile_path(market: str, path: str) -> Pattern | None:
    segs = [s for s in path.strip("/").split("/") if s]
    lead_v2 = market == "ym" and segs and segs[0] == "v2"
    body_segs = segs[1:] if lead_v2 else segs
    static = [s for s in body_segs if "{" not in s]
    if len(static) < 2:
        return None             # «/ping», «/{id}» — такое найдётся где угодно
    runs, cur = [], []
    for s in body_segs:
        if "{" in s:
            runs.append(cur)
            cur = []
        else:
            cur.append(s)
    runs.append(cur)
    needle = max(("/".join(r) for r in runs if r), key=len)
    parts = [_PARAM if re.fullmatch(r"\{[^}]*\}", s)
             else re.sub(r"\\\{[^\\]*\\\}", lambda _m: _PARAM, re.escape(s)) for s in body_segs]
    body = "/".join(parts)
    if lead_v2:
        body = "(?:v2/)?" + body   # ЯМ принимает пути и с /v2, и без
    rx = re.compile(r"(?<![\w.-])/?" + body + r"(?![\w/-])(?!\s*\+\s*['\"`]/)")
    return Pattern(market, path, needle, rx)


def strip_comment(text: str, ext: str) -> str:
    if ext in ("py", "sh", "rb"):
        return re.sub(r"(^|\s)#.*$", r"\1", text)
    if re.match(r"\s*(\*|/\*)", text):
        return ""
    text = re.sub(r"/\*.*?\*/", "", text)
    text = re.sub(r"(^|[^:\\])//.*$", r"\1", text)
    return re.sub(r"(^|\s)#.*$", r"\1", text) if ext == "php" else text


Loc = tuple[str, str, int]          # проект, файл, строка


def comment_lines(file: str, ext: str) -> set[int]:
    """Строки внутри многострочных комментариев и docstring'ов — путь метода в
    описании модуля («на смену /v2/…/act/create, который отключают») не вызов."""
    try:
        lines = Path(file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    out, inside = set(), None
    for i, line in enumerate(lines, 1):
        if inside:
            out.add(i)
            if inside in line:
                inside = None
            continue
        st = line.lstrip()
        if ext == "py":
            m = re.match(r"[rRbBuUfF]{0,2}(\"\"\"|\'\'\')", st)
            if m:                   # строка-выражение в тройных кавычках — docstring
                out.add(i)
                if m.group(1) not in st[m.end():]:
                    inside = m.group(1)
        elif ext not in ("sh", "rb") and st.startswith("/*"):
            out.add(i)
            if "*/" not in st[2:]:
                inside = "*/"
    return out


def scan(cfg: dict, known: dict[str, set[str]]) -> dict[tuple[str, str], list[Loc]]:
    pats = [p for m, paths in known.items() for path in paths if (p := compile_path(m, path))]
    by_needle: dict[str, list[Pattern]] = defaultdict(list)
    for p in pats:
        by_needle[p.needle].append(p)
    ignore = cfg.get("ignore") or []
    used: dict[tuple[str, str], list[Loc]] = defaultdict(list)
    skip_lines: dict[str, set[int]] = {}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as nf:
        nf.write("\n".join(by_needle))
    try:
        for name, root in roots(cfg).items():
            if not root.exists():
                log(f"usage: каталога {root} нет — пропущен")
                continue
            cmd = ["rg", "--no-ignore", "--hidden", "-n", "--no-heading", "--with-filename",
                   "-M", "3000", "-F", "-f", nf.name, "-g", CODE_GLOB,
                   *[a for d in SKIP_DIRS for a in ("-g", f"!{d}")], str(root)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout
            for line in out.splitlines():
                try:
                    file, ln, text = line.split(":", 2)
                except ValueError:
                    continue
                rel = os.path.relpath(file, root)
                where = f"{name}/{rel}"
                ext = rel.rsplit(".", 1)[-1]
                if file not in skip_lines:
                    skip_lines[file] = comment_lines(file, ext)
                if int(ln) in skip_lines[file]:
                    continue
                code = strip_comment(text, ext)
                for needle, group in by_needle.items():
                    if needle not in code:
                        continue
                    for p in group:
                        if p.rx.search(code) and not any(
                                fnmatch.fnmatch(where, g) or fnmatch.fnmatch(p.path, g)
                                or g == f"{p.market} {p.path}" for g in ignore):
                            used[(p.market, p.path)].append((name, rel, int(ln)))
    finally:
        os.unlink(nf.name)
    return used


def param_name(note: str) -> str | None:
    """«отключим параметр result.total в ответе» → total."""
    m = re.search(r"параметр\w*\s+[`'\"«]?([A-Za-z_][\w.\[\]]*)", note)
    return re.split(r"[.\[\]]+", m.group(1).strip(".[]"))[-1] if m else None


def field_readers(cfg: dict, locs: list[Loc], name: str, around: int = 60) -> list[Loc]:
    """Места вызова, рядом с которыми код читает поле name: ['total'], .total, "total".
    Тесты не в счёт: заглушка ответа — не чтение поля."""
    rx = re.compile(rf"""['"`]{re.escape(name)}['"`]|\.{re.escape(name)}\b""")
    where, out = roots(cfg), []
    for loc in locs:
        proj, rel, ln = loc
        if "test" in rel.lower():
            continue
        try:
            lines = (where[proj] / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, KeyError):
            out.append(loc)         # не прочитали — лучше предупредить
            continue
        if any(rx.search(x) for x in lines[max(0, ln - around):ln + around]):
            out.append(loc)
    return out


# ─── отчёт ───────────────────────────────────────────────────────────────────

@dataclass
class Item:
    icon: str
    market: str
    path: str
    what: str
    locs: list[Loc]
    key: str = ""          # для постоянного: чем запомнить
    sig: str = ""          # статус + дата: поменялся — сообщить заново
    left: int | None = None


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    used: int = 0
    standing: dict = field(default_factory=dict)    # key → (sig, left) всего постоянного

    def blocks(self) -> list[list[str]]:
        if not self.items:
            return []
        lines = [_line(i) for i in self.items]
        return [["<b>🔧 Касается нашего кода</b>", lines[0]]] + [[ln] for ln in lines[1:]]

    def summary(self) -> str:
        n = defaultdict(int)
        for i in self.items:
            n[i.icon] += 1
        return "наш код: " + " ".join(f"{k}{v}" for k, v in n.items()) if n else ""

    def commit(self) -> None:
        """После успешной отправки: запомнить сообщённое, забыть исправленное."""
        state = load_cache(REPORTED).get("items") or {}
        today = datetime.now(TZ).date().isoformat()
        for i in self.items:
            if i.key:
                state[i.key] = {"sig": i.sig, "last": today, "left": i.left}
        for key in list(state):
            if key not in self.standing:
                del state[key]
        save_cache(REPORTED, {"items": state, "at": now_iso()})


def _where(locs: list[Loc]) -> str:
    by_proj: dict[str, list[str]] = defaultdict(list)
    # Сначала рабочий код, тесты — в хвост: «где чинить» важнее «где проверяется».
    for proj, rel, ln in sorted(locs, key=lambda x: ("test" in x[1].lower(), x[1], x[2])):
        if f"{rel}:{ln}" not in by_proj[proj]:
            by_proj[proj].append(f"{rel}:{ln}")
    parts = []
    for proj, places in by_proj.items():
        shown = ", ".join(places[:LOCS]) + (f" (+{len(places) - LOCS})" if len(places) > LOCS else "")
        parts.append(f"{esc(proj)}: <code>{esc(shown)}</code>")
    return "; ".join(parts)


def _line(i: Item) -> str:
    line = f"{i.icon} {MARKET[i.market]} · <code>{esc(i.path)}</code> — {i.what}"
    return line + (f" · {_where(i.locs)}" if i.locs else "")


def _ru(d: date) -> str:
    return f"{d.day} {notify._GEN_MONTHS[d.month - 1]} {d.year}"


def _due(prev: dict | None, sig: str, left: int | None, broken: bool, today: date) -> bool:
    if not prev or prev.get("sig") != sig:
        return True
    if broken:
        last = date.fromisoformat(prev.get("last", "1970-01-01"))
        return (today - last).days >= BROKEN_EVERY
    if left is None:
        return False
    was = prev.get("left")
    return any(left <= t and (was is None or was > t) for t in REMIND_DAYS)


def check(f: notify.Findings | None, head: str, *, refresh: bool = False,
          everything: bool = False) -> Report | None:
    """None — проверка не настроена. everything — всё постоянное, без учёта напоминаний."""
    cfg = load_config()
    if not cfg:
        return None
    rep = Report()
    if refresh:
        rep.errors += refresh_gas(cfg)
    blobs = Blobs()
    try:
        known = known_paths(blobs, head)
        detail = head_detail(blobs, head)
        journal = journal_dates(blobs, head)
    finally:
        blobs.close()
    used = scan(cfg, known)
    rep.used = len(used)
    today = datetime.now(TZ).date()
    reported = load_cache(REPORTED).get("items") or {}

    changed: dict[tuple[str, str], str] = {}
    if f:
        for market, items in f.changed.items():
            for _s, op, _t in items:
                changed[(market, op.split(" ", 1)[1])] = "поменялась документация метода"
        for title, entries in f.entries.items():
            market = next((m for m, n in MARKET.items() if title.startswith(n + " ")), None)
            for e in entries:
                for p in _API_PATH.findall(e.head + " " + e.text):
                    changed[(market, "/" + p.lstrip("/"))] = "в журнале: " + esc(clip(e.text, 160))

    for (market, path), locs in sorted(used.items()):
        ops = detail.get(market, {}).get(path)
        notes = {id(o): off_notes(o.text, path) for o in ops or []}
        dep = [o for o in (ops or []) if o.deprecated or any(not n.param for n in notes[id(o)])]
        # Отключение параметра касается нас, только если код рядом с вызовом его читает;
        # показываем именно эти места.
        params = []
        for n in sorted((n for o in ops or [] for n in notes[id(o)] if n.param and n.when >= today),
                        key=lambda n: n.when):
            name = param_name(n.text)
            readers = field_readers(cfg, locs, name) if name else locs
            if readers:
                params.append((n, readers))
        key, sig, left = f"{market} {path}", "", None
        if not ops:
            sig, icon, what = "gone", "🔴", "метода больше нет в документации"
        elif dep:
            when = next((n.when for o in dep for n in notes[id(o)] if not n.param), None) \
                or journal.get(path)
            partly = "" if len(dep) == len(ops) else f" ({', '.join(o.method for o in dep)})"
            if when and when <= today:
                sig, icon = f"overdue:{when}", "🔴"
                what = f"устарел{partly}, отключение было {_ru(when)}"
            else:
                left = (when - today).days if when else None
                sig, icon = f"deprecated:{when or ''}", "🟠"
                what = (f"устарел{partly}, отключат {_ru(when)} "
                        f"(через {left} {plural(left, 'день', 'дня', 'дней')})" if when
                        else f"устарел{partly}, дата отключения не объявлена")
        elif params:
            n, locs = params[0]
            left = (n.when - today).days
            sig, icon = f"param:{n.when}:{len(params)}", "🟠"
            what = (f"через {left} {plural(left, 'день', 'дня', 'дней')}: {esc(clip(n.text, 170))}"
                    + (f" (и ещё {len(params) - 1})" if len(params) > 1 else "")
                    + f" — поле <code>{esc(param_name(n.text) or '?')}</code> читают")
        if sig:
            rep.standing[key] = (sig, left)
            if everything or _due(reported.get(key), sig, left, icon == "🔴", today):
                rep.items.append(Item(icon, market, path, what, locs, key, sig, left))
                continue
        if (market, path) in changed:
            rep.items.append(Item("✏️", market, path, changed[(market, path)], locs))

    for key, prev in reported.items():
        if key not in rep.standing and not everything:
            market, path = key.split(" ", 1)
            rep.items.append(Item("✅", market, path, "предупреждение снято: метод снова в порядке"
                                  if (market, path) in used else "в коде больше не встречается",
                                  used.get((market, path), [])))
    order = {"🔴": 0, "🟠": 1, "✏️": 2, "✅": 3}
    rep.items.sort(key=lambda i: (order[i.icon], i.left if i.left is not None else 10 ** 6,
                                  i.market, i.path))
    return rep


def cli(refresh: bool, as_json: bool) -> int:
    rep = check(None, notify.rev("HEAD"), refresh=refresh, everything=True)
    if rep is None:
        print(f"проверка не настроена: нет {CONFIG}")
        return 2
    if as_json:
        print(json.dumps([{"icon": i.icon, "market": i.market, "path": i.path, "what": i.what,
                           "where": [f"{p}/{f}:{n}" for p, f, n in i.locs]} for i in rep.items],
                         ensure_ascii=False, indent=1))
        return 0
    import html as _html
    print(f"методов площадок в нашем коде: {rep.used}; требуют внимания: {len(rep.items)}")
    for e in rep.errors:
        print("⚠️", e)
    for i in rep.items:
        print(_html.unescape(re.sub(r"<[^>]+>", "", _line(i))))
    return 0
