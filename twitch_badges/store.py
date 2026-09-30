"""Репозитории поверх db: снапшоты, кампании («что знает читатель»), стадии, алиасы."""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field

from .db import get_meta, parse_ts, set_meta, ts, utcnow

KEEP_SNAPSHOTS = 48


# ── снапшоты ──

def put_snapshot(conn, snapshot: dict, *, signature: str, committed_at=None) -> int:
    """Сохранить снапшот и сделать его текущим. Вызывать внутри транзакции."""
    payload = zlib.compress(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode(), 6)
    cur = conn.execute(
        "INSERT INTO snapshots(fetched_at, committed_at, signature, payload) VALUES(?, ?, ?, ?)",
        (snapshot.get("fetched_at") or ts(committed_at or utcnow()),
         ts(committed_at or utcnow()), signature, payload))
    sid = cur.lastrowid
    set_meta(conn, "current_snapshot_id", sid)
    conn.execute("DELETE FROM snapshots WHERE id NOT IN "
                 "(SELECT id FROM snapshots ORDER BY id DESC LIMIT ?)", (KEEP_SNAPSHOTS,))
    return sid


@dataclass
class Snapshot:
    id: int
    fetched_at: str
    committed_at: str
    signature: str
    data: dict

    @property
    def committed(self):
        return parse_ts(self.committed_at)


def current_snapshot(conn) -> Snapshot | None:
    sid = get_meta(conn, "current_snapshot_id")
    if not sid:
        return None
    row = conn.execute("SELECT * FROM snapshots WHERE id=?", (int(sid),)).fetchone()
    if not row:
        return None
    return Snapshot(row["id"], row["fetched_at"], row["committed_at"], row["signature"],
                    json.loads(zlib.decompress(row["payload"])))


# ── кампании ──

@dataclass
class Campaign:
    """Что знает читатель канала о кампании — ровно то, что было в постах."""
    id: str
    title: str | None = None
    grp: str | None = None
    from_orphan: bool = False
    known_start: str | None = None
    known_end: str | None = None
    known_vague: bool | None = None
    known_condition: str | None = None
    known_cost: str | None = None
    first_posted_at: str | None = None
    last_posted_at: str | None = None
    last_seen_live_at: str | None = None
    stages: set = field(default_factory=set)
    aliases: set = field(default_factory=set)

    FIELDS = ("title", "grp", "from_orphan", "known_start", "known_end", "known_vague",
              "known_condition", "known_cost", "first_posted_at", "last_posted_at",
              "last_seen_live_at")


def load_campaigns(conn) -> dict[str, Campaign]:
    out = {}
    for r in conn.execute("SELECT * FROM campaigns"):
        out[r["id"]] = Campaign(
            id=r["id"], title=r["title"], grp=r["grp"], from_orphan=bool(r["from_orphan"]),
            known_start=r["known_start"], known_end=r["known_end"],
            known_vague=None if r["known_vague"] is None else bool(r["known_vague"]),
            known_condition=r["known_condition"], known_cost=r["known_cost"],
            first_posted_at=r["first_posted_at"], last_posted_at=r["last_posted_at"],
            last_seen_live_at=r["last_seen_live_at"])
    for r in conn.execute("SELECT campaign_id, stage FROM stages"):
        out[r[0]].stages.add(r[1])
    for r in conn.execute("SELECT alias, campaign_id FROM aliases"):
        out[r[1]].aliases.add(r[0])
    return out


def aliases(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT alias, campaign_id FROM aliases")}


def upsert_campaign(conn, c: Campaign, now=None):
    now = ts(now or utcnow())
    vals = [getattr(c, f) for f in Campaign.FIELDS]
    vals[2] = int(bool(c.from_orphan))
    vals[5] = None if c.known_vague is None else int(bool(c.known_vague))
    if vals[10] is None:
        vals[10] = now
    cols = ", ".join(Campaign.FIELDS)
    marks = ", ".join("?" for _ in Campaign.FIELDS)
    upd = ", ".join(f"{f}=excluded.{f}" for f in Campaign.FIELDS)
    conn.execute(f"INSERT INTO campaigns(id, {cols}, created_at, updated_at) VALUES(?, {marks}, ?, ?) "
                 f"ON CONFLICT(id) DO UPDATE SET {upd}, updated_at=excluded.updated_at",
                 [c.id, *vals, now, now])


def add_stage(conn, campaign_id: str, stage: str, outbox_id=None, now=None) -> bool:
    """Занять стадию. False — уже занята (значит, пост уже был: второго не будет)."""
    cur = conn.execute("INSERT OR IGNORE INTO stages(campaign_id, stage, outbox_id, created_at) "
                       "VALUES(?, ?, ?, ?)", (campaign_id, stage, outbox_id, ts(now or utcnow())))
    return cur.rowcount == 1


def release_stage(conn, campaign_id: str, stage: str):
    conn.execute("DELETE FROM stages WHERE campaign_id=? AND stage=?", (campaign_id, stage))


def add_alias(conn, alias: str, campaign_id: str, reason: str, now=None):
    """alias → campaign_id; стадии алиаса переносятся в кампанию, сам алиас-кампания удаляется."""
    now_s = ts(now or utcnow())
    exists = conn.execute("SELECT 1 FROM campaigns WHERE id=?", (alias,)).fetchone()
    if exists:
        conn.execute("INSERT OR IGNORE INTO stages(campaign_id, stage, outbox_id, created_at) "
                     "SELECT ?, stage, outbox_id, created_at FROM stages WHERE campaign_id=?",
                     (campaign_id, alias))
        conn.execute("DELETE FROM stages WHERE campaign_id=?", (alias,))
        conn.execute("UPDATE aliases SET campaign_id=? WHERE campaign_id=?", (campaign_id, alias))
        conn.execute("DELETE FROM campaigns WHERE id=?", (alias,))
    conn.execute("INSERT INTO aliases(alias, campaign_id, reason, created_at) VALUES(?, ?, ?, ?) "
                 "ON CONFLICT(alias) DO UPDATE SET campaign_id=excluded.campaign_id",
                 (alias, campaign_id, reason, now_s))
