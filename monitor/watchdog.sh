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
# Перезапуск — штатное событие: twitch-badges-bot-reload.path поднимает бот заново
# при каждой правке исходников, а Restart=always делает это после сбоя. Оба states
# занимают ~1-2 секунды, и одиночная проверка успевала поймать их как «бот НЕ
# работает» (deactivating/stop-sigterm) — владельцу уходил ложный алерт о лежащем
# сервисе, который на самом деле уже поднимался. Даём процессу дожить рестарт:
# алертим, только если он не active НИ РАЗУ за несколько проб подряд.
bot_active() {
  for _ in 1 2 3 4 5 6; do
    systemctl is-active --quiet twitch-badges-bot.service && return 0
    sleep 5
  done
  return 1
}

if bot_active; then
  "$ALERT" --clear bot-down "бот снова активен"
else
  st=$(systemctl show twitch-badges-bot.service -p ActiveState -p SubState --value | tr '\n' '/')
  "$ALERT" bot-down "бот НЕ работает ($st)" \
    "twitch-badges-bot.service не active.
Смотреть:  journalctl -u twitch-badges-bot.service -n 50
Поднять:   sudo systemctl reset-failed twitch-badges-bot.service && sudo systemctl start twitch-badges-bot.service
(sudo обязателен: юнит системный, root-owned — без него будет 'Interactive authentication required')"
fi

# (4) Бот РАЗГОВАРИВАЕТ с Telegram? is_active выше этого не показывает: зависший
#     event loop оставляет юнит active, а данные обновляет отдельный refresh —
#     все индикаторы зелёные при лежащем продукте. bot_alive трогается только
#     после успешного ответа API (каждые 5 мин), порог 25 мин = запас в 5 пропусков.
ALIVE="$PROJ/data/bot_alive"
ALIVE_MAX=${ALIVE_MAX:-1500}
if systemctl is-active --quiet twitch-badges-bot.service && [ -f "$ALIVE" ]; then
  aage=$(( now - $(stat -c %Y "$ALIVE") ))
  if [ "$aage" -gt "$ALIVE_MAX" ]; then
    "$ALERT" bot-wedged "бот запущен, но молчит $(( aage/60 )) мин" \
      "Юнит active, но последний успешный ответ Telegram API был $(( aage/60 )) мин назад (порог $(( ALIVE_MAX/60 )) мин).
Похоже, бот повис, а не упал — systemd такое не перезапустит.
Смотреть:   journalctl -u twitch-badges-bot.service -n 50
Вылечить:   sudo systemctl restart twitch-badges-bot.service"
  else
    "$ALERT" --clear bot-wedged "бот отвечает ($(( aage/60 )) мин назад)"
  fi
fi

# (3) Диск
PCT=$(df --output=pcent / | tr -dc '0-9')
if [ "${PCT:-0}" -ge "${DISK_MAX:-90}" ]; then
  # Без sudo du не читает /var/lib/docker и /var/log/journal — то есть подсказка
  # уводила бы от главных пожирателей места именно на этом хосте.
  "$ALERT" disk-full "диск / заполнен ${PCT}%" \
    "порог ${DISK_MAX:-90}%.
Найти:      sudo du -xh --max-depth=1 / | sort -h | tail -15
            docker system df ; journalctl --disk-usage
Освободить (безопасно):
            docker builder prune -f        # кэш сборки, обычно самый жирный
            docker image prune -f          # только висячие слои
            sudo journalctl --vacuum-size=200M ; sudo apt-get clean

НЕ запускать 'docker system prune -a': снесёт локально собранные образы
— их нет ни в одном
реестре, восстановить можно только пересборкой."
else
  "$ALERT" --clear disk-full "диск ${PCT}%"
fi
