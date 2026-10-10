"""Каталог значков: курируемые списки, даты появления, поля каталога."""
import re
from datetime import datetime

from ..timeutil import parse_dt
from .text import strip_accents

IMG_UUID_RE = re.compile(r"/badges/v1/([0-9a-f-]+)/")


def image_cache_key(url: str):
    """UUID из URL картинки Twitch CDN — уникален на каждую версию/тир,
    в отличие от set_id (у sub-gifter, например, 28 версий на один set_id)."""
    if not url:
        return None
    m = IMG_UUID_RE.search(url)
    return m.group(1) if m else None


# Бейджи без event-данных, для которых условие получения — общеизвестный факт,
# а не то, что можно вычитать из StreamDatabase. Курировано вручную.
# (условие, cost) — cost используется для колонок "Бесплатно"/"Платно".
PERMANENT = {
    "premium": ("Привязать аккаунт Amazon Prime Gaming к Twitch", "paid"),
    "turbo": ("Оформить подписку Twitch Turbo", "paid"),
    "broadcaster": ("Владелец канала — присваивается автоматически создателю трансляции", "free"),
    "moderator": ("Быть назначенным модератором канала", "free"),
    "vip": ("Получить статус VIP от стримера", "free"),
    "subscriber": ("Оформить подписку на канал", "paid"),
    "partner": ("Стать участником программы Twitch Partner", "free"),
    "sub-gifter": ("Подарить хотя бы одну подписку", "paid"),
    "anonymous-cheerer": ("Отправить Bits анонимно", "paid"),
    "hype-train": ("Принять участие в Hype Train на канале", "paid"),
    "bits": ("Потратить Bits в чате канала", "paid"),
    "predictions": ("Участвовать в Predictions на канале", "free"),
    "moments": ("Создать Moment (клип-нарезку) на канале", "free"),
    "social-sharing": ("Поделиться трансляцией в соцсетях", "free"),
    "blossom-badge": ("Посещать один канал минимум 3 разных дня в неделю (Weekly Rewards)", "free"),
    "bloom-badge": ("Повышенная активность в программе Weekly Rewards", "free"),
}


STAFF = {
    "staff": "Служебная роль сотрудника Twitch — пользователям не выдаётся",
    "admin": "Служебная роль администратора Twitch — пользователям не выдаётся",
    "global_mod": "Служебная роль глобального модератора Twitch — пользователям не выдаётся",
}


INVITE = {
    "ambassador": "Программа Twitch Ambassador — только по приглашению Twitch",
}


RETIRED = {
    "founder": "Выдавался при оформлении Founder-подписки; программа закрыта в 2019 году",
    "clip-the-halls": "Праздничное событие Clip the Halls (2019) — сейчас не проводится",
    "glhf-pledge": "Разовая акция GLHF Pledge — сейчас не проводится",
}


TECHNICAL = {
    "no_audio": "Технический индикатор отключённого звука трансляции, не бейдж за достижение",
    "no_video": "Технический индикатор отключённого видео трансляции, не бейдж за достижение",
    "extension": "Технический индикатор сообщения от имени расширения Twitch",
}


PERIODIC = {
    "bits-charity": "Использовать Bits во время благотворительной кампании Twitch — проводится периодически, не всегда доступно",
}


NOTE_KIND_LABEL = {
    "ended": "ивент завершён",
    "staff": "служебная роль",
    "invite": "по приглашению",
    "retired-program": "программа закрыта",
    "technical": "технический",
    "periodic": "периодически",
    "removed": "удалён из Twitch",
    "cancelled": "кампания отменена",
    "evolution": "эволюция значка",
    "unknown": "неизвестно",
}


# Бейджи с вручную заданной семантикой (вечные роли, staff, инвайты и т.п.) —
# для них окна из дат не строим, у них своя классификация.
MANUAL_SET_IDS = set(PERMANENT) | set(STAFF) | set(INVITE) | set(RETIRED) | set(TECHNICAL) | set(PERIODIC)


# Эвристики (поиск имени в тексте событий) — только для свежих значков: у
# архивных нет окна штатно, и их однословные имена (Alliance, Horde, Diablo…)
# цеплялись бы к прозе новых событий. Та же глубина, что у сканирования страниц.
HEURISTIC_MAX_AGE_DAYS = 32


def group_key(event_title):
    return re.sub(r"\s*\([^)]*\)\s*$", "", event_title).strip()


def badge_first_seen(badge):
    """Когда значок появился: history (до ~27.09.2026) или added_at (теперь)."""
    added = [h for h in badge.get("history") or [] if h.get("type") == "added"]
    if added:
        return min(added, key=lambda h: h["timestamp"])["timestamp"]
    return badge.get("added_at") or None


def badge_added_dt(badge):
    seen = badge_first_seen(badge)
    try:
        return datetime.fromisoformat(seen.replace("Z", "+00:00")) if seen else None
    except (ValueError, AttributeError):
        return None


def event_slug(title):
    """Заголовок события → слаг, каким SD обычно называет его set_id:
    «SUBtember 2026» → «subtember-2026» (ср. реальные subtember-2024/2025)."""
    s = re.sub(r"[^a-z0-9]+", "-", strip_accents(title or "").lower())
    return s.strip("-")


def holders_count(uc):
    """Число владельцев значка. SD отдавал {"current": N}, с 27.09.2026 — просто N.
    Читаем оба вида: счётчик косметический, и из-за него нельзя ронять весь
    сбор (так и вышло — данные простояли полтора дня)."""
    if isinstance(uc, dict):
        uc = uc.get("current")
    return uc if isinstance(uc, int) and not isinstance(uc, bool) else None


def extract_num(title):
    m = re.search(r"([\d,]+)", title or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


CATALOG_COSTS = {"free", "paid"}


def catalog_window_fields(badge):
    """Окно и цена прямо со значка каталога (формат SD с ~27.09.2026):
    start_at_date/time, end_at_date/time, cost. {} — дат нет."""
    if not badge:
        return {}
    start = parse_dt(badge.get("start_at_date"), badge.get("start_at_time"))
    end = parse_dt(badge.get("end_at_date"), badge.get("end_at_time"))
    if not start and not end:
        return {}
    cost = badge.get("cost")
    return {"start": start, "end": end,
            "cost": cost if cost in CATALOG_COSTS else None,
            "dates_coarse": not (badge.get("start_at_time") or badge.get("end_at_time")),
            "from_catalog": True}


def catalog_by_set_id(badges):
    out = {}
    for b in badges or []:
        sid = (b.get("current") or {}).get("set_id")
        if sid and sid not in out:
            out[sid] = b
    return out
