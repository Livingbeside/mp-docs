#!/usr/bin/env bash
# Установка mp-docs на новую машину: зеркало + скилл для Claude Code (+ MCP).
# Идемпотентно: повторный запуск просто обновит зеркало и перепишет скилл.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTO_UPDATE=0
[ "${1:-}" = "--auto-update" ] && AUTO_UPDATE=1
MIRROR_REPO="${MP_DOCS_MIRROR_REPO:-https://github.com/Livingbeside/mp-docs-mirror.git}"
SKILL_DIR="${MP_DOCS_SKILL_DIR:-$HOME/.claude/skills/mp-docs}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "⛔ нет $1 — $2"; exit 1; }; }
need git    "поставьте git"
need python3 "поставьте python3"
if ! command -v rg >/dev/null 2>&1; then
  echo "⛔ нет ripgrep — без него не работает поиск по зеркалу."
  echo "   apt install ripgrep   (или: brew install ripgrep)"
  exit 1
fi

# 1. Зеркало — отдельный репозиторий, кладётся внутрь mirror/
if [ -d "$ROOT/mirror/.git" ]; then
  echo "→ обновляю зеркало"
  git -C "$ROOT/mirror" pull --ff-only
else
  echo "→ клонирую зеркало (~44 МБ, с историей — на ней работает 'mpdocs changes')"
  git clone "$MIRROR_REPO" "$ROOT/mirror"
fi

# 2. Скилл для Claude Code: абсолютные пути подставляются под эту машину
mkdir -p "$SKILL_DIR"
sed "s#__MP_DOCS_ROOT__#$ROOT#g" "$ROOT/skill/SKILL.md" > "$SKILL_DIR/SKILL.md"
echo "→ скилл установлен: $SKILL_DIR/SKILL.md"

# 3. MCP-сервер — те же поиски для клиентов, которые не читают скиллы
if command -v claude >/dev/null 2>&1; then
  if claude mcp get mp-docs >/dev/null 2>&1; then
    echo "→ MCP mp-docs уже подключён"
  else
    claude mcp add mp-docs -s user -- python3 "$ROOT/scripts/mcp_server.py" \
      && echo "→ MCP mp-docs подключён" \
      || echo "⚠️  MCP подключить не удалось, это необязательно"
  fi
fi

# 4. Ежедневный git pull: зеркало на машине-источнике обновляется каждую ночь,
#    и без этого копия молча отстаёт (mpdocs status предупредит на третий день).
CRON_LINE="17 9 * * * git -C $ROOT/mirror pull -q --ff-only"
if [ "$AUTO_UPDATE" = 1 ]; then
  if command -v crontab >/dev/null 2>&1; then
    if crontab -l 2>/dev/null | grep -qF "$ROOT/mirror pull"; then
      echo "→ ежедневное обновление уже стоит в crontab"
    else
      { crontab -l 2>/dev/null; echo "$CRON_LINE"; } | crontab - \
        && echo "→ ежедневное обновление добавлено в crontab, 09:17"
    fi
  else
    echo "⚠️  crontab не найден — поставьте обновление сами: $CRON_LINE"
  fi
fi

echo
"$ROOT/bin/mpdocs" status
echo
echo "Готово. Проверка: $ROOT/bin/mpdocs search \"v3/posting/fbs/list\""
if [ "$AUTO_UPDATE" = 0 ]; then
  echo
  echo "Источник обновляет зеркало каждую ночь. Чтобы копия не отставала:"
  echo "  $ROOT/install.sh --auto-update      # ежедневный git pull через crontab"
fi
