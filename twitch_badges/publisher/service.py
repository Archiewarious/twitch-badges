"""Тик публикации: снапшот из БД → записи с настоящим now → план → outbox.

Вызывается ботом раз в 2 минуты (и тестами напрямую)."""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from .. import db, store
from ..domain.catalog import image_cache_key
from ..domain.records import RecordsContext, build
from . import planner
from .locale import LOCALES, RU
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


_OTHER_PREFIXES = tuple(loc.prefix for loc in LOCALES.values() if loc.prefix)


def _mine(cid, loc):
    """Принадлежит ли id кампании каналу loc (у русского — без префикса)."""
    return cid.startswith(loc.prefix) if loc.prefix else not cid.startswith(_OTHER_PREFIXES)


def _strip(cid, loc):
    return cid[len(loc.prefix):] if loc.prefix else cid


def channel_view(campaigns: dict, aliases: dict, loc):
    """Кампании и алиасы канала loc — без префикса, как их знает планировщик."""
    if not loc.prefix:
        return ({k: c for k, c in campaigns.items() if _mine(k, loc)},
                {a: c for a, c in aliases.items() if _mine(a, loc)})
    camps = {}
    for k, c in campaigns.items():
        if _mine(k, loc):
            camps[_strip(k, loc)] = dataclasses.replace(
                c, id=_strip(k, loc), aliases={_strip(a, loc) for a in c.aliases})
    al = {_strip(a, loc): _strip(c, loc) for a, c in aliases.items() if _mine(a, loc)}
    return camps, al


def _prefixed(intent, loc):
    if not loc.prefix:
        return intent
    items = [dataclasses.replace(it, campaign_id=loc.prefix + it.campaign_id) for it in intent.items]
    return dataclasses.replace(intent, items=items)


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
    if overrides is None:
        overrides = db.kv_get(conn, "overrides", "data", {}) or {}     # проверенные сбором
    built = build(snap.data, RecordsContext(now=now, known_windows=known, overrides=overrides))
    loc = getattr(outbox, "loc", RU)
    p = loc.prefix
    campaigns, aliases = channel_view(store.load_campaigns(conn), store.aliases(conn), loc)
    cfg = planner.PlanConfig(**{**cfg.__dict__, "paused": cfg.paused or paused(conn)})
    busy = {_strip(r[0], loc) for r in conn.execute(
        "SELECT DISTINCT s.campaign_id FROM stages s JOIN outbox o ON o.id = s.outbox_id "
        "WHERE o.status != 'sent'") if _mine(r[0], loc)}
    res = planner.plan(built.records, campaigns, aliases, now=now,
                       data_at=snap.committed, has_art=has_art, cfg=cfg, busy=busy)
    if p:
        # Тревоги планировщика (burst, несвежие данные) — те же, что у русского
        # канала: дублировать их владельцу незачем.
        res.alerts = []
    with db.tx(conn):
        for a in res.aliases:
            store.add_alias(conn, p + a.alias, p + a.campaign_id, a.reason, now)
        if res.seen_live:
            conn.executemany("UPDATE campaigns SET last_seen_live_at=? WHERE id=?",
                             [(db.ts(now), p + cid) for cid in set(res.seen_live)])
        for cid in (p + c for c in res.cleanup):
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
            rid = outbox.enqueue(_prefixed(intent, loc), built.category_urls)
            if rid:
                enqueued.append(rid)
    if publish and media is not None:
        await media.ensure(outbox.media_keys())
    stats = await outbox.process() if publish else {}
    return TickResult(res, enqueued, stats)
