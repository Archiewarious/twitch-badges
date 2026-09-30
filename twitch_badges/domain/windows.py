"""Окна раздачи: из каких источников и в каком приоритете.

Приоритет (более точный НЕ перезаписывается):
  events[].availability  >  страница.availability  >  поля каталога  >
  page_info  >  events[].даты  >  текст событий  >  badges[].availability,
затем enrich_windows дополняет победившее окно, Helix — пустые поля, память —
потерянные даты, overrides — всё.
"""
import logging
import re

from ..timeutil import parse_dt
from .catalog import MANUAL_SET_IDS, HEURISTIC_MAX_AGE_DAYS, badge_added_dt, catalog_window_fields, group_key
from .categories import _category_box_art, _category_name, category_href, category_names, category_url_for
from .conditions import (PAGE_KIND_COST, PAGE_KIND_RU, _condition_from_content, av_objectives,
                         condition_from_helix, cost_from_steps, describe_condition_ru)
from .descriptions import category_from_description, channel_from_description
from .text import _content_ngrams, _norm_alnum, ru_duration_minutes

log = logging.getLogger(__name__)

def collect_windows_by_set_id(cx, events, twitch_links=None):
    twitch_links = twitch_links or {}
    # Сначала собираем все availability._id по кампании (group), чтобы каждое окно
    # знало ВСЕ id своей кампании — нужно боту для дедупа автопостов (тиры не поштучно).
    ids_by_group = {}
    for ev in events:
        grp = group_key(ev.get("title", ""))
        for badge in ev.get("twitch_global_badges", []):
            for av in badge.get("availability", []):
                if av.get("_id"):
                    ids_by_group.setdefault(grp, []).append(av["_id"])

    windows = {}
    for ev in events:
        ev_title = ev.get("title", "")
        grp = group_key(ev_title)
        for badge in ev.get("twitch_global_badges", []):
            set_id = badge.get("current", {}).get("set_id", "")
            for av in badge.get("availability", []):
                # hidden — черновик модератора SD, ещё не опубликованный. С августа
                # 2026 SD заводит такую запись сразу при появлении значка («badge
                # timeframe and unlock information is now moving to its own
                # availability tab»), и все поля в ней пустые. Источник этот —
                # высшего приоритета, поэтому пустышка перекрывала фолбэки, где
                # даты уже были: nasa-roman, оба Pikachu, dron-e, diablo разом
                # оказались «нет дат, не знаю, как классифицировать».
                # add_catalog_windows скрытые пропускал всегда — здесь не хватало.
                if av.get("hidden"):
                    continue
                start = parse_dt(av.get("start_at_date"), av.get("start_at_time"))
                end = parse_dt(av.get("end_at_date"), av.get("end_at_time"))
                cats = av.get("categories") or []
                # Списки участников больше не храним (см. fetch_streamdb) — только счётчик;
                # fallback на старый формат со списком, если снапшот ещё не обрезан.
                channel_count = av.get("channel_count", len(av.get("channels") or []))
                game = _category_name(cats)
                windows.setdefault(set_id, []).append({
                    "event_title": ev_title,
                    "group": grp,
                    "game": game,
                    "start": start,
                    "end": end,
                    "cost": cost_from_steps(av_objectives(av), ", ".join(av.get("costs") or [])),
                    "condition": describe_condition_ru(av),
                    # Билет на офлайн-мероприятие (TwitchCon и т.п.) — не "Twitch Drop"
                    # в смысле бота/канала: нельзя получить действием на Twitch, нужно
                    # физически купить билет и поехать. Бот/канал это скрывают (см.
                    # bot.py is_shown), сайт-каталог продолжает показывать — там уместно.
                    "offline_event": bool(av.get("twitchcon")),
                    # Диплинк-поля для кнопок бота (inline + автопост в канал):
                    "id": av.get("_id"),
                    "all_ids": ids_by_group.get(grp, []),
                    "category_href": category_href(cx, cats),
                    "box_art_url": _category_box_art(cats),
                    "categories": category_names(cats),
                    # Автономная ссылка Twitch (событие/категория) со страницы бейджа
                    # StreamDatabase — для каналовых бейджей без categories (EWC и т.п.).
                    "twitch_link": twitch_links.get(set_id),
                    "channel_count": channel_count,
                })
    return windows


