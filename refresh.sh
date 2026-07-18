#!/bin/bash
# Полный цикл обновления. Порядок важен: сначала готовим данные+картинки+карточки
# в staging, деплоим их на публичный фронт, и ТОЛЬКО ПОСЛЕ этого атомарно
# коммитим snapshot (incoming→latest) — так бот увидит новые данные лишь когда
# картинки/карточки уже доступны по публичному URL (commit-marker).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# 1. Тянем данные в staging (data/streamdb_incoming.json), картинки НЕ качаем тут
./venv/bin/python fetch_streamdb.py

# 2. Классификация + докачка только актуальных картинок + сайт (читают incoming)
./venv/bin/python generate_site.py

# 3. Пре-рендер карточек актуальных бейджей (читает incoming)
./venv/bin/python render_cards.py

# 4. Деплой сайта
sudo -n cp site/index.html /var/www/html/index.html
sudo -n chown root:root /var/www/html/index.html
sudo -n chmod 644 /var/www/html/index.html

# 5. Деплой картинок и карточек (--delete убирает устаревшие)
sudo -n mkdir -p /var/www/html/badges /var/www/html/cards
sudo -n rsync -a --delete data/images/ /var/www/html/badges/
sudo -n rsync -a --delete data/cards/  /var/www/html/cards/
sudo -n chown -R root:root /var/www/html/badges /var/www/html/cards
sudo -n chmod -R a+r /var/www/html/badges /var/www/html/cards

# 6. Commit-marker: атомарно публикуем свежий snapshot для бота.
# cp (не mv) для бэкапа — чтобы не было окна, когда latest.json отсутствует
# (иначе бот/generate между двумя mv увидят пропажу файла). mv incoming→latest —
# это rename(2), атомарен: читатель видит либо старый, либо новый файл, не пустоту.
cp -f data/streamdb_latest.json data/streamdb_latest.prev.json 2>/dev/null || true
mv -f data/streamdb_incoming.json data/streamdb_latest.json

echo "refresh done: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Успешный прогон снимает возможную OnFailure-тревогу о падении refresh (шлёт RECOVERED).
/home/archie/projects/twitch-badges/monitor/alert.sh --clear failed-twitch-badges-refresh.service "refresh снова успешен" 2>/dev/null || true
