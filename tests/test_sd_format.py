"""C02: формат SD с ~27.09.2026 и контентные ошибки (B1, A7, A8, B10, B11).

Всё на старом коде (generate_site, fetch_streamdb, poll_changes, check_format):
это кандидат на хотфикс в прод до переписывания.
"""
import copy
from datetime import datetime, timezone

import mutations as m
import pytest
from conftest import T0, load_fixture

import harness
from harness import FrozenDatetime

site = harness.site
bot = harness.bot
import check_format as cf  # noqa: E402
import fetch_streamdb as collector  # noqa: E402
import poll_changes as poll  # noqa: E402


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def raised(alerts):
    """Ключи поднятых тревог (снятия — не тревога)."""
    return [a.key for a in alerts if not a.cleared]


# ── время SD ──

@pytest.mark.parametrize("time_s, expected", [
    ("13:05", utc(2026, 9, 30, 13, 5)),
    ("13:05:59", utc(2026, 9, 30, 13, 5, 59)),
    ("13:05:59.000", utc(2026, 9, 30, 13, 5, 59)),
    ("9:05", utc(2026, 9, 30, 9, 5)),
    ("", utc(2026, 9, 30)),
    (None, utc(2026, 9, 30)),
    ("6pm", None),
    ("13.05", None),
    ("25:00", None),
])
def test_parse_dt_formats(time_s, expected):
    assert site.parse_dt("2026-09-30", time_s) == expected


def test_parse_dt_bad_date():
    assert site.parse_dt("30.09.2026", "13:00") is None
    assert site.parse_dt("", "13:00") is None


# ── дата появления значка ──

def test_added_at_fallback():
    b = {"added_at": "2026-09-28T12:52:49.244Z"}
    assert site.badge_first_seen(b) == "2026-09-28T12:52:49.244Z"
    assert collector._badge_added_at(b) == "2026-09-28T12:52:49.244Z"
    assert site.badge_added_dt(b) == utc(2026, 9, 28, 12, 52, 49, 244000)


def test_history_still_wins():
    b = {"added_at": "2026-09-28T00:00:00Z",
         "history": [{"type": "added", "timestamp": "2026-01-02T00:00:00Z"},
                     {"type": "added", "timestamp": "2026-03-04T00:00:00Z"}]}
    assert site.badge_first_seen(b) == "2026-01-02T00:00:00Z"         # первое появление
    assert collector._badge_added_at(b) == "2026-03-04T00:00:00Z"      # последнее добавление


def test_no_added_date():
    assert site.badge_first_seen({"added_at": None}) is None
    assert collector._badge_added_at({}) is None
    assert site.badge_added_dt({"added_at": "garbage"}) is None


# ── эффект на фикстуре (раздел 7.0 плана) ──

def _records(fx, legacy, now=T0):
    sim = legacy(now=now, **fx)
    return sim, {r["set_id"]: r for r in sim.records()}


def test_effect_on_fixture(fx, legacy):
    """Вернулись два значка RuneFest «без дат»; оба уже в published → 0 постов."""
    sim, recs = _records(fx, legacy)
    for sid in ("runescape-shrimp", "yellow-party-hat"):
        r = recs[sid]
        assert r["status"] == "upcoming" and bot.is_shown(r)
        assert r["window"]["start"] is None and r["condition"] == "Смотреть эфир 1 час"
        assert sid in fx["published"]
    assert sim.tick() == []


def test_blue_creeper_boss_trap(fx, legacy):
    """Ловушка: без дат каталога этот значок вернулся бы «скоро» (кончился 22.09)."""
    _, recs = _records(fx, legacy)
    r = recs["blue-creeper-boss"]
    assert r["status"] == "ended"
    assert r["window"]["end"] == utc(2026, 9, 22, 6, 58)
    assert r["window"]["from_catalog"]


def test_catalog_only_badge(fx, legacy):
    """Значок, который SD знает только по каталогу (как La Velada, EWC)."""
    start, end = T0 + m.days(1), T0 + m.days(8)
    fx["snapshot"]["badges"].append(m.catalog_badge(
        "test-cat", "Test Catalog", added_at=T0 - m.hours(6), start=start, end=end, cost="paid"))
    fx["media"]["images"].append(m.image_key("test-cat"))
    sim, recs = _records(fx, legacy)
    r = recs["test-cat"]
    assert r["status"] == "upcoming" and r["cost"] == "paid"
    assert (r["window"]["start"], r["window"]["end"]) == (start, end)
    assert not r["window"]["dates_coarse"]
    assert [p.head for p in sim.tick()] == ["📣 Скоро новый значок / 🟠 Платный значок Test Catalog"]


