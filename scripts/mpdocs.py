"""CLI зеркала документации маркетплейсов."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (MIRROR, commit_mirror, git, log, now_iso, push_enabled,  # noqa: E402
                    push_mirror)

MIN_FREE_MB = 1500   # ниже этого браузер не поднимаем: рядом работают бустеры
STALE_DAYS = 3       # источник обновляется каждую ночь; больше трёх дней = отстали


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
    "uzum-api": ("src_uzum_api", "Спека Uzum Market Seller API"),
    "uzum-kb": ("src_uzum_kb", "Инструкция для продавцов Uzum Market"),
}
BROWSER_SOURCES = ("ozon-api", "ozon-kb", "wb", "uzum-kb")


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

    head_before = git("rev-parse", "HEAD", check=False).strip() or None
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
    pushed = None
    if args.commit and changed:
        stat = commit_mirror(f"update {now_iso()} — {summary}")
        log(f"коммит: {stat}")
        if args.push and not push_enabled():
            log("пуш выключен (нет MP_DOCS_PUSH=1) — коммит остался только локально")
        elif args.push:
            err = push_mirror()
            pushed = err is None
            log("зеркало отправлено в общий репозиторий" if pushed
                else f"!! push не прошёл ({err}) — уедет следующим прогоном")
    elif args.commit:
        log("изменений нет, коммитить нечего")

    # Сводка в Telegram — только о том, что уже в истории зеркала (после коммита),
    # и никогда не роняет обновление: оно своё дело сделало.
    notified = None
    if args.commit and args.notify:
        try:
            import notify
            notified = notify.after_update(head_before, failed)
        except Exception as exc:
            msg = re.sub(r"bot\d+:[\w-]+", "bot***", f"{type(exc).__name__}: {exc}")
            log(f"!! сводка в Telegram упала: {msg}")
            notified = False

    print(json.dumps({"ok": not failed, "results": results, "failed": failed,
                      "changed": changed, "pushed": pushed, "notified": notified},
                     ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_notify(args) -> int:
    import notify
    return notify.cli(args.since, args.until, args.dry_run, args.html)


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

    # Копия обновляется через git pull, и протухает молча — предупреждаем сами.
    age = git("log", "-1", "--format=%ct", check=False).strip()
    if age:
        days = (time.time() - int(age)) / 86400
        if days >= STALE_DAYS:
            print(f"\n⚠️  зеркалу {int(days)} дн. — обнови: "
                  f"git -C {MIRROR} pull")
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
    up.add_argument("--no-push", dest="push", action="store_false",
                    help="не отправлять зеркало в общий репозиторий (пуш и так требует MP_DOCS_PUSH=1)")
    up.add_argument("--force", action="store_true",
                    help="качать браузерные источники даже при нехватке памяти")
    up.add_argument("--no-notify", dest="notify", action="store_false",
                    help="не слать сводку в Telegram (она и так только при настроенном боте)")
    up.set_defaults(func=cmd_update, commit=True, push=True, notify=True)

    no = sub.add_parser("notify",
                        help="сводка «что нового» в Telegram по истории зеркала")
    no.add_argument("--since", help="от какой ревизии (по умолчанию — от последней отправленной)")
    no.add_argument("--until", help="до какой ревизии (по умолчанию HEAD)")
    no.add_argument("-n", "--dry-run", action="store_true", help="показать, не отправляя")
    no.add_argument("--html", action="store_true", help="в --dry-run печатать разметку как есть")
    no.set_defaults(func=cmd_notify)

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
                    help="ограничить подкаталогом: ozon, ozon/kb, wb, ym, uzum")
    se.add_argument("-l", "--files", action="store_true", help="только имена файлов")
    se.add_argument("-m", "--max-count", type=int, default=3)
    se.set_defaults(func=cmd_search)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
