"""C09: процесс бота на фейковом Telegram."""
import asyncio
import socket
import threading
import time

import pytest
from conftest import T0
from fake_telegram import TOKEN, FakeRequest, FakeTelegram
from PIL import Image
from telegram import Update
from test_migration import make_data_dir

from twitch_badges import bot as botmod
from twitch_badges import config, db, sdnotify, state
from twitch_badges.domain.catalog import image_cache_key
from twitch_badges.lock import InstanceLock


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state.migrate_dir(make_data_dir(tmp_path), tmp_path / "twitch_badges.sqlite3", now=T0)
    tg = FakeTelegram()
    cfg = config.load(env={"DATA_DIR": str(tmp_path), "TELEGRAM_BOT_TOKEN": TOKEN,
                           "TELEGRAM_CHANNEL_ID": str(tg.channel_id),
                           "ALERT_CHAT_ID": str(tg.owner_id), "PUBLISH_ENABLED": "false"})
    conn = db.open_db(cfg.db_path)
    b = botmod.BotApp(cfg, conn)
    app = b.build(request=FakeRequest(tg), updates_request=FakeRequest(tg))
    return tg, cfg, conn, b, app


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class NotifyListener:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.settimeout(0.2)
        self.events = []
        self.stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self.stop:
            try:
                data = self.sock.recv(4096).decode()
            except socket.timeout:
                continue
            self.events.append((time.monotonic(), data))

    def pings(self):
        return [t for t, d in self.events if "WATCHDOG=1" in d]


def test_watchdog_pings_stop_when_loop_blocked(setup, tmp_path, monkeypatch):
    tg, cfg, conn, b, app = setup
    lst = NotifyListener(tmp_path / "notify.sock")
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify.sock"))
    monkeypatch.setattr(sdnotify, "watchdog_interval", lambda: 0.1)

    async def scenario():
        await app.initialize()
        await b.post_init(app)
        await app.start()
        await asyncio.sleep(2.0)
        blocked_at = time.monotonic()
        time.sleep(1.5)                           # цикл событий заблокирован
        await asyncio.sleep(1.0)
        await app.stop()
        await app.shutdown()
        return blocked_at
    blocked_at = _run(scenario())
    lst.stop = True
    assert any("READY=1" in d for _, d in lst.events)
    pings = lst.pings()
    before = [t for t in pings if t < blocked_at]
    during = [t for t in pings if blocked_at + 0.05 < t < blocked_at + 1.45]
    after = [t for t in pings if t > blocked_at + 1.5]
    assert len(before) >= 5 and during == [] and len(after) >= 3
    assert (tmp_path / "heartbeat-bot").exists()


def _card(cfg, r_image_url, conn, fid="FILE-WOLF"):
    key = image_cache_key(r_image_url)
    cfg.cards_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.cards_dir / f"{key}.png"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(p)
    from twitch_badges.cards import sha256_file
    with db.tx(conn):
        conn.execute("INSERT INTO media VALUES(?, ?, ?, 'U', ?)", (key, sha256_file(p), fid, db.ts(T0)))


def test_inline(setup, monkeypatch):
    tg, cfg, conn, b, app = setup
    monkeypatch.setattr(db, "utcnow", lambda: T0)

    async def scenario():
        await app.initialize()
        await b.post_init(app)
        recs, _ = b.records()
        wolf = next(r for r in recs if r["set_id"] == "wolf-medallion")
        _card(cfg, wolf["image"], conn)
        upd = Update.de_json({"update_id": 1, "inline_query": {
            "id": "q1", "from": {"id": 5, "is_bot": False, "first_name": "u"},
            "query": "wolf", "offset": ""}}, app.bot)
        await b.on_inline(upd, None)
        await app.shutdown()
    _run(scenario())
    ans = tg.inline_answers[-1]
    results = ans["results"]
    assert len(results) == 1 and results[0]["type"] == "photo"
    assert results[0]["photo_file_id"] == "FILE-WOLF" and "Wolf Medallion" in results[0]["caption"]


def _msg(app, chat_id, text):
    return Update.de_json({"update_id": 2, "message": {
        "message_id": 1, "date": int(T0.timestamp()), "text": text,
        "chat": {"id": chat_id, "type": "private", "first_name": "x"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "x"}}}, app.bot)


def test_private_owner_and_stranger(setup):
    tg, cfg, conn, b, app = setup

    async def scenario():
        await app.initialize()
        await b.post_init(app)
        await b.on_private(_msg(app, tg.owner_id, "/pause"), None)
        await b.on_private(_msg(app, 777, "hi"), None)
        await b.on_private(_msg(app, 777, "/pause"), None)       # чужой — не команда
        await app.shutdown()
    tg.chats[777] = type(tg.chats[tg.owner_id])(777, type="private")
    _run(scenario())
    texts = [(p["chat_id"], p["text"]) for api, p in tg.calls if api == "sendMessage"]
    assert any(c == tg.owner_id and "остановлены" in t for c, t in texts)
    assert [t for c, t in texts if c == 777][0].startswith("Type")
    assert len([t for c, t in texts if c == 777]) == 2
    assert db.kv_get(conn, "settings", "paused") is True


def test_storage_channel_remembered(setup):
    tg, cfg, conn, b, app = setup

    def upd(from_id):
        return Update.de_json({"update_id": 3, "my_chat_member": {
            "chat": {"id": -1009, "type": "channel", "title": "Склад"},
            "from": {"id": from_id, "is_bot": False, "first_name": "o"}, "date": int(T0.timestamp()),
            "old_chat_member": {"status": "left", "user": {"id": 123456, "is_bot": True, "first_name": "b"}},
            "new_chat_member": {"status": "administrator", "user": {"id": 123456, "is_bot": True, "first_name": "b"},
                                "can_be_edited": False, "can_manage_chat": True, "can_change_info": True,
                                "can_delete_messages": True, "can_invite_users": True,
                                "can_restrict_members": True, "can_promote_members": False,
                                "can_manage_video_chats": True, "is_anonymous": False,
                                "can_post_stories": True, "can_edit_stories": True,
                                "can_delete_stories": True, "can_post_messages": True}}}, app.bot)

    class Ctx:
        bot = app.bot

    async def scenario():
        await app.initialize()
        await b.on_my_chat_member(upd(999), Ctx)            # не владелец
        assert db.kv_get(conn, "channel", "storage_chat_id") is None
        await b.on_my_chat_member(upd(tg.owner_id), Ctx)
        await app.shutdown()
    _run(scenario())
    assert db.kv_get(conn, "channel", "storage_chat_id") == -1009


def test_second_instance_refused(tmp_path):
    cfg = config.load(env={"DATA_DIR": str(tmp_path), "TELEGRAM_BOT_TOKEN": TOKEN})
    with InstanceLock(tmp_path / "bot.lock"):
        assert botmod.main(cfg) == 3
