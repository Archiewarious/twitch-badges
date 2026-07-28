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
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "current" in obj[0]:
        return obj
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


def parse_badge_page_text(text, added_iso=None):
    """Описание бейджа → структура (даты + признаки). Возвращает None, если дат нет."""
    if not text:
        return None
    start, end, start_time_known, end_time_known = _parse_page_dates(text)
    if not start:
        return None
    start = _fix_stale_year(start, added_iso)
    end = _fix_stale_year(end, added_iso)
    if end and end < start:                  # защита от кривого парса
        end = None
    low = text.lower()
    if "watched" in low or "watch " in low:
        kind = "watch"
    elif "gifted a subscription" in low or "subscribed" in low:
        kind = "sub"
    elif "bought" in low or "ticket" in low:
        kind = "purchase"
    elif "cheer" in low or "bits" in low:
        kind = "bits"
    else:
        kind = None
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


def fetch_badge_page_text(build_id: str, set_id: str):
    """Текст описания со страницы бейджа StreamDatabase (contexts[].pending_content
    или .content). Одна загрузка — из неё достаём и ссылку, и даты."""
    try:
        data = fetch_next_data(build_id, f"twitch/global-badges/{set_id}/1")
    except requests.RequestException:
        return None
    b = data.get("pageProps", {}).get("twitchGlobalBadge", {})
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


# Насколько свежие бейджи вне events.json ещё сканируем (ограничивает число запросов)
PAGE_SCAN_DAYS = 21


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
            if not any(av.get("categories") for av in avs) or not has_cond:
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

    links, infos = {}, {}
    for sid, added_iso in need.items():
        text = fetch_badge_page_text(build_id, sid)
        if not text:
            continue
        link = extract_link_from_text(text)
        if link:
            links[sid] = link
        info = parse_badge_page_text(text, added_iso)
        if info:
            infos[sid] = info
    return links, infos


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
    twitch_links, page_info = collect_badge_pages(build_id, events, badge_list)

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
    }

    # Пишем в staging. Картинки качает generate_site.sync_images (только актуальные),
    # а commit-rename incoming→latest делает refresh.sh последним шагом.
    atomic_write_json(INCOMING_FILE, snapshot)

    print(f"OK: {len(badge_list)} бейджей, {len(events)} ивентов, "
          f"{len(twitch_links)} ссылок, {len(page_info)} описаний с датами "
          f"→ {INCOMING_FILE.name} ({snapshot['fetched_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
