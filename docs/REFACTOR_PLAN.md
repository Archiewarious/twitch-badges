# План исправления и рефакторинга @InfoTwitchBot

Статус: фаза 1 (аудит) завершена 30.09.2026. Решения по открытым вопросам приняты
(раздел 6). Документ ждёт «ок» владельца на фазу 2. Секретов не содержит.

Рабочая копия: `/home/alex/twitch-badges-next`, ветка `refactor`. Прод: `/home/alex/twitch-badges`,
ветка `main`. Номера строк везде даны по коммиту `58b725c`: в обеих копиях код одинаковый.

---

## 0. Правила работы

- Работаем по фазам. После каждой фазы и каждой контрольной точки — короткий отчёт,
  дальше только после «ок» владельца.
- До фазы 3 не трогаем прод-папку, юниты в `/etc/systemd/system` и sudo. Причина:
  `twitch-badges-bot-reload.path` перезапускает бота при правке `bot/bot.py`,
  `generate_site.py`, `fetch_streamdb.py` в прод-папке. Любые команды с sudo и
  переключение прода — только по явной команде владельца.
- Боевой токен в тестах не используем. Ничего не отправляем в канал @TwitchInfoRadar
  и в личку владельцу.
- Не трогаем Docker HDREZKA и rezka-tunnel, не ходим по SSH на Латвию, не выводим
  секреты из `.env`.
- В worktree не запускать `monitor/alert.sh` и `monitor/watchdog.sh`: путь `PROJ` в них
  зашит на прод, и они отправят алерт боевым токеном.
- Коммиты: `git -c user.name=archie -c user.email=<адрес автора прежних коммитов> commit …`
  (глобальной identity в git нет; адрес — из `git log -1 --format=%ae`). Не пушить: `origin` указывает на Латвию, настоящий
  репозиторий — github.com/Archiewarious/twitch-badges.
- Никогда не коммитить `.env`, `data/`, `data.local-old/`.

---

## 1. Контекст

### 1.1 Что делает система

- **Канал @TwitchInfoRadar.** Автопостинг жизненного цикла глобальных значков Twitch:
  появился → уточнили время → стало известно, как получить → стартовало → последний день.
- **Бот @InfoTwitchBot.** Inline-поиск по актуальным значкам в любом чате. В личке
  только перенаправляет к inline и в канал.
- **Сайт больше не нужен** (решение владельца). Сейчас он нужен лишь для того, чтобы
  Telegram забирал карточки по `SITE_URL/cards/*.png`.

Источники данных:
1. StreamDatabase (SD) — основной; Next.js `_next/data`: каталог, события, страницы значков.
2. Twitch Helix — описания и картинки.
3. Twitch GQL — ссылки на категории. Используется публичный web Client-Id.
4. `manual/overrides.json` — ручные данные (сейчас пустой).

### 1.2 Текущий поток

```
twitch-badges-poll.timer (2 мин) → poll_changes.py
    3 запроса к SD + до 6 страниц значков; изменилось → sudo -n systemctl start refresh
twitch-badges-refresh.timer (30 мин) → refresh.sh (set -e)
    fetch_streamdb.py  → data/streamdb_incoming.json (SD + Helix + GQL)
    generate_site.py   → записи, data/images/, site/index.html, known_windows.json
    render_cards.py    → data/cards/*.png (перерисовывает все при каждом прогоне)
    rsync ×3 на Латвию (ssh-ключ ~/.ssh/rezka_lv, sudo -n rsync на той стороне)
    mv incoming → streamdb_latest.json   ← «commit-marker»; бот читает только его
twitch-badges-bot.service → bot/bot.py (PTB 22.8, long polling)
    inline; publish_new каждые 2 мин (diff против data/published.json)
    check_anomalies (6 ч), check_blindspots (3 ч), heartbeat → data/bot_alive (5 мин)
twitch-badges-format.timer (1 ч) → check_format.py --snapshot + test_pipeline.py
    OnFailure → tg-alert@.service
twitch-badges-watchdog.timer (15 мин) → monitor/watchdog.sh → monitor/alert.sh (bash+curl)
twitch-badges-bot-reload.path → рестарт бота при правке исходников
twitch-badges-overrides.path  → refresh при правке manual/overrides.json
```

### 1.3 Окружение (achie-server)

| Что | Значение |
|---|---|
| ОС и железо | Linux 7.0 (oracle), ARM64; TZ UTC, NTP синхронизирован; IPv6 на хосте нет |
| Python | 3.14.4, venv `/home/alex/twitch-badges/venv` (им пользуются и бот, и сбор) |
| Пакеты | python-telegram-bot 22.8 (+APScheduler 3.11.3), httpx 0.28.1, requests 2.34.2, Pillow 12.3.0, python-dotenv 1.2.3 |
| Юниты | `/etc/systemd/system/twitch-badges-*`, `tg-alert@.service`, drop-in `hardening.conf` у bot и refresh; `User=alex` |
| Пользователь alex | группы sudo, docker, adm, lxd; журналы читать может |
| Журнал | на этом хосте только с 30.09 12:28 UTC (день переезда) |
| user-systemd | запущен, `Linger=no` |
| Worktree | `/home/alex/twitch-badges-next`: свой venv (те же версии), копия `data/` от 17:18, без `.env` |