def _page_too_late(page_info, set_id):
    return bool((page_info or {}).get(set_id, {}).get("too_late"))


def add_event_windows(windows, events, page_info=None, twitch_links=None):
    """ФОЛБЭК A: даты САМОГО СОБЫТИЯ (events[].start_at_date/end_at_date).
    У части бейджей availability пуст, но у события даты есть — напр. EWC 2026
    Co-Streamer идёт 29.06→23.08, а бот его не видел, потому что окна строились
    только из availability. Не перезаписываем более точные источники."""
    for ev in events or []:
        if ev.get("hidden"):
            continue
        start = parse_dt(ev.get("start_at_date"), ev.get("start_at_time"))
        end = parse_dt(ev.get("end_at_date"), ev.get("end_at_time"))
        if not start and not end:
            continue
        grp = group_key(ev.get("title", ""))
        badges_in_ev = ev.get("twitch_global_badges", [])
        # Офлайн-мероприятие: если хоть у одного бейджа события стоит twitchcon —
        # помечаем всю группу (билет, а не Twitch-дроп).
        offline = bool(re.search(r"twitchcon", ev.get("title", ""), re.I)) or any(
            av.get("twitchcon") for b in badges_in_ev for av in (b.get("availability") or []))
        for b in badges_in_ev:
            set_id = b.get("current", {}).get("set_id")
            if not set_id or set_id in MANUAL_SET_IDS or windows.get(set_id):
                continue
            if _page_too_late(page_info, set_id):     # SD: «окно уже прошло»
                continue
            windows[set_id] = [{
                "event_title": ev.get("title", ""), "group": grp, "game": "",
                "start": start, "end": end,
                "cost": ", ".join((b.get("availability") or [{}])[0].get("costs") or []),
                "condition": None,
                "id": None, "all_ids": [], "category_href": None, "box_art_url": None,
                "twitch_link": (twitch_links or {}).get(set_id), "channel_count": 0,
                "offline_event": offline,
                # Даты события — только день, без часа: не выдумываем точное время
                "dates_coarse": True,
            }]
    return windows


