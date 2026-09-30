# Twitch Badges Tracker (@InfoTwitchBot)

Следит за глобальными значками Twitch и рассказывает о них в Telegram: **что
можно получить, как именно и до какого числа**.

| | |
|---|---|
| **Канал** [@TwitchInfoRadar](https://t.me/TwitchInfoRadar) | посты о жизненном цикле значка: появился → уточнили время → стало известно, как получить → стартовало → последний день (→ продлили) |
| **Бот** [@InfoTwitchBot](https://t.me/InfoTwitchBot) | inline-поиск в любом чате: `@InfoTwitchBot`, `free`, `paid`, `soon` или название |

Неофициальный проект, с Twitch Interactive не связан.

---

## Как устроено

```
tb-collector.timer (2 мин) → python -m twitch_badges collect
    опрос StreamDatabase → полный сбор при изменении / раз в 30 мин
    SD + Twitch Helix + Twitch GQL → проверка формата → записи → картинки → карточки
    → ОДНА транзакция в SQLite: снапшот + память окон + кэш категорий
tb-bot.service (Type=notify, WatchdogSec) → python -m twitch_badges bot
    inline (карточки по file_id) · личка: команды владельца
    публикация раз в 2 мин: записи с текущим временем → планировщик → outbox → канал
    карточки → служебный канал · мониторы · алерты · бэкап БД раз в сутки
tb-watchdog.timer (15 мин) → monitor/watchdog.sh (bash + curl)
    бот и сбор живы? диск? Telegram и SD отвечают? → алерты; пинг dead-man
```

**Источники.** StreamDatabase (даты, условия; ведут люди, у свежих кампаний бывает
пусто), Twitch Helix (описание и арт сразу, как Twitch завёл значок), Twitch GQL
(настоящие ссылки на категории), `manual/overrides.json` (ручные данные, перекрывают
всё; проверяются по схеме).

**Почему нет дублей.** Каждый пост закрывает «стадии» кампании (`announce`,
`started`, `ending`, `dates`, `cond`, `extended:<дата>`), и пара (кампания, стадия)
уникальна в БД. Пост сначала записывается в outbox вместе со стадиями, потом
уходит в Telegram. Если ответ Telegram потерян, бот пересылает свежие сообщения
канала в служебный канал и проверяет, дошёл ли пост, — и только потом решает,
отправлять ли снова.

**Что знает читатель.** По каждой кампании хранится окно, условие и цена ровно в
том виде, в каком они ушли в последний пост. Новые посты — только когда данные
отличаются от того, что читатель уже знает.

Код:

```
twitch_badges/
  domain/        окна, классификация, записи — чистые функции (build_records)
  publisher/     captions (тексты), planner (что постить), outbox (отправка), inline
  sources/       StreamDatabase, Helix, GQL, HTTP-клиент, проверки формата
  collector.py   сбор   ·  bot.py  процесс бота  ·  db.py/store.py  SQLite
  alerts.py      каталог алертов  ·  monitors.py  ·  media.py  карточки в Telegram
deploy/          systemd/tb-*, deploy.sh, rollback.sh, env.example
monitor/         watchdog.sh, alert.sh (bash + curl)
tests/           pytest
tools/legacy_sim/ старая логика (legacy/) и харнесс для сравнения и проверки отката
manual/          overrides.json (ручные данные), ignore.txt (молчащие значки без алерта)
```

---

## Установка с нуля

Нужны: Linux с systemd, Python 3.14, git, curl.

```bash
# 1. Системный пользователь без sudo и docker
sudo useradd --system --home-dir /var/lib/twitch-badges --shell /usr/sbin/nologin twitchbadges

# 2. Секреты
sudo install -d -m 0750 -o root -g twitchbadges /etc/twitch-badges
sudo install -m 0640 -o root -g twitchbadges deploy/env.example /etc/twitch-badges/env
sudoedit /etc/twitch-badges/env          # заполнить токены, PUBLISH_ENABLED=false

# 3. Код и юниты (тесты, venv, симлинк /opt/twitch-badges/current)
sudo deploy/deploy.sh HEAD

# 4. БД: перенос состояния старой установки (или пустая база для новой)
sudo -u twitchbadges env DATA_DIR=/var/lib/twitch-badges \
    /opt/twitch-badges/current/venv/bin/python -m twitch_badges migrate --from /путь/к/старой/data
sudo -u twitchbadges env DATA_DIR=/var/lib/twitch-badges \
    /opt/twitch-badges/current/venv/bin/python -m twitch_badges doctor

# 5. Запуск
sudo systemctl enable --now tb-collector.timer tb-watchdog.timer tb-bot.service
```

Служебный канал: создать приватный канал, добавить туда бота администратором —
бот сам запомнит канал и напишет владельцу. Или задать `TELEGRAM_STORAGE_CHAT_ID`.

После проверки (`plan --dry-run`, inline, `status`) — `PUBLISH_ENABLED=true` и
`sudo systemctl restart tb-bot`.

---

## Эксплуатация

Команды (от имени сервиса):

```bash
tb() { sudo -u twitchbadges env DATA_DIR=/var/lib/twitch-badges \
         /opt/twitch-badges/current/venv/bin/python -m twitch_badges "$@"; }
tb status            # данные, очередь постов, тревоги
tb plan --dry-run    # что ушло бы в канал прямо сейчас
tb collect --force   # внеочередной сбор
tb doctor            # целостность БД
tb backup            # копия БД в /var/lib/twitch-badges/backups
tb export-state --to /tmp/published.json   # состояние в формате старого бота
```

В личке бота (только из чата `ALERT_CHAT_ID`): `/status`, `/pause`, `/resume`,
`/sent <id>`, `/resend <id>`.

Журналы: `journalctl -u tb-bot -u tb-collector -u tb-watchdog -n 100`.

### Выкатка и откат

```bash
sudo deploy/deploy.sh <ref>     # тесты → бэкап → проверка БД → переключение → рестарт → smoke
sudo deploy/rollback.sh         # на предыдущий релиз
```

Выкатка сама откатывается, если бот не поднялся за 2 минуты. Правка файлов в
`/opt` деплоем не является: код меняется только через `deploy.sh`.

`manual/overrides.json` едет вместе с кодом (через `deploy.sh`); сбор подхватывает
его на следующем прогоне (≤ 2 мин), плохие записи пропускает с алертом.
Формат: `{ "<set_id>": {"group", "condition", "cost": "free|paid",
"start"/"end": "YYYY-MM-DD[THH:MM]" (UTC) или null, "link": {"label", "url"}} }`.

### Восстановление из бэкапа

```bash
sudo systemctl stop tb-bot.service tb-collector.timer
ls /var/lib/twitch-badges/backups/                    # или документ в служебном канале
sudo -u twitchbadges cp /var/lib/twitch-badges/backups/twitch_badges-YYYYMMDD-HHMM.sqlite3 \
                        /var/lib/twitch-badges/twitch_badges.sqlite3
sudo -u twitchbadges rm -f /var/lib/twitch-badges/twitch_badges.sqlite3-wal \
                           /var/lib/twitch-badges/twitch_badges.sqlite3-shm
tb doctor && sudo systemctl start tb-collector.timer tb-bot.service
```

Посты, ушедшие после бэкапа, бот не помнит: перед стартом проверь `tb plan --dry-run`
и при необходимости включи `/pause`.

---

## Алерты: что делать

Каждый алерт сам говорит, что случилось, как это влияет на канал, чья это сторона
и что делать. Кратко:

| Ключ | Что значит | Что делать |
|---|---|---|
| `collector-failing` | нет успешного сбора > 3 ч | SD лежит — ждать (данные догонят сами); иначе `journalctl -u tb-collector` |
| `format-drift` | StreamDatabase сменил формат | правка кода сбора; в тексте — какие поля |
| `posting-paused-stale` | данным > 6 ч: анонсы стоят, «стартовало»/«последний день» идут до 48 ч | см. `collector-failing` |
| `post-failed` | Telegram отклонил пост (400) — ошибка бота | исправить, выкатить; пост уйдёт повторно один раз |
| `channel-forbidden` | у бота нет прав в канале (403) | вернуть права администратора — посты догонят |
| `post-unknown` | не удалось понять, ушёл ли пост | посмотреть канал; `/sent <id>` или `/resend <id>` |
| `telegram-unreachable` | > 15 мин нет связи с Telegram | обычно ждать |
| `burst` | > 5 групп в очереди | если это мусор — `/pause` |
| `anomalies`, `blindspots` | пробел данных источника | обычно ничего; `manual/overrides.json` или `manual/ignore.txt` |
| `helix-auth` | Twitch не принимает ключи | проверить приложение на dev.twitch.tv |
| `overrides-invalid` | ошибка в `manual/overrides.json` | исправить запись |
| `db-integrity` | БД повреждена, постинг остановлен | «Восстановление из бэкапа» |
| `bot-down`, `bot-wedged`, `bot-restarting` | (watchdog.sh) бот лежит / цикл завис / рестарты | `journalctl -u tb-bot`; при «Telegram недоступен» — ждать |
| `collector-stale`, `disk-full` | (watchdog.sh) | по тексту алерта |
| dead-man (healthchecks.io) | сервер или watchdog не отвечают 30 мин | зайти на сервер |

---

## Разработка

```bash
python3.14 -m venv venv && venv/bin/pip install -r requirements.lock -r requirements-dev.txt
venv/bin/python -m pytest -q                 # всё (~6 мин)
venv/bin/python -m pytest -q -m "not slow"   # быстро (~1 мин)
```

Тесты не ходят в сеть и не используют боевой токен: Telegram — `tests/fake_telegram.py`
(подмена сетевого слоя PTB с инъекцией сбоев), StreamDatabase и Twitch —
`tests/fake_sd.py`. Дифференциальные тесты сравнивают новую логику со старой
(`tools/legacy_sim`) на снапшоте 30.09.2026 и его мутациях.

План и история решений — `docs/REFACTOR_PLAN.md`.
