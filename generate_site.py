#!/usr/bin/env python3
"""
Рендерит статическую HTML-страницу из снапшота data/streamdb_latest.json.

Логика классификации (см. README.md за разбором источников):
- StreamDatabase's events.json даёт точные окна дат для активных/будущих кампаний.
- StreamDatabase's global-badges.json даёт условие (часто с "Unknown Tier"),
  но НИКОГДА не даёт дат для непривязанных к событию бейджей.
- Для вечных ролевых бейджей (Prime, Turbo, VIP...) StreamDatabase не даёт вообще
  ничего — это общеизвестные факты о Twitch, занесены вручную в PERMANENT/STAFF/
  INVITE/RETIRED/TECHNICAL ниже. Если бейджа нет ни в событиях, ни в этих словарях,
  он честно помечается "неизвестно", а не додумывается.

Никакого сервера в рантайме — только готовый HTML-файл, который отдаёт nginx
как обычную статику. Пересобрать — просто перезапустить скрипт заново.
"""
import html
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
INCOMING_FILE = DATA_DIR / "streamdb_incoming.json"  # свежий staging (пишет fetch_streamdb)
LATEST_FILE = DATA_DIR / "streamdb_latest.json"      # закоммиченный (читает бот)
OUT_DIR = ROOT / "site"
OUT_FILE = OUT_DIR / "index.html"


def input_data_file():
    """Во время refresh читаем свежий staging; при ручном запуске — latest."""
    return INCOMING_FILE if INCOMING_FILE.exists() else LATEST_FILE

RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


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
    "unknown": "неизвестно",
}


