"""C06: outbox на фейковом Telegram — в каждом сценарии ровно один пост."""
import asyncio
from datetime import timedelta

import mutations as m
import pytest
from conftest import T0, load_fixture
from fake_telegram import FakeTelegram
from test_migration import make_data_dir

from twitch_badges import db, owner, state, store
from twitch_badges.lock import AlreadyRunning, InstanceLock
from twitch_badges.publisher import planner
from twitch_badges.publisher.outbox import Outbox
from twitch_badges.publisher.service import tick


class Env:
    """Мигрированная БД + фейковый Telegram + часы, которыми управляет тест."""

    def __init__(self, tmp_path, mutate=None, storage=True, last_id=99):
        Env.n = getattr(Env, "n", 0) + 1
        tmp_path = tmp_path / f"env{Env.n}"
        tmp_path.mkdir()
        fx = load_fixture()
        self.images = set(fx["media"]["images"])
        if mutate:
            keys = mutate(fx["snapshot"]) or []
            self.images |= set(keys if isinstance(keys, list) else [keys])
        d = make_data_dir(tmp_path, fx=fx)
        self.path = tmp_path / "tb.sqlite3"
        state.migrate_dir(d, self.path, now=T0)
        self.now = T0
        self.tg = FakeTelegram()
        self.tg.now = lambda: self.now.timestamp()
        self.bot = self.tg.bot()
        self.alerts = []
        self.storage = self.tg.storage_id if storage else None
        self.conn = db.open_db(self.path)
        if last_id is not None:
            with db.tx(self.conn):
                db.kv_set(self.conn, "channel", "last_message_id", last_id)
        self.outbox = self.make_outbox()
        # снапшот «свежий» относительно часов теста
        self.conn.execute("UPDATE snapshots SET committed_at=?", (db.ts(T0),))

    def make_outbox(self):
        async def nosleep(*a):
            pass
        return Outbox(self.conn, self.bot, channel_id=self.tg.channel_id,
                      storage_chat_id=self.storage, alert=self.alerts.append,
                      sleep=nosleep, clock=lambda: self.now)

    def has_art(self, r):
        from twitch_badges.domain.catalog import image_cache_key
        return image_cache_key(r.get("image") or "") in self.images

    def tick(self, advance=timedelta(0)):
        self.now += advance
        self.conn.execute("UPDATE snapshots SET committed_at=?", (db.ts(self.now),))
        return asyncio.run(tick(self.conn, self.outbox, now=self.now, has_art=self.has_art,
                                cfg=planner.PlanConfig()))

    def rows(self):
        return [tuple(r) for r in self.conn.execute("SELECT id, status FROM outbox ORDER BY id")]

    def raised(self):
        return [a.key for a in self.alerts if a.active]


def new_badge(s):
    return m.new_badge(s, "test-new", "Test New", start=T0 + m.days(2), end=T0 + m.days(9),
                       added_at=T0 - m.hours(1))


def tiers(n):
    def mutate(s):
        return m.new_campaign(s, "Test Tiers", [(f"tier-{i:02d}", f"Tier {i:02d}") for i in range(n)],
                              start=T0 + m.days(2), end=T0 + m.days(9), added_at=T0 - m.hours(1))
    return mutate


@pytest.fixture
def env(tmp_path):
    return lambda **kw: Env(tmp_path, **kw)


def settle(e, ticks=6, step=timedelta(minutes=2)):
    for _ in range(ticks):
        e.tick(step)


def test_normal(env):
    e = env(mutate=new_badge)
    r = e.tick()
    assert r.enqueued == [1] and e.rows() == [(1, "sent")]
    assert e.tg.posts_count() == 1
    c = store.load_campaigns(e.conn)["test-new"]
    assert c.stages == {"announce"} and c.known_start == "2026-10-02T17:20:00Z"
    assert db.kv_get(e.conn, "channel", "last_message_id") == 100
    settle(e)
    assert e.tg.posts_count() == 1


@pytest.mark.parametrize("fault", ["lost_response", "502_after"])
def test_delivered_but_response_lost(env, fault):
    e = env(mutate=new_badge)
    e.tg.fail("sendPhoto", fault)
    e.tick()
    assert e.rows() == [(1, "unknown")] and e.tg.posts_count() == 1
    e.tick(timedelta(minutes=2))
    assert e.rows() == [(1, "sent")]
    settle(e)
    assert e.tg.posts_count() == 1
    assert e.tg.chats[e.tg.storage_id].messages == {}          # пересланные копии удалены
    assert "post-unknown" not in e.raised()