def add_event_content_windows(windows, events, badges, page_info=None, twitch_links=None,
                              now=None):
    """ФОЛБЭК A2: событие НАЗЫВАЕТ бейдж в тексте content, но не заполнило
    структурную связь twitch_global_badges.

    Так было со Spider-Man (24.07.2026): событие «Marvel Tōkon Open Beta» имело
    даты и content «The Spiderman badge will be available…», но список бейджей
    события был пуст — бот значок не увидел и прислал алерт «нет дат», хотя у
    StreamDatabase все данные были. Источник не виноват: SD просто не связал
    объекты. Здесь связываем сами — по имени бейджа в тексте события.

    Осторожно с ложными совпадениями: сопоставляем ТОЛЬКО бейджи-сироты (без окна
    из более надёжных источников выше), только СВЕЖИЕ (HEURISTIC_MAX_AGE_DAYS) и
    требуем достаточно длинное имя, чтобы короткие общие слова не липли к
    случайной прозе."""
    assert now is not None, "now обязателен: доменная логика не читает часы"
    orphans = []
    for b in badges or []:
        sid = (b.get("current") or {}).get("set_id")
        if not sid or sid in MANUAL_SET_IDS or windows.get(sid):
            continue
        if _page_too_late(page_info, sid):
            continue
        added = badge_added_dt(b)
        if not added or (now - added).days > HEURISTIC_MAX_AGE_DAYS:
            continue
        title = ((b.get("current") or {}).get("version") or {}).get("title") or ""
        orphans.append((sid, title, _norm_alnum(title), _norm_alnum(sid)))
    if not orphans:
        return windows

    for ev in events or []:
        if ev.get("hidden"):
            continue
        grams = _content_ngrams(ev.get("content"))
        if not grams:
            continue
        start = parse_dt(ev.get("start_at_date"), ev.get("start_at_time"))
        end = parse_dt(ev.get("end_at_date"), ev.get("end_at_time"))
        if not start and not end:
            continue
        raw = (ev.get("content") or "").lower()
        # Стоимость — по однозначным словам, но НЕ метим платным, если рядом сказано
        # «бесплатно/free» («gifted for free», «no purchase»): иначе ложная пилюля.
        paid = bool(re.search(r"subscri|gift|bits|purchas|\bbuy\b", raw))
        free = bool(re.search(r"\bfree\b|no purchase|no cost|бесплатн", raw))
        cost = "paid" if (paid and not free) else None
        cond = _condition_from_content(raw)
        grp = group_key(ev.get("title", ""))
        offline = bool(re.search(r"twitchcon", ev.get("title", ""), re.I))
        for sid, title, ntitle, nsid in orphans:
            if windows.get(sid):     # уже подхвачен другим событием в этом же проходе
                continue
            # Совпадение только по ЦЕЛЫМ словам/фразам события (см. _content_ngrams).
            matched = (len(ntitle) >= 5 and ntitle in grams) or (len(nsid) >= 6 and nsid in grams)
            if not matched:
                continue
            windows[sid] = [{
                "event_title": ev.get("title", ""), "group": grp, "game": "",
                "start": start, "end": end,
                "cost": cost, "condition": cond,
                "id": None, "all_ids": [], "category_href": None, "box_art_url": None,
                "twitch_link": (twitch_links or {}).get(sid), "channel_count": 0,
                "offline_event": offline, "dates_coarse": True,
                "from_event_content": True,
            }]
            log.debug(f"  event-content: «{ev.get('title')}» → {sid} ({title})")
    return windows


def event_dates_by_set_id(events):
    """Даты САМОГО события для каждого привязанного значка. Нужны, чтобы залатать
    пустые availability (см. enrich_windows)."""
    out = {}
    for ev in events or []:
        if ev.get("hidden"):
            continue
        start = parse_dt(ev.get("start_at_date"), ev.get("start_at_time"))
        end = parse_dt(ev.get("end_at_date"), ev.get("end_at_time"))
        if not start and not end:
            continue
        coarse = not (ev.get("start_at_time") or ev.get("end_at_time"))
        for b in ev.get("twitch_global_badges") or []:
            sid = (b.get("current") or {}).get("set_id")
            if sid and sid not in out:
                out[sid] = (start, end, coarse, ev.get("title") or "")
    return out