def plural(n, one, few, many):
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def ru_duration_minutes(total_minutes):
    h, m = divmod(int(total_minutes), 60)
    parts = []
    if h:
        parts.append(f"{h} {plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{m} {plural(m, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts) or "0 минут"


def tier_ru(n):
    return f"{n} уровня" if n else "неизвестного уровня"


def describe_condition_ru(av):
    """Строит русское описание условия из структурных полей StreamDatabase,
    а не переводом английского objective — так честнее для 'unknown'-полей."""
    parts = []
    if av.get("watch"):
        mins = av.get("watch_minutes")
        parts.append(f"смотреть эфир {ru_duration_minutes(mins)}" if mins else "смотреть эфир")

    sub_parts = []
    if av.get("subscription"):
        amount = av.get("subscription_amount")
        cnt = f"{amount} " if amount and amount > 1 else ""
        sub_parts.append(f"оформить {cnt}подписку {tier_ru(av.get('subscription_tier'))}")
    if av.get("subscription_gift"):
        gamount = av.get("subscription_gift_amount")
        cnt = f"{gamount} " if gamount and gamount > 1 else ""
        sub_parts.append(f"подарить {cnt}подписку {tier_ru(av.get('subscription_gift_tier'))}")
    if sub_parts:
        parts.append(" или ".join(sub_parts))

    if av.get("twitchcon"):
        days = av.get("twitchcon_days")
        # Явно "офлайн-мероприятие" — иначе можно принять за обычный Twitch-дроп
        # за просмотр стрима (условие принципиально другое: физический билет).
        parts.append(f"купить билет на офлайн-мероприятие TwitchCon ({days} "
                      f"{plural(days, 'день', 'дня', 'дней')})" if days
                      else "купить билет на офлайн-мероприятие TwitchCon")
    if av.get("bits"):
        parts.append("потратить Bits")
    if av.get("clip") and not av.get("watch"):
        parts.append("создать клип")
    if av.get("turbo") and not sub_parts:
        parts.append("оформить подписку Turbo")

    if not parts:
        return None
    text = "; ".join(parts)
    return text[0].upper() + text[1:]


def parse_dt(date_s, time_s):
    if not date_s:
        return None
    time_s = time_s or "00:00"
    try:
        return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_dt(dt):
    if not dt:
        return "—"
    return f"{dt.day} {RU_MONTHS[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')} UTC"


def fmt_date_short(dt):
    if not dt:
        return "—"
    return f"{dt.day} {RU_MONTHS[dt.month - 1]}"


def group_key(event_title):
    return re.sub(r"\s*\([^)]*\)\s*$", "", event_title).strip()


def badge_first_seen(badge):
    added = [h for h in badge.get("history", []) if h.get("type") == "added"]
    if not added:
        return None
    return min(added, key=lambda h: h["timestamp"])["timestamp"]


def esc(s):
    return html.escape(str(s), quote=True)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def collect_windows_by_set_id(events, twitch_links=None):
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
                start = parse_dt(av.get("start_at_date"), av.get("start_at_time"))
                end = parse_dt(av.get("end_at_date"), av.get("end_at_time"))
                cats = av.get("categories") or []
                # Списки участников больше не храним (см. fetch_streamdb) — только счётчик;
                # fallback на старый формат со списком, если снапшот ещё не обрезан.
                channel_count = av.get("channel_count", len(av.get("channels") or []))
                game = cats[0].get("game", {}).get("name", "") if cats else ""
                windows.setdefault(set_id, []).append({
                    "event_title": ev_title,
                    "group": grp,
                    "game": game,
                    "start": start,
                    "end": end,
                    "cost": ", ".join(av.get("costs") or []),
                    "condition": describe_condition_ru(av),
                    # Билет на офлайн-мероприятие (TwitchCon и т.п.) — не "Twitch Drop"
                    # в смысле бота/канала: нельзя получить действием на Twitch, нужно
                    # физически купить билет и поехать. Бот/канал это скрывают (см.
                    # bot.py is_shown), сайт-каталог продолжает показывать — там уместно.
                    "offline_event": bool(av.get("twitchcon")),
                    # Диплинк-поля для кнопок бота (inline + автопост в канал):
                    "id": av.get("_id"),
                    "all_ids": ids_by_group.get(grp, []),
                    "category_href": cats[0].get("href") if cats else None,
                    "box_art_url": cats[0].get("game", {}).get("box_art_url") if cats else None,
                    # Автономная ссылка Twitch (событие/категория) со страницы бейджа
                    # StreamDatabase — для каналовых бейджей без categories (EWC и т.п.).
                    "twitch_link": twitch_links.get(set_id),
                    "channel_count": channel_count,
                })
    return windows


# Условие из разобранного описания страницы бейджа (когда структурных данных нет)
PAGE_KIND_RU = {
    "sub": "Оформить или подарить подписку",
    "purchase": "Купить билет",
    "bits": "Потратить Bits",
}


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
            "cost": "paid" if info.get("kind") in ("sub", "purchase") else "free",
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


def classify(set_id, catalog_badge, windows_by_id, now):
    windows = windows_by_id.get(set_id, [])
    active = [w for w in windows if w["start"] and w["end"] and w["start"] <= now <= w["end"]]
    upcoming = [w for w in windows if w["start"] and now < w["start"]]
    ended = [w for w in windows if w["end"] and now > w["end"]]

    if active:
        w = min(active, key=lambda x: x["end"])
        return {"status": "active", "window": w, "group": w["group"], "note_kind": None}
    if upcoming:
        w = min(upcoming, key=lambda x: x["start"])
        return {"status": "upcoming", "window": w, "group": w["group"], "note_kind": None}
    if ended:
        w = max(ended, key=lambda x: x["end"])
        return {"status": "ended", "window": w, "group": w["group"], "note_kind": "ended"}

    if set_id in PERMANENT:
        note, cost = PERMANENT[set_id]
        return {"status": "active", "window": None, "group": "__permanent__",
                "note_kind": None, "manual_note": note, "manual_cost": cost}
    if set_id in STAFF:
        return {"status": "ended", "window": None, "group": None,
                "note_kind": "staff", "manual_note": STAFF[set_id]}
    if set_id in INVITE:
        return {"status": "ended", "window": None, "group": None,
                "note_kind": "invite", "manual_note": INVITE[set_id]}
    if set_id in RETIRED:
        return {"status": "ended", "window": None, "group": None,
                "note_kind": "retired-program", "manual_note": RETIRED[set_id]}
    if set_id in TECHNICAL:
        return {"status": "ended", "window": None, "group": None,
                "note_kind": "technical", "manual_note": TECHNICAL[set_id]}
    if set_id in PERIODIC:
        return {"status": "ended", "window": None, "group": None,
                "note_kind": "periodic", "manual_note": PERIODIC[set_id]}
    if catalog_badge.get("added") is False:
        return {"status": "ended", "window": None, "group": None, "note_kind": "removed"}
    return {"status": "ended", "window": None, "group": None, "note_kind": "unknown"}


def extract_num(title):
    m = re.search(r"([\d,]+)", title or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


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


def build_records(snapshot):
    now = datetime.now(timezone.utc)
    windows_by_id = collect_windows_by_set_id(
        snapshot.get("events", []), snapshot.get("twitch_links"))
    # Бейджи вне events.json (свежие анонсы) — окна из разобранного описания страницы
    windows_by_id = add_page_windows(
        windows_by_id, snapshot.get("page_info"), snapshot.get("badges", []),
        snapshot.get("twitch_links"))
    raw = []
    for b in snapshot.get("badges", []):
        cur = b.get("current", {})
        set_id = cur.get("set_id", "")
        version = cur.get("version", {})
        cls = classify(set_id, b, windows_by_id, now)

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
            "holders": (b.get("user_count") or {}).get("current"),
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
    return [aggregate_family(members) for members in by_set.values()]


COST_LABEL = {"paid": "платно", "free": "бесплатно"}


def render_mini_badge(r, mode):
    img = f'<img src="{esc(r["image"])}" alt="" loading="lazy">' if r["image"] else ""
    cond = esc(r["condition"]) if r["condition"] else "Условие не определено"
    w = r["window"]
    meta_bits = []
    urgent = False
    if r.get("version_count", 1) > 1:
        meta_bits.append(f'{r["version_count"]} уровней')
    if w and w.get("game"):
        meta_bits.append(esc(w["game"]))
    if mode == "active" and w and w.get("end"):
        remaining = (w["end"] - datetime.now(timezone.utc)).days
        meta_bits.append(f"осталось {remaining} {plural(remaining, 'день', 'дня', 'дней')}" if remaining >= 1 else "заканчивается сегодня")
        urgent = remaining <= 1
    elif mode == "upcoming" and w and w.get("start"):
        starts_in = (w["start"] - datetime.now(timezone.utc)).days
        meta_bits.append(f"начнётся через {starts_in} {plural(starts_in, 'день', 'дня', 'дней')}" if starts_in >= 1 else "начнётся сегодня")
    meta = " · ".join(meta_bits)
    cost_pill = ""
    if r.get("cost") in COST_LABEL:
        cost_pill = f'<span class="cost-pill cost-{r["cost"]}"><span class="dot"></span>{COST_LABEL[r["cost"]]}</span>'
    if urgent:
        meta_bits_urgent = '<span class="urgent-pill">скоро истекает</span>'
        meta = meta_bits_urgent + meta
    glow_class = f"glow-{r['cost']}" if r.get("cost") in COST_LABEL else ""
    return f"""
      <div class="mini-badge">
        <div class="mini-badge-img {glow_class}">{img}</div>
        <div>
          <div class="mini-title">{esc(r['title'])}</div>
          <div class="mini-cond">{cond}</div>
          <div class="mini-meta">{cost_pill}{meta}</div>
        </div>
      </div>"""


def render_group_cards(records, mode, cost):
    groups = {}
    for r in records:
        if r["status"] != mode or r.get("cost") != cost:
            continue
        groups.setdefault(r["group"] or r["set_id"], []).append(r)

    if not groups:
        return '<p class="empty">Пусто.</p>'

    def sort_key(item):
        key, items = item
        if key == "__permanent__":
            return (1, "")
        ws = [r["window"] for r in items if r["window"]]
        if mode == "active" and ws:
            return (0, min(w["end"] for w in ws))
        if mode == "upcoming" and ws:
            return (0, min(w["start"] for w in ws))
        return (0, key)

    cards = []
    for key, items in sorted(groups.items(), key=sort_key):
        if key == "__permanent__":
            title = "Постоянные роли и достижения"
            subtitle = "Доступно всегда"
        else:
            title = items[0]["group"] or items[0]["title"]
            ws = [r["window"] for r in items if r["window"]]
            starts = [w["start"] for w in ws if w["start"]]
            ends = [w["end"] for w in ws if w["end"]]
            lo = min(starts) if starts else None
            hi = max(ends) if ends else None
            subtitle = f"{fmt_date_short(lo)} – {fmt_date_short(hi)} {hi.year}" if lo and hi else ""
        badges_html = "\n".join(render_mini_badge(r, mode) for r in items)
        is_urgent = mode == "active" and any(
            r["window"] and r["window"].get("end")
            and (r["window"]["end"] - datetime.now(timezone.utc)).days <= 1
            for r in items
        )
        card_class = "group-card wide" if key == "__permanent__" else "group-card"
        if is_urgent:
            card_class += " urgent"
        cards.append(f"""
    <div class="{card_class}">
      <div class="group-head">
        <h4>{esc(title)}</h4>
        <span class="group-sub">{esc(subtitle)}</span>
      </div>
      <div class="group-badges">{badges_html}
      </div>
    </div>""")
    return f'<div class="group-grid">{"".join(cards)}</div>'


def render_archive_row(r):
    img = f'<img src="{esc(r["image"])}" alt="" loading="lazy">' if r["image"] else ""
    cond = esc(r["condition"]) if r["condition"] else "—"
    group = esc(r["group"]) if r["group"] and r["group"] != "__permanent__" else "—"
    seen = r["first_seen"][:10] if r["first_seen"] else "—"
    if r.get("version_count", 1) > 1:
        holders = f'{r["version_count"]} уровней'
    elif r["holders"] is not None:
        holders = f'{r["holders"]:,}'.replace(",", " ")
    else:
        holders = "—"
    note_label = NOTE_KIND_LABEL.get(r["note_kind"], "неизвестно")
    search_blob = esc(strip_accents(f"{r['title']} {r['set_id']} {r['group'] or ''}".lower()))
    return f"""
    <tr data-search="{search_blob}">
      <td class="badge-cell">{img}<div class="badge-title">{esc(r['title'])}</div></td>
      <td>{group}</td>
      <td>{cond}</td>
      <td><span class="tag tag-{esc(r['note_kind'] or 'unknown')}"><span class="dot"></span>{esc(note_label)}</span></td>
      <td>{esc(seen)}</td>
      <td>{holders}</td>
    </tr>"""


def render_cost_section(records, mode):
    n_free = sum(1 for r in records if r["status"] == mode and r.get("cost") == "free")
    n_paid = sum(1 for r in records if r["status"] == mode and r.get("cost") == "paid")
    free_html = render_group_cards(records, mode, "free")
    paid_html = render_group_cards(records, mode, "paid")
    return f"""
  <div class="cost-columns">
    <div class="cost-column">
      <div class="cost-column-head cost-free"><span class="dot"></span><h3>Бесплатно</h3><span class="count">{n_free}</span></div>
      {free_html}
    </div>
    <div class="cost-column">
      <div class="cost-column-head cost-paid"><span class="dot"></span><h3>Платно</h3><span class="count">{n_paid}</span></div>
      {paid_html}
    </div>
  </div>"""


def build_html(snapshot):
    fetched_at = snapshot.get("fetched_at", "")
    records = build_records(snapshot)

    active_html = render_cost_section(records, "active")
    upcoming_html = render_cost_section(records, "upcoming")

    archive = [r for r in records if r["status"] == "ended"]
    archive.sort(key=lambda r: r["first_seen"] or "", reverse=True)
    archive_html = "\n".join(render_archive_row(r) for r in archive)

    n_active = sum(1 for r in records if r["status"] == "active")
    n_upcoming = sum(1 for r in records if r["status"] == "upcoming")
    n_archive = len(archive)

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Twitch Badges Tracker</title>
<meta name="description" content="Каталог глобальных бейджей Twitch: что можно получить сейчас, что скоро, условия получения на русском.">
<style>
  :root{{
    --bg:#0b1220;--panel:#131c2e;--panel2:#0f1728;--ink:#e6edf6;--mut:#8ea1bd;
    --acc:#4aa8ff;--acc2:#5eead4;--line:#243247;--ok:#39d98a;--warn:#f5c451;--bad:#8a94a6;--radius:12px;
    --free:#199e70;--paid:#d95926;--purple:#9146ff;
  }}
  @media (prefers-color-scheme: light){{
    :root{{--bg:#f4f7fb;--panel:#ffffff;--panel2:#f0f4f9;--ink:#132033;--mut:#5b6b82;--line:#e2e8f2;--free:#1baf7a;--paid:#eb6834}}
  }}
  *{{box-sizing:border-box}}
  .visually-hidden{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
  body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}}
  a{{color:var(--acc);text-decoration:none}}
  header{{display:flex;align-items:center;gap:12px;padding:16px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap}}
  .logo{{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,var(--purple),var(--acc))}}
  header b{{font-size:19px;letter-spacing:.2px}}
  header span{{color:var(--mut);font-size:13px;margin-left:6px}}
  .wrap{{max-width:1400px;margin:0 auto;padding:26px 18px 60px}}

  .hero{{display:flex;gap:0;margin-bottom:8px;border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;background:var(--panel)}}
  .hero-stat{{flex:1;padding:22px 24px;position:relative}}
  .hero-stat+.hero-stat{{border-left:1px solid var(--line)}}
  .hero-num{{font-size:44px;font-weight:800;letter-spacing:-.02em;line-height:1;background:linear-gradient(135deg,var(--purple),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}}
  .hero-stat.dim .hero-num{{background:none;-webkit-text-fill-color:unset;color:var(--mut)}}
  .hero-label{{margin-top:6px;color:var(--mut);font-size:13px}}
  @media (max-width:700px){{.hero{{flex-direction:column}} .hero-stat+.hero-stat{{border-left:none;border-top:1px solid var(--line)}}}}
  h2{{font-size:16px;margin:34px 0 4px;color:var(--ink);display:flex;align-items:center;gap:10px}}
  h2 .count{{background:var(--panel2);color:var(--mut);font-size:12px;padding:2px 9px;border-radius:999px;font-weight:600}}
  .sub{{color:var(--mut);font-size:13px;margin:4px 0 16px}}
  input[type=search]{{width:100%;max-width:340px;padding:9px 13px;border-radius:999px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);font-size:14px;margin-bottom:14px}}

  .cost-columns{{display:flex;gap:22px;align-items:flex-start}}
  .cost-column{{flex:1;min-width:0}}
  .cost-column-head{{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;padding-bottom:10px;margin-bottom:12px;border-bottom:2px solid var(--line)}}
  .cost-column-head h3{{margin:0;font:inherit;color:inherit;text-transform:inherit;letter-spacing:inherit}}
  .cost-column-head .dot{{width:9px;height:9px;border-radius:50%;flex:none}}
  .cost-column-head.cost-free{{color:var(--free)}}
  .cost-column-head.cost-free .dot{{background:var(--free)}}
  .cost-column-head.cost-paid{{color:var(--paid)}}
  .cost-column-head.cost-paid .dot{{background:var(--paid)}}
  .cost-column-head .count{{margin-left:auto;background:var(--panel2);color:var(--mut);font-size:11px;padding:2px 9px;border-radius:999px;text-transform:none;letter-spacing:0;font-weight:600}}
  @media (max-width:760px){{.cost-columns{{flex-direction:column}}}}

  .cost-pill{{display:inline-flex;align-items:center;gap:5px;padding:2px 9px 2px 7px;border-radius:999px;font-size:11px;font-weight:600;background:var(--panel2);color:var(--ink);margin-right:7px}}
  .cost-pill .dot{{width:7px;height:7px;border-radius:50%;flex:none}}
  .cost-pill.cost-free .dot{{background:var(--free)}}
  .cost-pill.cost-paid .dot{{background:var(--paid)}}

  .group-grid{{column-count:2;column-gap:14px}}
  @media (max-width:1180px){{.group-grid{{column-count:1}}}}
  .group-card{{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;display:flex;flex-direction:column;gap:12px;break-inside:avoid;margin-bottom:14px}}
  .group-card.wide{{column-span:all}}
  .group-card.wide .group-badges{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px 18px}}
  .group-card.urgent{{border-left:3px solid var(--warn)}}
  .group-head{{display:flex;align-items:baseline;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);padding-bottom:10px}}
  .group-head h4{{margin:0;font-size:14.5px;font-weight:600}}
  .group-sub{{color:var(--mut);font-size:12px;white-space:nowrap}}
  .group-badges{{display:flex;flex-direction:column;gap:12px}}
  .mini-badge{{display:flex;gap:14px;align-items:flex-start}}
  .mini-badge-img{{flex:none;line-height:0}}
  .mini-badge img{{width:60px;height:60px;border-radius:12px;flex:none;background:#eef1f6;padding:6px;object-fit:contain}}
  .mini-badge-img.glow-free img{{box-shadow:0 0 0 2px rgba(25,158,112,.22),0 0 14px -3px rgba(25,158,112,.75)}}
  .mini-badge-img.glow-paid img{{box-shadow:0 0 0 2px rgba(217,89,38,.22),0 0 14px -3px rgba(217,89,38,.75)}}
  .mini-title{{font-weight:600;font-size:13.5px}}
  .mini-cond{{color:var(--ink);font-size:12.5px;margin-top:2px;opacity:.85}}
  .mini-meta{{color:var(--mut);font-size:11.5px;margin-top:3px;display:flex;align-items:center;flex-wrap:wrap;gap:4px 0}}
  .urgent-pill{{background:var(--warn);color:#0b1220;font-weight:700;font-size:10px;padding:2px 7px;border-radius:999px;margin-right:6px;text-transform:uppercase;letter-spacing:.02em}}
  .empty{{color:var(--mut);font-size:13px}}

  .table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius);background:var(--panel)}}
  table{{border-collapse:collapse;width:100%;font-size:13.5px}}
  th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}}
  th{{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;background:var(--panel2)}}
  tr:last-child td{{border-bottom:none}}
  .badge-cell{{display:flex;align-items:center;gap:14px;min-width:200px}}
  .badge-cell img{{width:40px;height:40px;border-radius:9px;flex:none;background:#eef1f6;padding:4px;object-fit:contain}}
  .badge-title{{font-weight:600}}

  .tag{{display:inline-flex;align-items:center;gap:6px;padding:3px 10px 3px 8px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap;background:var(--panel2);color:var(--ink)}}
  .tag .dot{{width:7px;height:7px;border-radius:50%;flex:none;background:var(--bad)}}
  .tag-staff .dot,.tag-invite .dot{{background:var(--acc)}}
  .tag-retired-program .dot,.tag-technical .dot{{background:var(--warn)}}

  footer{{color:var(--mut);font-size:12px;text-align:center;padding:30px 18px}}
</style>
</head>
<body>
<header>
  <div class="logo"></div>
  <b>Twitch Badges Tracker</b>
  <span>Что можно получить сейчас, что скоро, и что уже нельзя</span>
</header>
<div class="wrap">

  <div class="hero">
    <div class="hero-stat">
      <div class="hero-num">{n_active}</div>
      <div class="hero-label">можно получить сейчас</div>
    </div>
    <div class="hero-stat">
      <div class="hero-num">{n_upcoming}</div>
      <div class="hero-label">скоро откроются</div>
    </div>
    <div class="hero-stat dim">
      <div class="hero-num">{n_archive}</div>
      <div class="hero-label">в архиве — уже недоступны</div>
    </div>
  </div>

  <h2>Можно получить сейчас <span class="count">{n_active}</span></h2>
  <p class="sub">Активные кампании и постоянные роли — условие получения на русском, сколько осталось времени.</p>
  {active_html}

  <h2>Скоро <span class="count">{n_upcoming}</span></h2>
  <p class="sub">Ещё не началось, но окно уже известно.</p>
  {upcoming_html}

  <h2>Больше недоступно <span class="count">{n_archive}</span></h2>
  <p class="sub">Ивент завершился, программа закрыта, служебная роль — или условие получения неизвестно.</p>
  <label for="search" class="visually-hidden">Поиск по архиву бейджей</label>
  <input type="search" id="search" placeholder="Поиск по названию, set_id или группе…">
  <div class="table-wrap">
    <table id="archive">
      <thead><tr><th>Бейдж</th><th>Группа</th><th>Условие</th><th>Статус</th><th>Впервые замечен</th><th>Держателей</th></tr></thead>
      <tbody>{archive_html}</tbody>
    </table>
  </div>
</div>
<footer>
  Данные: streamdatabase.com · обновлено {esc(fetched_at)} UTC
</footer>
<script>
  function stripAccents(s){{ return s.normalize('NFKD').replace(/[\\u0300-\\u036f]/g, ''); }}
  document.getElementById('search').addEventListener('input', function(e){{
    var q = stripAccents(e.target.value.trim().toLowerCase());
    document.querySelectorAll('#archive tbody tr').forEach(function(tr){{
      var blob = tr.getAttribute('data-search') || '';
      tr.style.display = (!q || blob.indexOf(q) !== -1) ? '' : 'none';
    }});
  }});
</script>
</body>
</html>
"""


def sync_images(records):
    """Держит data/images/ = ровно картинки актуальных (active+upcoming) бейджей:
    докачивает недостающие, удаляет те, что больше не актуальны. Сайт архивные
    картинки грузит напрямую с Twitch CDN, локально они не нужны — только боту
    для actual-бейджей (Telegram фетчит их по нашему публичному URL)."""
    import fetch_streamdb as collector

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    wanted = {}  # uuid -> cdn_url
    for r in records:
        # Только актуальные не-технические — совпадает с тем, что показывает бот
        # (сайт архивные/постоянные картинки грузит напрямую с Twitch CDN).
        if r["status"] in ("active", "upcoming") and r.get("group") != "__permanent__":
            key = collector.image_cache_key(r["image"])
            if key:
                wanted[key] = r["image"]

    downloaded = 0
    for key, url in wanted.items():
        if not (IMAGES_DIR / f"{key}.png").exists():
            if collector.download_image(url):
                downloaded += 1

    removed = 0
    for f in IMAGES_DIR.glob("*.png"):
        if f.stem not in wanted:
            f.unlink()
            removed += 1

    print(f"images: {len(wanted)} актуальных ({downloaded} докачано, {removed} удалено)")
    return wanted


def main():
    # Любой крэш здесь (SD прислал нестандартные данные) должен давать НЕнулевой код,
    # чтобы refresh.sh под set -e абортил ДО commit-marker (старые данные целы) и
    # сработал OnFailure-алерт — иначе тихо замерзают данные без единого сигнала.
    try:
        snapshot = json.loads(input_data_file().read_text())
        records = build_records(snapshot)
        sync_images(records)
        OUT_DIR.mkdir(exist_ok=True)
        OUT_FILE.write_text(build_html(snapshot))
        print(f"OK: {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)")
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
