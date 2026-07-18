#!/bin/bash
# Level-based watchdog: раз в ~15 мин проверяет «данные свежие + бот жив + диск ок»
# и поднимает/снимает тревоги через alert.sh (с дедупом и recovery).
# Ставится на systemd timer. Дополняет OnFailure=, который ловит мгновенные падения.
set -uo pipefail
PROJ="/home/archie/projects/twitch-badges"
ALERT="$PROJ/monitor/alert.sh"

now=$(date +%s)

# (1) Свежесть данных: latest.json обновляется каждым успешным refresh (~30 мин).
#     >3ч без обновления = минимум 6 пропущенных циклов => refresh тихо сломан.
LATEST="$PROJ/data/streamdb_latest.json"
STALE_MAX=${STALE_MAX:-10800}   # 3 часа
if [ -f "$LATEST" ]; then
  mtime=$(stat -c %Y "$LATEST")
  age=$(( now - mtime ))
  if [ "$age" -gt "$STALE_MAX" ]; then
    "$ALERT" data-stale "данные протухли" \
      "streamdb_latest.json не обновлялся $(( age/60 )) мин (порог $(( STALE_MAX/60 )) мин). refresh не доносит свежие данные — проверь: journalctl -u twitch-badges-refresh.service -n50"
  else
    "$ALERT" --clear data-stale "данным $(( age/60 )) мин"
  fi
else
  "$ALERT" data-stale "нет данных" "$LATEST отсутствует"
fi

# (2) Бот жив? (ловит failed/StartLimit crash-loop и просто inactive)
if systemctl is-active --quiet twitch-badges-bot.service; then
  "$ALERT" --clear bot-down "бот снова активен"
else
  st=$(systemctl show twitch-badges-bot.service -p ActiveState -p SubState --value | tr '\n' '/')
  "$ALERT" bot-down "бот НЕ работает ($st)" \
    "twitch-badges-bot.service не active. journalctl -u twitch-badges-bot.service -n50 ; systemctl reset-failed twitch-badges-bot.service && systemctl start twitch-badges-bot.service"
fi

# (3) Диск
PCT=$(df --output=pcent / | tr -dc '0-9')
if [ "${PCT:-0}" -ge "${DISK_MAX:-90}" ]; then
  "$ALERT" disk-full "диск / заполнен ${PCT}%" "порог ${DISK_MAX:-90}%. du -xh --max-depth=1 / | sort -h | tail"
else
  "$ALERT" --clear disk-full "диск ${PCT}%"
fi
