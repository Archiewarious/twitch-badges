#!/usr/bin/env python3
"""
Тянет данные о глобальных Twitch-бейджах со StreamDatabase (streamdatabase.com) —
единственного источника, где условия получения и даты уже структурированы
(Twitch Helix/GQL этого не отдают, только картинки и названия).

Сайт на Next.js: сначала вытаскиваем текущий buildId со страницы, потом
дёргаем его /_next/data/<buildId>/*.json — те же данные, что видит браузер,
без рендеринга HTML.
"""
import json
import os
import re
import socket
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3.util.connection as urllib3_cn
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# На этом хосте IPv6-egress к Cloudflare (за ним StreamDatabase и Twitch CDN)
# не работает — SYN к IPv6 виснет в SYN-SENT, а таймаут requests на нём не
# срабатывает надёжно. Форсируем IPv4-резолвинг для всех исходящих запросов.
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# Сессия с ретраями/backoff: StreamDatabase за Cloudflare регулярно отдаёт
# транзиентные 502/503/timeout — глушим их, не теряя 30-мин цикл.
SESSION = requests.Session()
_retry = Retry(total=3, connect=3, read=3, backoff_factor=2,
               status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://", HTTPAdapter(max_retries=_retry))

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
INCOMING_FILE = DATA_DIR / "streamdb_incoming.json"  # staging; refresh.sh делает commit-rename в latest
LATEST_FILE = DATA_DIR / "streamdb_latest.json"

BASE = "https://www.streamdatabase.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; twitch-badges-tracker/1.0)"}

IMG_UUID_RE = re.compile(r"/badges/v1/([0-9a-f-]+)/")


