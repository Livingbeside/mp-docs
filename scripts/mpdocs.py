"""CLI зеркала документации маркетплейсов."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import MIRROR, commit_mirror, git, log, now_iso  # noqa: E402

MIN_FREE_MB = 1500   # ниже этого браузер не поднимаем: рядом работают бустеры


def available_mb() -> int:
    """MemAvailable — сколько реально можно занять, не выдавив чужие страницы."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 1 << 30


SOURCES = {
    "ozon-api": ("src_ozon_api", "Спека Ozon Seller API"),
    "ozon-kb": ("src_ozon_kb", "База знаний Ozon (seller-edu)"),
    "ozon-chat": ("src_ozon_chat", "Обновления Seller API из служебных чатов кабинета"),
    "wb": ("src_wb", "Wildberries: спеки всех разделов"),
    "ym": ("src_ym", "Яндекс Маркет: справка + спека"),
}
BROWSER_SOURCES = ("ozon-api", "ozon-kb", "wb")


def cmd_update(args) -> int:
    names = args.sources or list(SOURCES)
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        print(f"неизвестный источник: {', '.join(unknown)}", file=sys.stderr)
        return 2

    free = available_mb()
    if free < MIN_FREE_MB and not args.force:
        browser_names = [n for n in names if n in BROWSER_SOURCES]
        if browser_names:
            log(f"свободно {free} МБ (< {MIN_FREE_MB}) — Camoufox не поднимаю, "
                f"пропускаю {', '.join(browser_names)}; рядом работают бустеры")
            names = [n for n in names if n not in BROWSER_SOURCES]
            if not names:
                print(json.dumps({"ok": True, "skipped": browser_names,
                                  "reason": f"мало памяти: {free} МБ", "changed": 0},
                                 ensure_ascii=False, indent=2))
                return 0

    results, failed = [], []
    for name in names:
        module_name, title = SOURCES[name]
        log(f"── {name}: {title}")
        try:
            module = __import__(module_name)
            kwargs = {"channel": args.channel}
            if name == "ozon-kb":
                kwargs["max_docs"] = args.max_docs
                kwargs["delay"] = args.delay
            results.append(module.run(**kwargs))
        except Exception as exc:
            log(f"!! {name} упал: {type(exc).__name__}: {exc}")
            failed.append({"source": name, "error": f"{type(exc).__name__}: {exc}"})

    changed = sum(r.get("changed", 0) for r in results)
    summary = ", ".join(f"{r['source']}:{r.get('changed', 0)}" for r in results)
    if args.commit and changed:
        stat = commit_mirror(f"update {now_iso()} — {summary}")
        log(f"коммит: {stat}")
    elif args.commit:
        log("изменений нет, коммитить нечего")

    print(json.dumps({"ok": not failed, "results": results, "failed": failed,
                      "changed": changed}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_status(_args) -> int:
    rows = []
    for marker in sorted(MIRROR.rglob(".fetched")):
        rel = marker.parent.relative_to(MIRROR)
        files = sum(1 for _ in marker.parent.rglob("*.md"))
        rows.append((str(rel), files, marker.read_text(encoding="utf-8").strip()))
    if not rows:
        print("зеркало пустое — запусти: mpdocs update")
        return 0
    width = max(len(r[0]) for r in rows)
    print(f"{'раздел'.ljust(width)}  файлов  обновлено")
    for rel, files, when in rows:
        print(f"{rel.ljust(width)}  {files:6}  {when}")
    total = sum(1 for _ in MIRROR.rglob("*.md"))
    size = subprocess.run(["du", "-sh", str(MIRROR)], capture_output=True,
                          text=True).stdout.split()[0]
    print(f"\nвсего markdown: {total}, объём: {size}")
    head = git("log", "-1", "--format=%h %ad %s", "--date=short", check=False).strip()
    if head:
        print(f"последний коммит: {head}")
    return 0


def cmd_changelog(args) -> int:
    """Что маркетплейс ОБЪЯВИЛ. Дополняет `changes`, который показывает,
    что реально изменилось в файлах — включая то, о чём не объявляли."""
    # Названия разные: у Ozon/WB это changelog.md, у ЯМ — каталог changelog/.
    files = sorted(set(MIRROR.rglob("changelog.md")) |
                   {f for f in MIRROR.rglob("changelog/*.md")})
    if args.source:
        files = [f for f in files if args.source in str(f.relative_to(MIRROR))]
    if not files:
        print("ченджлогов в зеркале нет")
        return 0
    for f in files:
        rel = f.relative_to(MIRROR)
        body = f.read_text(encoding="utf-8").split("---", 2)[-1]
        # Даты у Ozon под ##, у ЯМ под ### — режем по обоим уровням.
        blocks = re.split(r"^(#{2,3} )", body, flags=re.M)
        pairs = list(zip(blocks[1::2], blocks[2::2]))
        print(f"\n═══ {rel} ═══")
        if not pairs:
            print("(без датированных разделов)")
        for level, block in pairs[:args.count]:
            print(level + block.rstrip()[:args.width])
    return 0


def cmd_changes(args) -> int:
    out = git("log", f"--since={args.since}", "--stat", "--format=%h %ad %s",
              "--date=short", check=False)
    print(out.strip() or f"изменений за {args.since} нет")
    return 0


def cmd_search(args) -> int:
    root = MIRROR / args.scope if args.scope else MIRROR
    cmd = ["rg", "--color=never", "-n", "-i", "--max-count", str(args.max_count)]
    if args.files:
        cmd.append("-l")
    cmd += ["-g", args.glob, args.query, str(root)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    text = r.stdout.replace(str(MIRROR) + "/", "")
    print(text.strip() or "ничего не найдено")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mpdocs",
                                 description="зеркало документации маркетплейсов")
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="обновить зеркало")
    up.add_argument("sources", nargs="*", help=f"из: {', '.join(SOURCES)}")
    up.add_argument("--channel", default="optic")
    up.add_argument("--max-docs", type=int, default=4000)
    up.add_argument("--delay", type=float, default=0.35)
    up.add_argument("--no-commit", dest="commit", action="store_false")
    up.add_argument("--force", action="store_true",
                    help="качать браузерные источники даже при нехватке памяти")
    up.set_defaults(func=cmd_update, commit=True)

    st = sub.add_parser("status", help="что есть в зеркале и когда обновлялось")
    st.set_defaults(func=cmd_status)

    ch = sub.add_parser("changes",
                        help="что реально изменилось в файлах, по git-истории")
    ch.add_argument("--since", default="2.weeks")
    ch.set_defaults(func=cmd_changes)

    cl = sub.add_parser("changelog",
                        help="что маркетплейс объявил сам (в отличие от changes)")
    cl.add_argument("-n", "--count", type=int, default=5, help="сколько последних записей")
    cl.add_argument("-s", "--source", default="", help="ozon, wb, ym")
    cl.add_argument("-w", "--width", type=int, default=2000)
    cl.set_defaults(func=cmd_changelog)

    se = sub.add_parser("search", help="поиск по зеркалу")
    se.add_argument("query")
    se.add_argument("-g", "--glob", default="*.md")
    se.add_argument("-s", "--scope", default="",
                    help="ограничить подкаталогом: ozon, ozon/kb, wb, ym")
    se.add_argument("-l", "--files", action="store_true", help="только имена файлов")
    se.add_argument("-m", "--max-count", type=int, default=3)
    se.set_defaults(func=cmd_search)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
