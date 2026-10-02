"""C08: сбор на заглушке SD/Twitch."""
import json
from datetime import timedelta

import mutations as m
import pytest
from conftest import T0, load_fixture
from fake_sd import FakeSD
from test_migration import make_data_dir

from twitch_badges import alerts, db, state, store
from twitch_badges.collector import Sources, run_once, signature
from twitch_badges.sources import gql
from twitch_badges.sources.helix import Helix
from twitch_badges.sources.http import Http
from twitch_badges.sources.streamdb import StreamDB

pytestmark = pytest.mark.slow


class C:
    def __init__(self, tmp_path, helix=True, deadline=None):
        fx = load_fixture()
        state.migrate_dir(make_data_dir(tmp_path, fx=fx), tmp_path / "tb.sqlite3", now=T0)
        self.conn = db.open_db(tmp_path / "tb.sqlite3")
        self.sd = FakeSD(fx["snapshot"])
        self.tmp = tmp_path
        self.now = T0
        self.helix_on = helix
        self.deadline = deadline
        self.overrides = tmp_path / "overrides.json"

    def sources(self):
        http = self.sd.http(deadline=self.deadline) if self.deadline else self.sd.http()
        self.http = http

        def token_set(v):
            with db.tx(self.conn):
                if v is None:
                    self.conn.execute("DELETE FROM kv WHERE ns='helix_token'")
                else:
                    db.kv_set(self.conn, "helix_token", "app", v)
        helix = Helix(http, "cid", "secret", lambda: db.kv_get(self.conn, "helix_token", "app"),
                      token_set, lambda: self.now) if self.helix_on else None

        def fetch_image(url):
            r = http.get(url, ok_404=True)
            return r.status_code, r.headers.get("content-type"), r.content
        return Sources(StreamDB(http, page_pause=None), helix, gql.Categories(http).lookup, fetch_image)

    def run(self, advance=timedelta(0), **kw):
        self.now += advance
        return run_once(self.conn, self.sources(), now=self.now, images_dir=self.tmp / "images",
                        cards_dir=self.tmp / "cards", overrides_path=self.overrides, **kw)

    def snap(self):
        return store.current_snapshot(self.conn)

    def raised(self):
        return set(alerts.active(self.conn))

    def last_run(self):
        return dict(self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())


@pytest.fixture
def c(tmp_path):
    return C(tmp_path)


def test_first_collect_and_no_change(c):
    r = c.run()
    assert r.action == "collected" and r.reason == "изменились каталог или события"
    snap = c.snap()
    assert len(snap.data["badges"]) == 523 and snap.signature == signature(c.sd.badges, c.sd.events)
    assert len(snap.data["helix"]) == 394
    assert r.stats["pages"] == 22 and r.stats["pages_failed"] == []
    assert r.stats["images"]["downloaded"] > 30 and r.stats["cards"]["written"] > 30
    assert len(db.kv_all(c.conn, "known_windows")) > 40
    assert c.raised() == {"burst"}                               # из перенесённых alerts/
    n = len(c.sd.log)
    r2 = c.run(timedelta(minutes=2))
    assert r2.action == "no-change"
    assert len(c.sd.log) - n <= 3 + 6 * 2                         # опрос + пробы страниц
    r3 = c.run(timedelta(minutes=2))
    assert r3.action == "no-change" and c.last_run()["ok"] == 1


def test_change_and_schedule(c):
    c.run()
    m.set_window({"badges": c.sd.badges, "events": c.sd.events, "page_availability": {}},
                 "wolf-medallion", end=T0 + m.days(40))
    assert c.run(timedelta(minutes=2)).reason == "изменились каталог или события"
    assert c.run(timedelta(minutes=2)).action == "no-change"
    assert c.run(timedelta(minutes=31)).reason == "плановый сбор"


def test_page_probe_triggers_collect(c):
    c.run()
    fresh = "runescape-shrimp"                # показываем, условия со страницы нет
    c.sd.pages[fresh] = {"availability": [], "contexts": [{"content":
        "This badge was awarded between October 2nd 2026 (18:00 UTC) and October 9th 2026 "
        "(18:00 UTC) to people who watched 60 minutes."}]}
    r = c.run(timedelta(minutes=2))
    assert r.action == "collected" and fresh in r.reason
    assert c.snap().data["page_info"][fresh]["start"] == "2026-10-02T18:00:00Z"
    assert c.run(timedelta(minutes=2)).action == "no-change"   # без вечного цикла