def enrich_windows(windows, page_info=None, twitch_links=None, event_dates=None,
                   catalog=None):
    """Победившее окно дополняем тем, чего в нём нет, из остальных источников.

    Цепочка приоритетов выбирает окно целиком: первый источник с датами отдаёт и
    все прочие поля. Из-за этого более точные ДАТЫ стирали более полное ОПИСАНИЕ —
    у Egg событие «Egg Hunt 2026» дало точные 29.07 10:00→26.08, но с пустыми
    флагами условия и без ссылки, и пост выродился в «Условия уточняются» без
    ссылки, хотя на странице SD было «subscribed or gifted» и ссылка на ROBLOX.

    ДАТЫ тоже заполняем — но только когда их нет ВООБЩЕ. С августа 2026 SD
    заводит значку availability сразу, а даты в неё вписывает позже, и такая
    пустая оболочка выигрывала приоритет у источников, где даты уже были:
    все фолбэки ниже пропускают значок, если окно для него уже существует.
    Итог — семь значков разом (nasa-roman, оба Pikachu, dron-e, diablo…) висели
    как «нет дат, не знаю, как классифицировать», хотя у их событий даты стояли.
    Существующие даты не трогаем: availability точнее события.

    Первыми идут даты из КАТАЛОГА (с ~27.09.2026 SD кладёт их прямо на значок):
    это структурные поля с часами, точнее разбора текста и дат события."""
    page_info = page_info or {}
    twitch_links = twitch_links or {}
    event_dates = event_dates or {}
    catalog = catalog or {}
    for set_id, wins in windows.items():
        info = page_info.get(set_id) or {}
        link = twitch_links.get(set_id)
        cat = catalog_window_fields(catalog.get(set_id))
        for w in wins:
            if not w.get("start") and not w.get("end"):
                if cat:
                    # цену не трогаем здесь: у окна она может быть точнее (см. ниже)
                    w.update({k: v for k, v in cat.items() if k != "cost"})
                elif info.get("start"):
                    w["start"] = parse_dt(info["start"].split("T")[0],
                                          info["start"].split("T")[1][:5])
                    if info.get("end"):
                        w["end"] = parse_dt(info["end"].split("T")[0],
                                            info["end"].split("T")[1][:5])
                    w["from_page"] = True
                    w["start_time_known"] = bool(info.get("start_time_known"))
                    w["end_time_known"] = bool(info.get("end_time_known"))
                    w["dates_unconfirmed"] = bool(info.get("unconfirmed"))
                    w["too_late"] = bool(info.get("too_late"))
                elif set_id in event_dates:
                    start, end, coarse, title = event_dates[set_id]
                    w["start"], w["end"] = start, end
                    w["dates_coarse"] = coarse
                    if not w.get("event_title"):
                        w["event_title"] = title
                    if not w.get("group"):
                        w["group"] = group_key(title)
            if not w.get("condition"):
                cond = PAGE_KIND_RU.get(info.get("kind"))
                if cond and info.get("watch_minutes"):
                    cond = f"Смотреть эфир {ru_duration_minutes(info['watch_minutes'])}"
                if cond:
                    w["condition"] = cond
            if not w.get("twitch_link") and link:
                w["twitch_link"] = link
            # costs у события SD тоже бывает пустым — берём из типа со страницы
            # (тот же вывод, что делает add_page_windows).
            if not w.get("cost") and cat.get("cost"):
                w["cost"] = cat["cost"]
            if not w.get("cost") and info.get("kind"):
                w["cost"] = PAGE_KIND_COST.get(info["kind"])
    return windows


def add_catalog_fields_windows(windows, badges, page_info=None, twitch_links=None):
    """Окно из полей САМОГО значка каталога. С ~27.09.2026 SD перенёс даты и цену
    из badges[].availability прямо на значок, и значок, про который SD знает
    только из каталога (так были заведены La Velada и EWC), остался бы без окна:
    фолбэк B ниже читает только availability, которой больше нет.

    Приоритет — сразу после структурной availability (событий и страницы): это
    те же машинные поля, и они точнее разбора текста страницы и дат события."""
    for b in badges or []:
        set_id = (b.get("current") or {}).get("set_id")
        if not set_id or set_id in MANUAL_SET_IDS or windows.get(set_id):
            continue
        if _page_too_late(page_info, set_id):
            continue
        fields = catalog_window_fields(b)
        if not fields:
            continue
        windows[set_id] = [{
            "event_title": "", "group": None, "game": "",
            "condition": None,
            "id": None, "all_ids": [], "category_href": None, "box_art_url": None,
            "twitch_link": (twitch_links or {}).get(set_id), "channel_count": 0,
            "offline_event": False,
            **fields,
        }]
    return windows


