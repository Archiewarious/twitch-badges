"""Характеристика СТАРОЙ логики на фикстуре 30.09.2026.

Эти тесты фиксируют, что делает прод сейчас, — вместе с известными ошибками
(помечены ID из реестра проблем плана). Когда исправление меняет поведение,
тест меняется в том же коммите, и это видно в диффе.

Эталон прокрутки времени: tests/golden/legacy_timeline_<tag>.txt.
Перегенерировать: UPDATE_GOLDEN=1 pytest tests/test_legacy_baseline.py
"""
import os
from datetime import timedelta

import mutations as m
import pytest
from conftest import REPO, T0, TAG, load_fixture

GOLDEN = REPO / "tests" / "golden" / f"legacy_timeline_{TAG}.txt"


def fmt_posts(posts, t0):
    out = []
    for p in posts:
        off = p.at - t0
        out.append(f"+{off.days:>2}d{off.seconds // 3600:02d}h {p.kind} x{len(p.media)} | {p.head}")
    return out


def test_fixture_matches_prod_state(fx, legacy):
    """Состояние согласовано с данными: очередь пуста, 37 значков на показе."""
    sim = legacy(**fx)
    assert len(sim.records()) == 406
    assert len(sim.shown()) == 37
    assert sim.tick() == []
    assert sim.state == fx["published"]


@pytest.mark.slow
def test_forty_days_timeline(fx, legacy):
    """Прокрутка на 40 дней на неизменных данных: 17 постов, без дублей."""
    sim = legacy(**fx)
    lines = fmt_posts(sim.run(T0, timedelta(hours=1), timedelta(days=40)), T0)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text("\n".join(lines) + "\n")
    assert lines == GOLDEN.read_text().splitlines()
    assert len(lines) == 17
    assert len(sim.state) == 6          # чистка отработала


def _sim_with(legacy, fx, mutate, now=T0):
    keys = mutate(fx["snapshot"]) or []
    fx["media"]["images"] += keys if isinstance(keys, list) else [keys]
    return legacy(now=now, **fx)


def test_new_badge_announced_once(fx, legacy):
    start, end = T0 + m.days(2), T0 + m.days(20)
    sim = _sim_with(legacy, fx, lambda s: m.new_badge(
        s, "test-new", "Test New", start=start, end=end, added_at=T0 - m.hours(1)))
    posts = sim.tick()
    assert [p.head for p in posts] == ["📣 Скоро новый значок / 🟢 Бесплатный значок Test New"]
    assert sim.tick() == []
    assert sim.state["test-new"]["appeared"]


def test_tiers_go_as_one_album(fx, legacy):
    start, end = T0 - m.hours(1), T0 + m.days(10)
    sim = _sim_with(legacy, fx, lambda s: m.new_campaign(
        s, "Test Tiers", [(f"test-tier-{i}", f"Test Tier {i}") for i in (1, 2, 3)],
        start=start, end=end, added_at=T0 - m.hours(2)))
    posts = sim.tick()
    assert len(posts) == 1 and posts[0].kind == "album" and len(posts[0].media) == 3
    assert posts[0].head == "🎁 Можно получить уже сейчас! / Test Tiers — 3 значка"
    assert sim.tick() == []


def test_no_art_no_post(fx, legacy):
    """Без файла арта в канал не идём и запись не заводим."""
    m.new_badge(fx["snapshot"], "test-noart", "Test No Art", start=T0 + m.days(1),
                end=T0 + m.days(9), added_at=T0 - m.hours(1))
    sim = legacy(**fx)
    assert sim.tick() == []
    assert "test-noart" not in sim.state


def test_A5_condition_with_start_double_post(fx, legacy):
    """A5: условие стало известно вместе со стартом → два поста с тем же текстом."""
    fx["published"]["ampersand"].update(started=False, cond_vague=True, cond_known=False,
                                        ending=False)
    sim = legacy(**fx)
    heads = [p.head for p in sim.tick() + sim.tick() + sim.tick()]
    assert heads == ["▶️ Стартовало — можно получать сейчас! / 🟠 Платный значок Ampersand",
                     "📝 Стало известно, как получить / 🟠 Платный значок Ampersand"]