@pytest.mark.parametrize("layout", ["split", "split-stale"])
def test_page_layout_split(c, layout):
    """02.10.2026 SD вынес contexts/availabilities из twitchGlobalBadge в pageProps."""
    c.sd.page_layout = layout
    fresh = "runescape-shrimp"
    c.sd.pages[fresh] = {"availability": c.sd.page_avail[fresh], "contexts": [{"content":
        "This badge was awarded between October 2nd 2026 (18:00 UTC) and October 9th 2026 "
        "(18:00 UTC) to people who watched 60 minutes."}]}
    r = c.run()
    assert r.stats["pages_shapeless"] == [] and "format-drift" not in c.raised()
    data = c.snap().data
    assert data["page_availability"] == c.sd.page_avail
    assert data["page_info"][fresh]["start"] == "2026-10-02T18:00:00Z"


def test_page_layout_unknown_keeps_prev_and_alerts(c):
    c.run()
    before = c.snap().data["page_availability"]
    assert before
    c.sd.page_layout = "bare"
    r = c.run(timedelta(minutes=31))
    assert r.action == "collected" and len(r.stats["pages_shapeless"]) == r.stats["pages"]
    assert c.snap().data["page_availability"] == before
    assert "format-drift" in c.raised()
    assert "страницы значков" in alerts.active(c.conn)["format-drift"]["body"]
    c.sd.page_layout = "split"
    c.run(timedelta(minutes=31))
    assert "format-drift" not in c.raised()


def test_sd_down_backoff(c):
    c.run()
    c.sd.fail(r"global-badges\.json", 503, times=100)
    r = c.run(timedelta(minutes=31))
    assert r.action == "failed" and r.reason.startswith("source_http")
    assert c.last_run()["error_kind"] == "source_http"
    assert c.run(timedelta(minutes=1)).action == "skip-backoff"
    fails = []
    for _ in range(8):
        st = db.kv_get(c.conn, "collector", "state")
        fails.append(st["fail_count"])
        c.now = __import__("datetime").datetime.fromisoformat(st["next_try_at"])
        c.run()
    st = db.kv_get(c.conn, "collector", "state")
    gap = __import__("datetime").datetime.fromisoformat(st["next_try_at"]) - c.now
    assert timedelta(minutes=45) <= gap <= timedelta(minutes=72)     # потолок ~60 мин с джиттером
    c.sd.faults.clear()
    c.now = __import__("datetime").datetime.fromisoformat(st["next_try_at"])
    assert c.run().action == "collected"
    assert db.kv_get(c.conn, "collector", "state")["fail_count"] == 0


def test_build_id_rotation(c):
    c.run()
    c.sd.build_id = "build-2"
    r = c.run(timedelta(minutes=31))
    assert r.action == "collected" and c.snap().data["build_id"] == "build-2"


def test_empty_catalog_no_commit(c):
    c.run()
    sid = c.snap().id
    c.sd.badges = []
    r = c.run(timedelta(minutes=31))
    assert r.action == "failed" and "source_format" in r.reason and c.snap().id == sid


def test_catalog_collapse_no_commit(c):
    c.run()
    sid = c.snap().id
    c.sd.badges = c.sd.badges[:100]
    r = c.run(timedelta(minutes=31))
    assert r.action == "failed" and "обвал" in r.reason and c.snap().id == sid


def test_format_drift_alert(c):
    c.run()
    for b in c.sd.badges:
        b.pop("added_at", None)
    r = c.run(timedelta(minutes=31))          # added_at не в сигнатуре — ловит плановый сбор
    assert r.action == "collected" and "format-drift" in c.raised()
    body = alerts.active(c.conn)["format-drift"]["body"]
    assert "дата появления" in body
    c.sd.badges = load_fixture()["snapshot"]["badges"]
    c.run(timedelta(minutes=31))
    assert "format-drift" not in c.raised()


def test_no_drift_when_sd_stops_writing_false_flags(c):
    """02.10.2026 SD убрал cancelled/system (раньше false у каждого значка)."""
    c.run()
    for b in c.sd.badges:
        b.pop("cancelled", None)
        b.pop("system", None)
    c.run(timedelta(minutes=31))
    assert "format-drift" not in c.raised()


