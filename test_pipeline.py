#!/usr/bin/env python3
"""
Тесты пайплайна на РЕАЛЬНОМ снапшоте (data/streamdb_latest.json).

Два вопроса, на которые отвечают эти тесты:
  1. Заметим ли мы, если StreamDatabase снова поменяет формат? За трое суток
     конца августа 2026 он сделал это четырежды, и каждый раз мы узнавали
     постфактум — по упавшему refresh или по молчащим значкам.
  2. Подхватится ли новая кампания, каким бы из способов SD её ни завёл?
     Способов три, и раньше выпадение любого из них означало молчание канала.

Не юнит-тесты: подкладываем изменённые копии снапшота и смотрим на итог —
дошёл ли значок до показа, с каким условием и ссылкой. Так проверяется вся
цепочка, а не отдельная функция.

Запуск: ./venv/bin/python test_pipeline.py
Коды выхода: 0 — всё прошло, 1 — есть провалы.
"""
import contextlib
import copy
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot"))

import check_format as cf  # noqa: E402
import generate_site as site  # noqa: E402

SNAPSHOT = ROOT / "data" / "streamdb_latest.json"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'ok  ' if ok else 'ПРОВАЛ'} {name}" + (f"\n         {detail}" if detail and not ok else ""))


def quiet(fn, *a, **kw):
    """build_records и проверки шумят в stdout/stderr — глушим."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


# ── 1. Ловим ли смену формата ────────────────────────────────────────────────

def format_problems(snap):
    p = cf.Problems()
    cf.check_catalog(p, snap.get("badges") or [])
    cf.check_events(p, snap.get("events") or [])
    cf.check_availability(p, snap.get("events") or [], snap.get("page_availability"))
    cf.check_steps(p, snap.get("events") or [], snap.get("page_availability"))
    quiet(cf.check_records, p, snap)
    return p


def each_availability(snap):
    for e in snap.get("events") or []:
        for b in e.get("twitch_global_badges") or []:
            for av in b.get("availability") or []:
                yield av
    for lst in (snap.get("page_availability") or {}).values():
        for av in lst:
            yield av


def test_format_drift(base):
    print("Смена формата StreamDatabase — замечаем?")

    def wrap_catalog(s):
        s["badges"] = [{"twitchGlobalBadge": b} for b in s["badges"]]

    def drop_steps(s):
        for av in each_availability(s):
            av.pop("steps", None)
            av.pop("objectives", None)

    def break_categories(s):
        for av in each_availability(s):
            av["categories"] = [{"unknown_shape": True} for _ in av.get("categories") or []]

    def drop_hidden(s):
        for av in each_availability(s):
            av.pop("hidden", None)

    def unknown_step(s):
        for av in each_availability(s):
            if av.get("objectives") or av.get("steps"):
                av["objectives"] = [[{"type": "quest_complete", "quest_id": 7}]]
                av.pop("steps", None)
                return

    def drop_added_at(s):
        for b in s["badges"]:
            b.pop("added_at", None)
            b.pop("history", None)

    def catalog_time_format(s):
        for b in s["badges"]:
            for f in ("start_at_time", "end_at_time"):
                if b.get(f):
                    b[f] = b[f][:5].replace(":", ".")

    def drop_catalog_dates(s):
        for b in s["badges"]:
            for f in ("start_at_date", "start_at_time", "end_at_date", "end_at_time"):
                b.pop(f, None)

    def unknown_cost(s):
        for b in s["badges"]:
            if b.get("cost") == "paid":
                b["cost"] = "premium"

    def event_time_format(s):
        for e in s["events"]:
            if e.get("start_at_time"):
                e["start_at_time"] = "6pm"

    def break_image_urls(s):
        for b in s["badges"]:
            v = (b.get("current") or {}).get("version") or {}
            if v.get("image_url_4x"):
                v["image_url_4x"] = "https://cdn.example/new-scheme/abc.png"

    cases = [
        ("каталог завернули в twitchGlobalBadge", wrap_catalog),
        ("условия (objectives/steps) исчезли", drop_steps),
        ("категории сменили форму", break_categories),
        ("пропало поле hidden", drop_hidden),
        ("появился незнакомый тип шага", unknown_step),
        ("Twitch сменил схему URL картинок", break_image_urls),
        ("каталог обвалился", lambda s: s.__setitem__("badges", s["badges"][:10])),
        ("события опустели", lambda s: s.__setitem__("events", [])),
        ("ссылки на категории перестали находиться",
         lambda s: s.__setitem__("category_urls", {})),
        ("пропала дата появления значка (added_at)", drop_added_at),
        ("время в каталоге в незнакомом виде", catalog_time_format),
        ("даты ушли с каталога", drop_catalog_dates),
        ("незнакомое значение цены в каталоге", unknown_cost),
        ("время в событиях в незнакомом виде", event_time_format),
    ]
    for name, mutate in cases:
        snap = copy.deepcopy(base)
        mutate(snap)
        check(name, bool(format_problems(snap)), "поломка прошла незамеченной")

    # Косметическое поле поменяло форму — сбор обязан пережить это молча.
    # 27.09.2026 user_count из {"current": N} стал числом, build_records упал, и
    # данные простояли полтора дня.
    # Время в каталоге бывает и HH:MM, и HH:MM:SS.mmm — оба вида штатные.
    snap = copy.deepcopy(base)
    for b in snap["badges"]:
        for f in ("start_at_time", "end_at_time"):
            if b.get(f):
                b[f] = b[f][:5]
    check("время в каталоге как HH:MM — не поломка", not format_problems(snap),
          "; ".join(format_problems(snap)))

    for name, val in [("user_count — число", 5), ("user_count — строка", "5"),
                      ("user_count — список", [1])]:
        snap = copy.deepcopy(base)
        for b in snap["badges"]:
            b["user_count"] = val
        try:
            quiet(site.build_records, snap)
            ok, why = True, ""
        except Exception as e:
            ok, why = False, repr(e)
        check(f"косметическое поле сменило форму: {name} — сбор не падает", ok, why)

    check("на настоящих данных ложных срабатываний нет",
          not format_problems(copy.deepcopy(base)),
          "; ".join(format_problems(copy.deepcopy(base))))


# ── 2. Подхватим ли новую кампанию ───────────────────────────────────────────

NEW_ID = "__test_new_badge"

# Даты ОТНОСИТЕЛЬНЫЕ. С фиксированным далёким годом тест проваливался на ровном
# месте: бот прячет анонсы дальше UPCOMING_HORIZON_DAYS (45 дней), и «2099»
# честно не показывался.
AVAILABILITY = {
    "hidden": False, "time_limited": True,
    "start_at_time": "17:00", "end_at_time": "17:00",
    "watch": True, "watch_minutes": 20,
    "steps": [[{"type": "watch", "watch_minutes": 20, "watch_days": 3}]],
    "costs": ["free"],
    "categories": [{"id": "490655", "name": "Pokémon GO"}],
    "channels": [],
}


def set_now(now):
    """Точка отсчёта для относительных дат. pytest ставит время фикстуры."""
    global _NOW, _START, _END, _ADDED
    _NOW = now
    _START = (now + timedelta(days=5)).strftime("%Y-%m-%d")
    _END = (now + timedelta(days=25)).strftime("%Y-%m-%d")
    _ADDED = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    AVAILABILITY.update(start_at_date=_START, end_at_date=_END)


set_now(datetime.now(timezone.utc))


def _badge(with_availability=False, catalog_dates=False):
    """Значок в формате каталога SD с ~27.09.2026: added_at, cost, cancelled и
    (если SD их знает) даты прямо на значке, время — HH:MM:SS.mmm."""
    b = {
        "_id": "test", "added": True, "user_count": 0, "removed": False,
        "added_at": _ADDED, "cost": "free", "system": False, "cancelled": False,
        "current": {"set_id": NEW_ID, "version": {
            "id": "1", "title": "Test Badge",
            "image_url_4x": "https://static-cdn.jtvnw.net/badges/v1/"
                            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/3"}},
    }
    if catalog_dates:
        b.update(time_limited=True,
                 start_at_date=_START, start_at_time="17:00:00.000",
                 end_at_date=_END, end_at_time="16:59:59.000")
    if with_availability:
        b["availability"] = [dict(AVAILABILITY)]
    return b


def _host_event(snap):
    """Событие-носитель: берём любое и очищаем — важна не его начинка, а путь."""
    ev = copy.deepcopy((snap.get("events") or [{}])[0])
    ev.update({"title": "Test Campaign", "content": "", "hidden": False,
               "start_at_date": "", "start_at_time": "",
               "end_at_date": "", "end_at_time": "", "twitch_global_badges": []})
    snap["events"].append(ev)
    return ev


def test_new_campaign(base):
    print("\nНовая кампания — подхватим, как бы SD её ни завёл?")

    def linked(s):
        ev = _host_event(s)
        ev["twitch_global_badges"] = [_badge(with_availability=True)]
        s["badges"].append(_badge())

    def page_only(s):
        ev = _host_event(s)
        ev["twitch_global_badges"] = [_badge()]
        s["badges"].append(_badge())
        s.setdefault("page_availability", {})[NEW_ID] = [dict(AVAILABILITY)]

    def event_dates_only(s):
        ev = _host_event(s)
        ev["start_at_date"], ev["end_at_date"] = _START, _END
        ev["content"] = "The Test Badge badge will be available for watching."
        s["badges"].append(_badge())

    def catalog_only(s):
        """Значок только в каталоге: даты на значке, событий нет, условие —
        из описания Twitch (так SD завёл La Velada и EWC)."""
        s["badges"].append(_badge(catalog_dates=True))
        s.setdefault("helix", {})[NEW_ID] = {
            "title": "Test Badge", "click_url": "", "image_url_4x": "",
            "description": "This badge was earned by watching a streamer in the "
                           "Pokémon GO category for 20 minutes"}

    def orphan_no_badge(s):
        """Значка нет вовсе — только событие с датами (случай LEGO)."""
        ev = _host_event(s)
        ev["start_at_date"], ev["end_at_date"] = _START, _END
        ev["title"] = "Test Orphan Campaign"
        ev["content"] = "A badge will be available for subscribing or gifting a subscription."

    import bot as bot_mod

    for name, mutate, expect_id in [
        ("значок привязан к событию, даты в availability", linked, NEW_ID),
        ("даты только на странице значка", page_only, NEW_ID),
        ("значок в каталоге, даты у события, связи нет", event_dates_only, NEW_ID),
        ("значок только в каталоге, даты на самом значке", catalog_only, NEW_ID),
        ("значка ещё нет — только событие с датами", orphan_no_badge, "test-orphan-campaign"),
    ]:
        snap = copy.deepcopy(base)
        mutate(snap)
        recs = {r["set_id"]: r for r in quiet(site.build_records, snap)}
        r = recs.get(expect_id)
        if not r:
            check(name, False, "записи не появилось вовсе")
            continue
        shown = r["status"] in ("active", "upcoming") and bot_mod.is_shown(r)
        check(name, shown and bool(r.get("condition")),
              f"status={r['status']}, показываем={bot_mod.is_shown(r)}, условие={r.get('condition')!r}")


def main() -> int:
    try:
        base = json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError) as e:
        print(f"не прочитать снапшот {SNAPSHOT}: {e}", file=sys.stderr)
        return 1

    test_format_drift(base)
    test_new_campaign(base)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\nитог: {len(results) - len(failed)} из {len(results)} прошло")
    if failed:
        print("провалились:")
        for n in failed:
            print(f"  · {n}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
