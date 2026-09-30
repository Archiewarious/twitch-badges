"""Записи значков из снапшота SD — чистая функция, без файлов, часов и глобалов.

    recs = build_records(snapshot, RecordsContext(now=..., known_windows=..., overrides=...))

Раньше (generate_site.build_records) читала known_windows и overrides с диска,
брала текущее время сама и держала категории/Helix в глобальных переменных
модуля, которые потом читали бот и подписи. Теперь всё это — входы.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..timeutil import effective_end, parse_dt
from .catalog import badge_first_seen, catalog_by_set_id, event_slug, extract_num, group_key, holders_count
from .categories import canonicalize_categories, category_url_for, inherit_group_category
from .classify import classify
from .conditions import _condition_from_content, describe_condition_ru
from .descriptions import badge_name_from_description, category_from_description
from .text import _norm_alnum, plural
from .windows import (add_catalog_fields_windows, add_catalog_windows, add_event_content_windows,
                      add_event_windows, add_page_availability_windows, add_page_windows,
                      apply_helix_info, apply_known_windows, apply_manual_overrides,
                      collect_windows_by_set_id, enrich_windows, event_dates_by_set_id)

log = logging.getLogger(__name__)


@dataclass
class RecordsContext:
    """Входы build_records помимо снапшота."""
    now: datetime
    known_windows: dict = field(default_factory=dict)   # память окон (kv known_windows)
    overrides: dict = field(default_factory=dict)       # manual/overrides.json, уже очищенный


@dataclass
class Sources:
    """Справочники снапшота, нужные построителям окон (бывшие глобалы модуля).
    category_urls пополняется канонизацией — подписи берут ссылки отсюда."""
    category_urls: dict
    helix: dict
    page_avail: dict


@dataclass
class Built:
    records: list
    category_urls: dict


def aggregate_family(members):
    """Схлопывает версии-тиры одного set_id (напр. 28 порогов 'sub-gifter')
    в одну карточку с диапазоном — иначе они заваливают страницу дублями."""
    if len(members) == 1:
        r = dict(members[0])
        r["version_count"] = 1
        return r

    statuses = [m["status"] for m in members]
    if "active" in statuses:
        status = "active"
        primary = next(m for m in members if m["status"] == "active")
    elif "upcoming" in statuses:
        status = "upcoming"
        primary = next(m for m in members if m["status"] == "upcoming")
    else:
        status = "ended"
        primary = members[0]

    nums = [(extract_num(m["title"]), m) for m in members]
    numeric = [n for n, _ in nums if n is not None]

    manual = primary.get("manual_note")
    if manual:
        condition = manual
    elif numeric:
        condition = (f"{len(members)} {plural(len(members), 'уровень', 'уровня', 'уровней')}, "
                      f"порог от {min(numeric)} до {max(numeric)}")
    else:
        condition = primary.get("condition")

    # ключ тотально упорядочен: None-номера получают -1, чтобы max не сравнивал None с None
    rep = max(nums, key=lambda x: (x[0] is not None, x[0] if x[0] is not None else -1))[1] if numeric else members[0]
    clean = next((m for m in members if extract_num(m["title"]) is None), None)
    title = clean["title"] if clean else rep["title"]

    return {
        "set_id": primary["set_id"],
        "title": title,
        "image": rep["image"],
        "holders": None,
        "version_count": len(members),
        "first_seen": min((m["first_seen"] for m in members if m["first_seen"]), default=None),
        "status": status,
        "group": primary["group"],
        "window": primary["window"],
        "condition": condition,
        "note_kind": primary.get("note_kind"),
        "cost": primary.get("cost"),
    }


def _previous_year_art(slug, badges):
    """Арт прошлогоднего выпуска той же серии: для «subtember-2026» ищем
    «subtember-YYYY» с максимальным годом меньше текущего. Нужен как заглушка,
    пока Twitch не выложил финальный значок — пост без картинки бот не отправит
    (см. post_badge)."""
    m = re.match(r"^(.*)-(\d{4})$", slug)
    if not m:
        return None, None
    family, year = m.group(1), int(m.group(2))
    best = None
    for b in badges or []:
        sid = (b.get("current") or {}).get("set_id") or ""
        mm = re.match(rf"^{re.escape(family)}-(\d{{4}})$", sid)
        if mm and int(mm.group(1)) < year:
            if best is None or int(mm.group(1)) > best[0]:
                best = (int(mm.group(1)), b)
    if not best:
        return None, None
    url = ((best[1].get("current") or {}).get("version") or {}).get("image_url_4x") or ""
    return (url or None), f"{family}-{best[0]}"


def add_orphan_event_records(cx, records, snapshot, now):
    """ФОЛБЭК C: событие с датами, у которого НЕТ НИ ОДНОГО бейджа — ни привязанного
    (twitch_global_badges пуст), ни своего в каталоге.

    Так было с SUBtember 2026 (19.08.2026): у SD есть событие с точным окном
    28.08 18:00 → 01.10 18:00 UTC и текстом «badge will be available for
    subscribing, gifting, or using Bits», но объекта бейджа ещё нет вообще.
    Весь остальной пайплайн бейдж-центричный (build_records идёт по badges), а
    add_event_content_windows умеет цеплять событие только к УЖЕ существующему
    бейджу-сироте — поэтому кампания не попадала никуда, и канал молчал, пока
    конкуренты анонс уже опубликовали.

    set_id берём как слаг заголовка: у SD серии называются предсказуемо
    (subtember-2024/2025 → subtember-2026), а dedup бота завязан ровно на set_id —
    значит, когда настоящий бейдж появится, он схлопнется с этой записью и второго
    поста не будет. Если SD назовёт его иначе, повтор всё же возможен: страхуемся
    тем, что синтетическую запись выключает любой бейдж с таким же заголовком."""

    badges = snapshot.get("badges") or []
    known_ids = {(b.get("current") or {}).get("set_id") for b in badges}
    known_titles = {
        _norm_alnum(((b.get("current") or {}).get("version") or {}).get("title"))
        for b in badges
    }
    out = []
    for ev in snapshot.get("events") or []:
        if ev.get("hidden") or ev.get("twitch_global_badges"):
            continue
        title = (ev.get("title") or "").strip()
        content = (ev.get("content") or "")
        if not title or not re.search(r"badge|значок", content, re.I):
            continue
        slug = event_slug(title)
        if not slug or slug in known_ids or _norm_alnum(title) in known_titles:
            continue
        # Офлайн-мероприятие — билет, а не Twitch-дроп (ср. is_shown в боте).
        if re.search(r"twitchcon", title, re.I):
            continue
        start = parse_dt(ev.get("start_at_date"), ev.get("start_at_time"))
        end = parse_dt(ev.get("end_at_date"), ev.get("end_at_time"))
        if not start and not end:
            continue
        raw = content.lower()
        paid = bool(re.search(r"subscri|gift|bits|purchas|\bbuy\b", raw))
        free = bool(re.search(r"\bfree\b|no purchase|no cost|бесплатн", raw))
        # Тот же шаблон «in the ELDEN RING category», что и в описаниях Twitch,
        # SD пишет и в тексте события. Без разбора пост о будущей кампании
        # выходил без единого слова о том, где значок получать.
        cat = category_from_description(content)
        window = {
            "event_title": title, "group": group_key(title), "game": cat or "",
            "start": start, "end": end,
            "cost": "paid" if (paid and not free) else ("free" if free else None),
            "condition": _condition_from_content(raw),
            "id": None, "all_ids": [],
            "category_href": category_url_for(cx, cat),
            "box_art_url": None,
            "twitch_link": (snapshot.get("twitch_links") or {}).get(slug),
            "channel_count": 0, "offline_event": False,
            # Часы у события есть — грубыми даты помечаем, только если их не дали.
            "dates_coarse": not (ev.get("start_at_time") or ev.get("end_at_time")),
            "from_orphan_event": True,
        }
        if start and end and start <= now <= effective_end(window):
            status = "active"
        elif start and now < start:
            status = "upcoming"
        else:
            continue          # уже прошло — воскрешать нечего
        # Сначала — арт от самого Twitch: он заводит значок раньше, чем SD
        # внесёт его в каталог. Ищем по слагу кампании и по имени значка из
        # текста события; берём только точное совпадение, чужая картинка хуже
        # отсутствующей.
        art = art_from = None
        hx_name = badge_name_from_description(content)
        for hsid, hinfo in cx.helix.items():
            if not hinfo.get("image_url_4x"):
                continue
            htitle = _norm_alnum(hinfo.get("title"))
            if hsid == slug or (hx_name and htitle == _norm_alnum(hx_name)):
                art, art_from = hinfo["image_url_4x"], None
                break
        if not art:
            art, art_from = _previous_year_art(slug, badges)
        # Прошлогоднего арта может не быть вовсе — серия новая или не годовая
        # (LEGO Harley Quinn/Joker). Раньше такие кампании просто выбрасывались,
        # и анонс не выходил, хотя у SD были и точные даты, и условие. Теперь
        # рисуем карточку с названием кампании (см. render_cards.draw_title_text).
        # Показываем имя ЗНАЧКА, если SD его назвал: читатель ищет «Festering
        # Bloody Finger», а не «Questline 2: Path of the Cardinal Sin».
        # set_id при этом остаётся слагом события — два разных квестлайна у SD
        # называют один и тот же значок, и общий слаг склеил бы их в одну запись.
        badge_name = hx_name
        out.append({
            "set_id": slug,
            "title": badge_name or title,
            "image": art or "",
            "card_key": None if art else f"event-{slug}",
            "holders": None,
            "version_count": 1,
            "first_seen": None,
            "status": status,
            "group": window["group"],
            "window": window,
            "condition": window["condition"],
            "note_kind": None,
            "cost": window["cost"],
            # Картинка — прошлогодняя заглушка. Бот обязан сказать это в подписи,
            # иначе читатель примет её за финальный арт 2026-го.
            "art_placeholder_from": art_from,
        })
        log.debug(f"  orphan-event: «{title}» → {slug} (арт от {art_from})")
    return records + out



def build(snapshot, ctx: RecordsContext) -> Built:
    now = ctx.now
    cx = Sources(category_urls=dict(snapshot.get("category_urls") or {}),
                 helix=snapshot.get("helix") or {},
                 page_avail=snapshot.get("page_availability") or {})
    windows_by_id = collect_windows_by_set_id(
        cx, snapshot.get("events", []), snapshot.get("twitch_links"))
    # Приоритет источников окна — см. domain/windows.py
    page_info = snapshot.get("page_info")
    links = snapshot.get("twitch_links")
    badges = snapshot.get("badges", [])
    windows_by_id = add_page_availability_windows(
        cx, windows_by_id, snapshot.get("page_availability"), links)
    windows_by_id = add_catalog_fields_windows(windows_by_id, badges, page_info, links)
    windows_by_id = add_page_windows(windows_by_id, page_info, badges, links)
    windows_by_id = add_event_windows(windows_by_id, snapshot.get("events", []), page_info, links)
    windows_by_id = add_event_content_windows(windows_by_id, snapshot.get("events", []), badges,
                                              page_info, links, now)
    windows_by_id = add_catalog_windows(cx, windows_by_id, badges, page_info, links)
    windows_by_id = enrich_windows(windows_by_id, page_info, links,
                                   event_dates_by_set_id(snapshot.get("events", [])),
                                   catalog_by_set_id(badges))
    # Второй автоматический источник — описание значка у Twitch (Helix).
    windows_by_id = apply_helix_info(cx, windows_by_id, snapshot.get("helix"))
    # Память: возвращаем даты, которые источник когда-то отдавал, а теперь потерял.
    windows_by_id = apply_known_windows(windows_by_id, ctx.known_windows or {}, now)
    # Человек — последний и главный источник: перекрывает всё, что выведено выше.
    windows_by_id = apply_manual_overrides(windows_by_id, ctx.overrides or {})
    raw = []
    for b in snapshot.get("badges", []):
        cur = b.get("current", {})
        set_id = cur.get("set_id", "")
        version = cur.get("version", {})
        cls = classify(cx, set_id, b, windows_by_id, now, page_info, links)

        condition = None
        w = cls.get("window")
        if w:
            condition = w["condition"]
        if not condition:
            for av in b.get("availability") or []:
                condition = describe_condition_ru(av)
                if condition:
                    break
        if not condition:
            condition = cls.get("manual_note")

        cost = (w or {}).get("cost") or cls.get("manual_cost")

        raw.append({
            "set_id": set_id,
            "title": version.get("title") or set_id,
            "image": version.get("image_url_4x", ""),
            "holders": holders_count(b.get("user_count")),
            "first_seen": badge_first_seen(b),
            "status": cls["status"],
            "group": cls["group"],
            "window": w,
            "condition": condition,
            "note_kind": cls.get("note_kind"),
            "manual_note": cls.get("manual_note"),
            "cost": cost,
        })

    by_set = {}
    for rec in raw:
        by_set.setdefault(rec["set_id"], []).append(rec)
    records = [aggregate_family(members) for members in by_set.values()]
    records = add_orphan_event_records(cx, records, snapshot, now)
    records = canonicalize_categories(cx, records, snapshot.get("category_names") or {})
    return Built(inherit_group_category(records), cx.category_urls)


def build_records(snapshot, ctx: RecordsContext) -> list:
    return build(snapshot, ctx).records