Ключи `.env` (только имена): `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `PUBLISH_ENABLED` (=true), `ALERT_CHAT_ID`,
`QUIET_HOURS_START`/`END` (=0/0, тихие часы выключены), `SITE_URL` (домен на duckdns,
смотрит на Латвию). `ALERT_BOT_TOKEN` не задан, поэтому алерты идут боевым токеном.

Файлы `data/`: кто пишет и кто читает.

| Файл | Пишет | Читает | Примечание |
|---|---|---|---|
| `streamdb_incoming.json` | fetch_streamdb | generate_site, render_cards | staging |
| `streamdb_latest.json` | refresh.sh (mv) | бот, poll, check_format, test_pipeline | ~590 КБ |
| `streamdb_latest.prev.json` | refresh.sh (cp) | никто | мёртвый |
| `published.json` | бот (атомарно) | бот | главное состояние, 49 записей |
| `monitor_state.json` | бот | бот | `incomplete_since`, `blindspots` (оба пустые) |
| `known_windows.json` | generate_site (до коммита снапшота!) | build_records (и в боте) | 63 записи, не чистится |
| `category_urls.json` | fetch_streamdb | fetch_streamdb | 144: 139 найдено, 5 «нет такой» |
| `.token_cache.json` | fetch_badges (неатомарно) | fetch_badges | токен Helix, истекает через ~29 дней |
| `images/`, `cards/` | generate_site / render_cards | бот (проверка наличия), rsync | 37 / 40 файлов |
| `alerts/<key>` | alert.sh | alert.sh | активных тревог нет |
| `bot_alive` | бот | watchdog.sh | |
| `poll_parse_fails.json` | poll | poll | |

### 1.4 Данные на 30.09.2026 (снапшот 17:04:56Z, копия в worktree)

| Показатель | Значение |
|---|---|
| Значков в каталоге | 523 (514 `added`, 9 `removed`), 196 `time_limited` |
| Событий | 27 |
| **Значков с `history`** | **0** (раньше дата появления бралась отсюда) |
| Значков с `added_at` | 297 |
| Значков с `availability` в каталоге | 0 (фолбэк B рассчитан на неё) |
| Значков с датами прямо в каталоге | 189. Время: 138 × `HH:MM:SS.mmm`, 44 × `HH:MM`, 1 × `HH:MM:SS`, 6 пусто |
| Время в событиях и availability | только `HH:MM` |
| Helix | 394 описания |
| page_info / page_availability / twitch_links | по 11 |
| Записей после build_records | 406; показываемых 38–40: статус считается от текущего времени, число меняется с ходом окон |
| Уникальных живых ключей для публикации | 37 |
| `published.json` | 49 записей: started 40, ending 15, dates_vague 15, dates_confirmed 3, cond_vague 2, cond_known 1, finished 1, from_orphan 0 |
| Записи с `end=None` | 23; из них у **19 живых** окно в данных известно, а в состоянии пусто |
| Страниц значков за один refresh | 11 (с исправлением формата станет 22) |
| Время | build_records 0,12 с; check_format 0,2 с; test_pipeline 0,9 с (17/17 зелёные) |

### 1.5 Что уже проверено

- Секретов в git-истории нет. Искал токены ботов, client secret, приватные ключи,
  chat id канала и алертов.
- `check_format --snapshot`: без замечаний. `test_pipeline`: 17/17. Но оба используют
  старый формат SD, см. B1.
- Холостой прогон `publish_new` на копии данных (`tools/legacy_sim/sim_publish.py`): очередь
  пуста, то есть прод-состояние согласовано.
- Прокрутка времени на 40 дней на неизменных данных (`sim_time.py`): 17 постов жизненного
  цикла, дублей нет, чистка отработала. **Вывод:** дубли и ложные посты возникают только
  при изменении данных (A3–A7, B5).
- Сценарий «условие появилось одновременно со стартом» (`sim_cond.py`) даёт два поста
  подряд с одинаковым текстом. Подтверждено.
- PTB при штатной остановке дожидается текущей джобы (`job_queue.stop(wait=True)`).
  Значит, рестарт через reload.path посредине публикации дубля не даёт.
- Latvia/nginx сейчас отдают карточки корректно: `image/png`, 200.
- Не проверено:
  - есть ли у alex NOPASSWD на `systemctl start` для poll: sudoers и polkit не читаются,
    а poll на этом хосте ещё ни разу не запускал refresh;
  - были ли реальные дубли из-за таймаутов: латвийские логи недоступны;
  - включено ли у канала «Restrict saving content».

---

## 2. Критерии готовности и как их проверяем

| Критерий (из задачи) | Чем обеспечиваем | Чем доказываем |
|---|---|---|
| Ни дублей | Уникальность стадии в БД (`stages`), outbox, сверка неоднозначных отправок, блокировка экземпляра | Тесты на фейковом Telegram: доставка с потерянным ответом, kill между отправкой и записью, 429 — ровно один пост. Дифференциальный прогон старой и новой логики на 40 днях |
| Ни пропусков | Модель «что знает читатель», адаптация к формату SD, живой монитор слепых зон, битая БД = стоп и алерт (без засева) | Сценарные тесты планировщика; dry-run после миграции; 24–48 ч параллельного прогона на тестовом канале рядом с продом |
| Самовосстановление (сеть, Telegram, источник) | Backoff сбора, retry-политики outbox, `Restart=always`, systemd watchdog | Сценарии T7–T10 фазы 3 |
| Нет зависаний | Таймауты на каждом сетевом вызове, дедлайн прогона сбора, `WatchdogSec`, flock без ожидания | T10 (искусственно заблокированный event loop) |
| Понятные алерты | Каталог алертов: что, влияние, чья сторона, что делать; отдельный токен; внешний dead-man | Снапшот-тесты текстов; T6–T9 |
| Простой деплой | Один `deploy.sh <ref>` + `rollback.sh`, lock-файл зависимостей, юниты из репозитория один в один | Установка с нуля на тестовом каталоге |
| Без Латвии | Посты и inline по `file_id`, сбор без rsync/ssh/sudo | `grep -r "SITE_URL\|rsync\|ssh " twitch_badges deploy` пуст; e2e при недоступном `SITE_URL` |

---

## 3. Реестр проблем

Серьёзность: **К** — критично, **В** — важно, **М** — мелочь. Объём: S / M / L.
В колонке «Шаг» — коммит плана, который закрывает проблему (раздел 7).

### A. Дубли и ложные посты

| ID | Проблема | Где | Данные и доказательство | Сер. | Объём | Шаг |
|---|---|---|---|---|---|---|
| A1 | Таймаут отправки считается неудачей, хотя пост мог уйти, и через 2 мин он уходит повторно. `read_timeout` PTB по умолчанию 5 с, при этом Telegram сам скачивает картинку с Латвии | `bot/bot.py:765`, `:842-862`, `:1432` | Механизм подтверждён по коду и дефолтам `HTTPXRequest` (5/5/5/1 с). Логов с Латвии нет | К | M | C06 |
| A2 | Нет журнала «отправляю/отправил»: при OOM, kill или падении между отправкой и `save_state` пост уйдёт снова. Альбом из нескольких частей сохраняется только после всех частей (между ними sleep 2 с) | `bot/bot.py:833-863`, `:1054-1075` | Штатный рестарт безопасен (PTB ждёт джобу). Риск остаётся при OOM (`MemoryMax=400M`), kill -9, питании | В | M | C04, C06 |
| A3 | Чистка удаляет запись через 7 дней после сохранённого `end`. Этот `end` записывается при анонсе и дальше не обновляется. Если кампанию продлили больше чем на 7 дней, значок будет объявлен заново | `bot/bot.py:618`, `:1136-1148` | У 19 из 36 живых записей `end=None`, у dron-e конец расходится с данными | К | S | C05 |
| A4 | В `supersedes_orphan` стоит `return None` вместо перехода к следующему орфану. Если у анонса нет конца окна, совпадение не найдётся никогда, и кампания уйдёт повторным анонсом | `bot/bot.py:663-666` | Чтение кода | В | S | C05 |
| A5 | Смысловой дубль: «Стартовало», а через 2 мин «Стало известно, как получить» с тем же текстом. Пост «Стартовало» уже содержит условие, но флаг `cond_known` не ставит | `bot/bot.py:945-969`, `:1062` | Воспроизведено `sim_cond.py` на `ampersand`. Кэш статуса на 30 мин (B7) делает это типичным | В | S | C05 |
| A6 | Нет блокировки второго экземпляра. Ручной `bot.py`, `backfill.py` (в его docstring прямо написано «можно при работающем боте») или две версии при переключении дают двух постеров на одном `published.json` | `bot/bot.py:1431`, `bot/backfill.py` | Чтение кода | В | S | C06, C09 |
| A7 | Поиск имени значка в тексте событий перебирает 483 архивных значка без окна. Среди них 59 однословных: Alliance, Horde, Diablo, Artist, Superhot… Старый значок получит даты нового события и уйдёт как «новый» | `generate_site.py:578` | Посчитано на снапшоте. Сейчас совпадений 0, но риск открыт | В | S | C02 |
| A8 | `_fix_stale_year` переносит настоящие старые даты поздно добавленного значка в текущий год, и выходит ложное «идёт сейчас» | `fetch_streamdb.py:194` | Чтение кода; защищает только фраза SD «added after the timeframe» | М | S | C02 |

### B. Пропуски и неверный контент

| ID | Проблема | Где | Данные и доказательство | Сер. | Объём | Шаг |
|---|---|---|---|---|---|---|
| B1 | **SD сменил формат каталога.** `history` → `added_at`, `badges[].availability` → поля прямо на значке. Молча отключились: монитор слепых зон (всегда «всё ок»), проверка страниц в poll, сканирование страниц у значков вне событий, оба фолбэка «без дат», фолбэк каталога, сортировка inline по новизне. `check_format` и `test_pipeline` зелёные: фикстуры в старом формате, а `known_windows` 14 дней подставляет потерянные даты | `fetch_streamdb.py:337`, `generate_site.py:308`, `:719`, `:929`, `:974`, `bot/bot.py:1332`, `poll_changes.py:179`, `test_pipeline.py:168`, `check_format.py:53` | `history` у 0 из 523, `availability` у 0, `added_at` у 297. Сейчас все 32 живых по данным каталога значка видны через события; значок только из каталога (как La Velada и EWC) пропадёт без тревоги | К | S–M | C02 |
| B2 | Латвия в цепочке. rsync стоит под `set -e` до коммита снапшота: нет SSH → данные не обновляются → через 6 ч посты встают. Telegram забирает картинки с Латвии (плюс DNS duckdns). 21.07 уже была ошибка «Wrong type of the web page content» | `refresh.sh:19-25`, `bot/bot.py:147-159` | Чтение кода; история в README | К | L | C07, C08 |
| B3 | Битый или пропавший `published.json` означает тихий «холодный старт»: всё текущее помечается опубликованным, включая значки без арта, которым ещё только предстоит анонс | `bot/bot.py:683-691`, `:920-924` | Чтение кода | В | S | C04, C05 |
| B4 | Подпись одиночного поста не ограничена 1024 символами. Выходит BadRequest на каждом тике, пост не уйдёт никогда. Альбом из одной части тоже не режется. Постоянные ошибки не отличаются от сетевых | `bot/bot.py:723`, `:765`, `:842` | Чтение кода | В | S | C06 |
| B5 | Когда настоящий значок замещает анонс-орфан, запись создаётся заново через `make_entry`, и теряются «Стартовало» и «Стало известно, как получить» | `bot/bot.py:937-944` | Чтение кода | В | S | C05 |
| B6 | Значок без дат получает «скоро» и больше ничего. `window_vague` считает окно без дат точным, поэтому не будет и «Уточнили время» | `bot/bot.py:218`, `generate_site.py:920-1005` | RuneFest 2026 (`runescape-shrimp`, `yellow-party-hat`): объявлен «скоро», событие прошло, потом тишина | В | M | C05 (+Q6) |
| B7 | Статус считается при загрузке снапшота и кэшируется до смены его mtime. «Стартовало» опаздывает до 30 мин, при лежащем SD — до 6 ч | `bot/bot.py:123-134` | Чтение кода | В | S | C05 |
| B8 | Стоп-кран на 6 ч глушит и посты, которые считаются из уже известных дат («Стартовало», «Последний день»). При долгом падении SD «Последний день» пропадёт | `bot/bot.py:903-909` | Чтение кода | В | S | C05 |
| B9 | Картинка не проверяется. HTML вместо PNG считается артом (`has_badge_art` смотрит только наличие файла), карточка рисуется без арта и уходит в канал. Файл не перекачивается никогда: докачиваются только отсутствующие | `fetch_streamdb.py:530-545`, `bot/bot.py:162`, `render_cards.py:122-128`, `generate_site.py:1894` | Чтение кода | В | S | C07 |
| B10 | Bits-значок помечается «бесплатно», неизвестный тип тоже. В канал уходит «🟢 Бесплатный значок … Потратить Bits» | `generate_site.py:715`, `:823`, `:948` | Чтение кода | В | S | C02 |
| B11 | Флаг `cancelled` игнорируется. Сейчас отменённых кампаний нет | `generate_site.py:861` | Посчитано | М | S | C02 |
| B12 | У `overrides.json` нет проверки схемы: `link` строкой вместо объекта роняет inline и пост этого значка (`AttributeError`) | `generate_site.py:1261`, `bot/bot.py:316-317` | Чтение кода | В | S | C08 |

### C. Самовосстановление и зависания

| ID | Проблема | Где | Сер. | Объём | Шаг |
|---|---|---|---|---|---|
| C1 | Если refresh стабильно падает, poll перезапускает тяжёлый сбор каждые 2 мин без backoff. Нагрузка на SD и риск бана Cloudflare | `poll_changes.py:271-311` | В | S | C08 |
| C2 | poll запускает refresh через `sudo -n systemctl` (нужен NOPASSWD, не проверено). При отказе тихо выходит с кодом 1, реакция падает до 30 мин | `poll_changes.py:305` | В | S | C08 |
| C3 | Зависший бот не лечится сам: watchdog только шлёт алерт. heartbeat — это `getMe`, он не доказывает, что публикация жива | `monitor/watchdog.sh:74-90`, `bot/bot.py:1372` | В | S | C09 |
| C4 | Запрос с ретраями может длиться до ~2 мин, Retry-After не ограничен сверху. Ошибки страниц значков глотаются, данные молча беднеют | `fetch_streamdb.py:34-36`, `:270-276` | В | S | C08 |
| C5 | Helix при 401 или битом кэше токена молча выключается до истечения токена (до ~60 дней). Кэш пишется неатомарно | `fetch_badges.py:40-61`, `:170` | В | S | C08 |
| C6 | `known_windows.json` пишется до коммита снапшота, а бот читает его вживую. `build_records` зависит от трёх файлов вне снапшота и от глобальных переменных модуля | `generate_site.py:1191`, `:1442-1452` | В | M | C03, C08 |
| C7 | ssh без `ConnectTimeout`/`ServerAliveInterval`: зависание refresh до `TimeoutStartSec=600`. Уйдёт вместе с rsync | `refresh.sh:21` | М | S | C08 |

### D. Алерты

| ID | Проблема | Где | Сер. | Объём | Шаг |
|---|---|---|---|---|---|
| D1 | Алерты идут тем же токеном и с того же хоста. Отзыв токена или падение сервера — и сигналов нет вообще. Внешнего dead-man нет | `monitor/alert.sh:27` | В | S | C10 |
| D2 | `PROJ` зашит в `alert.sh` и `watchdog.sh`. Запуск из любой копии шлёт алерт боевым токеном | `monitor/alert.sh:15`, `monitor/watchdog.sh:7` | В | S | C10 |
| D3 | Токен передаётся в аргументах curl (виден в `/proc`), `.env` исполняется как bash | `monitor/alert.sh:24-37` | В | S | C10 |
| D4 | Алерт упавшей проверки формата говорит только «юнит упал», без того, какая проверка не прошла | `systemd/tg-alert@.service` | М | S | C10 |
| D5 | «Бот повис», когда на деле недоступен Telegram: причина названа неверно | `monitor/watchdog.sh:74-90` | М | S | C10 |
| D6 | В тексте алерта burst «по одной за 10 минут», фактически раз в 2 мин | `bot/bot.py:1024` | М | S | C05 |
| D7 | `check_anomalies` глотает ошибку без лога | `bot/bot.py:1191` | М | S | C10 |

### E. Безопасность

| ID | Проблема | Где | Сер. | Объём | Шаг |
|---|---|---|---|---|---|
| E1 | Ключ `rezka_lv` вместе с `sudo -n rsync` на Латвии фактически дают root там. Сервисы работают от alex (sudo, docker = root здесь). Непроверенные данные SD и картинки (Pillow) разбираются под этим пользователем | `refresh.sh:20-22`, юниты `User=alex` | К | M | C11, уборка |
| E2 | Изоляция юнитов фиктивная: `ReadWritePaths` без `ProtectSystem` ничего не делает | `systemd/twitch-badges-bot.service` | М | S | C11 |
| E3 | В коммите `58b725c` IP, порт и пользователь Латвии. Не пушить в публичный репозиторий без решения (Q10) | `refresh.sh:20-21` | М | S | Q10 |
| E4 | `allowed_updates=ALL_TYPES`. В лог пишутся username и chat_id всех, кто пишет боту в личку | `bot/bot.py:493`, `:1458` | М | S | C09 |
| — | Секретов в истории нет; HTML в постах и на сайте экранируется корректно | — | — | — | — |

### F. Деплой, тесты, мёртвый код

| ID | Проблема | Где | Сер. | Объём | Шаг |
|---|---|---|---|---|---|
| F1 | Юниты в репозитории устарели (`/home/archie`, `bot/venv`), в `requirements.txt` нет PTB и APScheduler, версии не закреплены, README устарел | `systemd/*`, `requirements.txt`, `README.md` | В | M | C11 |
| F2 | Деплой делается правкой файла: reload.path рестартит прод при изменении исходников | `systemd/twitch-badges-bot-reload.path` | В | S | C11 |
| F3 | На публикацию и состояние нет ни одного теста. `test_pipeline` гоняется в проде раз в час на живых данных; фикстуры в старом формате | `test_pipeline.py` | В | M | C01 |
| F4 | `Persistent=true` не работает с `OnUnitActiveSec` | `systemd/*.timer` | М | S | C11 |
| F5 | Карточки перерисовываются и перезаливаются каждые 30 мин (новый ETag, ~3,6 МБ). При смене содержимого под тем же `?v=` Telegram может показать старую | `render_cards.py:274`, `bot/bot.py:144` | М | S | C07 |
| F6 | Мёртвое: HTML-сайт (~700 строк `generate_site.py`), `site/`, `backfill.py`, поле `finished`, `published.json.bak*`, `streamdb_latest.prev.json`, `camouflage_backup/` в `.gitignore`. `data.local-old/` в `.gitignore` отсутствует | разное | М | S | C12 |
| F7 | IPv4-хак подменяет `socket.getaddrinfo` на весь процесс. На хосте IPv6 нет (connect падает мгновенно) | `bot/bot.py:29-37` | М | S | C09 |
| F8 | Записи с `end=None` не удаляются никогда (23 из 49) | `bot/bot.py:1136` | М | — | закрывает A3 |
| F9 | Очередь сортируется по ключу, а не по возрасту: одиночные посты всегда раньше групп | `bot/bot.py:1041` | М | S | C05 |
| F10 | Шум в журнале: `orphan-event` каждые 2 мин, APScheduler на INFO | `generate_site.py:1429` | М | S | C08, C09 |
| F11 | GQL на неофициальном web Client-Id может сломаться. Официальная альтернатива — Helix `/games?name=` плюс URL `/directory/category/<slug>` из кэша | `fetch_streamdb.py:421` | М | S | заметка, C08 |

---

## 4. Архитектурные решения

Каждое решение: что выбрали, почему, альтернативы и статус.

**D1. Хранилище — SQLite (WAL, stdlib).** *Принято.*
- Почему: транзакция «пост ушёл + состояние» атомарна (закрывает A2, B3). Уникальность
  стадий обеспечивает сама БД. Бэкап — один файл. Писателей два (сбор и бот), для WAL с
  `busy_timeout` это штатно.
- Альтернатива: JSON + журнал отправок. Правок меньше, но транзакций между файлами нет,
  и снова появятся самописные атомарные записи.
- Альтернатива: Postgres. Лишний сервис и бэкапы при одном хосте и двух писателях;
  Postgres HDREZKA трогать нельзя.

**D2. Картинки — загрузка в Telegram и `file_id`.** *Принято (Q1).*
- Бот один раз загружает карточку в приватный служебный канал, хранит `file_id` по sha256
  содержимого. Посты и альбомы отправляются по `file_id`: быстро, без внешнего скачивания,
  без «Wrong type…», без Латвии.
- Inline переходит на `InlineQueryResultCachedPhoto` с подписью и кнопками. Минус: в
  мобильных клиентах результаты сеткой картинок, а не списком с описанием. Но карточки
  изначально сделаны так, чтобы объяснять себя: на них название и плашка цены.
- Альтернатива: раздавать карточки с этого сервера (Caddy + TLS). Список в inline
  сохранится, но нужен домен и открытый 443.
- Альтернатива: бакет OCI Object Storage. Портов не нужно, но появляются ключи и ещё
  один провайдер.

**D3. Процессы — collector (таймер) + bot (демон) + watchdog (bash).** *Принято.*
- Collector — один процесс вместо poll + refresh: дешёвый опрос раз в 2 мин, полный сбор
  при изменении или раз в 30 мин. flock, backoff, дедлайн прогона, без sudo.
- Бот — единственный, кто пишет в Telegram. `Type=notify` + `WatchdogSec`.
- `watchdog.sh` — независимый dead-man на bash + curl, плюс внешний пинг.
- Альтернатива: один демон на всё. Процессов меньше, но падение сбора или Pillow
  роняет бота, а блокирующий HTTP сидит в async-цикле.
- Альтернатива: оставить poll и refresh раздельно. Нужен sudo или polkit.

**D4. Модель состояния — «что знает читатель» + уникальность стадий.** *Принято.*
- Вместо флагов храним по кампании последние опубликованные окно, условие и цену, а
  также набор выполненных стадий: `announce`, `started`, `ending`, `dates`, `cond`,
  `extended:<дата>`. Пара (кампания, стадия) уникальна на уровне БД.
- Любой пост обновляет знание читателя тем, что реально было в подписи. Это закрывает
  A3, A5, B5 и даёт честный пост «Продлили» (Q7).
- Орфаны и переименования обрабатываются через таблицу алиасов: алиас указывает на
  каноническую кампанию, её стадии переносятся.
- Чистка — по «давно не видели живым» (30 дней), а не по сохранённому `end`.

**D5. Отправка — outbox и сверка неоднозначных случаев.** *Принято (Q2).*
- Порядок: строка outbox `sending` (commit) → отправка → `sent` + знание + стадии (commit).
- Ошибки классифицируются по причине из httpx (`__cause__` у исключения PTB):
  - connect error / pool timeout — запрос не ушёл, повторяем с backoff;
  - 429 — ждём `retry_after`;
  - 403 — алерт «нет прав»; повтор раз в 30 мин, после возврата прав посты уйдут сами;
  - 400 — наш баг: алерт и стоп, пока не выкатят исправление;
  - read timeout / обрыв после отправки — `unknown`, дальше сверка.
- Сверка (рекомендация): бот пересылает сообщения канала `last_id+1 … last_id+10` в
  служебный канал и сравнивает время (≥ момента отправки) и первые строки подписи.
  Нашёл — `sent`, копию удаляет. Не нашёл — отправляет заново.
- Если у канала включено «Restrict saving content», пересылка невозможна. Тогда алерт
  владельцу с командами `/sent <id>` и `/resend <id>` в личке бота.
- Альтернатива: `unknown` → только алерт, без автоповтора. Дублей нет гарантированно,
  но возможен пропуск до реакции владельца.

**D6. Источник — новый формат SD как структурная основа.** *Принято.*
- Даты, цена, `time_limited`, `cancelled` и `added_at` из каталога.
- Эвристики (поиск по тексту событий, разбор текста страниц) — только для свежих значков
  (≤ 32 дней по `added_at`).
- Канарейка формата проверяет каждое поле, которое мы читаем, и работает без памяти окон.

**D7. Алерты — каталог, отдельный токен, внешний dead-man.** *Принято (Q8).*
- Каждый алерт отвечает на четыре вопроса: что случилось, влияние на канал, чья сторона
  (источник / Telegram / мы), что делать (готовая команда).
- Состояние алертов в БД, семантика прежняя: дедуп, повтор раз в 6 ч, сообщение о
  восстановлении.
- Отправка отдельным `ALERT_BOT_TOKEN`. `watchdog.sh` пингует healthchecks.io: если хост
  умер, владелец узнает от внешнего сервиса.

**D8. Деплой — релизы из git, отдельный пользователь, юниты из репозитория.** *Принято (Q5).*
- Код в `/opt/twitch-badges/releases/<sha>`, симлинк `current`, venv по `requirements.lock`.
- Данные в `/var/lib/twitch-badges` (`StateDirectory`), секреты в `/etc/twitch-badges/env`
  (0640 `root:twitchbadges`), пользователь `twitchbadges` без sudo и docker.
- `deploy.sh <ref>`: тесты → бэкап БД → миграции → переключение симлинка → рестарт →
  smoke → автооткат при провале.
- Правка файлов больше не деплой: reload.path убираем.
- Новые юниты называются иначе (`tb-*`), чтобы старые остались для отката.
- Альтернатива: оставить `User=alex` и прод-папку как checkout тега, закрыв доступ к
  `/run/docker.sock` через `InaccessiblePaths`. Проще, но изоляция слабее.

**D9. Выбрасываем.** HTML-сайт и `site/`, rsync/ssh/sudo, `SITE_URL`, `backfill.py`,
reload.path и overrides.path (collector сам увидит правку за ≤ 2 мин), `tg-alert@`,
`test_pipeline` из прода (тесты переезжают в `deploy.sh`), IPv4-хак, `.prev.json`,
`.bak`-файлы. Сохраняем доменную логику: классификацию, фолбэки с их историей, тексты
постов, тихие часы, «одна группа за тик».

**Принцип переписывания.** Инфраструктуру пишем заново: состояние, публикация, сбор,
деплой, алерты. Доменную логику (`generate_site`, тексты из `bot.py`) переносим почти
без изменений, под тестами-характеристиками. В ней много выстраданных знаний о
странностях SD, и переписывать её с нуля опаснее, чем переносить.

---

## 5. Целевая архитектура

### 5.1 Процессы

```
tb-collector.timer (2 мин) → tb-collector.service (oneshot, flock, дедлайн 8 мин)
    опрос SD (3 запроса) → сигнатура (включая даты, цену, cancelled в каталоге)
    полный сбор, если сигнатура изменилась / страница свежего значка изменилась /
      прошло 30 мин с успешного — и только если истёк backoff после ошибок
    SD → Helix → GQL → проверки формата → записи → картинки (проверка PNG, атомарно)
      → карточки (sha256, пишем только изменённые) → ОДНА транзакция: снапшот + kv + runs
tb-bot.service (Type=notify, WatchdogSec=180, Restart=always)
    inline (CachedPhoto) · личка: редирект + команды владельца
    публикация раз в 2 мин: записи из текущего снапшота с now → планировщик → outbox
    медиа: загрузка недостающих карточек в служебный канал
    мониторы: anomalies, blindspots, возраст данных, доступность Telegram → алерты
    ежедневный бэкап БД: локально + в служебный канал
tb-watchdog.timer (15 мин) → watchdog.sh (bash + curl)
    heartbeat бота и сбора, состояние юнитов, диск, доступность SD и Telegram, пинг dead-man
```

### 5.2 Структура кода

```
twitch_badges/
  __main__.py      CLI: bot | collect | migrate | plan [--dry-run] | export-state | status
                        | media-sync | backup | doctor
  config.py        чтение env, пути (DATA_DIR), проверка значений
  db.py            соединение (WAL, FULL, FK, busy_timeout), миграции по schema_version, репозитории
  timeutil.py      время SD: HH:MM | HH:MM:SS | HH:MM:SS.mmm; МСК
  sources/http.py  общий клиент: таймауты, ограниченные ретраи, потолок Retry-After, circuit breaker
  sources/streamdb.py  SD + проверки формата
  sources/helix.py     токен с инвалидацией на 401, атомарный кэш в kv
  sources/gql.py       категории; отрицательный кэш с TTL вместо «4 попытки навсегда»
  domain/          windows.py, classify.py, conditions.py, records.py
                   (build_records(snapshot, ctx) — чистая функция без файлов и глобалов)
  collector.py     опрос, полный сбор, коммит
  cards.py         рендер (из render_cards) + sha256
  media.py         загрузка в Telegram, кэш file_id
  publisher/planner.py   plan(records, state, now, cfg) -> [Intent]  (чистая)
  publisher/captions.py  тексты постов и inline (из bot.py), лимиты 1024/4096
  publisher/outbox.py    исполнение, классификация ошибок, сверка
  bot.py           PTB: хендлеры, джобы, sd_notify, блокировка экземпляра
  alerts.py        каталог, дедуп в БД, отправка отдельным токеном
  monitors.py      anomalies, blindspots, возраст данных
  sdnotify.py      READY/WATCHDOG без зависимостей
tests/             фикстуры (снапшот 30.09 в новом формате, published.json), fake_telegram.py,
                   test_format, test_records, test_planner, test_outbox, test_migration,
                   test_collector, test_media, test_captions, test_differential
tools/legacy_sim/  харнесс старой логики (приложение C)
deploy/            systemd/tb-*.{service,timer}, deploy.sh, rollback.sh, env.example
monitor/           watchdog.sh, alert.sh — без зашитых путей
manual/            overrides.json + схема
assets/            шрифт Manrope (OFL)
```

### 5.3 Схема БД (v1)

```sql
PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- schema_version, migrated_from, current_snapshot_id

CREATE TABLE snapshots(                      -- пишет только collector
  id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, committed_at TEXT NOT NULL,
  signature TEXT NOT NULL, payload BLOB NOT NULL);           -- zlib(JSON); храним последние 48

CREATE TABLE campaigns(                      -- «что знает читатель», бывший published.json
  id TEXT PRIMARY KEY, title TEXT, grp TEXT, from_orphan INTEGER NOT NULL DEFAULT 0,
  known_start TEXT, known_end TEXT, known_vague INTEGER, known_condition TEXT, known_cost TEXT,
  first_posted_at TEXT, last_posted_at TEXT, last_seen_live_at TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE aliases(alias TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id), reason TEXT, created_at TEXT NOT NULL);

CREATE TABLE outbox(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, grp TEXT,
  payload TEXT NOT NULL,     -- caption_html, caption_text, media[{card_key,sha256}], buttons
  knowledge TEXT NOT NULL,   -- {campaign_id: {start,end,vague,condition,cost}} — что узнает читатель
  status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','unknown','retry','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, sending_at TEXT, sent_at TEXT,
  message_ids TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE stages(                         -- гарантия «никогда дважды»
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  stage TEXT NOT NULL,                       -- announce|started|ending|dates|cond|extended:YYYY-MM-DD
  outbox_id INTEGER REFERENCES outbox(id),   -- NULL = перенесено миграцией
  created_at TEXT NOT NULL, PRIMARY KEY(campaign_id, stage));

CREATE TABLE media(card_key TEXT NOT NULL, sha256 TEXT NOT NULL, file_id TEXT NOT NULL,
  file_unique_id TEXT, uploaded_at TEXT NOT NULL, PRIMARY KEY(card_key, sha256));

CREATE TABLE kv(ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(ns, key));  -- known_windows, category_urls, helix_token, monitor, channel, settings

CREATE TABLE runs(id INTEGER PRIMARY KEY, job TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, ok INTEGER, error_kind TEXT, error TEXT, stats TEXT);

CREATE TABLE alerts(key TEXT PRIMARY KEY, active INTEGER NOT NULL, subject TEXT, body TEXT,
  first_at TEXT, last_sent_at TEXT, cleared_at TEXT);
```

Кто пишет: collector — `snapshots`, `kv` (known_windows, category_urls, helix_token), `runs`.
Бот — всё остальное. Стадия занимается строкой в `stages` в той же транзакции, что и
создание строки outbox. Освобождается только явно: `/resend` или окончательный отказ,
при котором Telegram ничего не доставил.

### 5.4 Правила планировщика (перенос `publish_new` с исправлениями)

- Записи строятся на каждом тике из текущего снапшота с настоящим `now` (0,12 с). Это закрывает B7.
- **announce.** Кампании нет ни в `campaigns`, ни в `aliases`. Если это замещение
  орфана — совпадение start ±2 дня, end ±2 дня (или оба пустые), общая группа или
  категория — создаём алиас, стадии и знание переносятся, поста нет (A4, B5). Иначе вид
  поста: `appeared_active`, `appeared_upcoming`, `active_short` или «новый значок,
  сроки неизвестны» (Q6).
- **started.** Статус active, стадии нет. Если заодно выполняется условие для `ending`,
  уходит `active_short`, который закрывает обе стадии.
- **cond.** Читатель не знает условия (`known_condition IS NULL`), а в данных оно есть.
  Если в этом же тике уходит пост, содержащий условие, стадия считается выполненной (A5).
- **dates.** Окно читателя было неточным (включая «без дат»), теперь точное, до старта
  больше суток (B6).
- **ending.** Active, до конца ≤ 24 ч, стадии нет.
- **extended:\<дата\>** (Q7). Стадия `ending` выполнена, а конец в данных сдвинулся
  больше чем на сутки позже того, что знает читатель.
- **Свежесть данных.** Анонсы, `cond` и `dates` — не старше 6 ч. `started` и `ending` по
  точным датам — до 48 ч (B8). Старше 48 ч — стоп и алерт.
- Без настоящего арта не постим (как сейчас). Одна группа за тик, очередь по возрасту
  (`added_at`, затем `first_seen`, F9). Больше 5 групп — алерт burst с командой `/pause`.
- **Чистка.** Удаляем кампанию, которую не видели живой 30 дней (A3, F8).
- Битая БД или несовпавшая `schema_version` — не постим и шлём алерт `db-integrity`.
  Никакого засева заново (B3).
- Подписи режутся до лимитов: сначала необязательные части (список категорий, дисклеймер
  арта), потом хвост условия (B4).

### 5.5 Каталог алертов (черновик)

| Ключ | Когда | Что в тексте | Что делать |
|---|---|---|---|
| `collector-failing` | нет успешного сбора > 3 ч | последняя ошибка из `runs`, отвечает ли SD, с какого времени | источник лежит — ждать; иначе чинить |
| `format-drift` | проверка формата упала | какие поля и проверки, пример значения | правка кода |
| `post-failed` | 400 от Telegram | какой пост, текст ошибки | баг: исправить и выкатить, пост уйдёт повторно один раз |
| `channel-forbidden` | 403 | бота удалили из канала или сняли права | вернуть права, посты догонят сами |
| `post-unknown` | сверка не удалась | какой пост; `/sent <id>`, `/resend <id>` | проверить канал |
| `telegram-unreachable` | нет успешных вызовов API > 15 мин | с какого времени, последняя ошибка | обычно ждать |
| `bot-down` / `bot-restarting` | watchdog.sh | состояние юнита, число рестартов | журнал |
| `posting-paused-stale` | данным > 6 ч, анонсы остановлены | возраст данных, причина сбора | см. collector-failing |
| `burst` | > 5 групп в очереди | группы; `/pause` | решить, мусор ли это |
| `anomalies`, `blindspots` | как сейчас | как сейчас | пробел источника |
| `helix-auth` | 401 Helix 3 прогона подряд | — | ключи dev.twitch.tv |
| `disk-full` | как сейчас | как сейчас | как сейчас |
| `db-integrity` | `integrity_check` не ok | постинг остановлен | команда восстановления из бэкапа |
| dead-man (внешний) | healthchecks.io не получил пинг 30 мин | хост, сеть или watchdog | зайти на сервер |

### 5.6 Конфигурация (.env)

- Остаются: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `PUBLISH_ENABLED`, `ALERT_CHAT_ID`,
  `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `QUIET_HOURS_START`, `QUIET_HOURS_END`.
- Новые:
  - `TELEGRAM_STORAGE_CHAT_ID` — служебный канал;
  - `ALERT_BOT_TOKEN` — отдельный бот для алертов;
  - `DEADMAN_URL` — пинг healthchecks.io, опционально;
  - `DATA_DIR`;
  - только для тестов: `TELEGRAM_API_BASE`, `SD_BASE_URL`, `TB_FAULTS`.
- Удаляются: `SITE_URL`, `DEPLOY_HOST`, `DEPLOY_PORT`.
- Парсится как `KEY=VALUE`, без исполнения bash (D3).

---

## 6. Решения по открытым вопросам

Приняты 30.09.2026. Владелец поручил выбор решений исполнителю: цель — чтобы всё
работало надёжно без его участия. Владельцу остаются только разрешения на действия с
продом и то, что физически может сделать только он.

| # | Вопрос | Решение | Почему | Что нужно от владельца |
|---|---|---|---|---|
| Q1 | Inline без внешнего хостинга | Сетка карточек (`InlineQueryResultCachedPhoto` по `file_id`) | Ноль внешних зависимостей; на карточке уже есть название и цена; отправленное сообщение выглядит как пост канала | — |
| Q2 | Пост с неизвестной судьбой | Автосверка пересылкой в служебный канал. Если у канала защита контента — алерт с командами `/sent` / `/resend` | Восстанавливается без человека. Защиту бот проверяет сам при старте (`getChat.has_protected_content`) | — |
| Q3 | Служебный канал | Приватный канал, бот — админ. Бот сам запоминает его id, когда его добавляют админом (`my_chat_member`) | Хранилище карточек и бэкапов без внешних сервисов | Создать тестовый (фаза 3) и боевой (переключение) по пошаговой инструкции |
| Q4 | Хотфикс формата SD в прод до фазы 3 | Да: C02 выкатывается отдельно, как только готов и проверен | Регрессия живая; эффект посчитан — 0 постов, 0 тревог | Одно слово «выкатывай» на КТ1 (правило: прод только по команде) |
| Q5 | Системный пользователь | Да: `twitchbadges`, код в `/opt`, данные в `/var/lib`, без sudo и docker | Взлом бота не даёт root ни здесь, ни на Латвии | Ничего: всё делает `deploy.sh`, запуск по команде в фазе 3 |
| Q6 | Значки без дат | «Новый значок — сроки пока неизвестны», затем «Уточнили время», когда даты появятся | Честно и без тишины; не обещаем «скоро», если раздача, возможно, уже идёт | — |
| Q7 | «Продлили до …» | Да | Исправляет устаревший «Последний день»; случается редко | — |
| Q8 | Отдельный алерт-бот + healthchecks.io | Да; без них — фолбэк на основной токен | Алерты должны работать, даже если лёг основной бот или весь сервер | Создать по инструкции к фазе 3 (~5 минут) |
| Q9 | Ресурсы фазы 3 | Тестовые бот, канал, служебный канал, чат для алертов | Тесты без боевого токена и без вашего канала | Создать по инструкции к фазе 3 |
| Q10 | GitHub | Не пушим. Если понадобится — отдельная ветка без истории с IP Латвии (squash) | Правило задачи; IP уходит из кода на уборке | Решение, только если захотите выложить код |

---

## 7. План работ

### 7.0 Этап 0 (по Q4): хотфикс формата SD в прод

**Состав.** Коммит C02, перенесённый на ветку `hotfix/sd-format` от `main`:
1. Дата появления значка: `history` с фолбэком на `added_at`
   (`fetch_streamdb._badge_added_at`, `generate_site.badge_first_seen`).
2. Окна из полей каталога (`start_at_*`, `end_at_*`, `cost`, `cancelled`). Время
   `HH:MM[:SS[.mmm]]`, `parse_dt` принимает секунды.
3. Поиск по тексту событий — только для значков с `added_at` ≤ 32 дней (A7).
4. `cancelled` → ended (B11). Bits → платно, неизвестный тип → цена не указана (B10).
5. Сигнатура poll включает новые поля каталога.
6. `check_format` проверяет: долю значков с `added_at`/`history`, разбор дат каталога,
   формат времени в событиях, наличие каждого читаемого поля; запускается без
   `known_windows`. Фикстуры `test_pipeline` переводятся на новый формат.

**Ожидаемый эффект (посчитан на снапшоте 17:04Z).**
- Показываемых значков (замер в один и тот же момент): 38 → 40. Вернутся `runescape-shrimp` и `yellow-party-hat` как
  «без дат». Оба уже в `published.json`, поэтому **0 постов**.
- Тревог «слепых зон» после оживления монитора: **0**.
- Страниц значков за refresh: 11 → 22.
- **Ловушка:** исправление только `added_at` без дат каталога вернёт `blue-creeper-boss`
  (по каталогу закончился 22.09) как «скоро». После следующего сбора, когда скачается
  картинка, был бы ложный анонс. Поэтому пункт 2 обязателен.

**Выкатка (только по команде).**
1. В worktree: `pytest`, `check_format`, прогоны `tools/legacy_sim` с патчем — 0 постов.
2. В прод-папке: `git merge --ff-only hotfix/sd-format`. reload.path перезапустит бота
   штатно; poll и refresh подхватят код со следующего запуска.
3. Проверка: следующий refresh без ошибок (`journalctl -u twitch-badges-refresh`),
   `check_format --snapshot` ок, холостой прогон 0 постов, лог `check_blindspots` без тревог.
4. Откат: `git reset --hard 58b725c` в прод-папке, бот перезапустится сам.

### 7.1 Фаза 2: коммиты в `refactor`

У каждого коммита: цель, содержание, тесты, критерий приёмки, что закрывает.
Контрольные точки с отчётом владельцу: **КТ1** после C02, **КТ2** после C06,
**КТ3** после C12 (конец фазы 2).

**C01. Каркас тестов, фикстуры нового формата, харнесс старой логики** (M; F3).
- `requirements-dev.txt` с pytest.
- `tests/fixtures/`:
  - урезанный снапшот 30.09 (реальный новый формат, gzip);
  - копия `published.json`;
  - мутации: новый значок, продление, орфан → значок, условие одновременно со стартом,
    без дат, bits, смена формата.
- `tools/legacy_sim/` превращается в модуль для дифференциальных тестов.
- Приёмка: `pytest -q` зелёный; прокрутка на 40 дней на фикстуре даёт те же 17 постов,
  что и сейчас.

**C02. Формат SD и контентные ошибки — кандидат на хотфикс** (S–M; B1, A7, A8, B10, B11).
- Состав — пункты 1–6 из 7.0. Плюс `_fix_stale_year` не трогает даты, если значок
  добавлен после окна.
- Приёмка: эффект совпадает с 7.0; тесты формата ловят пропажу `added_at`, смену формата
  времени, пропажу дат каталога.
- **КТ1:** отчёт и решение по хотфиксу.

**C03. Пакет `twitch_badges`: перенос доменной логики без изменения поведения** (M; C6, часть F6).
- `generate_site` раскладывается по `domain/*`, HTML удаляется.
- `build_records(snapshot, ctx)` получает на вход overrides, known_windows, имена
  категорий и `now` — без чтения файлов и без глобальных переменных.
- Тексты постов из `bot.py` переезжают в `publisher/captions.py`.
- Приёмка: записи на фикстуре до и после переноса побайтно равны (сериализация);
  подписи на фикстуре равны.

**C04. SQLite, миграции, перенос и экспорт состояния** (M; B3, основа для A2).
- `db.py` со схемой из 5.3, `schema_version`.
- `migrate --from <data_dir>`: перенос по приложению B, исходные файлы только читаются.
- `export-state --to published.json`: для отката.
- `doctor`: `integrity_check` и проверка инвариантов.
- Тесты:
  - после миграции `plan --dry-run` не даёт ни одного анонса для ключей из `published.json`;
  - круговой перенос `migrate(export(migrate(x))) == migrate(x)`;
  - битая БД → отказ и алерт, не засев.

**C05. Планировщик и модель «что знает читатель»** (L; A3, A4, A5, B5, B6, B7, B8, D6, F9).
- Правила из 5.4; чистая функция, никакого ввода-вывода.
- Тесты — таблица сценариев:
  - новый значок, группа тиров, продление, орфан → значок (с переносом стадий);
  - переименование орфана;
  - условие вместе со стартом;
  - без дат → даты появились;
  - данные старше 6 и 48 ч;
  - нет арта;
  - burst;
  - чистка;
  - порядок очереди.
- Дифференциальный прогон против старой логики: 40 дней + мутации. Расхождения только
  из списка намеренных исправлений, и этот список фиксируется в тесте.

**C06. Outbox и отправка** (L; A1, A2, A6, B4).
- Retry-политики и классификация ошибок из D5, сверка (Q2), лимиты подписей.
- flock на экземпляр.
- Команды владельца в личке (только `ALERT_CHAT_ID`): `/status`, `/pause`, `/resume`,
  `/sent <id>`, `/resend <id>`.
- `tests/fake_telegram.py` — подмена `BaseRequest` с инъекцией сбоев:
  - доставил, но ответ потерян;
  - connect error; 429; 400; 403;
  - kill между `sending` и `sent` (новый процесс на той же БД);
  - канал с защитой контента.
- Приёмка: в каждом сценарии ровно один пост; неразрешимые случаи дают алерт `post-unknown`.
- **КТ2:** отчёт.

**C07. Медиа через Telegram** (M; B2 со стороны Telegram, B9, F5).
- Проверка картинок: `Content-Type`, размер, `Image.verify`, размеры в пикселях.
  Атомарная запись; битый файл перекачивается.
- Карточка перезаписывается только при изменении sha256.
- Загрузка в служебный канал (не чаще 1 в 2 с), кэш `file_id`.
- Посты по `file_id`; inline по Q1.
- Тесты: одна загрузка на sha256; изменённая карточка → новая загрузка; битый PNG
  отклонён и перекачан; пост использует `file_id`.

**C08. Collector** (L; B2 со стороны сбора, B12, C1, C2, C4, C5, C6, C7, F10, F11).
- Один процесс: опрос + полный сбор. flock, backoff с джиттером (2→60 мин), дедлайн
  прогона 8 мин.
- Вежливость к SD: страницы последовательно с паузой 0,3–0,5 с, потолок Retry-After 60 с.
- Одна функция «чьи страницы сканировать» — общая для опроса и сбора (так не бывает
  вечного цикла refresh).
- Helix: при 401 сброс токена и одна попытка заново.
- GQL: отрицательный кэш с TTL 7 дней.
- Проверка схемы overrides: плохая запись → алерт, запись пропускается.
- `known_windows` пишется в той же транзакции, что и снапшот. Ошибки попадают в `runs`
  с видом ошибки.
- Нет rsync, ssh, sudo.
- Тесты на заглушке SD: 503 → backoff; 404 buildId → повтор; пустой каталог → стоп без
  коммита; дрейф формата → запись и алерт; 401 Helix.

**C09. Процесс бота** (S; C3, E4, F7, F10).
- PTB: `HTTPXRequest(connect 10, read 15, write 30, pool 5)`; у отправок в канал
  `read_timeout=60`; `base_url` настраивается.
- `run_polling(allowed_updates=["message","inline_query"], drop_pending_updates=True)`.
- sd_notify: `READY=1` после старта, `WATCHDOG=1` из джобы раз в 60 с — пинг доказывает,
  что цикл жив, но не зависит от доступности Telegram, иначе будет бесконечный рестарт.
- Heartbeat-файлы для `watchdog.sh`.
- Бэкап БД раз в сутки: локально 14 копий + документ в служебный канал.
- IPv4-хак удаляется, логирование личных сообщений сокращается.
- Тест: при заблокированном цикле пинги watchdog прекращаются.

**C10. Алерты** (M; D1–D5, D7).
- `alerts.py` по каталогу 5.5, отдельный токен (Q8), дедуп и восстановление в БД.
- `watchdog.sh` и `alert.sh`: пути из env/аргументов, токен через `curl -K -` (stdin),
  разбор `.env` без `source`, проверка доступности Telegram и SD, пинг `DEADMAN_URL`,
  режим `ALERT_DRY_RUN=1`.
- Тесты: снапшоты текстов алертов, дедуп, повтор, восстановление; shellcheck.

**C11. Деплой** (M; E1 со стороны рантайма, E2, F1, F2, F4).
- `deploy/systemd/tb-bot.service`, `tb-collector.{service,timer}`, `tb-watchdog.{service,timer}`.
- Изоляция: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  `PrivateDevices`, `ProtectKernel*`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`,
  `SystemCallFilter=@system-service`, `CapabilityBoundingSet=`, `StateDirectory`,
  `MemoryMax`. Цель `systemd-analyze security` ≤ 3,0.
- `requirements.lock`, `deploy.sh` и `rollback.sh`, README: установка, эксплуатация,
  runbook по алертам.
- Приёмка: `systemd-analyze verify`; установка с нуля в тестовый каталог по README.

**C12. Уборка** (S; F6, F8).
- Удалить `poll_changes.py`, `refresh.sh`, `generate_site.py`, `render_cards.py`,
  `fetch_*.py` (всё переехало в пакет), `bot/`, старые `systemd/*`, `backfill.py`.
- Поправить `.gitignore`, финальный README.
- **КТ3:** отчёт по фазе 2.

### 7.2 Фаза 3: тестовый стенд, проверка, переключение

**Стенд (без sudo).**
- Код — worktree. `DATA_DIR=~/tb-test-data`: мигрированная копия прод-состояния на
  момент старта.
- `~/tb-test.env` (600): тестовый бот, тестовый канал, служебный канал, тестовый чат для
  алертов.
- Запуск через `systemd-run --user`: так проверяются `Type=notify` и `WatchdogSec`. Юниты
  живут, пока открыта сессия; `Linger=no`, и для тестов этого достаточно.
- Боевой токен не используется ни в одном сценарии.

**Сценарии.**

| # | Сценарий | Как вызвать | Ожидаем |
|---|---|---|---|
| T1 | Старт на мигрированной копии | `plan --dry-run`, затем включить постинг | 0 анонсов; inline работает |
| T2 | Новый значок | подложить мутированный снапшот (`SD_BASE_URL` → заглушка) | ровно 1 пост |
| T3 | Доставлено, ответ потерян | `TELEGRAM_API_BASE` → fault-прокси (пересылает и рвёт ответ) | 1 пост, `sent` после сверки |
| T4 | kill -9 между отправкой и записью | `TB_FAULTS=kill_after_send` | после рестарта 1 пост |
| T5 | 429 | fault-прокси | ожидание, 1 пост |
| T6 | Бота убрали из тестового канала | вручную | алерт `channel-forbidden`; после возврата прав посты догоняют |
| T7 | SD лежит | заглушка отдаёт 503 | backoff; через порог понятный алерт; после восстановления догоняет |
| T8 | SD сменил формат | заглушка без `added_at` / время в новом формате | алерт `format-drift` с названием поля |
| T9 | Telegram недоступен | прокси выключен | без рестарт-цикла; алерт через 15 мин; после восстановления без дублей |
| T10 | Зависание цикла | `TB_FAULTS=block_loop` | systemd watchdog перезапускает |
| T11 | Битая картинка | заглушка CDN отдаёт HTML | отклонена, перекачана, пост без арта не уходит |
| T12 | Длинная подпись | мутация с 20 категориями | подпись обрезана, пост ушёл |
| T13 | Рестарт посреди альбома | рестарт во время отправки | без дублей |
| T14 | Откат | `export-state` → харнесс старой логики на той же минуте | 0 постов |
| T15 | Параллельный прогон 24–48 ч | стенд на живом SD рядом с продом | посты тестового канала соответствуют прод-каналу; расхождения только из списка исправлений |

**Переключение** — по вашей явной команде, в окне, когда `plan --dry-run` не показывает
постов в ближайший час. Даты и команды приведены для варианта Q5 = а.

1. Заранее, без влияния на прод:
   - `useradd --system twitchbadges`;
   - `deploy.sh <tag>` в `/opt`;
   - `/etc/twitch-badges/env`;
   - `cp deploy/systemd/* /etc/systemd/system && systemctl daemon-reload` (юниты не включать);
   - бот — админ служебного канала; `media-sync` заливает карточки только в служебный канал.
2. Заморозка старого:
   - `systemctl stop twitch-badges-{poll,refresh,format,watchdog}.timer twitch-badges-{bot-reload,overrides}.path`;
   - дождаться, пока `twitch-badges-refresh.service` станет inactive;
   - `systemctl stop twitch-badges-bot.service` (штатно: дожидается текущей публикации).
3. Бэкап: `tar czf ~/tb-cutover-$(date +%F-%H%M).tgz -C /home/alex/twitch-badges data .env`.
4. Миграция: `python -m twitch_badges migrate --from <копия data>`, затем `doctor`.
5. Проверка: `plan --dry-run` → 0 анонсов; сравнение с `tools/legacy_sim` на эту же минуту.
   Совпало — дальше, иначе откат.
6. Старт с `PUBLISH_ENABLED=false`: `systemctl start tb-collector.timer tb-watchdog.timer tb-bot.service`.
   Проверить: inline в любом чате, прогон collector, `status`, один тестовый алерт владельцу
   (с его согласия в этот момент).
7. `PUBLISH_ENABLED=true`, `systemctl restart tb-bot`, наблюдать 3–5 тиков.
8. `systemctl disable` старых юнитов (файлы остаются для отката на 14 дней).

**Откат (≤ 5 мин).**
1. `systemctl stop tb-bot tb-collector.timer tb-watchdog.timer`.
2. `export-state` → `/home/alex/twitch-badges/data/published.json`: старый бот узнает о
   постах, сделанных новым.
3. Харнесс старой логики → 0 постов.
4. `systemctl enable --now` старых юнитов.

Латвийский rsync до уборки остаётся рабочим, поэтому откат возвращает полностью
рабочую старую систему.

**После переключения.**
- 48 ч наблюдения: алерты, посты, журнал outbox (`status`), ручная сверка канала.
- Через 14 дней без инцидентов — уборка:
  - удалить старые юниты и `tg-alert@`;
  - убрать NOPASSWD-правило для poll (проверить `sudo -l`);
  - удалить `~/.ssh/rezka_lv` с этого хоста; ключ и sudoers на Латвии владелец убирает
    сам (мы туда не ходим);
  - удалить `data.local-old/` и старую прод-папку (или оставить её как dev-клон).

---

## 8. Тестовая стратегия

| Уровень | Что | Где |
|---|---|---|
| Unit | время SD, условия и цены, классификация, подписи и лимиты | `tests/test_records.py`, `test_captions.py` |
| Планировщик | таблица сценариев; инварианты: не больше одного поста на (кампания, стадия), знание читателя равно содержимому последней подписи | `test_planner.py` |
| Outbox | фейковый Telegram (`BaseRequest`) с инъекцией сбоев; kill и перезапуск на одной БД | `test_outbox.py` |
| Миграция | фикстура `published.json` → БД → dry-run → экспорт → круговой перенос | `test_migration.py` |
| Формат | фикстуры нового формата + мутации (каждое читаемое поле пропадает или меняет форму) | `test_format.py` |
| Дифференциальный | старая логика (`tools/legacy_sim`) против новой: 40 дней + мутации; расхождения только из утверждённого списка | `test_differential.py` |
| E2E | тестовый бот и канал, fault-прокси, заглушка SD | фаза 3, T1–T15 |

Прогон: `pytest -q` локально и в `deploy.sh`, до переключения симлинка. В проде тесты
больше не гоняются. В проде работает только проверка формата внутри collector — это
канарейка по живым данным.

---

## 9. Риски

| Риск | Мера |
|---|---|
| Сверка невозможна (у канала защита контента) | проверить заранее (Q2); фолбэк — команды владельцу |
| Inline сеткой не понравится | слой медиа с двумя реализациями: `file_id` и URL-хостинг (D2 б/в) |
| SD снова сменит формат во время работ | канарейка формата + путь хотфикса (7.0) |
| Миграция упустит нюанс состояния | дифференциальные прогоны, dry-run, круговой перенос, T14 |
| Старая и новая версии запущены одновременно | разные имена юнитов, заморозка старого в чек-листе, алерт Conflict |
| Лимиты Telegram при заливке карточек | 1 загрузка в 2 с; карточек ~40 |
| Объём работ | контрольные точки КТ1–КТ3; хотфикс отдельно и первым |
| Совместимость Python 3.14 / PTB 22.8 | закреплённые версии, тесты в `deploy.sh` |

---

## Приложение A. Формат SD: было и стало (каталог значков)

| Поле | Было (до ~27.09.2026) | Стало (30.09.2026) |
|---|---|---|
| Дата появления | `history[{type:"added", timestamp}]` | `added_at` (ISO) у 297 из 523; `history` нет ни у одного |
| Окно и условие | `availability[…]` у значка | `availability` нет; у 189 значков `start_at_date/time`, `end_at_date/time` прямо на значке |
| Время | `HH:MM` | в каталоге `HH:MM:SS.mmm` (138), `HH:MM` (44), `HH:MM:SS` (1), пусто (6); в событиях и availability по-прежнему `HH:MM` |
| Новые поля | — | `cost` (free/paid), `time_limited` (196 true), `cancelled` (0), `removed` (9), `system` |
| `user_count` | число | менялся по форме (исправлено в f898acc) |

События: `title`, `content`, `start_at_*`, `end_at_*`, `hidden`, `twitch_global_badges[].availability[]`
(поля `objectives`/`steps`, `categories`, `costs`, `hidden`, флаги условий, `upvote_count`/`downvote_count`, `user`).

## Приложение B. Перенос `published.json` в БД

| Поле записи | Куда |
|---|---|
| ключ | `campaigns.id` |
| `title`, `group` | `title`, `grp` |
| `appeared` | стадия `announce` (`outbox_id` NULL) |
| `started` | стадия `started` |
| `ending` | стадия `ending` |
| `dates_vague`, `dates_confirmed` | `known_vague = dates_vague && !dates_confirmed`; `dates_confirmed` → стадия `dates` |
| `cond_vague`, `cond_known` | условие неизвестно, если `cond_vague && !cond_known`; иначе `known_condition` = текущее условие или `<migrated>`; `cond_known` → стадия `cond` |
| `start`, `end` | `known_start`/`known_end` из **текущей** живой записи, если значок жив (чтобы не было ложного «Продлили»); иначе сохранённые |
| `from_orphan`, `superseded` | `from_orphan`; `superseded` → алиас, если нашлась живая кампания с совпадающим окном, иначе запись без стадий |
| `finished` | игнорируется (пост удалён из продукта) |
| — | `last_seen_live_at` = сейчас (чистка через 30 дней, если не жив) |

Прочее:
- `monitor_state.json` → `kv(monitor)`;
- `known_windows.json` → `kv(known_windows)`;
- `category_urls.json` → `kv(category_urls)`;
- `.token_cache.json` не переносится, токен получим заново;
- `data/alerts/*` → `alerts(active=1)`.

Экспорт для отката — обратное отображение в прежний формат `published.json`.

## Приложение C. Инструменты (`tools/legacy_sim/`)

- `harness.py` — модуль: `LegacySim` гоняет настоящий `publish_new` на отдельном каталоге
  с подменёнными часами, фейковым Telegram и заглушками алертов. Им пользуются тесты
  (`tests/test_legacy_baseline.py`) и скрипты ниже.
- `sim_publish.py <outdir> [ticks] [--data DIR]` — холостой прогон на копии `data/`.
- `sim_time.py <outdir> <шаг_часов> <дней> [--data DIR]` — прокрутка времени.
- `sim_cond.py <outdir> <set_id> [--data DIR]` — сценарий A5.
- `tools/make_fixtures.py <data_dir> <tag>` — снять фикстуры `tests/fixtures/*_<tag>.*`.

Все пишут только в `<outdir>`, исходный каталог данных только читают. Боевой токен и
сеть не используются, `alert.sh` не вызывается. Путь к репозиторию берётся от файла.

## Приложение D. Команды проверки (только чтение)

```bash
cd /home/alex/twitch-badges-next
TELEGRAM_BOT_TOKEN=0:dummy ./venv/bin/python check_format.py --snapshot
TELEGRAM_BOT_TOKEN=0:dummy ./venv/bin/python test_pipeline.py
./venv/bin/python tools/legacy_sim/sim_publish.py "$(mktemp -d)" 10
systemctl list-timers 'twitch-badges*' --no-pager
journalctl -u twitch-badges-bot -u twitch-badges-refresh -u twitch-badges-poll --since today --no-pager -o cat | tail -50
```
