#!/bin/bash
# Выкатка релиза: тесты → бэкап БД → проверка схемы → юниты → переключение
# симлинка → рестарт → smoke → автооткат при провале.
#
#   sudo deploy/deploy.sh <git-ref>
#
# Код: $TB_BASE/releases/<sha>, симлинк $TB_BASE/current, venv по requirements.lock.
# Данные: $TB_DATA (StateDirectory). Секреты: $TB_ENV (0640 root:twitchbadges).
# Переменные для тестовой установки (без root): TB_NO_ROOT=1 SYSTEMCTL=true
#   TB_BASE=… TB_DATA=… TB_UNITS=… TB_ENV=… TB_TESTS=quick|full|skip
set -euo pipefail

REF="${1:?использование: deploy.sh <git-ref>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TB_REPO="${TB_REPO:-$(cd "$HERE/.." && pwd)}"
TB_BASE="${TB_BASE:-/opt/twitch-badges}"
TB_DATA="${TB_DATA:-/var/lib/twitch-badges}"
TB_ENV="${TB_ENV:-/etc/twitch-badges/env}"
TB_UNITS="${TB_UNITS:-/etc/systemd/system}"
TB_USER="${TB_USER:-twitchbadges}"
TB_PYTHON="${TB_PYTHON:-/usr/bin/python3.14}"
TB_TESTS="${TB_TESTS:-full}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SMOKE_WAIT="${SMOKE_WAIT:-120}"

log() { printf '\033[1m[deploy]\033[0m %s\n' "$*"; }
die() { printf '[deploy] ОШИБКА: %s\n' "$*" >&2; exit 1; }

if [ "${TB_NO_ROOT:-0}" != 1 ]; then
  [ "$(id -u)" = 0 ] || die "нужен root (sudo)"
  id "$TB_USER" >/dev/null 2>&1 || die "нет пользователя $TB_USER — см. README, «Установка»"
  as_user() { runuser -u "$TB_USER" -- env DATA_DIR="$TB_DATA" TB_ENV_FILE="$TB_ENV" "$@"; }
else
  as_user() { env DATA_DIR="$TB_DATA" TB_ENV_FILE="$TB_ENV" "$@"; }
fi

SHA="$(git -C "$TB_REPO" rev-parse --verify "$REF^{commit}")" || die "нет такого ref: $REF"
REL="$TB_BASE/releases/$SHA"
mkdir -p "$TB_BASE/releases"

# 1. Релиз: код из git (без рабочих файлов и секретов) + свой venv
if [ ! -f "$REL/.ready" ]; then
  log "собираю релиз ${SHA:0:12}"
  rm -rf "$REL.tmp"
  mkdir -p "$REL.tmp"
  git -C "$TB_REPO" archive "$SHA" | tar -x -C "$REL.tmp"
  "$TB_PYTHON" -m venv "$REL.tmp/venv"
  "$REL.tmp/venv/bin/pip" install -q --disable-pip-version-check -r "$REL.tmp/requirements.lock"
  touch "$REL.tmp/.ready"
  rm -rf "$REL"
  mv "$REL.tmp" "$REL"
fi

# 2. Тесты — в отдельном venv, прод-venv не трогаем
if [ "$TB_TESTS" != skip ]; then
  log "тесты ($TB_TESTS)"
  TV="$(mktemp -d)"
  trap 'rm -rf "$TV"' EXIT
  "$TB_PYTHON" -m venv "$TV/venv"
  "$TV/venv/bin/pip" install -q --disable-pip-version-check -r "$REL/requirements.lock" \
    -r "$REL/requirements-dev.txt"
  MARK=(); [ "$TB_TESTS" = quick ] && MARK=(-m "not slow")
  (cd "$REL" && env -u TELEGRAM_BOT_TOKEN HOME="$TV" "$TV/venv/bin/python" -m pytest -q -x \
     -p no:cacheprovider "${MARK[@]}") || die "тесты не прошли — релиз не выкатываю"
fi

if [ "${TB_NO_ROOT:-0}" != 1 ]; then
  chown -R root:root "$REL"
  chmod -R go-w "$REL"
fi

DB="$TB_DATA/twitch_badges.sqlite3"
# 3. Бэкап БД и проверка схемы новым кодом
if [ -f "$DB" ]; then
  log "бэкап БД"
  as_user "$REL/venv/bin/python" -m twitch_badges backup
  log "проверка БД новым кодом"
  as_user "$REL/venv/bin/python" -m twitch_badges doctor || die "doctor не прошёл — не переключаю"
fi

# 4. Юниты из репозитория один в один
log "юниты → $TB_UNITS"
mkdir -p "$TB_UNITS"
changed=0
for f in "$REL"/deploy/systemd/tb-*; do
  dst="$TB_UNITS/$(basename "$f")"
  if ! cmp -s "$f" "$dst"; then install -m 0644 "$f" "$dst"; changed=1; fi
done
[ "$changed" = 1 ] && "$SYSTEMCTL" daemon-reload

# 5. Переключение симлинка (атомарно)
PREV="$(readlink "$TB_BASE/current" 2>/dev/null || true)"
ln -sfn "releases/$SHA" "$TB_BASE/current.new"
mv -T "$TB_BASE/current.new" "$TB_BASE/current"
if [ -n "$PREV" ] && [ "$PREV" != "releases/$SHA" ]; then echo "$PREV" > "$TB_BASE/previous"; fi
log "current → ${SHA:0:12} (было: ${PREV:-нет})"

# 6. Рестарт и smoke — только если бот уже включён (первая установка — вручную)
if "$SYSTEMCTL" is-enabled --quiet tb-bot.service 2>/dev/null; then
  t0=$(date +%s)
  "$SYSTEMCTL" restart tb-bot.service
  log "smoke: жду бота до ${SMOKE_WAIT} с"
  ok=0
  for _ in $(seq 1 "$SMOKE_WAIT"); do
    hb="$TB_DATA/heartbeat-bot"
    if "$SYSTEMCTL" is-active --quiet tb-bot.service && [ -f "$hb" ] && [ "$(stat -c %Y "$hb")" -ge "$t0" ]; then
      ok=1; break
    fi
    sleep 1
  done
  if [ "$ok" != 1 ] || ! as_user "$REL/venv/bin/python" -m twitch_badges doctor >/dev/null; then
    if [ -n "$PREV" ]; then
      log "smoke не прошёл — откатываюсь на $PREV"
      TB_BASE="$TB_BASE" SYSTEMCTL="$SYSTEMCTL" "$HERE/rollback.sh" "$PREV"
      die "релиз ${SHA:0:12} не поднялся; откат на $PREV выполнен"
    fi
    die "релиз ${SHA:0:12} не поднялся, а откатываться некуда (первая установка): journalctl -u tb-bot"
  fi
  log "бот поднялся на ${SHA:0:12}"
fi

# 7. Старые релизы: оставляем 5 последних (и текущий/предыдущий)
keep="$(readlink "$TB_BASE/current"; cat "$TB_BASE/previous" 2>/dev/null || true)"
n=0
while read -r r; do
  n=$((n + 1))
  [ "$n" -le 5 ] && continue
  case "$r" in *.tmp) continue ;; esac
  case "$keep" in *"$r"*) continue ;; esac
  rm -rf "${TB_BASE:?}/releases/$r"
done < <(find "$TB_BASE/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' | sort -rn | cut -d' ' -f2)
log "готово"
