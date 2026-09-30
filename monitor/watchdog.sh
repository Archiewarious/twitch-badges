#!/bin/bash
# Независимый dead-man (tb-watchdog.timer, раз в 15 мин): жив ли бот и сбор,
# хватает ли диска, отвечают ли Telegram и StreamDatabase. Тревоги — через
# alert.sh (bash + curl). В конце — пинг внешнего сервиса (DEADMAN_URL,
# healthchecks.io): если умрёт весь сервер, владелец узнает оттуда.
#
# Пути и имена — из окружения/TB_ENV_FILE, ничего не зашито:
#   DATA_DIR, TB_BOT_UNIT (tb-bot.service), TB_COLLECTOR_UNIT (tb-collector.service),
#   DEADMAN_URL. ALERT_DRY_RUN=1 — ничего не отправлять.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALERT="${ALERT_SH:-$HERE/alert.sh}"

env_get() {
  local key="$1" val="${!1:-}"
  if [ -z "$val" ] && [ -n "${TB_ENV_FILE:-}" ] && [ -r "$TB_ENV_FILE" ]; then
    val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$TB_ENV_FILE" | tail -1 \
          | sed -E "s/^[[:space:]]*(export[[:space:]]+)?${key}=//; s/^[\"'](.*)[\"']$/\\1/")
  fi
  printf '%s' "$val"
}

DATA_DIR="$(env_get DATA_DIR)"
: "${DATA_DIR:?DATA_DIR не задан}"
BOT_UNIT="${TB_BOT_UNIT:-tb-bot.service}"
COLL_UNIT="${TB_COLLECTOR_UNIT:-tb-collector.service}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
now=$(date +%s)

age_of() { [ -f "$1" ] && echo $(( now - $(stat -c %Y "$1") )) || echo 999999; }
reach() { curl -4 -s -o /dev/null -m 12 "$1"; }

TG_OK=1; reach "https://api.telegram.org/" || TG_OK=0
SD_OK=1; reach "https://www.streamdatabase.com/" || SD_OK=0

# (1) Бот: юнит активен и цикл жив (heartbeat пишет сам цикл раз в минуту).
bot_active() {
  for _ in 1 2 3 4 5 6; do
    "$SYSTEMCTL" is-active --quiet "$BOT_UNIT" && return 0
    sleep "${WATCHDOG_PROBE_SLEEP:-5}"
  done
  return 1
}
if bot_active; then
  "$ALERT" --clear bot-down "бот снова работает"
  hb=$(age_of "$DATA_DIR/heartbeat-bot")
  if [ "$hb" -gt "${BOT_HB_MAX:-600}" ]; then
    "$ALERT" bot-wedged "бот запущен, но его цикл молчит $(( hb / 60 )) мин" \
      "systemd должен был перезапустить его сам (WatchdogSec). Если тревога повторяется —
Смотреть: journalctl -u $BOT_UNIT -n 50"
  else
    "$ALERT" --clear bot-wedged "цикл бота жив"
  fi
else
  st=$("$SYSTEMCTL" show "$BOT_UNIT" -p ActiveState -p SubState -p NRestarts --value 2>/dev/null | tr '\n' ' ')
  "$ALERT" bot-down "бот не работает ($st)" \
    "Смотреть: journalctl -u $BOT_UNIT -n 50
Поднять:  sudo systemctl reset-failed $BOT_UNIT && sudo systemctl start $BOT_UNIT"
fi

# (2) Бот часто перезапускается (цикл рестартов). Причину называем честно:
#     если Telegram недоступен — это он, а не «бот повис» (D5).
restarts=$("$SYSTEMCTL" show "$BOT_UNIT" -p NRestarts --value 2>/dev/null || echo 0)
prev=$(cat "$DATA_DIR/.watchdog-restarts" 2>/dev/null || echo "$restarts")
echo "${restarts:-0}" > "$DATA_DIR/.watchdog-restarts"
if [ "${restarts:-0}" -gt $(( ${prev:-0} + ${RESTART_MAX:-3} )) ]; then
  if [ "$TG_OK" = 0 ]; then WHY="Telegram с сервера недоступен — перезапуски, скорее всего, из-за него"
  else WHY="Telegram доступен — значит, дело в боте"; fi
  "$ALERT" bot-restarting "бот перезапускался $(( restarts - prev )) раз за 15 мин" \
    "$WHY
Смотреть: journalctl -u $BOT_UNIT -n 100"
else
  "$ALERT" --clear bot-restarting "перезапуски прекратились"
fi

# (3) Сбор: последний прогон без исключения (heartbeat) не старше 3 ч.
ch=$(age_of "$DATA_DIR/heartbeat-collector")
if [ "$ch" -gt "${COLLECTOR_HB_MAX:-10800}" ]; then
  if [ "$SD_OK" = 1 ]; then WHO="StreamDatabase отвечает — значит, дело на нашей стороне"
  else WHO="StreamDatabase не отвечает — почти наверняка это источник; данные догонят сами"; fi
  "$ALERT" collector-stale "сбор данных не отрабатывает $(( ch / 60 )) мин" \
    "$WHO
Смотреть: journalctl -u $COLL_UNIT -n 50"
else
  "$ALERT" --clear collector-stale "сбор снова отрабатывает"
fi

# (4) Диск
PCT=$(df --output=pcent "$DATA_DIR" | tr -dc '0-9')
if [ "${PCT:-0}" -ge "${DISK_MAX:-90}" ]; then
  "$ALERT" disk-full "диск заполнен на ${PCT}%" \
    "Найти: sudo du -xh --max-depth=1 / | sort -h | tail -15 ; docker system df ; journalctl --disk-usage
Безопасно освободить: docker builder prune -f ; docker image prune -f ; sudo journalctl --vacuum-size=200M
НЕ запускать docker system prune -a: снесёт локально собранные образы."
else
  "$ALERT" --clear disk-full "диск ${PCT}%"
fi

# (5) Внешний dead-man: пинг, пока этот скрипт вообще отрабатывает.
DEADMAN="$(env_get DEADMAN_URL)"
if [ -n "$DEADMAN" ] && [ "${ALERT_DRY_RUN:-0}" != "1" ]; then
  curl -4 -fsS -m 10 --retry 2 "$DEADMAN" >/dev/null || echo "deadman ping не прошёл" >&2
fi
exit 0