def test_A3_extension_reannounced_after_gc(fx, legacy):
    """A3: чистка по сохранённому end. Продлили на 20 дней — через неделю после
    старого конца запись удалена, и значок объявляется заново."""
    b = m.find_badge(fx["snapshot"], "wolf-medallion")
    assert b["end_at_date"] == "2026-10-27"
    sim = legacy(**fx)

    def wolf(posts):
        return [p.head for p in posts if "Wolf Medallion" in p.head]

    t = T0 + timedelta(days=26, hours=12)          # последний день по старому окну
    assert wolf(sim.tick(t) + sim.tick(t) + sim.tick(t)) == [
        "⏳ Последний день! Успей получить / 🟠 Платный значок Wolf Medallion"]
    old_end = sim.state["wolf-medallion"]["end"]
    snap = fx["snapshot"]
    m.set_window(snap, "wolf-medallion", end=T0 + m.days(47))
    sim.set_snapshot(snap)
    heads = []
    for h in range(0, 24 * 10, 2):
        heads += wolf(sim.tick(t + timedelta(hours=h)))
    assert old_end == "2026-10-27T08:59:00Z"
    assert heads == ["🎁 Новый значок — можно получить уже сейчас! / "
                     "🟠 Платный значок Wolf Medallion"]


def _orphan_with_art(snap, fx, title, slug, **kw):
    m.orphan_event(snap, title, **kw)
    snap["helix"][slug] = {"title": title, "description": "", "click_url": "",
                           "image_url_4x": m.image_url(slug)}
    if fx is not None:
        fx["media"]["images"].append(m.image_key(slug))


def test_A4_orphan_without_end_reannounced(fx, legacy):
    """A4: у анонса-орфана нет конца окна → настоящий значок не узнаётся
    (return None вместо перехода к следующему орфану) и уходит вторым анонсом."""
    start = T0 + m.days(3)
    _orphan_with_art(fx["snapshot"], fx, "Test Orphan Fest", "test-orphan-fest",
                     start=start, end=None)
    sim = legacy(**fx)
    assert [p.head for p in sim.tick()] == [
        "📣 Скоро новый значок / 🟠 Платный значок Test Orphan Fest"]
    assert sim.state["test-orphan-fest"]["end"] is None

    snap2 = load_fixture()["snapshot"]
    _orphan_with_art(snap2, None, "Test Orphan Fest", "test-orphan-fest", start=start, end=None)
    key = m.new_badge(snap2, "real-badge", "Real Badge", start=start, end=start + m.days(14),
                      added_at=T0 + m.hours(1), cost="paid", objectives=m.SUB_OR_GIFT)
    (sim.workdir / "images" / f"{key}.png").touch()
    sim.set_snapshot(snap2)
    assert [p.head for p in sim.tick(T0 + m.hours(2))] == [
        "📣 Скоро новый значок / 🟠 Платный значок Real Badge"]


def test_orphan_then_badge_no_second_post(fx, legacy):
    """Орфан с полным окном и артом → анонс; настоящий значок с тем же окном
    замещает его без второго поста."""
    start, end = T0 + m.days(3), T0 + m.days(20)
    _orphan_with_art(fx["snapshot"], fx, "Test Orphan Fest", "test-orphan-fest",
                     start=start, end=end)
    sim = legacy(**fx)
    assert [p.head for p in sim.tick()] == [
        "📣 Скоро новый значок / 🟠 Платный значок Test Orphan Fest"]
    assert sim.state["test-orphan-fest"]["from_orphan"]

    snap2 = load_fixture()["snapshot"]
    m.orphan_event(snap2, "Test Orphan Fest", start=start, end=end)
    keys = m.new_badge(snap2, "real-badge", "Real Badge", start=start + m.hours(2),
                       end=end, added_at=T0 + m.hours(1), cost="paid",
                       objectives=m.SUB_OR_GIFT)
    (sim.workdir / "images" / f"{keys}.png").touch()
    sim.set_snapshot(snap2)
    assert sim.tick(T0 + m.hours(2)) == []
    assert sim.state["test-orphan-fest"]["superseded"]
    assert "real-badge" in sim.state


def test_B1_no_date_badge_silent(fx, legacy):
    """B1: новый формат — у значка added_at вместо history, и фолбэк «без дат»
    его не видит: значок с условием от Twitch молчит."""
    key = m.no_date_badge(fx["snapshot"], "test-nodate", "Test No Date",
                          added_at=T0 - m.hours(5),
                          description="This badge was earned by watching Arc Raiders "
                                      "for 30 minutes")
    fx["media"]["images"].append(key)
    sim = legacy(**fx)
    rec = {r["set_id"]: r for r in sim.records()}["test-nodate"]
    assert rec["status"] == "ended" and rec["first_seen"] is None
    assert sim.tick() == []


def test_B10_bits_badge_marked_free(fx, legacy):
    """B10: значок за Bits со страницы SD помечается бесплатным."""
    key = m.bits_page_badge(fx["snapshot"], "test-bits", "Test Bits",
                            start=T0 + m.days(2), end=T0 + m.days(9),
                            added_at=T0 - m.hours(4))
    fx["media"]["images"].append(key)
    sim = legacy(**fx)
    posts = sim.tick()
    assert [p.head for p in posts] == ["📣 Скоро новый значок / 🟢 Бесплатный значок Test Bits"]
    assert "Потратить Bits" in posts[0].caption