def test_format_drift_on_weird_cancelled(c):
    c.run()
    c.sd.badges[0]["cancelled"] = "yes"
    c.run(timedelta(minutes=31))
    assert "format-drift" in c.raised()
    assert "cancelled" in alerts.active(c.conn)["format-drift"]["body"]


def test_format_drift_on_dropped_required_key(c):
    c.run()
    for b in c.sd.badges:
        b.pop("cost", None)
    c.run(timedelta(minutes=31))
    assert "format-drift" in c.raised()
    assert "cost" in alerts.active(c.conn)["format-drift"]["body"]


def test_helix_401_token_refresh(c):
    c.run()
    c.sd.token = "rotated"                      # старый токен больше не принимают
    r = c.run(timedelta(minutes=31))
    assert r.action == "collected" and "helix_error" not in r.stats
    assert len(c.snap().data["helix"]) == 394


def test_helix_auth_broken_keeps_data_and_alerts(c):
    c.run()
    c.sd.oauth_fail = True
    c.sd.token = "rotated"
    for i in range(3):
        r = c.run(timedelta(minutes=31))
        assert r.action == "collected" and r.stats["helix_error"].startswith("auth")
        assert len(c.snap().data["helix"]) == 394            # прошлые данные, а не пусто
        assert ("helix-auth" in c.raised()) == (i == 2)
    c.sd.oauth_fail = False
    c.run(timedelta(minutes=31))
    assert "helix-auth" not in c.raised()


def test_overrides_schema(c):
    c.overrides.write_text(json.dumps({
        "_readme": ["x"],
        "wolf-medallion": {"condition": "Вручную", "link": "https://not-an-object"},
        "ampersand": {"condition": "Ручное условие", "cost": "paid",
                      "link": {"label": "x", "url": "https://www.twitch.tv/x"}}}))
    c.run()
    assert "overrides-invalid" in c.raised()
    assert "wolf-medallion" in alerts.active(c.conn)["overrides-invalid"]["body"]
    assert set(db.kv_get(c.conn, "overrides", "data")) == {"ampersand"}
    c.overrides.write_text("{}")
    c.run(timedelta(minutes=2))
    assert "overrides-invalid" not in c.raised() and db.kv_get(c.conn, "overrides", "data") == {}


def test_deadline_no_commit(tmp_path):
    c = C(tmp_path, deadline=5)
    clock = iter(range(0, 1000, 1))
    sid = c.snap().id

    def sources():
        s = C.sources(c)
        c.http.monotonic = lambda: next(clock)
        c.http.deadline_at = 5
        return s
    c.sources = sources
    r = c.run()
    assert r.action == "failed" and r.reason.startswith("deadline") and c.snap().id == sid


def test_page_errors_keep_previous(c):
    c.run()
    prev = c.snap().data["page_availability"]
    assert prev
    c.sd.fail(r"global-badges/[^/]+/1\.json", 503, times=4 * 30)
    r = c.run(timedelta(minutes=31))
    assert r.action == "collected" and len(r.stats["pages_failed"]) == 22
    assert c.snap().data["page_availability"] == prev


def test_gql_negative_ttl():
    calls = []

    def lookup(names):
        calls.append(list(names))
        return {n: None for n in names}
    now = T0
    cache, urls, names, err = gql.resolve(["Nope"], {"Old": 4}, lookup, now)
    assert cache["Nope"]["missing"] and calls == [["Nope"]]
    gql.resolve(["Nope"], cache, lookup, now + timedelta(days=6))
    assert len(calls) == 1
    gql.resolve(["Nope", "Old"], cache, lookup, now + timedelta(days=8))
    assert calls[-1] == ["Nope", "Old"]


def test_http_retry_after_cap():
    import httpx
    sleeps = []
    n = [0]

    def handler(req):
        n[0] += 1
        return httpx.Response(429, headers={"Retry-After": "3600"}) if n[0] < 3 else httpx.Response(200)
    h = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    assert h.get("https://x.test/").status_code == 200
    assert all(s <= 66 for s in sleeps) and len(sleeps) == 2


def test_no_rsync_ssh_sudo_in_package():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "twitch_badges"
    text = "\n".join(p.read_text() for p in root.rglob("*.py"))
    for bad in ("rsync", "ssh ", "sudo", "SITE_URL"):
        assert bad not in text, bad