def add_catalog_windows(cx, windows, badges, page_info=None, twitch_links=None):
    """ФОЛБЭК B: availability из САМОГО КАТАЛОГА (badges[].availability[]).
    Раньше build_records брал оттуда только condition, а start/end выбрасывал —
    хотя это 131 бейдж (107 из них вообще не представлены в events.json).
    С ~27.09.2026 availability в каталоге нет: см. add_catalog_fields_windows."""
    for b in badges or []:
        set_id = b.get("current", {}).get("set_id")
        if not set_id or set_id in MANUAL_SET_IDS or windows.get(set_id):
            continue
        if _page_too_late(page_info, set_id):
            continue
        for av in b.get("availability") or []:
            if av.get("hidden"):
                continue
            start = parse_dt(av.get("start_at_date"), av.get("start_at_time"))
            end = parse_dt(av.get("end_at_date"), av.get("end_at_time"))
            if not start and not end:
                continue
            windows[set_id] = [{
                "event_title": "", "group": None,
                "start": start, "end": end,
                "cost": cost_from_steps(av_objectives(av), ", ".join(av.get("costs") or [])),
                "condition": describe_condition_ru(av),
                "id": av.get("_id"), "all_ids": [],
                "category_href": category_href(cx, av.get("categories")),
                "box_art_url": _category_box_art(av.get("categories")),
                "categories": category_names(av.get("categories")),
                "game": _category_name(av.get("categories")),
                "twitch_link": (twitch_links or {}).get(set_id),
                "channel_count": av.get("channel_count", 0),
                "offline_event": bool(av.get("twitchcon")),
                "dates_coarse": not (av.get("start_at_time") or av.get("end_at_time")),
            }]
            break
    return windows


def add_page_availability_windows(cx, windows, page_avail, twitch_links=None):
    """ФОЛБЭК A0: структурная availability СО СТРАНИЦЫ значка.

    Приоритет сразу после events[].availability и выше разбора текста: это те же
    машинные поля, просто SD с августа 2026 наполняет их на странице значка, а в
    событие может не проставить ничего. DRON-E и Diablo молчали именно так —
    события «Wasteland Circuit» и «BlizzCon 2026» заведены без дат, а на страницах
    лежали и окно, и условие. Скрытые записи сюда не попадают (отфильтрованы в
    fetch_streamdb.page_availability)."""
    for set_id, avs in (page_avail or {}).items():
        if set_id in MANUAL_SET_IDS or windows.get(set_id):
            continue
        for av in avs:
            start = parse_dt(av.get("start_at_date"), av.get("start_at_time"))
            end = parse_dt(av.get("end_at_date"), av.get("end_at_time"))
            if not start and not end:
                continue
            cats = av.get("categories") or []
            windows[set_id] = [{
                "event_title": "", "group": None,
                "game": _category_name(cats),
                "start": start, "end": end,
                "cost": cost_from_steps(av_objectives(av), ", ".join(av.get("costs") or [])),
                "condition": describe_condition_ru(av),
                "id": av.get("_id"), "all_ids": [],
                "category_href": category_href(cx, cats),
                "box_art_url": _category_box_art(cats),
                    "categories": category_names(cats),
                "twitch_link": (twitch_links or {}).get(set_id),
                "channel_count": len(av.get("channels") or []),
                "offline_event": bool(av.get("twitchcon")),
                "dates_coarse": not (av.get("start_at_time") or av.get("end_at_time")),
                "from_page_availability": True,
            }]
            break
    return windows