@pytest.mark.parametrize("fault", ["connect_error", "pool_timeout", "429:40"])
def test_not_sent_then_retry(env, fault):
    e = env(mutate=new_badge)
    e.tg.fail("sendPhoto", fault)
    e.tick()
    assert e.rows() == [(1, "retry")] and e.tg.posts_count() == 0
    settle(e, ticks=3, step=timedelta(minutes=1))
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1


def test_bad_request_stops_and_alerts(env):
    e = env(mutate=new_badge)
    e.tg.fail("sendPhoto", "400:Bad Request: can't parse entities")
    e.tick()
    assert e.rows() == [(1, "failed")] and "post-failed" in e.raised()
    settle(e)
    assert e.tg.posts_count() == 0 and e.rows() == [(1, "failed")]
    # «выкатили исправление» — перезапуск: один повтор
    e.outbox = e.make_outbox()
    e.outbox.recover()
    e.tick(timedelta(minutes=2))
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1


def test_forbidden_then_restored(env):
    e = env(mutate=new_badge)
    e.tg.chats[e.tg.channel_id].bot_is_admin = False
    e.tick()
    assert e.rows() == [(1, "retry")] and "channel-forbidden" in e.raised()
    settle(e, ticks=5)
    assert e.tg.posts_count() == 0
    e.tg.chats[e.tg.channel_id].bot_is_admin = True
    e.tick(timedelta(minutes=31))
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1
    assert any(a.key == "channel-forbidden" and not a.active for a in e.alerts)


@pytest.mark.parametrize("delivered", [True, False])
def test_killed_between_sending_and_sent(env, delivered):
    """Процесс убит: после доставки до записи в БД (или до самой отправки).
    Новый процесс на той же БД — ровно один пост."""
    e = env(mutate=new_badge)

    class Killed(BaseException):
        pass

    ob = e.outbox
    if delivered:
        def boom(*a, **k):
            raise Killed
        ob._part_done = boom
    else:
        async def boom(*a, **k):
            raise Killed
        ob._send_part = boom
    with pytest.raises(Killed):
        e.tick()
    assert e.rows() == [(1, "sending")]
    assert e.tg.posts_count() == (1 if delivered else 0)
    e.outbox = e.make_outbox()                 # новый процесс
    e.outbox.recover()
    settle(e, ticks=2)
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1


def test_protected_channel_asks_owner(env):
    e = env(mutate=new_badge)
    e.tg.chats[e.tg.channel_id].protected = True
    e.tg.fail("sendPhoto", "lost_response")
    e.tick()
    e.tick(timedelta(minutes=2))
    assert e.rows() == [(1, "unknown")] and "post-unknown" in e.raised()
    settle(e)
    assert e.tg.posts_count() == 1
    reply = owner.handle(e.conn, e.outbox, "/sent 1", e.now)
    assert "отправленным" in reply and e.rows() == [(1, "sent")]
    settle(e)
    assert e.tg.posts_count() == 1


def test_protected_channel_resend(env):
    e = env(mutate=new_badge)
    e.tg.chats[e.tg.channel_id].protected = True
    e.tg.fail("sendPhoto", "lost_response")
    e.tick()
    # владелец видит, что поста нет (в фейке он есть — удалим его, как будто не дошёл)
    e.tg.chats[e.tg.channel_id].messages.clear()
    e.tick(timedelta(minutes=2))
    assert "post-unknown" in e.raised()
    owner.handle(e.conn, e.outbox, "/resend 1", e.now)
    e.tick(timedelta(minutes=2))
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1


def test_no_storage_or_last_id_asks_owner(env):
    for kw in ({"storage": False}, {"last_id": None}):
        e = env(mutate=new_badge, **kw)
        e.tg.fail("sendPhoto", "lost_response")
        e.tick()
        e.tick(timedelta(minutes=2))
        assert e.rows() == [(1, "unknown")] and "post-unknown" in e.raised()
        settle(e)
        assert e.tg.posts_count() == 1


def test_album_second_part_lost(env):
    e = env(mutate=tiers(12))
    e.tg.fail("sendMediaGroup", "lost_response")
    e.tg.faults.insert(0, ("sendMediaGroup", "none"))          # первая часть проходит
    e.tick()
    assert e.rows() == [(1, "unknown")]
    e.tick(timedelta(minutes=2))
    assert e.rows() == [(1, "sent")]
    assert e.tg.posts_count() == 2 and len(e.tg.channel_posts()) == 12
    camps = store.load_campaigns(e.conn)
    assert all("announce" in camps[f"tier-{i:02d}"].stages for i in range(12))
    assert all(camps[f"tier-{i:02d}"].known_start for i in range(12))


