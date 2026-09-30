#!/bin/bash
# Откат кода на предыдущий релиз (или указанный): переключить симлинк и
# перезапустить бота. БД не трогает: схема v1 совместима в обе стороны; для
# отката БД — README, «Восстановление из бэкапа».
#
#   sudo deploy/rollback.sh [releases/<sha>]
set -euo pipefail
TB_BASE="${TB_BASE:-/opt/twitch-badges}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
TARGET="${1:-$(cat "$TB_BASE/previous" 2>/dev/null || true)}"
[ -n "$TARGET" ] || { echo "rollback: некуда откатываться (нет $TB_BASE/previous)" >&2; exit 1; }
case "$TARGET" in releases/*) ;; *) TARGET="releases/$TARGET" ;; esac
[ -d "$TB_BASE/$TARGET" ] || { echo "rollback: нет $TB_BASE/$TARGET" >&2; exit 1; }
CUR="$(readlink "$TB_BASE/current" 2>/dev/null || true)"
ln -sfn "$TARGET" "$TB_BASE/current.new"
mv -T "$TB_BASE/current.new" "$TB_BASE/current"
[ -n "$CUR" ] && echo "$CUR" > "$TB_BASE/previous"
echo "[rollback] current → $TARGET (было: ${CUR:-нет})"
if "$SYSTEMCTL" is-enabled --quiet tb-bot.service 2>/dev/null; then
  "$SYSTEMCTL" restart tb-bot.service
  echo "[rollback] бот перезапущен"
fi