def atomic_write_json(path: Path, data) -> None:
    """Пишет JSON атомарно (temp+fsync+os.replace), чтобы читатель (бот/генератор)
    никогда не увидел полузаписанный файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def image_cache_key(url: str):
    """UUID из URL картинки Twitch CDN — уникален на каждую версию/тир,
    в отличие от set_id (у sub-gifter, например, 28 версий на один set_id)."""
    if not url:
        return None
    m = IMG_UUID_RE.search(url)
    return m.group(1) if m else None


def get_build_id() -> str:
    resp = SESSION.get(BASE + "/", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    m = re.search(r'"buildId":"([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("Не нашёл buildId на streamdatabase.com — вёрстка сайта изменилась")
    return m.group(1)


def fetch_next_data(build_id: str, path: str) -> dict:
    url = f"{BASE}/_next/data/{build_id}/{path}.json"
    resp = SESSION.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def find_badge_list(obj):
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        if "current" in obj[0]:
            return obj
        # 27.08.2026 SD завернул каждый значок каталога в {"twitchGlobalBadge": {...}}
        # (pageProps.data вместо плоского списка). Формат событий при этом не менялся.
        # Пока распаковки не было, find_badge_list возвращал None → guard «пустой
        # список бейджей» ронял refresh каждые полчаса, а опрос молча пропускал тик:
        # данные не обновлялись ~35 минут.
        if "twitchGlobalBadge" in obj[0]:
            inner = [it["twitchGlobalBadge"] for it in obj
                     if isinstance(it, dict) and isinstance(it.get("twitchGlobalBadge"), dict)]
            if inner and "current" in inner[0]:
                return inner
    if isinstance(obj, dict):
        for v in obj.values():
            found = find_badge_list(v)
            if found is not None:
                return found
    return None


# Ссылка Twitch (событие/категория) в markdown-описании бейджа на StreamDatabase:
# [Esports World Cup 2026](https://www.twitch.tv/directory/event/ewc-2026)
DIR_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((https://www\.twitch\.tv/directory/(?:event|category)/[a-z0-9_-]+(?:\?[^)]*)?)\)")

# Fallback: ЛЮБАЯ markdown-ссылка (напр. на официальный сайт офлайн-мероприятия —
# TwitchCon-бейджи не привязаны ни к категории, ни к каналам, а условие — "купить
# билет на twitchcon.com"). Картинки (![...](...)) не матчатся — другой синтаксис.
# Домены-списки участников (не место, куда идти за бейджем) — игнорируем.
ANY_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https://[^)]+)\)")
IGNORED_LINK_DOMAINS = ("pastebin.com",)
# Канал стримера на Twitch (НЕ директория): https://www.twitch.tv/ibai
CHANNEL_LINK_RE = re.compile(r"https://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)/?$")


# ── Парсер описания со страницы бейджа ────────────────────────────────────────
# Многие бейджи (особенно свежие: La Velada, Budz, EWC Co-Streamer) НЕ попадают в
# events.json со структурными данными — там пусто. Но StreamDatabase пишет описание
# по жёсткому шаблону, который парсится детерминированно (LLM не нужен):
#   "This badge was awarded on July 15th 2026 (19:51 UTC) to people who watched 60 minutes..."
#   "This badge was awarded between July 23rd 2026 (X:X UTC) and July 25th 2026 (X:X UTC) to people who subscribed..."
MONTHS_EN = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}

# "July 25th 2026 (19:51 UTC)" / "July 25th 2026 (X:X UTC)" — время может быть неизвестно
PAGE_DATE_RE = re.compile(
    r"(" + "|".join(MONTHS_EN) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})"
    r"(?:\s*\(\s*(\d{1,2}):(\d{2})\s*UTC\s*\))?", re.I)

# SD сам помечает, что даты/часы ещё не подтверждены
UNCONFIRMED_RE = re.compile(r"AREN'?T\s+YET\s+CONFIRMED", re.I)
# SD помечает, что бейдж заведён уже ПОСЛЕ окончания окна (получить уже нельзя)
TOO_LATE_RE = re.compile(r"added\s+after\s+the\s+timeframe", re.I)


def _parse_page_dates(text):
    """Достаёт (start, end) из описания. 'between A and B' → (A, B); 'on A' → (A, None)."""
    matches = list(PAGE_DATE_RE.finditer(text))
    if not matches:
        return None, None, False, False

    def to_dt(m):
        month, day, year = MONTHS_EN[m.group(1).capitalize()], int(m.group(2)), int(m.group(3))
        has_time = bool(m.group(4))
        hh, mm = (int(m.group(4)), int(m.group(5))) if has_time else (0, 0)
        try:
            return datetime(year, month, day, hh, mm, tzinfo=timezone.utc), has_time
        except ValueError:
            return None, False

    # "between X and Y" — берём первые две даты; иначе одна дата = начало
    low = text.lower()
    first, first_t = to_dt(matches[0])
    if "between" in low[:low.find(matches[0].group(0).lower()) + 40] and len(matches) >= 2:
        second, second_t = to_dt(matches[1])
        return first, second, first_t, second_t
    return first, None, first_t, False


def _fix_stale_year(dt, added_iso):
    """SD иногда копипастит описание с прошлогоднего бейджа (у La Velada VI стоял
    2025 год, хотя бейдж заведён в 2026). Если дата раньше момента добавления бейджа
    больше чем на полгода — год явно устаревший, подтягиваем к году добавления."""
    if not dt or not added_iso:
        return dt
    try:
        added = datetime.fromisoformat(added_iso.replace("Z", "+00:00"))
    except ValueError:
        return dt
    if (added - dt).days > 180:
        try:
            fixed = dt.replace(year=added.year)
        except ValueError:
            return dt
        if (added - fixed).days > 180:      # всё ещё в прошлом → следующий год
            fixed = fixed.replace(year=added.year + 1)
        return fixed
    return dt


def _page_kind(low):
    """Тип условия по тексту описания. Порядок веток важен: у co-streamer-текстов
    встречается и «watched», и «subscription» — «смотреть» считаем первичным."""
    if "watched" in low or "watch " in low:
        return "watch"
    if "gifted a subscription" in low or "subscribed" in low:
        return "sub"
    if "bought" in low or "ticket" in low:
        return "purchase"
    if "cheer" in low or "bits" in low:
        return "bits"
    return None


def parse_badge_page_text(text, added_iso=None):
    """Описание бейджа → структура (даты + признаки). Возвращает None, если дат нет."""
    if not text:
        return None
    start, end, start_time_known, end_time_known = _parse_page_dates(text)
    # Дат может не быть вообще: у свежих кампаний SD выкладывает шаблон с
    # заглушками («August Xth 2026 (X:X UTC)»). Раньше мы возвращали None и теряли
    # ВСЁ, включая условие — хотя из текста видно «subscribed or gifted».
    # Возвращаем структуру без дат: окно из неё не построить, но условие и
    # стоимость подтянет enrich_windows, и значок можно анонсировать.
    if not start:
        low_ = text.lower()
        return {
            "start": None, "end": None,
            "start_time_known": False, "end_time_known": False,
            "kind": _page_kind(low_),
            "watch_minutes": None,
            "unconfirmed": bool(UNCONFIRMED_RE.search(text)),
            "too_late": bool(TOO_LATE_RE.search(text)),
        }
    start = _fix_stale_year(start, added_iso)
    end = _fix_stale_year(end, added_iso)
    if end and end < start:                  # защита от кривого парса
        end = None
    low = text.lower()
    kind = _page_kind(low)
    m = re.search(r"watched\s+(?:for\s+)?(\d+)\s+minutes", low)
    return {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
        # Время в описании указано не всегда ("July 25th 2026" без часов, или "(X:X UTC)").
        # Тогда 00:00 — заглушка, и показывать её как точное время нельзя.
        "start_time_known": bool(start_time_known),
        "end_time_known": bool(end_time_known),
        "kind": kind,
        "watch_minutes": int(m.group(1)) if m else None,
        "unconfirmed": bool(UNCONFIRMED_RE.search(text)),
        "too_late": bool(TOO_LATE_RE.search(text)),
    }


def fetch_badge_page(build_id: str, set_id: str):
    """Объект бейджа со страницы StreamDatabase (одна загрузка на бейдж)."""
    try:
        data = fetch_next_data(build_id, f"twitch/global-badges/{set_id}/1")
    except requests.RequestException:
        return None
    return (data.get("pageProps") or {}).get("twitchGlobalBadge") or {}


def page_availability(badge):
    """ОПУБЛИКОВАННЫЕ записи availability со страницы бейджа.

    С августа 2026 SD переносит сюда всё: «badge timeframe/availability and unlock
    information is now moving to its own availability tab». Мы же читали
    availability ТОЛЬКО внутри events.json, а со страницы брали лишь текст
    описания — и теряли структурные данные там, где у события дат нет.
    Так молчали DRON-E и Diablo: события «Wasteland Circuit»/«BlizzCon 2026»
    заведены без дат, а на страницах значков лежали и окно, и условие в steps.

    hidden — черновик модератора, его не берём (см. collect_windows_by_set_id)."""
    return [av for av in (badge.get("availability") or []) if not av.get("hidden")]


def badge_page_text(badge):
    """Текст описания (contexts[].pending_content или .content)."""
    b = badge or {}
    for ctx in b.get("contexts") or []:
        for field in ("pending_content", "content"):
            if ctx.get(field):
                return ctx[field]
    return None


def extract_link_from_text(text):
    """Ссылка «куда идти за бейджем» из markdown-описания.
    Приоритет: директория Twitch → канал стримера на Twitch → внешний сайт.
    (В описании часто несколько ссылок: у La Velada и сайт события, и канал ibai —
    смотреть-то надо на Twitch, поэтому канал важнее сайта.)"""
    if not text:
        return None
    m = DIR_LINK_RE.search(text)
    if m:
        return {"label": m.group(1).strip(), "url": m.group(2)}
    fallback = None
    for m in ANY_LINK_RE.finditer(text):
        url = m.group(2)
        if any(d in url for d in IGNORED_LINK_DOMAINS):
            continue
        ch = CHANNEL_LINK_RE.match(url)
        if ch:
            return {"label": ch.group(1), "url": url}
        if fallback is None:
            fallback = {"label": m.group(1).strip(), "url": url}
    return fallback


# Насколько свежие бейджи вне events.json ещё сканируем (ограничивает число запросов).
# ДОЛЖНО быть не меньше BLINDSPOT_DAYS в боте (30): иначе получается абсурд —
# про значок ещё 30 дней приходит тревога «молчит, не знаю, как классифицировать»,
# а искать его данные мы перестали на 21-м дне. Так вышло с тройкой Audible
# (EnchantedBigBoiBoxers, PrincessDonutBrown/Pink, заведены 06.08): SD удалил
# завершившееся событие, а на СТРАНИЦЕ значка всё лежало — окно 13–27.08 и условие
# «2 подписки или 2 гифта». Мы просто перестали туда ходить, и тревога ныла
# впустую про давно закрытую кампанию. Разница в цене мала: 18 страниц вместо 14.
PAGE_SCAN_DAYS = 32


def _badge_added_at(badge):
    stamps = [h.get("timestamp") for h in badge.get("history", []) if h.get("type") == "added"]
    return max(stamps) if stamps else None


def collect_badge_pages(build_id, events, badges):
    """Один проход по страницам бейджей → (ссылки, разобранные описания).
    Качаем страницу только тем, кому это нужно:
      1) бейджи без категории в events — ради ссылки на событие/сайт;
      2) НЕДАВНО ДОБАВЛЕННЫЕ бейджи, которых вообще нет в events со структурными
         данными (La Velada, Budz и т.п.) — у SD там пусто, и вся информация о
         датах/условии живёт только в тексте описания. Без этого бот их не видит."""
    in_events_with_avail = set()
    need = {}                       # set_id -> added_iso (или None)
    for ev in events:
        for badge in ev.get("twitch_global_badges", []):
            sid = badge.get("current", {}).get("set_id")
            if not sid:
                continue
            avs = badge.get("availability") or []
            if avs:
                in_events_with_avail.add(sid)
            # Страница нужна, если из структурных полей не выходит ни ссылки, ни
            # условия. categories дают только ИМЯ игры (href у SD там нет), а
            # собирать URL категории из имени нельзя — Twitch отвечает 200 на любой
            # слаг, проверить догадку невозможно, и в пост уйдёт битая ссылка.
            # Флаги условия (subscription/watch/...) SD тоже заполняет не всегда:
            # у Egg все false, хотя в тексте страницы «subscribed or gifted».
            has_cond = any(av.get(f) for av in avs
                           for f in ("subscription", "subscription_gift", "bits",
                                     "watch", "clip", "twitchcon", "turbo"))
            # ...и ДАТЫ. Без этой проверки страница не запрашивалась, если у
            # события уже есть категория и условие, — а даты при этом могли
            # отсутствовать: у DRON-E и Diablo (30.08.2026) в событии лежала
            # запись с categories/steps/costs, но без start_at_date, тогда как на
            # странице значка окно было. Значок оставался «нет дат — не знаю, как
            # классифицировать», и тревога о молчащих ныла впустую.
            has_dates = any(av.get("start_at_date") or av.get("end_at_date") for av in avs)
            if not any(av.get("categories") for av in avs) or not has_cond or not has_dates:
                need.setdefault(sid, None)

    cutoff = datetime.now(timezone.utc) - timedelta(days=PAGE_SCAN_DAYS)
    for badge in badges:
        sid = badge.get("current", {}).get("set_id")
        if not sid or sid in in_events_with_avail or not badge.get("added"):
            continue
        ts = _badge_added_at(badge)
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                need[sid] = ts
        except ValueError:
            continue

    links, infos, avails = {}, {}, {}
    for sid, added_iso in need.items():
        badge = fetch_badge_page(build_id, sid)
        if badge is None:
            continue
        # Структурные данные со страницы — приоритетнее разбора текста: именно
        # туда SD переносит окна и условия (page_availability).
        avs = page_availability(badge)
        if avs:
            avails[sid] = avs
        text = badge_page_text(badge)
        if not text:
            continue
        link = extract_link_from_text(text)
        if link:
            links[sid] = link
        info = parse_badge_page_text(text, added_iso)
        if info:
            infos[sid] = info
    return links, infos, avails


CATEGORY_URLS_FILE = DATA_DIR / "category_urls.json"
TWITCH_DIRECTORY = "https://www.twitch.tv/directory/category/"
OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]+)"')


# Twitch отдаёт разметку с og:title только браузерным клиентам; нашему обычному
# User-Agent прилетает урезанная страница, и проверка ложно проваливалась.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def category_slug(name: str) -> str:
    """Имя категории Twitch → слаг директории: «Pokémon GO» → «pokemon-go».

    Знаки ™ ® © и апострофы Twitch ВЫБРАСЫВАЕТ, а не заменяет дефисом: иначе
    «LEGO® Batman™» дало бы «lego-batmantm», а «Tom Clancy's» — «tom-clancy-s»."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[\u2122\u00ae\u00a9'\u2019]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def verify_category_url(name: str):
    """Проверенный URL категории или None.

    Проверка обязательна: Twitch отвечает 200 на ЛЮБОЙ слаг, поэтому по коду
    ответа догадку не отличить от правды, а битая ссылка в inline-кнопке роняет
    весь ответ (Button_url_invalid). Настоящая страница категории отдаёт
    og:title с её именем, выдуманная — не отдаёт вовсе; на это и смотрим.

    Раньше ссылку брали только из поля href у SD, но 27.08.2026 он это поле
    убрал, и кнопка «Смотреть» пропала у восьми значков сразу."""
    slug = category_slug(name)
    if not slug:
        return None
    url = TWITCH_DIRECTORY + slug
    try:
        resp = SESSION.get(url, headers=BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    # Twitch не объявляет charset в заголовке, и requests угадывает latin-1:
    # «Pokémon GO» превращается в «PokÃ©mon GO», слаг не совпадает, ссылка
    # молча признаётся неподтверждённой.
    resp.encoding = "utf-8"
    m = OG_TITLE_RE.search(resp.text)
    if not m:
        return None
    # og:title выглядит как «Pokémon GO - Twitch» — сверяем начало с именем.
    title = m.group(1).rsplit(" - Twitch", 1)[0].strip()
    return url if category_slug(title) == slug else None


def resolve_category_urls(names):
    """{имя категории: проверенный URL}. Результат кэшируется на диске, включая
    отрицательный (url=null): без этого каждый refresh перепроверял бы одни и те
    же категории, а неудачные — бесконечно."""
    try:
        cache = json.loads(CATEGORY_URLS_FILE.read_text())
    except (OSError, ValueError):
        cache = {}
    import time

    changed = False
    for name in names:
        if not name or name in cache:
            continue
        # Пауза и повтор: подряд идущие запросы Twitch троттлит, отдавая 200 без
        # og:title. Без этого «Pokémon UNITE» и «Diablo» ложно признавались
        # неподтверждёнными, хотя поодиночке проверяются нормально. Проверка
        # редкая (результат кэшируется), так что медлительность не мешает.
        url = verify_category_url(name)
        if url is None:
            time.sleep(2)
            url = verify_category_url(name)
        cache[name] = url
        changed = True
        time.sleep(1)
    if changed:
        atomic_write_json(CATEGORY_URLS_FILE, cache)
    return {k: v for k, v in cache.items() if v}


def download_image(url: str) -> bool:
    """Качает одну картинку по URL в data/images/<uuid>.png. True если скачал.
    Вызывается из generate_site.sync_images() — только для актуальных бейджей,
    не для всего каталога (иначе тянем 444 картинки вместо ~37)."""
    key = image_cache_key(url)
    if not key:
        return False
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        resp = SESSION.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        (IMAGES_DIR / f"{key}.png").write_bytes(resp.content)
        return True
    except requests.RequestException as e:
        print(f"  WARN: не скачал {key}: {e}", file=sys.stderr)
        return False


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)

    build_id = get_build_id()
    catalog_raw = fetch_next_data(build_id, "twitch/global-badges")
    events_raw = fetch_next_data(build_id, "events")

    badge_list = find_badge_list(catalog_raw["pageProps"]) or []
    # StreamDatabase переименовал поле событий initialData → initialEvents (июль 2026).
    # Читаем оба ключа (новый приоритетнее) — устойчиво к переименованию туда-обратно.
    _pp = events_raw.get("pageProps", {})
    events = _pp.get("initialEvents") or _pp.get("initialData") or []

    # GUARD против тихого обнуления: если StreamDatabase сменит вёрстку, find_badge_list
    # вернёт [] → снапшот с badges:[] → refresh.sh rsync --delete вычистит /var/www
    #. Лучше упасть с ненулевым кодом: под set -e refresh абортит
    # ДО commit-marker → старые latest.json и картинки сохраняются, плюс сработает алерт.
    if "pageProps" not in catalog_raw:
        raise RuntimeError("нет pageProps в ответе каталога — StreamDatabase сменил формат")
    if not badge_list:
        raise RuntimeError("пустой список бейджей — вёрстка StreamDatabase изменилась")
    if not events:
        raise RuntimeError("пустой список событий — структура StreamDatabase изменилась")
    if LATEST_FILE.exists():
        try:
            prev_count = len(json.loads(LATEST_FILE.read_text()).get("badges", []))
        except (json.JSONDecodeError, OSError):
            prev_count = 0
        if prev_count and len(badge_list) < 0.5 * prev_count:
            raise RuntimeError(
                f"обвал каталога: {len(badge_list)} бейджей против {prev_count} прежних — "
                "не пишу incoming (частичный/битый ответ SD)")

    # Один проход по страницам бейджей: ссылки Twitch + разбор описаний (даты/условие
    # для бейджей, которых нет в events.json — La Velada и т.п.).
    # Использует av.channels — ДО обрезки ниже.
    twitch_links, page_info, page_avail = collect_badge_pages(build_id, events, badge_list)

    # Второй источник: Twitch Helix. Пустой словарь, если ключей нет — тогда всё
    # работает ровно как раньше, на одном StreamDatabase.
    import fetch_badges
    helix = fetch_badges.try_collect_info()

    # Ссылки на категории Twitch: SD убрал поле href 27.08.2026, и кнопка
    # «Смотреть» пропала у восьми значков. Строим URL из имени категории и
    # ПРОВЕРЯЕМ его (Twitch отвечает 200 на любой слаг — см. verify_category_url).
    cat_names = set()
    for ev in events:
        for b in ev.get("twitch_global_badges") or []:
            for av in b.get("availability") or []:
                for c in av.get("categories") or []:
                    cat_names.add(c.get("name") or (c.get("game") or {}).get("name"))
    for lst in page_avail.values():
        for av in lst:
            for c in av.get("categories") or []:
                cat_names.add(c.get("name") or (c.get("game") or {}).get("name"))
    # ...и категории, названные в описаниях Twitch («in the ELDEN RING category»).
    # Для многих значков это единственное указание места: у SD категорий нет.
    for sid, info in (helix or {}).items():
        cat = fetch_badges.category_from_description(info.get("description"))
        if cat:
            cat_names.add(cat)
    # ...и категории из текста событий SD (кампании, у которых значка ещё нет).
    for ev in events:
        cat = fetch_badges.category_from_description(ev.get("content"))
        if cat:
            cat_names.add(cat)
    category_urls = resolve_category_urls(sorted(n for n in cat_names if n))

    # Экономия хранения: списки участников (EWC ~1387 стримеров и т.п.) — это ~90%
    # снапшота, а боту/сайту нужен только ФАКТ наличия каналов (детали — логины/аватары —
    # нигде не используются с тех пор, как ведём на страницу события Twitch). Оставляем
    # только счётчик — снапшот ужимается в ~10 раз.
    for ev in events:
        for badge in ev.get("twitch_global_badges", []):
            for av in badge.get("availability", []):
                chans = av.pop("channels", None)
                av["channel_count"] = len(chans) if isinstance(chans, list) else 0

    snapshot = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_id": build_id,
        "badges": badge_list,
        "events": events,
        "twitch_links": twitch_links,
        "page_info": page_info,
        "page_availability": page_avail,
        "category_urls": category_urls,
        "helix": helix,
    }

    # Пишем в staging. Картинки качает generate_site.sync_images (только актуальные),
    # а commit-rename incoming→latest делает refresh.sh последним шагом.
    atomic_write_json(INCOMING_FILE, snapshot)

    helix_note = f", {len(helix)} описаний Twitch" if helix else ", Helix выключен (нет ключей)"
    avail_note = (f", {len(page_avail)} availability со страниц"
                  f", {len(category_urls)} ссылок на категории")
    print(f"OK: {len(badge_list)} бейджей, {len(events)} ивентов, "
          f"{len(twitch_links)} ссылок, {len(page_info)} описаний с датами"
          f"{avail_note}{helix_note} → {INCOMING_FILE.name} ({snapshot['fetched_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