def test_long_caption_fits(env):
    def mutate(s):
        k = new_badge(s)
        b = m.find_badge(s, "test-new")
        b["current"]["version"]["title"] = "Very Long Badge Name " * 8
        ev = m.find_event(s, "Test New")
        av = ev["twitch_global_badges"][0]["availability"][0]
        av["categories"] = [{"id": str(i), "name": f"Category Number {i}"} for i in range(30)]
        av["objectives"] = [[{"type": "watch", "watch_minutes": 30}]]
        return k
    e = env(mutate=mutate)
    e.tick()
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1


def test_no_pileup_while_telegram_down(env):
    def mutate(s):
        keys = []
        for i in range(3):
            keys += m.new_campaign(s, f"G{i}", [(f"g-{i}", f"G {i}")], start=T0 + m.days(2),
                                   end=T0 + m.days(9), added_at=T0 - m.hours(5 - i))
        return keys
    e = env(mutate=mutate)
    e.tg.fail("sendPhoto", "connect_error", times=6)
    settle(e, ticks=6, step=timedelta(minutes=2))
    assert len(e.rows()) == 1                      # не копим очередь, пока Telegram лежит
    settle(e, ticks=12, step=timedelta(minutes=10))
    assert [s for _, s in e.rows()] == ["sent"] * 3 and e.tg.posts_count() == 3


def test_expired_row_replanned(env):
    e = env(mutate=new_badge)
    e.tg.fail("sendPhoto", "connect_error", times=100)
    e.tick()
    e.tick(timedelta(hours=7))
    assert e.rows()[0] == (1, "failed")
    assert "announce" not in store.load_campaigns(e.conn)["test-new"].stages
    e.tg.faults.clear()
    e.tick(timedelta(minutes=2))
    assert e.rows() == [(1, "failed"), (2, "sent")] and e.tg.posts_count() == 1


def test_owner_pause_resume_status(env):
    e = env(mutate=new_badge)
    assert "остановлены" in owner.handle(e.conn, e.outbox, "/pause", e.now)
    e.tick()
    assert e.rows() == [] and e.tg.posts_count() == 0
    txt = owner.handle(e.conn, e.outbox, "/status", e.now)
    assert "на паузе" in txt and "Outbox: пусто" in txt
    owner.handle(e.conn, e.outbox, "/resume", e.now)
    e.tick(timedelta(minutes=2))
    assert e.tg.posts_count() == 1
    assert "Последний пост: #1" in owner.handle(e.conn, e.outbox, "/status", e.now)
    assert owner.handle(e.conn, e.outbox, "/sent", e.now).startswith("Нужен номер")
    assert owner.handle(e.conn, e.outbox, "/sent 99", e.now) == "Поста #99 нет"


def test_stage_never_twice_even_if_planned_again(env):
    e = env(mutate=new_badge)
    e.tick()
    intent = planner.Intent(planner.APPEARED_UPCOMING, None, [planner.Item(
        "test-new", {"set_id": "test-new", "title": "x", "window": {}}, ["announce"], {})])
    assert e.outbox.enqueue(intent, {}) is None


def test_instance_lock(tmp_path):
    with InstanceLock(tmp_path / "bot.lock"):
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path / "bot.lock").acquire()
    with InstanceLock(tmp_path / "bot.lock"):
        pass


# ── лимиты подписей (B4) ──

from twitch_badges.publisher import captions as cap  # noqa: E402


def _rec(**kw):
    r = {"set_id": "x", "title": "Badge", "status": "active", "cost": "paid", "group": None,
         "condition": "Смотреть эфир " + "очень длинное условие " * 120,
         "window": {"start": T0, "end": T0 + m.days(3), "game": "", "category_href": None},
         "art_placeholder_from": "subtember-2025"}
    r.update(kw)
    return r


@pytest.mark.parametrize("kind", ["appeared_active", "started", "ending", "extended"])
def test_fit_channel_caption(kind):
    r = _rec()
    assert cap.tg_len(cap.channel_caption(kind, r)) > 1024
    out = cap.fit_channel_caption(kind, r)
    assert cap.tg_len(out) <= 1024 and "…" in out
    assert "Badge" in out


def test_fit_long_title_and_album():
    r = _rec(title="Name " * 400)
    assert cap.tg_len(cap.fit_channel_caption("started", r)) <= 1024
    assert cap.tg_len(cap.fit_album_caption([("x", r)], "ending", None)) <= 1024


def test_short_caption_untouched():
    r = _rec(condition="Смотреть эфир 30 минут")
    assert cap.fit_channel_caption("started", r) == cap.channel_caption("started", r)