def test_catalog_dates_fill_empty_event_window(fx, legacy):
    """Событие есть, но его availability без дат: даты берутся из каталога,
    а не из разбора текста или дат события."""
    snap = fx["snapshot"]
    start, end = T0 - m.hours(2), T0 + m.days(5)
    m.new_badge(snap, "test-evnodate", "Test Ev No Date", start=start, end=end,
                added_at=T0 - m.days(1))
    ev = m.find_event(snap, "Test Ev No Date")
    av = ev["twitch_global_badges"][0]["availability"][0]
    for f in ("start_at_date", "start_at_time", "end_at_date", "end_at_time"):
        av[f] = ""
    ev["start_at_date"], ev["end_at_date"] = "2026-01-01", "2026-01-02"   # неверные даты события
    av["objectives"] = m.SUB_OR_GIFT                           # цена события точнее
    m.find_badge(snap, "test-evnodate")["cost"] = None
    _, recs = _records(fx, legacy)
    assert recs["test-evnodate"]["cost"] == "paid"
    w = recs["test-evnodate"]["window"]
    assert (w["start"], w["end"]) == (start, end) and w["from_catalog"]
    assert recs["test-evnodate"]["status"] == "active"


def test_cancelled_is_ended(fx, legacy):
    """B11: отменённая кампания не анонсируется и не считается слепой зоной."""
    snap = fx["snapshot"]
    m.new_badge(snap, "test-cancel", "Test Cancel", start=T0 + m.days(2),
                end=T0 + m.days(9), added_at=T0 - m.days(2))
    m.find_badge(snap, "test-cancel")["cancelled"] = True
    fx["media"]["images"].append(m.image_key("test-cancel"))
    sim, recs = _records(fx, legacy)
    r = recs["test-cancel"]
    assert r["status"] == "ended" and r["note_kind"] == "cancelled"
    assert bot.hidden_reason(r) == "кампания отменена"
    assert sim.tick() == []
    assert raised(sim.monitor()) == []


def test_A7_archive_badge_not_matched_by_event_text(fx, legacy):
    """A7: архивный «Artist» не получает даты нового события, где есть это слово."""
    snap = fx["snapshot"]
    m.orphan_event(snap, "Creator Week", start=T0 + m.days(1), end=T0 + m.days(6),
                   content="Every Artist can earn a badge by streaming this week.")
    assert m.find_badge(snap, "artist-badge").get("added_at") is None
    _, recs = _records(fx, legacy)
    assert recs["artist-badge"]["status"] == "ended"
    assert not (recs["artist-badge"]["window"] or {}).get("from_event_content")


def test_A7_fresh_badge_still_matched(fx, legacy):
    snap = fx["snapshot"]
    snap["badges"].append(m.catalog_badge("test-fresh", "Glowworm", added_at=T0 - m.days(3)))
    m.orphan_event(snap, "Glow Week", start=T0 + m.days(1), end=T0 + m.days(6),
                   content="The Glowworm badge will be available for watching.")
    _, recs = _records(fx, legacy)
    assert recs["test-fresh"]["window"]["from_event_content"]
    assert recs["test-fresh"]["status"] == "upcoming"


def test_A7_old_badge_by_added_at_not_matched(fx, legacy):
    snap = fx["snapshot"]
    snap["badges"].append(m.catalog_badge("test-old", "Glowworm", added_at=T0 - m.days(40)))
    m.orphan_event(snap, "Glow Week", start=T0 + m.days(1), end=T0 + m.days(6),
                   content="The Glowworm badge will be available for watching.")
    _, recs = _records(fx, legacy)
    assert recs["test-old"]["status"] == "ended"


# ── A8: год в тексте страницы ──

PAGE = "This badge was awarded between July 10th 2025 (18:00 UTC) and July 20th 2025 (18:00 UTC)."


def test_A8_copy_paste_year_fixed():
    info = collector.parse_badge_page_text(PAGE, "2026-07-01T00:00:00Z")
    assert info["start"] == "2026-07-10T18:00:00Z"


def test_A8_added_after_timeframe_keeps_year():
    text = PAGE + " This badge was added after the timeframe."
    info = collector.parse_badge_page_text(text, "2026-07-01T00:00:00Z")
    assert info["start"] == "2025-07-10T18:00:00Z" and info["too_late"]


def test_A8_catalog_window_before_added_keeps_year():
    info = collector.parse_badge_page_text(PAGE, "2026-07-01T00:00:00Z", "2025-07-20")
    assert info["start"] == "2025-07-10T18:00:00Z"


