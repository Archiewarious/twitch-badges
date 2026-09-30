"""C03: доменная логика перенесена в пакет без изменения поведения.

Записи twitch_badges.domain.records.build_records на фикстуре и мутациях
побайтно равны записям старого generate_site.build_records.
"""
import contextlib
import io
import json
from datetime import timedelta

import mutations as m
import pytest
from conftest import T0, load_fixture

import harness
from twitch_badges.domain.records import RecordsContext, build_records

site = harness.site


def ser(records):
    return json.dumps(records, default=str, ensure_ascii=False, sort_keys=True, indent=0)


def legacy_records(snap, now, known):
    harness._Clock.value = now
    with contextlib.redirect_stderr(io.StringIO()):
        return site.build_records(snap, known_windows=known, overrides={})


def new_records(snap, now, known):
    return build_records(snap, RecordsContext(now=now, known_windows=known, overrides={}))


def _mut_new(s):
    m.new_campaign(s, "Test Tiers", [(f"test-tier-{i}", f"Test Tier {i}") for i in (1, 2)],
                   start=T0 + m.days(1), end=T0 + m.days(9), added_at=T0 - m.hours(2))


def _mut_orphan(s):
    m.orphan_event(s, "Test Orphan Fest", start=T0 + m.days(3), end=T0 + m.days(20))
    m.orphan_event(s, "Creator Week", start=T0 + m.days(1), end=T0 + m.days(6),
                   content="The Glowworm badge will be available for watching in the Arc Raiders category.")


def _mut_nodate(s):
    m.no_date_badge(s, "test-nodate", "Test No Date", added_at=T0 - m.hours(5),
                    description="This badge was earned by watching Arc Raiders for 30 minutes")
    m.bits_page_badge(s, "test-bits", "Test Bits", start=T0 + m.days(2), end=T0 + m.days(9),
                      added_at=T0 - m.hours(4))
    s["badges"].append(m.catalog_badge("test-cat", "Test Catalog", added_at=T0 - m.hours(6),
                                       start=T0 + m.days(1), end=T0 + m.days(8), cost="paid"))


def _mut_extend(s):
    m.set_window(s, "wolf-medallion", end=T0 + m.days(47))
    m.clear_condition(s, "ampersand")
    m.find_badge(s, "chains")["cancelled"] = True


@pytest.mark.parametrize("mutate", [None, _mut_new, _mut_orphan, _mut_nodate, _mut_extend])
@pytest.mark.parametrize("offset_h", [0, 13, 24 * 3 + 5, 24 * 12, 24 * 30])
@pytest.mark.parametrize("with_memory", [True, False])
def test_records_identical(mutate, offset_h, with_memory):
    fx = load_fixture()
    snap = fx["snapshot"]
    if mutate:
        mutate(snap)
    known = fx["known_windows"] if with_memory else {}
    now = T0 + timedelta(hours=offset_h)
    old = ser(legacy_records(json.loads(json.dumps(snap)), now, known))
    new = ser(new_records(json.loads(json.dumps(snap)), now, known))
    assert new == old


def test_pure_no_clock_needed():
    """Новая функция не читает часы: результат зависит только от ctx.now."""
    snap = load_fixture()["snapshot"]
    a = ser(new_records(json.loads(json.dumps(snap)), T0, {}))
    harness._Clock.value = T0 + timedelta(days=100)       # «часы» старого кода не при чём
    b = ser(new_records(json.loads(json.dumps(snap)), T0, {}))
    assert a == b


# ── подписи ──

from twitch_badges.domain.records import build  # noqa: E402
from twitch_badges.publisher import captions as cap  # noqa: E402

bot = harness.bot
KINDS = ["appeared_active", "active_short", "appeared_upcoming", "dates_confirmed",
         "cond_confirmed", "started", "ending"]


def _markup_rows(markup):
    return [[{"text": b.text, "url": b.url} for b in row] for row in markup.inline_keyboard]


@pytest.mark.parametrize("mutate", [None, _mut_new, _mut_orphan, _mut_nodate])
@pytest.mark.parametrize("offset_h", [0, 24 * 12])
def test_captions_identical(mutate, offset_h):
    fx = load_fixture()
    snap = fx["snapshot"]
    if mutate:
        mutate(snap)
    now = T0 + timedelta(hours=offset_h)
    old = legacy_records(json.loads(json.dumps(snap)), now, fx["known_windows"])
    built = build(json.loads(json.dumps(snap)), RecordsContext(now=now, known_windows=fx["known_windows"]))
    urls = built.category_urls
    assert len(old) == len(built.records)
    n_shown = 0
    groups = {}
    for ro, rn in zip(old, built.records):
        assert bot.is_shown(ro) == cap.is_shown(rn, now)
        assert bot.window_vague(ro.get("window")) == cap.window_vague(rn.get("window"))
        assert bot.dedup_key(ro) == cap.dedup_key(rn)
        if not cap.is_shown(rn, now):
            continue
        n_shown += 1
        for kind in KINDS:
            assert cap.channel_caption(kind, rn, urls) == bot.channel_caption(kind, ro)
        assert cap.inline_caption(rn, urls) == bot.inline_caption(ro)
        assert cap.inline_desc(rn) == bot.inline_desc(ro)
        assert cap.channel_buttons(rn) == _markup_rows(bot.channel_buttons(ro))
        assert cap.twitch_buttons(rn) == _markup_rows(bot.twitch_buttons(ro))
        groups.setdefault(rn.get("group"), []).append((ro, rn))
    assert n_shown >= 10
    for g, pairs in groups.items():
        items_o = [(r["set_id"], r) for r, _ in pairs]
        items_n = [(r["set_id"], r) for _, r in pairs]
        for kind in KINDS:
            assert cap.album_caption(items_n, kind, g, urls) == bot.album_caption(items_o, kind, g)
