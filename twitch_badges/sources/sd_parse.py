"""Разбор ответов StreamDatabase без сети: каталог, страницы значков, описания.

Перенос из fetch_streamdb.py без изменения поведения (+ pages_to_scan — общая
для опроса и полного сбора функция «чьи страницы смотреть»)."""
import re
from datetime import datetime, timedelta, timezone

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
    больше чем на полгода — год явно устаревший, подтягиваем к году добавления.

    Не вызывать для значков, добавленных уже после своего окна (см.
    parse_badge_page_text): у них старая дата настоящая."""
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


def parse_badge_page_text(text, added_iso=None, catalog_end=None):
    """Описание бейджа → структура (даты + признаки). Возвращает None, если дат нет.

    added_iso и catalog_end — из page_parse_args: разбор обязан давать одно и то же
    в сборе и в опросе (иначе опрос вечно видит «изменение»)."""
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
    # Значок заведён уже после своего окна: SD так и пишет («added after the
    # timeframe»), либо это видно по датам каталога. Тогда старые даты в тексте —
    # правда, а не копипаста, и «чинить» год нельзя: вышло бы ложное «идёт сейчас».
    late = bool(TOO_LATE_RE.search(text)) or _window_before_added(catalog_end, added_iso)
    if not late:
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
    """Когда значок появился в каталоге (ISO-строка или None).

    До ~27.09.2026 SD отдавал history[{type: "added", timestamp}], теперь —
    поле added_at (history нет ни у одного значка). Без фолбэка молча
    отключились сканирование страниц, монитор слепых зон и фолбэк «без дат»."""
    stamps = [h.get("timestamp") for h in badge.get("history") or [] if h.get("type") == "added"]
    if stamps:
        return max(stamps)
    return badge.get("added_at") or None


def _window_before_added(end_iso_date, added_iso):
    """Окно из каталога закончилось раньше, чем значок завели."""
    if not end_iso_date or not added_iso:
        return False
    return str(end_iso_date)[:10] < str(added_iso)[:10]


def page_parse_args(badge):
    """Аргументы parse_badge_page_text для значка каталога: (added_iso, catalog_end).
    Одна функция для сбора и опроса — разбор должен совпадать побайтно."""
    if not badge:
        return None, None
    return _badge_added_at(badge), badge.get("end_at_date") or None


def pages_to_scan(events, badges, now) -> dict:
    """{set_id: (added_iso, catalog_end)} — страницы значков, которые нужны сбору.
    Та же логика, что у collect_badge_pages: значки событий без категории,
    условия или дат, и свежие (PAGE_SCAN_DAYS) значки вне событий со
    структурными данными. Опрос проверяет только подмножество этого списка —
    иначе вечный цикл «страница изменилась → сбор → ничего не изменилось»."""
    catalog = {}
    for badge in badges:
        sid = (badge.get("current") or {}).get("set_id")
        if sid and sid not in catalog:
            catalog[sid] = badge
    in_events_with_avail = set()
    need = {}
    for ev in events:
        for badge in ev.get("twitch_global_badges", []):
            sid = badge.get("current", {}).get("set_id")
            if not sid:
                continue
            avs = badge.get("availability") or []
            if avs:
                in_events_with_avail.add(sid)
            has_cond = any(av.get(f) for av in avs
                           for f in ("subscription", "subscription_gift", "bits",
                                     "watch", "clip", "twitchcon", "turbo"))
            has_dates = any(av.get("start_at_date") or av.get("end_at_date") for av in avs)
            if not any(av.get("categories") for av in avs) or not has_cond or not has_dates:
                need.setdefault(sid, page_parse_args(catalog.get(sid)))
    cutoff = now - timedelta(days=PAGE_SCAN_DAYS)
    for badge in badges:
        sid = badge.get("current", {}).get("set_id")
        if not sid or sid in in_events_with_avail or not badge.get("added"):
            continue
        ts = _badge_added_at(badge)
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                need[sid] = page_parse_args(badge)
        except ValueError:
            continue
    return need


def trim_channels(events):
    """Списки участников (у EWC ~1400 стримеров) — 90% снапшота, а нужен только
    факт наличия каналов: оставляем счётчик."""
    for ev in events:
        for badge in ev.get("twitch_global_badges", []):
            for av in badge.get("availability", []):
                chans = av.pop("channels", None)
                if "channel_count" not in av:
                    av["channel_count"] = len(chans) if isinstance(chans, list) else 0
    return events

