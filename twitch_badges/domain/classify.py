"""Статус значка (active / upcoming / ended) по его окнам."""
from datetime import timedelta

from ..timeutil import effective_end
from .catalog import INVITE, PERIODIC, PERMANENT, RETIRED, STAFF, TECHNICAL, badge_added_dt
from .categories import _category_name, category_href, category_url_for
from .conditions import (PAGE_KIND_COST, PAGE_KIND_RU, av_objectives, condition_from_helix,
                         cost_from_steps, describe_condition_ru)
from .descriptions import category_from_description, channel_from_description, pick_twitch_link
from .text import ru_duration_minutes

NO_DATE_ANNOUNCE_DAYS = 14   # свежий бейдж без дат ещё считаем новостью


# Сколько ждём, прежде чем анонсировать значок БЕЗ дат: SD обычно дозаполняет их
# в первые часы, и поспешный анонс оборачивается вторым постом «стартовало».
NO_DATE_GRACE_HOURS = 3


def classify(cx, set_id, catalog_badge, windows_by_id, now, page_info=None, twitch_links=None):
    windows = windows_by_id.get(set_id, [])
    # Кампанию отменили: SD ставит флаг на значок каталога. Окно могло остаться
    # «будущим», но получить значок уже нельзя — не анонсируем и не шлём «стартовало».
    if catalog_badge.get("cancelled") is True:
        w = max(windows, key=lambda x: x.get("end") or x.get("start") or now, default=None)
        return {"status": "ended", "window": w, "group": (w or {}).get("group"),
                "note_kind": "cancelled"}
    active = [w for w in windows if w["start"] and w["end"] and w["start"] <= now <= effective_end(w)]
    upcoming = [w for w in windows if w["start"] and now < w["start"]]
    ended = [w for w in windows if w["end"] and now > effective_end(w)]

    # Открытое окно: начало известно, конца нет (SD часто так для одноразовых
    # событий — La Velada). Без этого бакета такой бейдж не попадал НИКУДА и в день
    # старта молча исчезал из бота посреди собственного события.
    # too_late — SD прямо пишет «бейдж заведён уже после окончания окна»: такой
    # бейдж получить нельзя, воскрешать его открытым окном нельзя (иначе Dreamers
    # снова станет «доступен сейчас»).
    open_now = [w for w in windows
                if w["start"] and not w["end"] and w["start"] <= now and not w.get("too_late")]

    if active:
        w = min(active, key=lambda x: x["end"])
        return {"status": "active", "window": w, "group": w["group"], "note_kind": None}
    if open_now:
        w = max(open_now, key=lambda x: x["start"])
        return {"status": "active", "window": w, "group": w["group"], "note_kind": None}
    if upcoming:
        w = min(upcoming, key=lambda x: x["start"])
        return {"status": "upcoming", "window": w, "group": w["group"], "note_kind": None}
    if ended:
        w = max(ended, key=lambda x: x["end"])
        return {"status": "ended", "window": w, "group": w["group"], "note_kind": "ended"}

    # Окно есть, но получить уже нельзя (SD: «заведён после окончания»). Окно НЕ
    # выбрасываем: иначе бейдж выглядит как «есть на Twitch, дат нет, непонятно» и
    # монитор слепых зон будит владельца каждые 3 часа из-за осознанного решения.
    too_late = [w for w in windows if w.get("too_late")]
    if too_late:
        w = max(too_late, key=lambda x: x["start"] or now)
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

    # Свежий бейдж, дат которого нет НИГДЕ: ни в событиях, ни в каталоге, ни в
    # тексте страницы — SD выкладывает шаблон с заглушками («August Xth 2026
    # (X:X UTC)»). Раньше такие падали в «ended/unknown» и бот молчал, хотя это
    # новость: значок уже висит на Twitch, условие из текста читается, и
    # конкуренты публикуют их с пометкой «даты неизвестны». Публикуем и мы —
    # честно, без выдуманных дат. Ограничение по свежести, чтобы не воскресить
    # весь архив бездатных бейджей.
    info = (page_info or {}).get(set_id) or {}
    kind = info.get("kind")
    if kind and not info.get("too_late"):
        added_dt = badge_added_dt(catalog_badge)
        if added_dt and (now - added_dt).days <= NO_DATE_ANNOUNCE_DAYS:
            # Условие — тем же способом, что и add_page_windows (watch-минуты, если
            # известны), а не голым PAGE_KIND_RU: для kind="watch" тот словарь пуст
            # и раньше отдавал None вместо «Смотреть эфир». Ссылку берём из
            # twitch_links напрямую — дат тут нет вообще, поэтому её никогда не
            # доносит ни один построитель окна (все требуют start).
            if kind == "watch":
                mins = info.get("watch_minutes")
                condition = f"Смотреть эфир {ru_duration_minutes(mins)}" if mins else "Смотреть эфир"
            else:
                condition = PAGE_KIND_RU.get(kind)
            window = {
                "event_title": "", "group": None, "game": "",
                "start": None, "end": None,
                "cost": PAGE_KIND_COST.get(kind),
                "condition": condition,
                "id": None, "all_ids": [], "category_href": None, "box_art_url": None,
                "twitch_link": (twitch_links or {}).get(set_id), "channel_count": 0,
                "offline_event": kind == "purchase", "dates_coarse": True,
            }
            return {"status": "upcoming", "window": window, "group": None,
                    "note_kind": None}

    # То же самое, но по описанию TWITCH: у SD не осталось ни дат, ни kind, а
    # Helix при этом знает и условие, и категорию. 31.08.2026 SD переписал
    # описания пяти значков, потеряв даты (в структурные поля их ещё не вписал), —
    # и все пятеро замолчали, хотя раздача идёт и мы знаем, что и где делать.
    # Даты не выдумываем: пост честно скажет «даты уточняются».
    hx = cx.helix.get(set_id) or {}
    hx_cond = condition_from_helix(hx.get("description"))
    # Условие могло прийти и из ОПУБЛИКОВАННОЙ availability со страницы значка —
    # просто без дат. У WSCI 2026 SD знает «подписка или гифт» (steps), а описание
    # Twitch условия не несёт («earned by supporting the … Invitational»). Фолбэк
    # смотрел только на Twitch, и значок двое суток молчал как «нет дат — не знаю,
    # как классифицировать», хотя сказать было что.
    page_avs = [av for av in (cx.page_avail.get(set_id) or []) if not av.get("hidden")]
    pa_cond = next((c for c in (describe_condition_ru(av) for av in page_avs) if c), None)
    pa_cost = next((cost_from_steps(av_objectives(av), None) for av in page_avs
                    if av_objectives(av)), None)
    if hx_cond or pa_cond:
        added_dt = badge_added_dt(catalog_badge)
        # ...и не раньше, чем через NO_DATE_GRACE_HOURS после появления значка.
        # Обычно SD проставляет даты в первые часы: у WARDOG и WARLORD они
        # появились через 12 минут, и канал получил по ДВА поста на значок —
        # сначала «скоро, даты неизвестны», следом «стартовало». Значку без дат
        # спешить некуда: сказать, когда он начнётся, мы всё равно не можем.
        fresh = added_dt and (now - added_dt) < timedelta(hours=NO_DATE_GRACE_HOURS)
        if added_dt and not fresh and (now - added_dt).days <= NO_DATE_ANNOUNCE_DAYS:
            cat = category_from_description(hx.get("description"))
            href = category_url_for(cx, cat)
            # Описание Twitch категорию называет не всегда («watching Dragon's
            # Dogma 2: Dark Arisen for 1 hour»), а у SD она есть в availability
            # события или страницы. Без этого первый пост TheDragonsDogma
            # (01.10.2026) повёл в Steam вместо категории на Twitch.
            if not cat:
                w_game = next((w for w in windows if w.get("game")), None)
                if w_game:
                    cat, href = w_game["game"], w_game.get("category_href")
                else:
                    for av in page_avs:
                        name = _category_name(av.get("categories") or [])
                        if name:
                            cat, href = name, category_href(cx, av["categories"])
                            break
            login = channel_from_description(hx.get("description"))
            window = {
                # Группа по категории: WARDOG и WARLORD — одна кампания, и без
                # группы они уходили двумя отдельными постами вместо альбома.
                "event_title": cat or "", "group": cat or None, "game": cat or "",
                "start": None, "end": None,
                "cost": pa_cost, "condition": pa_cond or hx_cond,
                "id": None, "all_ids": [],
                "category_href": href,
                "box_art_url": None,
                "twitch_link": ({"label": login, "url": f"https://www.twitch.tv/{login}"}
                                if login else pick_twitch_link((twitch_links or {}).get(set_id),
                                                               next(iter(page_avs), None))),
                "channel_count": 0, "offline_event": False, "dates_coarse": True,
                "dates_unknown": True,
            }
            return {"status": "upcoming", "window": window, "group": window["group"],
                    "note_kind": None}

    return {"status": "ended", "window": None, "group": None, "note_kind": "unknown"}
