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
from datetime import datetime, timezone
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


def fetch_twitch_link(build_id: str, set_id: str):
    """Авторитетная ссылка Twitch со страницы бейджа StreamDatabase (в contexts[]
    .pending_content/.content как markdown-ссылка). Автономно, без ручной курации.
    Возвращает {'label','url'} или None."""
    try:
        data = fetch_next_data(build_id, f"twitch/global-badges/{set_id}/1")
    except requests.RequestException:
        return None
    b = data.get("pageProps", {}).get("twitchGlobalBadge", {})
    for ctx in b.get("contexts") or []:
        for field in ("pending_content", "content"):
            m = DIR_LINK_RE.search(ctx.get(field) or "")
            if m:
                return {"label": m.group(1).strip(), "url": m.group(2)}
    return None


def collect_twitch_links(build_id, events) -> dict:
    """Для каналовых бейджей (есть channels, нет categories) достаёт ссылку на
    событие/категорию со страницы бейджа. {set_id: {'label','url'}}."""
    links = {}
    for ev in events:
        for badge in ev.get("twitch_global_badges", []):
            set_id = badge.get("current", {}).get("set_id")
            avs = badge.get("availability") or []
            has_cat = any(av.get("categories") for av in avs)
            has_chan = any(av.get("channels") for av in avs)
            if set_id and has_chan and not has_cat and set_id not in links:
                link = fetch_twitch_link(build_id, set_id)
                if link:
                    links[set_id] = link
    return links


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
    events = events_raw.get("pageProps", {}).get("initialData", [])

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

    # Ссылки Twitch (событие/категория) для каналовых бейджей — автономно со страниц
    # (использует av.channels — ДО обрезки ниже).
    twitch_links = collect_twitch_links(build_id, events)

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
    }

    # Пишем в staging. Картинки качает generate_site.sync_images (только актуальные),
    # а commit-rename incoming→latest делает refresh.sh последним шагом.
    atomic_write_json(INCOMING_FILE, snapshot)

    print(f"OK: {len(badge_list)} бейджей, {len(events)} ивентов, "
          f"{len(twitch_links)} ссылок событий → {INCOMING_FILE.name} ({snapshot['fetched_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
