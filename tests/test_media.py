"""C07: картинки, карточки, загрузка в Telegram и file_id в постах и inline."""
import asyncio
import io
from datetime import timedelta

import mutations as m
import pytest
from conftest import T0, load_fixture
from fake_telegram import FakeTelegram
from PIL import Image
from test_outbox import Env, new_badge

from twitch_badges import cards, images
from twitch_badges.domain.records import RecordsContext, build_records
from twitch_badges.media import MediaStore
from twitch_badges.publisher import inline


def png(w=72, h=72, color=(200, 30, 30, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


# ── проверка картинки ──

def test_check_png():
    assert images.check_png(png(), "image/png") is None
    assert "Content-Type" in images.check_png(b"<html>oops</html>", "text/html")
    assert images.check_png(b"<html>oops</html>", None).startswith("не картинка")
    assert images.check_png(png()[:60], "image/png").startswith("не картинка")
    assert images.check_png(png(4, 4), "image/png").startswith("размер")
    assert images.check_png(b"", "image/png") == "пустой ответ"


def _records(now=T0):
    return build_records(load_fixture()["snapshot"], RecordsContext(now=now))


def test_sync_images(tmp_path):
    recs = _records()
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, "image/png", png()

    st = images.sync_images(recs, tmp_path, fetch)
    assert st["downloaded"] == st["wanted"] > 30 and st["rejected"] == {}
    n = len(calls)
    assert images.sync_images(recs, tmp_path, fetch)["downloaded"] == 0 and len(calls) == n
    # битый файл перекачивается, лишний удаляется
    victim = sorted(tmp_path.glob("*.png"))[0]
    victim.write_bytes(b"<html>")
    (tmp_path / "stale.png").write_bytes(png())
    st = images.sync_images(recs, tmp_path, fetch)
    assert st["downloaded"] == 1 and st["removed"] == 1 and images.file_ok(victim)


def test_sync_images_rejects_html(tmp_path):
    recs = _records()
    st = images.sync_images(recs, tmp_path, lambda url: (200, "text/html", b"<html>"))
    assert st["downloaded"] == 0 and len(st["rejected"]) == st["wanted"]
    assert list(tmp_path.glob("*.png")) == []
    st = images.sync_images(recs, tmp_path, lambda url: (404, "text/plain", b""))
    assert all(v == "HTTP 404" for v in st["rejected"].values())


def test_sync_images_fail_close(tmp_path):
    recs = _records()
    for r in recs:
        r["image"] = "https://cdn.example/new-scheme/x.png"
    (tmp_path / "keep.png").write_bytes(png())
    with pytest.raises(RuntimeError):
        images.sync_images(recs, tmp_path, lambda u: (200, "image/png", png()))
    assert (tmp_path / "keep.png").exists()


# ── карточки ──

def test_cards_written_only_on_change(tmp_path):
    recs = _records()
    img = tmp_path / "img"
    images.sync_images(recs, img, lambda u: (200, "image/png", png()))
    out = tmp_path / "cards"
    st1 = cards.sync_cards(recs, img, out)
    assert st1["written"] == len(st1["cards"]) > 30
    mt = {p.name: p.stat().st_mtime_ns for p in out.glob("*.png")}
    st2 = cards.sync_cards(recs, img, out)
    assert st2["written"] == 0 and st2["cards"] == st1["cards"]
    assert mt == {p.name: p.stat().st_mtime_ns for p in out.glob("*.png")}
    r = next(r for r in recs if cards.media_shown(r))
    r["cost"] = "free" if r.get("cost") == "paid" else "paid"
    st3 = cards.sync_cards(recs, img, out)
    assert st3["written"] == 1 and st3["cards"][cards.card_key(r)] != st1["cards"][cards.card_key(r)]


# ── Telegram ──

def _store(tmp_path, tg, conn=None):
    from twitch_badges import db
    if conn is None:
        conn = db.create(tmp_path / "m.sqlite3")

    async def nosleep(*a):
        pass
    return MediaStore(conn, tg.bot(), storage_chat_id=tg.storage_id, cards_dir=tmp_path / "cards",
                      sleep=nosleep)


def test_one_upload_per_sha(tmp_path):
    tg = FakeTelegram()
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "a.png").write_bytes(png())
    (tmp_path / "cards" / "b.png").write_bytes(png(color=(0, 0, 255, 255)))
    ms = _store(tmp_path, tg)
    res = asyncio.run(ms.ensure(["a", "b", "a"]))
    assert set(res) == {"a", "b"} and len(tg.chats[tg.storage_id].messages) == 2
    assert asyncio.run(ms.ensure(["a", "b"])) == {}
    fid = ms.file_id("a")
    (tmp_path / "cards" / "a.png").write_bytes(png(color=(0, 255, 0, 255)))   # карточка изменилась
    assert ms.file_id("a") is None
    asyncio.run(ms.ensure(["a"]))
    assert ms.file_id("a") not in (None, fid) and len(tg.chats[tg.storage_id].messages) == 3
    ms.prune({"a"})
    assert ms.conn.execute("SELECT count(*) FROM media WHERE card_key='b'").fetchone()[0] == 0


def test_upload_failure_retried(tmp_path):
    tg = FakeTelegram()
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "a.png").write_bytes(png())
    ms = _store(tmp_path, tg)
    tg.fail("sendPhoto", "connect_error")
    assert "NetworkError" in asyncio.run(ms.ensure(["a"]))["a"]
    assert ms.file_id("a") is None
    asyncio.run(ms.ensure(["a"]))
    assert ms.file_id("a")


