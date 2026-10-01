#!/bin/bash
# Переключение прода со старой установки (/home/alex/twitch-badges, юниты
# twitch-badges-*) на новую (tb-*). Шаги с проверками; при любой ошибке —
# откат на старую систему (она остаётся установленной).
#
#   sudo deploy/cutover.sh [git-ref, по умолчанию refactor]
set -euo pipefail

REF="${1:-refactor}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLD="${OLD:-/home/alex/twitch-badges}"
OLD_OWNER="$(stat -c %U "$OLD")"
ENVF=/etc/twitch-badges/env
DATA=/var/lib/twitch-badges
BASE=/opt/twitch-badges
U=twitchbadges
PY="$BASE/current/venv/bin/python"
OLD_TIMERS="twitch-badges-poll.timer twitch-badges-refresh.timer twitch-badges-format.timer twitch-badges-watchdog.timer"
OLD_PATHS="twitch-badges-bot-reload.path twitch-badges-overrides.path"
STAGE=prepare

log() { printf '\n\033[1m[cutover] %s\033[0m\n' "$*"; }
tb() { runuser -u "$U" -- env DATA_DIR="$DATA" TB_ENV_FILE="$ENVF" "$PY" -m twitch_badges "$@"; }

[ "$(id -u)" = 0 ] || { echo "нужен sudo"; exit 1; }

rollback() {
  rc=$?
  [ "$STAGE" = "finished" ] && exit 0
  echo "[cutover] ОШИБКА на шаге «$STAGE» — откатываюсь на старую систему" >&2
  if [ "$STAGE" != prepare ]; then
    systemctl stop tb-bot.service tb-collector.timer tb-watchdog.timer 2>/dev/null || true
    systemctl disable tb-bot.service tb-collector.timer tb-watchdog.timer 2>/dev/null || true
    if [ "$STAGE" = published ] && [ -f "$DATA/twitch_badges.sqlite3" ]; then
      # новый бот мог постить — старому нужно знать об этих постах
      tb export-state --to "$DATA/export-published.json" \
        && install -o "$OLD_OWNER" -g "$OLD_OWNER" -m 0644 "$DATA/export-published.json" \
             "$OLD/data/published.json" || true
    fi
    # shellcheck disable=SC2086
    systemctl enable --now twitch-badges-bot.service $OLD_TIMERS $OLD_PATHS || true
    echo "[cutover] старая система снова работает" >&2
  fi
  exit "$rc"
}
trap rollback ERR

log "1/10 пользователь $U и секреты $ENVF"
id "$U" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$U"
install -d -m 0750 -o root -g "$U" /etc/twitch-badges
if [ ! -f "$ENVF" ]; then
  (umask 077
   grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHANNEL_ID|ALERT_CHAT_ID|ALERT_BOT_TOKEN|TWITCH_CLIENT_ID|TWITCH_CLIENT_SECRET|QUIET_HOURS_START|QUIET_HOURS_END|TELEGRAM_STORAGE_CHAT_ID|DEADMAN_URL)=' \
     "$OLD/.env" > "$ENVF.tmp"
   echo "PUBLISH_ENABLED=false" >> "$ENVF.tmp")
  chown root:"$U" "$ENVF.tmp"
  chmod 0640 "$ENVF.tmp"
  mv "$ENVF.tmp" "$ENVF"
fi
grep -q '^TELEGRAM_BOT_TOKEN=.' "$ENVF" || { echo "в $ENVF нет TELEGRAM_BOT_TOKEN" >&2; false; }
grep -q '^TELEGRAM_CHANNEL_ID=.' "$ENVF" || { echo "в $ENVF нет TELEGRAM_CHANNEL_ID" >&2; false; }
sed -i 's/^PUBLISH_ENABLED=.*/PUBLISH_ENABLED=false/' "$ENVF"
echo "ключи в $ENVF: $(cut -d= -f1 "$ENVF" | tr '\n' ' ')"

