"""C05: сценарии планировщика — таблица из раздела 5.4 плана.

Инвариант всех сценариев: не больше одного поста на (кампания, стадия).
"""
import copy
from datetime import timedelta

import mutations as m
import pytest
from conftest import T0, load_fixture
from newsim import NewSim

from twitch_badges.publisher import planner as P


def sim_with(mutate=None, now=T0, **kw):
    fx = load_fixture()
    if mutate:
        keys = mutate(fx["snapshot"]) or []
        fx["media"]["images"] += keys if isinstance(keys, list) else [keys]
    return NewSim(now=now, **fx, **kw)


def heads(posts):
    return [(p.kind, tuple(sorted(p.keys))) for p in posts]


def ticks(sim, start, hours, step=1):
    out = []
    for h in range(0, hours + 1, step):
        out += sim.tick(start + timedelta(hours=h))
    return out


def assert_no_double_stages(sim):
    seen = set()
    for res in sim.results:
        for intent in res.intents:
            for it in intent.items:
                for st in it.stages:
                    assert (it.campaign_id, st) not in seen, (it.campaign_id, st)
                    seen.add((it.campaign_id, st))


# ── базовое ──

def test_migrated_state_plans_nothing():
    """После переноса состояния на T0 — ни одного поста (dry-run миграции)."""
    sim = sim_with()
    assert sim.tick() == []
    res = sim.results[-1]
    assert res.intents == [] and res.aliases == [] and res.cleanup == []


def test_new_badge_lifecycle():
    start, end = T0 + m.days(2), T0 + m.days(9)
    sim = sim_with(lambda s: m.new_badge(s, "test-new", "Test New", start=start, end=end,
                                         added_at=T0 - m.hours(1)))
    posts = ticks(sim, T0, 24 * 10)
    mine = [(p.kind, (p.at - T0)) for p in posts if "test-new" in p.keys]
    assert mine == [(P.APPEARED_UPCOMING, timedelta(0)),
                    (P.STARTED, timedelta(days=2)),
                    (P.ENDING, timedelta(days=8))]
    assert_no_double_stages(sim)


def test_tiers_one_album():
    sim = sim_with(lambda s: m.new_campaign(
        s, "Test Tiers", [(f"test-tier-{i}", f"Test Tier {i}") for i in (1, 2, 3)],
        start=T0 - m.hours(1), end=T0 + m.days(5), added_at=T0 - m.hours(2)))
    posts = sim.tick()
    assert heads(posts) == [(P.APPEARED_ACTIVE, ("test-tier-1", "test-tier-2", "test-tier-3"))]
    assert "<b>Test Tiers</b> — 3 значка" in posts[0].caption


def test_short_window_one_post():
    sim = sim_with(lambda s: m.new_badge(s, "test-short", "Test Short", start=T0 - m.hours(1),
                                         end=T0 + m.hours(10), added_at=T0 - m.hours(2)))
    posts = ticks(sim, T0, 12)
    assert heads([p for p in posts if "test-short" in p.keys]) == [(P.ACTIVE_SHORT, ("test-short",))]
    assert sim.campaigns["test-short"].stages == {"announce", "started", "ending"}


def test_no_art_no_post_until_art():
    fx = load_fixture()
    m.new_badge(fx["snapshot"], "test-noart", "Test No Art", start=T0 + m.days(1),
                end=T0 + m.days(9), added_at=T0 - m.hours(1))
    sim = NewSim(now=T0, **fx)
    assert sim.tick() == [] and "test-noart" not in sim.campaigns
    assert ("test-noart", None, "нет арта") in sim.results[-1].held
    sim.media["images"].append(m.image_key("test-noart"))
    assert heads(sim.tick(T0 + m.hours(1))) == [(P.APPEARED_UPCOMING, ("test-noart",))]


# ── исправления ──

def test_A5_condition_with_start_single_post():
    fx = load_fixture()
    fx["published"]["ampersand"].update(started=False, cond_vague=True, cond_known=False,
                                        ending=False)
    sim = NewSim(now=T0, **fx)
    posts = ticks(sim, T0, 4)
    assert heads(posts) == [(P.STARTED, ("ampersand",))]
    assert sim.campaigns["ampersand"].known_condition


def test_condition_appears_later():
    """Анонс без условия → «Стало известно, как получить» ровно один раз."""
    base = load_fixture()["snapshot"]
    snap = copy.deepcopy(base)
    m.new_badge(snap, "test-nc", "Test NC", start=T0 + m.days(3), end=T0 + m.days(9),
                added_at=T0 - m.hours(1))
    full = copy.deepcopy(snap)
    m.clear_condition(snap, "test-nc")
    fx = load_fixture()
    fx["snapshot"] = snap
    fx["media"]["images"].append(m.image_key("test-nc"))
    sim = NewSim(now=T0, **fx)
    assert heads(sim.tick()) == [(P.APPEARED_UPCOMING, ("test-nc",))]
    assert sim.campaigns["test-nc"].known_condition is None
    sim.snapshot = full
    posts = ticks(sim, T0 + m.hours(1), 30)
    assert heads([p for p in posts if "test-nc" in p.keys]) == [(P.COND, ("test-nc",))]