def test_post_uses_file_id(tmp_path):
    """Пост уходит по file_id карточки; пока карточка не загружена — ждёт."""
    e = Env(tmp_path, mutate=new_badge)
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    key = m.image_key("test-new")
    (cards_dir / f"{key}.png").write_bytes(png())
    ms = MediaStore(e.conn, e.bot, storage_chat_id=e.tg.storage_id, cards_dir=cards_dir,
                    sleep=e.outbox.sleep, clock=lambda: e.now)
    e.outbox.media_for = ms.media_for
    e.tick()                                       # без media: карточки нет в Telegram
    assert e.rows() == [(1, "retry")] and e.tg.posts_count() == 0
    e.now += timedelta(minutes=2)
    from twitch_badges.publisher.planner import PlanConfig
    from twitch_badges.publisher.service import tick
    e.conn.execute("UPDATE snapshots SET committed_at=?", (e.now.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    asyncio.run(tick(e.conn, e.outbox, now=e.now, has_art=e.has_art, cfg=PlanConfig(), media=ms))
    assert e.rows() == [(1, "sent")] and e.tg.posts_count() == 1
    post = e.tg.channel_posts()[0]
    assert post["_media"] == [ms.file_id(key)]
    sends = [p for api, p in e.tg.calls if api == "sendPhoto" and str(p.get("chat_id")) == str(e.tg.channel_id)]
    assert sends[-1]["photo"] == ms.file_id(key)


# ── inline ──

def test_inline_results():
    recs = _records()
    fids = {r["set_id"]: f"FID-{r['set_id']}" for r in recs}
    out = inline.results(recs, "", now=T0, file_id_for=lambda r: fids[r["set_id"]])
    assert 20 <= len(out) <= inline.MAX_RESULTS
    assert all(o["photo_file_id"].startswith("FID-") and len(o["caption"]) <= 1024 for o in out)
    assert out[0]["title"].startswith("🎁 ")
    soon = inline.results(recs, "скоро", now=T0, file_id_for=lambda r: fids[r["set_id"]])
    assert soon and all(o["title"].startswith("⏳ ") for o in soon)
    paid = inline.results(recs, "paid", now=T0, file_id_for=lambda r: fids[r["set_id"]])
    free = inline.results(recs, "free", now=T0, file_id_for=lambda r: fids[r["set_id"]])
    assert paid and free and not ({o["id"] for o in paid} & {o["id"] for o in free})
    wolf = inline.results(recs, "wolf med", now=T0, file_id_for=lambda r: fids[r["set_id"]])
    assert [o["title"] for o in wolf] == ["🎁 Wolf Medallion"]
    # без карточки в Telegram — не показываем
    assert inline.results(recs, "", now=T0, file_id_for=lambda r: None) == []


def test_inline_same_caption_as_before():
    """Подпись inline та же, что у старого бота (captions.inline_caption)."""
    from twitch_badges.publisher.captions import inline_caption
    recs = _records()
    out = inline.results(recs, "wolf", now=T0, file_id_for=lambda r: "F")
    r = next(r for r in recs if r["set_id"] == "wolf-medallion")
    assert out[0]["caption"] == inline_caption(r, {})


def test_card_same_as_old_renderer(tmp_path):
    """Вид карточки не изменился: байты те же, что у старого render_cards."""
    import render_cards
    recs = [r for r in _records() if cards.media_shown(r)][:5]
    img = tmp_path / "img"
    images.sync_images(recs, img, lambda u: (200, "image/png", png()))
    render_cards.IMAGES_DIR = img
    for r in recs:
        render_cards.render_card(r, tmp_path / "old.png")
        assert cards.render_card(r, img) == (tmp_path / "old.png").read_bytes()