def add_page_windows(windows, page_info, badges, twitch_links=None):
    """Достраивает окна для бейджей, которых НЕТ в events.json (у SD там пусто),
    но чьё описание на странице бейджа удалось разобрать (даты + условие).
    Без этого свежие анонсы (La Velada VI и т.п.) не видны боту вообще."""
    if not page_info:
        return windows
    titles = {b.get("current", {}).get("set_id"): b.get("current", {}).get("version", {}).get("title")
              for b in badges}
    for set_id, info in page_info.items():
        if windows.get(set_id):          # структурные данные приоритетнее
            continue
        if not info.get("start"):        # описание без дат — окна из него нет
            continue
        start = parse_dt(*(info["start"].split("T")[0], info["start"].split("T")[1][:5])) \
            if info.get("start") else None
        end = parse_dt(*(info["end"].split("T")[0], info["end"].split("T")[1][:5])) \
            if info.get("end") else None
        if not start:
            continue
        if info.get("kind") == "watch":
            mins = info.get("watch_minutes")
            condition = f"Смотреть эфир {ru_duration_minutes(mins)}" if mins else "Смотреть эфир"
        else:
            condition = PAGE_KIND_RU.get(info.get("kind"))
        windows[set_id] = [{
            "event_title": titles.get(set_id) or "",
            "group": None,
            "game": "",
            "start": start,
            "end": end,
            "cost": PAGE_KIND_COST.get(info.get("kind")),
            "condition": condition,
            "id": None,
            "all_ids": [],
            "category_href": None,
            "box_art_url": None,
            "twitch_link": (twitch_links or {}).get(set_id),
            "channel_count": 0,
            "offline_event": info.get("kind") == "purchase",
            # Данные получены разбором текста, а не структурных полей: SD помечает
            # часть дат как неподтверждённые, а «too_late» — что окно уже прошло.
            "from_page": True,
            "dates_unconfirmed": bool(info.get("unconfirmed")),
            "too_late": bool(info.get("too_late")),
            # Время в описании было не всегда — 00:00 может быть заглушкой
            "start_time_known": bool(info.get("start_time_known")),
            "end_time_known": bool(info.get("end_time_known")),
        }]
    return windows


def apply_helix_info(cx, windows, helix, records_ids=None):
    """Дополняет окна вторым источником — описанием значка у самого Twitch.

    SD для свежих кампаний часто пуст («We don't yet know if this badge is earned
    by subscribing or watching»), и тогда единственные машинночитаемые данные
    лежат в Helix. Только ЗАПОЛНЯЕМ пустое: даты и разобранные условия SD
    точнее, перетирать их описанием нельзя."""

    for set_id, info in (helix or {}).items():
        wins = windows.get(set_id)
        if not wins:
            continue
        desc = info.get("description")
        for w in wins:
            if not w.get("condition"):
                cond = condition_from_helix(desc)
                if cond:
                    w["condition"] = cond
            # ГДЕ получать. Для 56 значков описание Twitch — единственный
            # источник этого: у SD для них нет ни категорий, ни каналов, и пост
            # выходил без единого слова о месте.
            if not w.get("game"):
                cat = category_from_description(desc)
                if cat:
                    w["game"] = cat
                    if not w.get("category_href"):
                        w["category_href"] = category_url_for(cx, cat)
            # Стоимость из того же описания: «subscribing or gifting» — платно,
            # «watching» — бесплатно. Иначе у значков, чьё окно построено из дат
            # события (там costs нет), пилюля цены не появлялась вовсе.
            if not w.get("cost") and desc:
                low = desc.lower()
                if re.search(r"subscrib|gift|bits|cheer", low):
                    w["cost"] = "paid"
                elif re.search(r"\bwatch", low):
                    w["cost"] = "free"
            if not w.get("twitch_link"):
                login = channel_from_description(desc)
                if login:
                    w["twitch_link"] = {"label": login,
                                        "url": f"https://www.twitch.tv/{login}"}
            url = info.get("click_url") or ""
            if not w.get("twitch_link") and url.startswith("https://"):
                w["twitch_link"] = {"label": info.get("title") or "на Twitch", "url": url}
    return windows


KNOWN_WINDOW_KEEP_DAYS = 14