def test_B6_nodates_then_dates():
    """Q6: «сроки пока неизвестны» → даты появились → «Уточнили время»."""
    fx = load_fixture()
    key = m.no_date_badge(fx["snapshot"], "test-nodate", "Test No Date",
                          added_at=T0 - m.hours(5),
                          description="This badge was earned by watching Arc Raiders for 30 minutes")
    fx["media"]["images"].append(key)
    sim = NewSim(now=T0, **fx)
    posts = sim.tick()
    assert heads(posts) == [(P.APPEARED_NODATES, ("test-nodate",))]
    assert "сроки пока неизвестны" in posts[0].caption
    assert sim.campaigns["test-nodate"].known_vague is True
    snap = copy.deepcopy(sim.snapshot)
    b = m.find_badge(snap, "test-nodate")
    start, end = T0 + m.days(4), T0 + m.days(10)
    b.update(m.catalog_badge("x", "x", added_at=T0, start=start, end=end))
    b["current"]["set_id"] = "test-nodate"
    b["current"]["version"]["title"] = "Test No Date"
    b["current"]["version"]["image_url_4x"] = m.image_url("test-nodate")
    sim.snapshot = snap
    posts = ticks(sim, T0 + m.hours(1), 24 * 11)
    mine = [(p.kind, p.at - T0) for p in posts if "test-nodate" in p.keys]
    assert mine == [(P.DATES, m.hours(1)), (P.STARTED, m.days(4)), (P.ENDING, timedelta(days=9, hours=0))]


def test_A3_Q7_extension():
    """Продлили после «Последнего дня»: один пост «Продлили», потом новый
    «Последний день» у нового конца. Повторного анонса нет (A3)."""
    sim = sim_with()
    t = T0 + timedelta(days=26, hours=12)
    posts = ticks(sim, t, 3)
    assert (P.ENDING, ("wolf-medallion",)) in heads(posts)
    snap = copy.deepcopy(sim.snapshot)
    new_end = T0 + m.days(47)
    m.set_window(snap, "wolf-medallion", end=new_end)
    sim.snapshot = snap
    posts = ticks(sim, t + m.hours(4), 24 * 25, step=2)
    mine = [(p.kind, p.keys) for p in posts if "wolf-medallion" in p.keys]
    assert mine == [(P.EXTENDED, ["wolf-medallion"]), (P.ENDING, ["wolf-medallion"])]
    ext = next(p for p in posts if p.kind == P.EXTENDED)
    assert "Продлили" in ext.caption and "16 ноября" in ext.caption
    assert {"extended:2026-11-16", "ending:2026-11-16"} <= sim.campaigns["wolf-medallion"].stages
    assert_no_double_stages(sim)


def _orphan(snap, title, **kw):
    m.orphan_event(snap, title, **kw)
    slug = title.lower().replace(" ", "-")
    snap["helix"][slug] = {"title": title, "description": "", "click_url": "",
                           "image_url_4x": m.image_url(slug)}
    return m.image_key(slug)


@pytest.mark.parametrize("orphan_end", [T0 + m.days(20), None])
def test_B5_A4_orphan_to_badge(orphan_end):
    """Орфан объявлен; SD завёл значок и привязал к событию — поста нет,
    стадии переносятся («Стартовало» уходит один раз)."""
    start = T0 + m.days(3)
    fx = load_fixture()
    fx["media"]["images"].append(_orphan(fx["snapshot"], "Test Orphan Fest", start=start, end=orphan_end))
    sim = NewSim(now=T0, **fx)
    assert heads(sim.tick()) == [(P.APPEARED_UPCOMING, ("test-orphan-fest",))]
    snap = load_fixture()["snapshot"]
    sim.media["images"].append(m.new_badge(snap, "real-badge", "Real Badge", start=start + m.hours(1),
                                           end=T0 + m.days(20), added_at=T0 + m.hours(1),
                                           cost="paid", objectives=m.SUB_OR_GIFT,
                                           event_title="Test Orphan Fest"))
    sim.snapshot = snap
    posts = ticks(sim, T0 + m.hours(2), 24 * 4)
    assert sim.aliases == {"real-badge": "test-orphan-fest"}
    assert heads([p for p in posts if "real-badge" in p.keys]) == [(P.STARTED, ("real-badge",))]
    assert_no_double_stages(sim)


