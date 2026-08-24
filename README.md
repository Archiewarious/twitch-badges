# Twitch Badges Tracker

Отслеживает глобальные бейджи Twitch (ивентовые дропы, Prime Gaming, VIP, Hype Train
и т.п.), показывает **что можно получить, как и до когда**.

## Откуда берутся данные (три источника, в порядке приоритета)

1. **[StreamDatabase](https://www.streamdatabase.com)** — основной. Даёт структурные
   даты/условия и события. Минус: описание значка там пишет **живой модератор**, и у
   свежих кампаний его несколько часов нет вообще («We don't yet know if this badge is
   earned by subscribing or watching»).
2. **Twitch Helix** (`fetch_badges.py`) — второй. Отдаёт `description`/`click_url`
   сразу, как Twitch завёл значок. Включается сам, когда в `.env` появятся
   `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`; без них всё работает на одном SD.
   Публичный `badges.twitch.tv` для этого больше не годится — Twitch его убрал.
3. **`manual/overrides.json`** — ручной, перекрывает всё. Для случаев, когда кампания
   уже идёт, а оба автоматических источника молчат (так было с покемонами:
   условия опубликованы Twitch отдельной схемой, машинам недоступной).

Три поверхности вывода:
- **Telegram-бот** `@InfoTwitchBot` — inline в любом чате (карточки бейджей + кнопки на Twitch).
- **Канал** `@TwitchInfoRadar` — автопостинг жизненного цикла бейджа (появился → стартовал → последний день).
- **Статический сайт** — единая страница-каталог (актуальное + архив). Не в этом репозитории.

## Компоненты

| Файл | Что делает |
|---|---|
| `poll_changes.py` | **Сторож изменений** (таймер 2 мин): дешёвая сигнатура каталога+событий против последнего снапшота + точечная проверка страниц значков без условия. Нашёл разницу — немедленно запускает refresh. Состояния не хранит: сравнивает со снапшотом, поэтому упавший refresh повторится сам. |
| `fetch_streamdb.py` | Тянет данные со StreamDatabase (Next.js `_next/data`), достаёт ссылки на события со страниц бейджей. Пишет staging-снапшот атомарно. Guard'ы против пустого/битого ответа. Форс IPv4, ретраи. |
| `generate_site.py` | Классифицирует бейджи (active/upcoming/ended), качает картинки актуальных, рендерит `site/index.html`. |
| `render_cards.py` | Pillow-рендер карточек 640×640 (фон в цвет значка, адаптивный чип). |
| `refresh.sh` | Полный цикл: fetch → generate → cards → deploy → атомарный commit-marker (бот видит новые данные только когда картинки уже публичны). |
| `bot/bot.py` | Telegram-бот (python-telegram-bot). Читает локальный снапшот, inline-выдача, автопостинг в канал, staleness-gate, алерты владельцу. |
| `manual/overrides.json` | Ручные данные (кампания/условие/цена/даты/ссылка) с наивысшим приоритетом — когда SD и Twitch молчат. |
| `fetch_badges.py` | Twitch Helix: `description`/`click_url` вторым источником. Без ключей возвращает пусто и не мешает. |
| `monitor/alert.sh` | Независимый (bash+curl) Telegram-алертер с дедупом и recovery. |
| `monitor/watchdog.sh` | Проверка «данные свежие / бот жив / диск ок» (по таймеру). |
| `systemd/` | Юниты и drop-in'ы (bot, refresh+timer, watchdog+timer, tg-alert@) для воспроизводимости. |

## Поток данных

Скорость важна: конкурирующие каналы публикуют новый значок за ~15 минут после его
появления. Поэтому тяжёлый сбор отделён от дешёвого обнаружения.

```
poll.timer (2 мин)  ← ОБНАРУЖЕНИЕ, дёшево: 3 запроса, ~0.7 c
  └─ poll_changes.py
       сравнивает сигнатуру каталога+событий с data/streamdb_latest.json
       плюс точечно тянет страницы показываемых значков без условия/ссылки
       (описание им дописывают позже, каталог при этом не меняется)
       изменилось → sudo systemctl start twitch-badges-refresh.service

refresh.timer (30 мин)  ← подстраховка, если опрос почему-то молчал
  └─ refresh.sh   ← СБОР, тяжёлый
       fetch_streamdb.py → data/streamdb_incoming.json (staging, атомарно)
                           + Helix, если заданы ключи
       generate_site.py  → site/index.html + data/images/
       render_cards.py    → data/cards/
       deploy → /var/www/html/ (rsync)
       commit-marker: mv incoming → data/streamdb_latest.json   ← бот читает это

bot.py (long-running, systemd)
  ├─ inline: карточки по data/streamdb_latest.json (по mtime)
  └─ publish_new (каждые 2 мин): diff vs data/published.json → посты в канал

bot-reload.path   ← правка bot.py/generate_site.py/fetch_streamdb.py = авто-рестарт бота
                    (модуль живёт в памяти процесса; без этого правка не применяется)
overrides.path    ← правка manual/overrides.json = полный refresh
                    (меняет классификацию → нужны новые карточки и сайт)
```

**Худшая задержка от появления значка до поста:** ~2 мин (опрос) + ~30 c (сбор) +
~2 мин (тик публикации) ≈ **5 минут**.

Жизненный цикл поста: появился → *уточнили время* → *стало известно, как получить* →
стартовало → последний день. Два средних — догоняющие: SD часто заводит значок
раньше, чем узнает его условия, и пост «Условия уточняются» иначе остался бы
последним словом.

## Надёжность (см. also `systemd/`)

- **Guard'ы** в fetch: не пишем снапшот, если StreamDatabase вернул пусто/обвал → под `set -e` refresh абортит до commit, старые данные целы.
- **Бот не остаётся мёртвым**: `StartLimitIntervalSec=0` + backoff; локальный `MemoryMax`.
- **Staleness-gate**: бот не постит по данным старше 6ч.
- **Алертинг** (не зависит от Python): watchdog каждые 15 мин + OnFailure на refresh + бот-сайд алерты владельцу (необработанные исключения, незнакомые бейджи). Пишет в личку.

## Настройка

```bash
cd /home/archie/projects/twitch-badges
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
python3 -m venv bot/venv && ./bot/venv/bin/pip install -r bot/requirements.txt
cp .env.example .env      # заполнить TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ALERT_CHAT_ID
sudo cp systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now twitch-badges-bot.service twitch-badges-refresh.timer \
  twitch-badges-watchdog.timer twitch-badges-poll.timer twitch-badges-bot-reload.path
```

## .env

| Ключ | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от BotFather |
| `TELEGRAM_CHANNEL_ID` | Числовой id канала (`-100…`); пусто = автопубликация выключена |
| `PUBLISH_ENABLED` | `true` — постить в канал; `false` — донастройка без постинга |
| `ALERT_CHAT_ID` | Приватный chat владельца для алертов (НЕ публичный канал) |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | Тихие часы МСК для несрочных постов. Равные значения = выключено (сейчас так: новости нужны немедленно) |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | Ключи приложения с [dev.twitch.tv](https://dev.twitch.tv/console/apps). Включают Helix вторым источником; без них проект работает на одном StreamDatabase |

Неофициальный проект, не связан с Twitch Interactive.