def apply_known_windows(windows, known, now):
    """Возвращает даты, которые источник когда-то отдавал, а теперь потерял.

    31.08.2026 StreamDatabase убрал даты из ТЕКСТА описания («This badge was
    awarded between September 23rd and October 13th» → «This badge was added to
    promote…»), а в структурные поля их ещё не вписал. Пять значков разом стали
    «нет дат — не знаю, как классифицировать», хотя утром того же дня мы окно
    знали и публиковали. Данные не изменились — их временно нет у источника.

    Забывать в такой ситуации хуже, чем помнить: значок продолжает выдаваться,
    а канал молчит. Берём запомненное ТОЛЬКО когда живых дат нет вовсе, и
    храним ограниченное время — если кампания давно кончилась, воскрешать её
    не надо."""
    for set_id, wins in windows.items():
        remembered = known.get(set_id)
        if not remembered:
            continue
        for w in wins:
            if w.get("start") or w.get("end"):
                continue
            start = parse_dt(*(remembered.get("start") or "").split("T")[:2]) \
                if remembered.get("start") else None
            end = parse_dt(*(remembered.get("end") or "").split("T")[:2]) \
                if remembered.get("end") else None
            if not start and not end:
                continue
            newest = end or start
            if newest and (now - newest).days > KNOWN_WINDOW_KEEP_DAYS:
                continue
            w["start"], w["end"] = start, end
            w["dates_remembered"] = True
    return windows


def apply_manual_overrides(windows, overrides):
    """Накладывает ручные данные ПОВЕРХ всех автоматических источников.

    Нужно там, где SD не просто «не связал объекты», а честно не знает ответа:
    у покемонов (Bulbasaur/Charmander/Pichu/Squirtle, 17.08.2026) страница значка
    отдаёт «We don't yet know if this badge is earned by subscribing or watching»,
    availability пуст, дат нет нигде — а кампания при этом уже идёт, и условия
    опубликованы Twitch отдельной схемой. Никакой фолбэк такого не выведет,
    поэтому единственный источник правды здесь — человек.

    Приоритет наивысший: перекрываем даже структурные поля SD."""
    for set_id, ov in (overrides or {}).items():
        w = (windows.get(set_id) or [None])[0]
        if w is None:
            w = {
                "event_title": "", "group": None, "game": "", "start": None, "end": None,
                "cost": None, "condition": None, "id": None, "all_ids": [],
                "category_href": None, "box_art_url": None, "twitch_link": None,
                "channel_count": 0, "offline_event": False,
            }
            windows[set_id] = [w]
        if ov.get("group"):
            w["group"] = ov["group"]
            w["event_title"] = ov["group"]
        for field in ("condition", "cost"):
            if ov.get(field):
                w[field] = ov[field]
        if ov.get("link"):
            w["twitch_link"] = ov["link"]
        # Даты: пишем, даже если значение None — «end: null» это осознанное
        # «конец неизвестен» (открытое окно), а не отсутствие мнения.
        # Дата без 'T<час>' — значит, часа мы не знаем: помечаем окно грубым, иначе
        # parse_dt подставит 00:00 и бот покажет выдуманную точность до минуты.
        coarse = False
        for field in ("start", "end"):
            if field not in ov:
                continue
            val = ov.get(field)
            if not val:
                w[field] = None
                continue
            date_s, _, time_s = val.partition("T")
            w[field] = parse_dt(date_s, time_s)
            if not time_s:
                coarse = True
        if coarse:
            w["dates_coarse"] = True
        w["from_manual"] = True
        # Ручное окно не должно глохнуть о «SD сказал, что уже поздно».
        w.pop("too_late", None)
    return windows


def remember_windows(known, records):
    """Новая память окон: known + окна, которые источник отдаёт СЕЙЧАС.
    Чистая версия generate_site.save_known_windows (запись — у вызывающего)."""
    known = dict(known or {})
    for r in records:
        w = r.get("window") or {}
        if w.get("dates_remembered"):
            continue                      # не перезаписываем память самой памятью
        if not (w.get("start") or w.get("end")):
            continue
        known[r["set_id"]] = {
            "start": w["start"].strftime("%Y-%m-%dT%H:%M") if w.get("start") else None,
            "end": w["end"].strftime("%Y-%m-%dT%H:%M") if w.get("end") else None,
            "title": r.get("title"),
        }
    return known


def clean_overrides(data):
    """manual/overrides.json → {set_id: dict}: ключи на «_» и не-словари отбрасываются."""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