def test_orphan_rename():
    """Событие-орфан переименовали — новый слаг, то же окно: второго анонса нет."""
    start, end = T0 + m.days(3), T0 + m.days(20)
    fx = load_fixture()
    fx["media"]["images"].append(_orphan(fx["snapshot"], "Test Orphan Fest", start=start, end=end))
    sim = NewSim(now=T0, **fx)
    assert len(sim.tick()) == 1
    snap = load_fixture()["snapshot"]
    sim.media["images"].append(_orphan(snap, "Test Orphan Festival", start=start, end=end))
    sim.snapshot = snap
    assert sim.tick(T0 + m.hours(1)) == []
    assert sim.aliases == {"test-orphan-festival": "test-orphan-fest"}


def test_unrelated_badge_same_window_not_merged():
    """Орфан ещё жив, и новый значок из другой группы и категории с тем же окном —
    это другая кампания: анонсируется."""
    start, end = T0 + m.days(3), T0 + m.days(20)
    fx = load_fixture()
    fx["media"]["images"].append(_orphan(fx["snapshot"], "Test Orphan Fest", start=start, end=end))
    sim = NewSim(now=T0, **fx)
    sim.tick()
    snap = copy.deepcopy(sim.snapshot)
    sim.media["images"].append(m.new_badge(snap, "other", "Other", start=start, end=end,
                                           added_at=T0, event_title="Other Event"))
    sim.snapshot = snap
    assert heads(sim.tick(T0 + m.hours(1))) == [(P.APPEARED_UPCOMING, ("other",))]


# ── свежесть данных (B8) ──

def test_stale_6h_only_exact_lifecycle():
    fx = load_fixture()
    fx["media"]["images"].append(m.new_badge(fx["snapshot"], "test-new", "Test New",
                                             start=T0 + m.days(2), end=T0 + m.days(9),
                                             added_at=T0 - m.hours(1)))
    sim = NewSim(now=T0, data_at=T0 - m.hours(7), **fx)
    assert sim.tick() == []
    assert ("test-new", P.APPEARED_UPCOMING, "данные несвежие") in sim.results[-1].held
    assert [a.key for a in sim.alerts] == ["posting-paused-stale"]
    # «Стартовало» Ultramarine по точным датам уходит и при данных 7 ч
    posts = ticks(sim, T0 + m.hours(14), 1)
    assert (P.STARTED, ("ultramarine",)) in heads(posts)


def test_stale_48h_nothing():
    sim = sim_with(data_at=T0 - m.hours(49))
    assert ticks(sim, T0 + m.hours(14), 2) == []
    assert "данным" in sim.alerts[0].subject and "остановлен" in sim.alerts[0].subject


# ── очередь ──

def test_burst_and_age_order():
    def mutate(s):
        keys = []
        for i in range(6):
            keys += m.new_campaign(s, f"Burst {i}", [(f"burst-{i}", f"Burst {i}")],
                                   start=T0 + m.days(2), end=T0 + m.days(9),
                                   added_at=T0 - m.hours(10 - i))
        return keys
    sim = sim_with(mutate)
    first = sim.tick()
    assert heads(first) == [(P.APPEARED_UPCOMING, ("burst-0",))]      # самый старый первым
    assert any(a.key == "burst" for a in sim.alerts)
    order = [p.keys[0] for p in ticks(sim, T0 + m.hours(1), 6)]
    assert order == [f"burst-{i}" for i in range(1, 6)]


def test_pause():
    sim = sim_with(lambda s: m.new_badge(s, "test-new", "Test New", start=T0 + m.days(2),
                                         end=T0 + m.days(9), added_at=T0 - m.hours(1)),
                   cfg=P.PlanConfig(paused=True))
    assert sim.tick() == []
    assert ("test-new", P.APPEARED_UPCOMING, "пауза владельца") in sim.results[-1].held


def test_quiet_hours_hold_nonurgent():
    cfg = P.PlanConfig(quiet_start=23, quiet_end=9)
    night = T0.replace(hour=22)                    # 01:00 МСК
    sim = sim_with(lambda s: m.new_badge(s, "test-new", "Test New", start=night + m.days(2),
                                         end=night + m.days(9), added_at=night - m.hours(1)),
                   now=night, cfg=cfg)
    assert sim.tick() == []
    assert heads(sim.tick(night + m.hours(9))) == [(P.APPEARED_UPCOMING, ("test-new",))]


# ── чистка ──

def test_cleanup_after_30_days_not_live():
    sim = sim_with()
    ticks(sim, T0, 24 * 60, step=24)
    left = set(sim.campaigns)
    # всё, что закончилось больше 30 дней назад, вычищено; живые на T0+60д остались
    assert "wardog" not in left and "wolf-medallion" not in left
    live_now = {p for p in left}
    assert all(sim.campaigns[c].last_seen_live_at >= "2026-10-29" for c in live_now)


def test_cleanup_keeps_recent():
    sim = sim_with()
    ticks(sim, T0, 24 * 10, step=24)
    assert "wolf-medallion" in sim.campaigns and "wardog" in sim.campaigns
