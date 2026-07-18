#!/bin/bash
# Независимый Telegram-алертер. НИКАКОГО Python/venv — только bash+curl,
# чтобы работать даже когда сам бот мёртв. Дедуп: одна тревога на инцидент,
# ре-напоминание раз в RENOTIFY, и авто-уведомление о восстановлении (--clear).
#
# Использование:
#   alert.sh <key> <subject> <body...>     # поднять/держать тревогу (идемпотентно)
#   alert.sh --clear <key> [<body...>]     # снять тревогу, если была активна
#
# Читает ALERT_BOT_TOKEN + ALERT_CHAT_ID из .env (fallback: TELEGRAM_BOT_TOKEN).
# ALERT_CHAT_ID ДОЛЖЕН быть приватным чатом/каналом владельца, НЕ публичным каналом.

set -uo pipefail

PROJ="/home/archie/projects/twitch-badges"
STATE_DIR="$PROJ/data/alerts"
RENOTIFY="${ALERT_RENOTIFY:-21600}"   # повторять активную тревогу не чаще, чем раз в 6ч
LOCK="$STATE_DIR/.lock"

mkdir -p "$STATE_DIR"

# --- секреты ---
set -a
# shellcheck disable=SC1091
[ -f "$PROJ/.env" ] && . "$PROJ/.env"
set +a
TOKEN="${ALERT_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
CHAT="${ALERT_CHAT_ID:-}"

send() {
  # $1 = текст. Молча ничего не делаем, если не настроено (лучше, чем краш).
  [ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "alert.sh: ALERT_BOT_TOKEN/ALERT_CHAT_ID не заданы" >&2; return 1; }
  local host; host="$(hostname)"
  curl -4 -fsS -m 20 --retry 3 --retry-delay 3 \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=[${host}] $1" \
    -d disable_web_page_preview=true >/dev/null
}

exec 9>"$LOCK"; flock 9   # сериализуем watchdog + OnFailure, чтобы не было гонок по state

MODE="raise"
if [ "${1:-}" = "--clear" ]; then MODE="clear"; shift; fi
KEY="${1:?key required}"; shift || true
STATE="$STATE_DIR/$KEY"

now=$(date +%s)

if [ "$MODE" = "clear" ]; then
  if [ -f "$STATE" ]; then
    rm -f "$STATE"
    send "✅ RECOVERED: $KEY ${*:-}" || true
  fi
  exit 0
fi

SUBJECT="${1:?subject required}"; shift || true
BODY="${*:-}"

if [ -f "$STATE" ]; then
  last=$(cat "$STATE" 2>/dev/null || echo 0)
  age=$(( now - last ))
  if [ "$age" -lt "$RENOTIFY" ]; then
    exit 0                      # уже alerted недавно — молчим (антиспам)
  fi
  PREFIX="🔴 STILL FAILING"
else
  PREFIX="🚨 ALERT"
fi

if send "$PREFIX: $SUBJECT
$BODY"; then
  echo "$now" > "$STATE"        # отметку ставим только при успешной отправке
fi
