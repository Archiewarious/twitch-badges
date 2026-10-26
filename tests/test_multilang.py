"""Каналы на английском и испанском (27.10.2026)."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from conftest import T0, load_fixture
from fake_telegram import Chat
from test_outbox import Env, env, new_badge, settle  # noqa: F401 (env — фикстура)

from twitch_badges import db, store
from twitch_badges.domain.records import RecordsContext, build_records
from twitch_badges.publisher import planner
from twitch_badges.publisher.captions import (album_caption, channel_buttons, fit_channel_caption,
                                              is_shown)
from twitch_badges.publisher.locale import EN, ES
from twitch_badges.publisher.outbox import Outbox, card_key
from twitch_badges.publisher.service import tick
from twitch_badges.publisher.translate import translate_condition
from twitch_badges.timeutil import in_quiet_hours

CYR = re.compile(r"[А-Яа-яЁё]")
EN_CHAT = -1003


# ── перевод условий ──

@pytest.mark.parametrize("ru, en, es", [
    ("Подписка или гифт", "Sub or gift sub", "Suscripción o regalo de suscripción"),
    ("Смотреть 4 часа", "Watch for 4 hours", "Ver 4 horas"),
    ("Смотреть 1 час 30 минут", "Watch for 1 hour 30 minutes", "Ver 1 hora 30 minutos"),
    ("Подписка или гифт, затем смотреть 20 минут в 3 разных дня",
     "Sub or gift sub, then watch for 20 minutes on 3 different days",
     "Suscripción o regalo de suscripción, luego ver 20 minutos en 3 días distintos"),
    ("Билет на TwitchCon — офлайн, 3 дня", "TwitchCon ticket — in-person event, 3 days",
     "Entrada para TwitchCon — evento presencial, 3 días"),
    ("Подписка, гифт или потратить Bits", "Sub, gift sub or spend Bits",
     "Suscripción, regalo de suscripción o usar Bits"),
    ("Иметь предыдущий значок эволюции, затем смотреть 20 минут в 3 разных дня",
     "Own the previous evolution badge, then watch for 20 minutes on 3 different days",
     "Tener la insignia de la evolución anterior, luego ver 20 minutos en 3 días distintos"),
    ("Оформить 2 подписки 1 уровня", "Get 2 tier 1 subs", "Suscribirse con 2 suscripciones de nivel 1"),
])
def test_translate(ru, en, es):
    assert translate_condition(ru, "en") == en
    assert translate_condition(ru, "es") == es


def test_untranslatable_is_none():
    """Ручной текст из overrides — не выдумываем, пост скажет «условия уточняются»."""
    assert translate_condition("Принять участие в Hype Train на канале", "en") is None


# ── подписи на всех реальных записях ──

def _shown():
    snap = load_fixture()["snapshot"]
    recs = build_records(snap, RecordsContext(now=T0))
    return [r for r in recs if is_shown(r, T0)]


@pytest.mark.parametrize("loc", [EN, ES])
@pytest.mark.parametrize("kind", ["appeared_active", "appeared_upcoming", "started", "ending",
                                  "appeared_nodates", "cond_confirmed", "extended"])
def test_captions_have_no_russian(loc, kind):
    for r in _shown():
        cap = fit_channel_caption(kind, r, {}, loc=loc)
        assert not CYR.search(cap), cap
        for row in channel_buttons(r, loc):
            for b in row:
                assert not CYR.search(b["text"]), b
    rs = _shown()[:4]
    cap = album_caption([(r["set_id"], r) for r in rs], kind, "Some Event", {}, loc)
    assert not CYR.search(cap), cap


def test_dates_local_and_utc():
    """27.10.2026: местное время с подписью пояса + UTC в скобках (во всех каналах)."""
    from twitch_badges.publisher.captions import window_text
    r = {"title": "X", "status": "active", "cost": "free", "condition": "Смотреть 1 час",
         "window": {"start": datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc),
                    "end": datetime(2026, 10, 31, 7, 59, tzinfo=timezone.utc)}}
    en = fit_channel_caption("started", r, {}, loc=EN)
    assert "From Oct 21, 12:00 PM ET (16:00 UTC) to Oct 31, 3:59 AM ET (07:59 UTC)" in en
    assert "Watch for 1 hour" in en
    es = fit_channel_caption("started", r, {}, loc=ES)
    assert ("Del 21 de octubre, 10:00 hora CDMX (16:00 UTC) al 31 de octubre, 01:59 hora CDMX "
            "(07:59 UTC)") in es
    assert window_text(r) == "С 21 октября 19:00 МСК (16:00 UTC) до 31 октября 10:59 МСК (07:59 UTC)"
    # в UTC уже другой день — дата в скобках
    late = dict(r, window={"end": datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)})
    assert window_text(late, EN) == "Until Sep 30, 9:30 PM ET (Oct 1, 01:30 UTC)"
    # без часа — только дата, без поясов
    coarse = dict(r, window={"end": datetime(2026, 10, 31, tzinfo=timezone.utc), "dates_coarse": True})
    assert window_text(coarse) == "До 31 октября"


def test_card_keys_per_language():
    r = {"image": "https://static-cdn.jtvnw.net/badges/v1/7833bb6e-d20d-48ff-a58d-67fe827a4f84/3"}
    base = card_key(r)
    assert card_key(r, EN) == f"{base}.en" and card_key(r, ES) == f"{base}.es"


def test_quiet_hours_by_channel_timezone():
    t = datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)      # 16:00 МСК, 09:00 Нью-Йорк, 07:00 CDMX
    assert not in_quiet_hours(t, 23, 9)
    assert not in_quiet_hours(t, 23, 9, "America/New_York")
    assert in_quiet_hours(t, 23, 9, "America/Mexico_City")


# ── публикация в два канала из одной БД ──

def _with_en(e):
    e.tg.chats[EN_CHAT] = Chat(EN_CHAT, title="en")

    async def nosleep(*a):
        pass
    return Outbox(e.conn, e.bot, channel_id=EN_CHAT, storage_chat_id=e.storage,
                  alert=e.alerts.append, sleep=nosleep, clock=lambda: e.now, loc=EN)


def _en_tick(e, ob):
    return asyncio.run(tick(e.conn, ob, now=e.now, has_art=e.has_art,
                            cfg=planner.PlanConfig(quiet_tz=EN.tz)))


def test_two_channels_independent(env):
    e = env(mutate=new_badge)
    en = _with_en(e)
    assert e.tick().enqueued                                    # русский: анонс нового значка
    ru_posts = e.tg.posts_count()
    r = _en_tick(e, en)                                         # английский: первый из живых
    assert r.enqueued
    posts = [m for _, m in sorted(e.tg.chats[EN_CHAT].messages.items())]
    assert posts and not CYR.search(posts[0].get("caption") or "")
    camps = store.load_campaigns(e.conn)
    assert "test-new" in camps and all(not k.startswith("en:") for k in camps if k == "test-new")
    en_ids = [k for k in camps if k.startswith("en:")]
    assert en_ids and all(camps[k].stages for k in en_ids)
    chs = {row[0] for row in e.conn.execute("SELECT DISTINCT ch FROM outbox")}
    assert chs == {"ru", "en"}
    # русский канал не получил ничего из английской очереди
    settle(e)
    assert e.tg.posts_count() == ru_posts
    # английский со временем объявляет и новый значок — отдельно от русского
    for _ in range(40):
        e.now += timedelta(minutes=2)
        e.conn.execute("UPDATE snapshots SET committed_at=?", (db.ts(e.now),))
        _en_tick(e, en)
    camps = store.load_campaigns(e.conn)
    assert "announce" in camps["en:test-new"].stages
    assert camps["test-new"].stages == {"announce"}


def test_old_db_gets_channel_column(tmp_path, env):
    e = env(mutate=new_badge)
    e.conn.execute("ALTER TABLE outbox DROP COLUMN ch")
    e.conn.close()
    conn = db.open_db(e.path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert "ch" in cols
