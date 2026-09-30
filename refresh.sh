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

# 4-5. Деплой на публичный веб — он остаётся на латвийском сервере (SITE_URL
# указывает туда). rsync под sudo на той стороне, владелец root, --delete убирает устаревшее.
LV_HOST="${DEPLOY_HOST:-user@old-host}"
LV_SSH="ssh -p ${DEPLOY_PORT:-22} -i $HOME/.ssh/rezka_lv -o BatchMode=yes"
RS=(rsync -rlt --chown=root:root --chmod=D755,F644 --rsync-path="sudo -n rsync" -e "$LV_SSH")
"${RS[@]}" site/index.html "$LV_HOST:/var/www/html/index.html"
"${RS[@]}" --delete data/images/ "$LV_HOST:/var/www/html/badges/"
"${RS[@]}" --delete data/cards/  "$LV_HOST:/var/www/html/cards/"
# 6. Commit-marker: атомарно публикуем свежий snapshot для бота.
# cp (не mv) для бэкапа — чтобы не было окна, когда latest.json отсутствует
# (иначе бот/generate между двумя mv увидят пропажу файла). mv incoming→latest —
# это rename(2), атомарен: читатель видит либо старый, либо новый файл, не пустоту.
cp -f data/streamdb_latest.json data/streamdb_latest.prev.json 2>/dev/null || true
mv -f data/streamdb_incoming.json data/streamdb_latest.json

echo "refresh done: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

