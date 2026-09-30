#!/bin/bash
# Независимый алертер владельцу: только bash + curl, чтобы работать, даже когда
# бот и Python мертвы. Дедуп: одна тревога на инцидент, напоминание раз в
# ALERT_RENOTIFY, «восстановлено» при --clear.
#
#   alert.sh <key> <subject> [<body>]   поднять/держать тревогу (идемпотентно)
#   alert.sh --clear <key> [<body>]     снять тревогу, если была
#
# Настройки — из env-файла TB_ENV_FILE (формат KEY=VALUE, файл НЕ исполняется):
#   ALERT_BOT_TOKEN (иначе TELEGRAM_BOT_TOKEN), ALERT_CHAT_ID, DATA_DIR.
# Токен передаётся curl через stdin (-K -), в командной строке его не видно.
# ALERT_DRY_RUN=1 — ничего не отправлять, печатать текст (для тестов).
set -uo pipefail

env_get() {  # env_get KEY — значение из окружения, иначе из TB_ENV_FILE
  local key="$1" val="${!1:-}"
  if [ -z "$val" ] && [ -n "${TB_ENV_FILE:-}" ] && [ -r "$TB_ENV_FILE" ]; then
    val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$TB_ENV_FILE" | tail -1 \
          | sed -E "s/^[[:space:]]*(export[[:space:]]+)?${key}=//; s/^[\"'](.*)[\"']$/\\1/")
  fi
  printf '%s' "$val"
}

DATA_DIR="$(env_get DATA_DIR)"
STATE_DIR="${ALERT_STATE_DIR:-${DATA_DIR:-/tmp}/alerts-watchdog}"
RENOTIFY="${ALERT_RENOTIFY:-21600}"
API="${TELEGRAM_API_BASE:-$(env_get TELEGRAM_API_BASE)}"
API="${API:-https://api.telegram.org/bot}"
mkdir -p "$STATE_DIR"

send() {
  local text host
  host="$(hostname)"
  text="[$host] $1"
  if [ "${ALERT_DRY_RUN:-0}" = "1" ]; then
    printf 'DRY-RUN alert: %s\n' "$text"
    return 0
  fi
  local token chat
  token="$(env_get ALERT_BOT_TOKEN)"
  [ -n "$token" ] || token="$(env_get TELEGRAM_BOT_TOKEN)"
  chat="$(env_get ALERT_CHAT_ID)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    echo "alert.sh: нет ALERT_BOT_TOKEN/TELEGRAM_BOT_TOKEN или ALERT_CHAT_ID" >&2
    return 1
  fi
  printf 'url = "%s%s/sendMessage"\n' "$API" "$token" \
    | curl -4 -fsS -m 20 --retry 3 --retry-delay 3 -K - \
        --data-urlencode "chat_id=${chat}" --data-urlencode "text=${text}" \
        -d disable_web_page_preview=true >/dev/null
}

exec 9>"$STATE_DIR/.lock"
flock 9

MODE=raise
if [ "${1:-}" = "--clear" ]; then MODE=clear; shift; fi
KEY="${1:?key required}"; shift || true
case "$KEY" in *[!A-Za-z0-9._-]*) echo "alert.sh: плохой ключ $KEY" >&2; exit 2 ;; esac
STATE="$STATE_DIR/$KEY"
now=$(date +%s)

if [ "$MODE" = clear ]; then
  # Сначала отправить, потом стереть: иначе сетевая заминка съест «восстановлено».
  if [ -f "$STATE" ] && send "✅ Восстановлено: $KEY ${*:-}"; then
    rm -f "$STATE"
  fi
  exit 0
fi

SUBJECT="${1:?subject required}"; shift || true
BODY="${*:-}"
if [ -f "$STATE" ]; then
  last=$(cat "$STATE" 2>/dev/null)
  case "$last" in ''|*[!0-9]*) last=$(stat -c %Y "$STATE" 2>/dev/null || echo "$now") ;; esac
  [ $(( now - last )) -lt "$RENOTIFY" ] && exit 0
  PREFIX="🔁 Всё ещё"
else
  PREFIX="🔴"
fi
if send "$PREFIX: $SUBJECT
$BODY"; then
  echo "$now" > "$STATE"
fi