def test_page_parse_args():
    b = {"added_at": "2026-09-01T00:00:00Z", "end_at_date": "2026-09-20"}
    assert collector.page_parse_args(b) == ("2026-09-01T00:00:00Z", "2026-09-20")
    assert collector.page_parse_args(None) == (None, None)


# ── B10 ──

@pytest.mark.parametrize("kind, cost", [("sub", "paid"), ("purchase", "paid"),
                                        ("bits", "paid"), ("watch", "free"), (None, None)])
def test_page_kind_cost(fx, legacy, kind, cost):
    snap = fx["snapshot"]
    m.bits_page_badge(snap, "test-kind", "Test Kind", start=T0 + m.days(2),
                      end=T0 + m.days(9), added_at=T0 - m.hours(4))
    snap["page_info"]["test-kind"]["kind"] = kind
    _, recs = _records(fx, legacy)
    assert recs["test-kind"]["cost"] == cost


# ── сканирование страниц и опрос ──

def test_page_scan_covers_catalog_badges(monkeypatch):
    """Страниц за сбор: 11 → 22 (свежие значки вне событий снова сканируются)."""
    snap = load_fixture()["snapshot"]
    asked = []
    monkeypatch.setattr(collector, "datetime", FrozenDatetime)
    monkeypatch.setattr(collector, "fetch_badge_page", lambda b, sid: asked.append(sid))
    harness._Clock.value = T0
    collector.collect_badge_pages("build", snap["events"], snap["badges"])
    assert len(asked) == 22
    assert {"blue-creeper-boss", "clipped-that", "harley-mayhem"} <= set(asked)


def _sig(snap):
    return poll.signature(snap["badges"], snap["events"])


@pytest.mark.parametrize("change", [
    lambda b: b.update(end_at_date="2026-11-30"),
    lambda b: b.update(end_at_time="10:00:00.000"),
    lambda b: b.update(cancelled=True),
    lambda b: b.update(cost="free"),
])
def test_poll_signature_sees_catalog_fields(change):
    snap = load_fixture()["snapshot"]
    before = _sig(snap)
    change(m.find_badge(snap, "wolf-medallion"))
    assert _sig(snap) != before


def test_poll_signature_ignores_user_count():
    snap = load_fixture()["snapshot"]
    before = _sig(snap)
    m.find_badge(snap, "wolf-medallion")["user_count"] = 999999
    assert _sig(snap) == before


# ── монитор слепых зон снова живой ──

def test_blindspots_quiet_on_fixture(fx, legacy):
    """После оживления монитора тревог на реальных данных нет."""
    assert raised(legacy(**fx).monitor()) == []


def test_blindspots_alive(fx, legacy):
    """Свежий значок без дат и без условия — монитор это видит (до C02 молчал)."""
    fx["snapshot"]["badges"].append(m.catalog_badge(
        "test-silent", "Test Silent", added_at=T0 - m.days(2)))
    assert raised(legacy(**fx).monitor()) == ["blindspots"]


# ── проверка формата ──

def problems(snap):
    p = cf.Problems()
    cf.check_catalog(p, snap.get("badges") or [])
    cf.check_events(p, snap.get("events") or [])
    return p


def test_format_ok_on_fixture():
    assert problems(load_fixture()["snapshot"]) == []


@pytest.mark.parametrize("mutate, needle", [
    (m.drop_added_at, "дата появления"),
    (m.drop_catalog_dates, "даты прямо на значке"),
    (m.rename_catalog_time, "даты не разбираются"),
])
def test_format_drift_detected(mutate, needle):
    snap = load_fixture()["snapshot"]
    mutate(snap)
    assert any(needle in p for p in problems(snap))


def test_short_catalog_time_is_fine():
    snap = load_fixture()["snapshot"]
    m.old_time_format(snap)
    assert problems(snap) == []


def test_check_records_ignores_known_windows(tmp_path, fx):
    """Канарейка не опирается на память окон."""
    harness._Clock.value = T0
    site.KNOWN_WINDOWS_FILE = tmp_path / "boom.json"
    site.KNOWN_WINDOWS_FILE.write_text("{not json")        # прочитали бы — было бы {} тихо
    called = []
    orig = site.load_known_windows
    site.load_known_windows = lambda: called.append(1) or orig()
    try:
        p = cf.Problems()
        cf.check_records(p, copy.deepcopy(fx["snapshot"]))
    finally:
        site.load_known_windows = orig
    assert called == [] and p == []
