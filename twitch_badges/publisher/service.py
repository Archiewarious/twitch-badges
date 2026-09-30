"""Тик публикации: снапшот из БД → записи с настоящим now → план → outbox.

Вызывается ботом раз в 2 минуты (и тестами напрямую)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import db, store
from ..domain.catalog import image_cache_key
from ..domain.records import RecordsContext, build
from . import planner
from .outbox import Outbox

log = logging.getLogger(__name__)


def paused(conn) -> bool:
    return bool(db.kv_get(conn, "settings", "paused", False))


def set_paused(conn, value: bool, now=None):
    with db.tx(conn):
        db.kv_set(conn, "settings", "paused", bool(value), now)


def art_checker(images_dir):
    """has_art: у значка есть настоящий арт (файл картинки), а не карточка-заглушка."""
    def has_art(r):
        key = image_cache_key(r.get("image") or "")
        return bool(key) and (images_dir / f"{key}.png").exists()
    return has_art


@dataclass
class TickResult:
    plan: planner.PlanResult | None
    enqueued: list
    outbox: dict


async def tick(conn, outbox: Outbox, *, now, has_art, cfg: planner.PlanConfig,
               publish: bool = True, overrides: dict | None = None, media=None) -> TickResult:
    snap = store.current_snapshot(conn)
    if snap is None:
        outbox.alert(planner.AlertSignal("collector-failing", True, "в БД нет ни одного снапшота",
                                         "Сбор ещё ни разу не отработал успешно."))
        return TickResult(None, [], {})
    known = db.kv_all(conn, "known_windows")
    built = build(snap.data, RecordsContext(now=now, known_windows=known, overrides=overrides or {}))
    campaigns = store.load_campaigns(conn)
    cfg = planner.PlanConfig(**{**cfg.__dict__, "paused": cfg.paused or paused(conn)})
    busy = {r[0] for r in conn.execute(
        "SELECT DISTINCT s.campaign_id FROM stages s JOIN outbox o ON o.id = s.outbox_id "
        "WHERE o.status != 'sent'")}
    res = planner.plan(built.records, campaigns, store.aliases(conn), now=now,
                       data_at=snap.committed, has_art=has_art, cfg=cfg, busy=busy)
    with db.tx(conn):
        for a in res.aliases:
            store.add_alias(conn, a.alias, a.campaign_id, a.reason, now)
        if res.seen_live:
            conn.executemany("UPDATE campaigns SET last_seen_live_at=? WHERE id=?",
                             [(db.ts(now), cid) for cid in set(res.seen_live)])
        for cid in res.cleanup:
            busy = conn.execute("SELECT 1 FROM stages s JOIN outbox o ON o.id=s.outbox_id "
                                "WHERE s.campaign_id=? AND o.status IN "
                                "('pending','sending','retry','unknown')", (cid,)).fetchone()
            if busy:
                continue
            conn.execute("UPDATE stages SET outbox_id=NULL WHERE campaign_id=?", (cid,))
            conn.execute("DELETE FROM stages WHERE campaign_id=?", (cid,))
            conn.execute("DELETE FROM aliases WHERE campaign_id=?", (cid,))
            conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    for sig in res.alerts:
        outbox.alert(sig)
    enqueued = []
    if publish and not outbox.open_rows():
        # Пока предыдущее не ушло, новое не ставим: иначе после простоя Telegram
        # очередь выплюнет накопившееся залпом.
        for intent in res.intents:
            rid = outbox.enqueue(intent, built.category_urls)
            if rid:
                enqueued.append(rid)
    if publish and media is not None:
        await media.ensure(outbox.media_keys())
    stats = await outbox.process() if publish else {}
    return TickResult(res, enqueued, stats)
