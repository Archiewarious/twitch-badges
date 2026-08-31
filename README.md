# Twitch Badges Tracker

Отслеживает глобальные бейджи Twitch (ивентовые дропы, Prime Gaming, VIP, Hype Train
и т.п.), показывает **что можно получить, как и до когда**.

## Откуда берутся данные (три источника, в порядке приоритета)

1. **[StreamDatabase](https://www.streamdatabase.com)** — основной. Даёт структурные
   даты/условия и события. Минус: описание значка там пишет **живой модератор**, и у
   свежих кампаний его несколько часов нет вообще («We don't yet know if this badge is
   earned by subscribing or watching»).
2. **Twitch Helix** (`fetch_badges.py`) — второй, ~355 описаний. Отдаёт
   `description` сразу, как Twitch завёл значок, не дожидаясь модератора SD.
   Включается сам при наличии `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`; без них
   всё работает на одном SD. `click_url` есть лишь у служебных значков (Bits и
   т.п.) — ссылок на кампании оттуда не будет. Публичный `badges.twitch.tv`
   Twitch убрал, хост не резолвится.
3. **`manual/overrides.json`** — ручной, перекрывает всё. Сейчас **пуст**: после
   разбора `steps` источники точнее любого ручного описания. Костыль устаревает
   молча (запись про NASA Roman держала закрытую кампанию как «скоро будет»),
   поэтому правило — удалять запись, как только источник отдал данные.

**Условия** берутся из структурного `steps`: внешний список — этапы («затем»),
внутренний — альтернативы («или»). Отсюда «подписка или гифт, затем смотреть
20 минут в 3 разных дня» — плоские поля этого не передают. Стоимость — по
наличию платного шага, а не по списку `costs` (тот относится ко всей кампании).

**Ссылки на категории** строятся из имени и **проверяются** по `og:title`
страницы: Twitch отвечает 200 на любой слаг, поэтому иначе догадку не отличить
от правды, а битая ссылка в inline-кнопке роняет весь ответ.

Три поверхности вывода:
- **Telegram-бот** `@InfoTwitchBot` — inline в любом чате (карточки бейджей + кнопки на Twitch).
- **Канал** `@TwitchInfoRadar` — автопостинг жизненного цикла бейджа (появился → стартовал → последний день).
- **Статический сайт** — единая страница-каталог (актуальное + архив). Не в этом репозитории.

## Компоненты

| Файл | Что делает |
|---|---|
| `check_format.py` | Проверки формата SD: структура ответа + здоровье результата (сколько значков доходит до показа, у скольких есть условие). Часовой таймер, OnFailure-алерт. |
| `test_pipeline.py` | Сквозные тесты на копиях реального снапшота: ловим ли смену формата (8 сценариев) и подхватим ли новую кампанию (4 способа завести). |
| `poll_changes.py` | **Сторож изменений** (таймер 2 мин): дешёвая сигнатура каталога+событий против последнего снапшота + точечная проверка страниц значков без условия. Нашёл разницу — немедленно запускает refresh. Состояния не хранит: сравнивает со снапшотом, поэтому упавший refresh повторится сам. |
| `fetch_streamdb.py` | Тянет данные со StreamDatabase (Next.js `_next/data`), достаёт ссылки на события со страниц бейджей. Пишет staging-снапшот атомарно. Guard'ы против пустого/битого ответа. Форс IPv4, ретраи. |
| `generate_site.py` | Классифицирует бейджи (active/upcoming/ended), качает картинки актуальных, рендерит `site/index.html`. |
| `render_cards.py` | Pillow-рендер карточек 640×640 (фон в цвет значка, адаптивный чип). |
| `refresh.sh` | Полный цикл: fetch → generate → cards → deploy → атомарный commit-marker (бот видит новые данные только когда картинки уже публичны). |
| `bot/bot.py` | Telegram-бот (python-telegram-bot). Читает локальный снапшот, inline-выдача, автопостинг в канал, staleness-gate, алерты владельцу. |
| `manual/overrides.json` | Ручные данные с наивысшим приоритетом — когда SD и Twitch молчат. Сейчас пуст; удалять запись, как только источник отдал данные. |
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
стартовало → последний день → *раздача завершилась*. Два средних — догоняющие: SD часто заводит значок
раньше, чем узнает его условия, и пост «Условия уточняются» иначе остался бы
последним словом.

## Надёжность (см. also `systemd/`)

- **Guard'ы** в fetch: не пишем снапшот, если StreamDatabase вернул пусто/обвал → под `set -e` refresh абортит до commit, старые данные целы.
- **Бот не остаётся мёртвым**: `StartLimitIntervalSec=0` + backoff; локальный `MemoryMax`.
- **Staleness-gate**: бот не постит по данным старше 6ч.
- **Алертинг** (не зависит от Python): watchdog каждые 15 мин + OnFailure на refresh + бот-сайд алерты владельцу (необработанные исключения, незнакомые бейджи). Пишет в личку.
- **Проверки формата** раз в час (`check_format.py` + `test_pipeline.py`): 13 сценариев — восемь смен формата SD и четыре способа завести кампанию. Ловят поломку сразу, а не по симптомам через сутки.

## Настройка

```bash
cd /home/archie/projects/twitch-badges
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
python3 -m venv bot/venv && ./bot/venv/bin/pip install -r bot/requirements.txt
cp .env.example .env      # заполнить TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ALERT_CHAT_ID
sudo cp systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now twitch-badges-bot.service twitch-badges-refresh.timer \
  twitch-badges-watchdog.timer twitch-badges-poll.timer twitch-badges-format.timer \
  twitch-badges-bot-reload.path twitch-badges-overrides.path
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