log "2/10 установка кода ($REF) с тестами"
TB_TESTS=quick "$HERE/deploy.sh" "$REF"

STAGE=frozen
log "3/10 останавливаю старую систему"
# shellcheck disable=SC2086
systemctl stop $OLD_TIMERS $OLD_PATHS
for _ in $(seq 1 120); do
  if ! systemctl is-active --quiet twitch-badges-refresh.service \
     && ! systemctl is-active --quiet twitch-badges-poll.service; then break; fi
  sleep 5
done
systemctl stop twitch-badges-bot.service

log "4/10 бэкап старой установки"
B="/home/$OLD_OWNER/tb-cutover-$(date +%F-%H%M).tgz"
tar czf "$B" -C "$OLD" data .env
chown "$OLD_OWNER": "$B"
chmod 600 "$B"
echo "бэкап: $B"

log "5/10 перенос состояния"
install -d -m 0750 -o "$U" -g "$U" "$DATA"
if [ -f "$DATA/twitch_badges.sqlite3" ]; then
  mv "$DATA/twitch_badges.sqlite3" "$DATA/twitch_badges.sqlite3.before-$(date +%s)"
  rm -f "$DATA/twitch_badges.sqlite3-wal" "$DATA/twitch_badges.sqlite3-shm"
fi
rm -rf "$DATA/import"
mkdir -p "$DATA/import" "$DATA/images"
for f in published.json streamdb_latest.json known_windows.json category_urls.json monitor_state.json; do
  if [ -f "$OLD/data/$f" ]; then cp -p "$OLD/data/$f" "$DATA/import/"; fi
done
if [ -d "$OLD/data/alerts" ]; then cp -rp "$OLD/data/alerts" "$DATA/import/"; fi
cp -p "$OLD"/data/images/*.png "$DATA/images/" 2>/dev/null || true
chown -R "$U":"$U" "$DATA"
tb migrate --from "$DATA/import"
tb doctor

log "6/10 живой сбор новым кодом"
tb collect --force

log "7/10 проверка: новых постов быть не должно"
PLAN="$(tb plan --dry-run)"
echo "$PLAN"
echo "$PLAN" | grep -q 'постов на этом тике: 0' || { echo "план не пуст — не включаю" >&2; false; }

log "8/10 запуск без публикации"
systemctl enable tb-collector.timer tb-watchdog.timer tb-bot.service
systemctl start tb-collector.service
[ "$(systemctl show tb-collector.service -p Result --value)" = success ] \
  || { journalctl -u tb-collector.service -n 30 --no-pager -o cat; false; }
systemctl start tb-collector.timer tb-watchdog.timer tb-bot.service
wait_bot() {
  t0=$(date +%s)
  for _ in $(seq 1 90); do
    if systemctl is-active --quiet tb-bot.service && [ -f "$DATA/heartbeat-bot" ] \
       && [ "$(stat -c %Y "$DATA/heartbeat-bot")" -ge "$t0" ]; then return 0; fi
    sleep 2
  done
  journalctl -u tb-bot.service -n 40 --no-pager -o cat | grep -vi token || true
  return 1
}
wait_bot
systemctl start tb-watchdog.service
[ "$(systemctl show tb-watchdog.service -p Result --value)" = success ] \
  || { journalctl -u tb-watchdog.service -n 30 --no-pager -o cat; false; }

STAGE=published
log "9/10 включаю публикацию в канал"
sed -i 's/^PUBLISH_ENABLED=.*/PUBLISH_ENABLED=true/' "$ENVF"
systemctl restart tb-bot.service
wait_bot

log "10/10 старые юниты выключены из автозапуска (файлы оставлены для отката)"
# shellcheck disable=SC2086
systemctl disable twitch-badges-bot.service $OLD_TIMERS $OLD_PATHS 2>/dev/null || true
STAGE=finished
tb status
log "готово: работает новая версия (tb-bot, tb-collector, tb-watchdog)"
