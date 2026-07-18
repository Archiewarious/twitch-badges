# Twitch Badges Tracker

Отслеживает глобальные бейджи Twitch (ивентовые дропы, Prime Gaming, VIP, Hype Train
и т.п.), показывает **что можно получить, как и до когда**. Данные берутся со
[StreamDatabase](https://www.streamdatabase.com) (там условия/даты уже структурированы;
Twitch Helix/GQL этого не отдают — только картинки и названия).

Три поверхности вывода:
- **Telegram-бот** `@InfoTwitchBot` — inline в любом чате (карточки бейджей + кнопки на Twitch).
- **Канал** `@TwitchInfoRadar` — автопостинг жизненного цикла бейджа (появился → стартовал → последний день).
- **Статический сайт** — единая страница-каталог (актуальное + архив). Не в этом репозитории.

## Компоненты

| Файл | Что делает |
|---|---|
| `fetch_streamdb.py` | Тянет данные со StreamDatabase (Next.js `_next/data`), достаёт ссылки на события со страниц бейджей. Пишет staging-снапшот атомарно. Guard'ы против пустого/битого ответа. Форс IPv4, ретраи. |
| `generate_site.py` | Классифицирует бейджи (active/upcoming/ended), качает картинки актуальных, рендерит `site/index.html`. |
| `render_cards.py` | Pillow-рендер карточек 640×640 (фон в цвет значка, адаптивный чип). |
| `refresh.sh` | Полный цикл: fetch → generate → cards → deploy → атомарный commit-marker (бот видит новые данные только когда картинки уже публичны). |
| `bot/bot.py` | Telegram-бот (python-telegram-bot). Читает локальный снапшот, inline-выдача, автопостинг в канал, staleness-gate, алерты владельцу. |
| `monitor/alert.sh` | Независимый (bash+curl) Telegram-алертер с дедупом и recovery. |
| `monitor/watchdog.sh` | Проверка «данные свежие / бот жив / диск ок» (по таймеру). |
| `systemd/` | Юниты и drop-in'ы (bot, refresh+timer, watchdog+timer, tg-alert@) для воспроизводимости. |

## Поток данных

```
refresh.timer (30 мин)
  └─ refresh.sh
       fetch_streamdb.py → data/streamdb_incoming.json (staging, атомарно)
       generate_site.py  → site/index.html + data/images/
       render_cards.py    → data/cards/
       deploy → /var/www/html/ (rsync)
       commit-marker: mv incoming → data/streamdb_latest.json   ← бот читает это

bot.py (long-running, systemd)
  ├─ inline: карточки по data/streamdb_latest.json (по mtime)
  └─ publish_new (каждые 10 мин): diff vs data/published.json → посты в канал
```

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
sudo systemctl enable --now twitch-badges-bot.service twitch-badges-refresh.timer twitch-badges-watchdog.timer
```

## .env

| Ключ | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от BotFather |
| `TELEGRAM_CHANNEL_ID` | Числовой id канала (`-100…`); пусто = автопубликация выключена |
| `PUBLISH_ENABLED` | `true` — постить в канал; `false` — донастройка без постинга |
| `ALERT_CHAT_ID` | Приватный chat владельца для алертов (НЕ публичный канал) |

Неофициальный проект, не связан с Twitch Interactive.
