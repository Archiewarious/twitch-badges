"""Симулятор НОВОЙ логики в памяти: build → plan → «отправлено» → apply_sent.

Зеркало tools/legacy_sim/harness.LegacySim для дифференциальных тестов:
    sim = NewSim(**load_fixture(), now=T0)
    sim.tick(now) -> [SimPost]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from twitch_badges import db, store
from twitch_badges.domain.catalog import image_cache_key
from twitch_badges.domain.records import RecordsContext, build
from twitch_badges.publisher import planner
from twitch_badges.publisher.captions import album_caption, channel_caption
from twitch_badges.state import campaign_from_entry, live_records


@dataclass
class SimPost:
    at: datetime
    kind: str
    keys: list
    caption: str = ""


@dataclass
class NewSim:
    snapshot: dict
    published: dict
    known_windows: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    now: datetime = None
    cfg: planner.PlanConfig = planner.PlanConfig()
    data_at: datetime = None                    # None — данные свежие на каждом тике

    def __post_init__(self):
        live, _ = live_records(self.snapshot, self.now, self.known_windows, {})
        self.campaigns = {k: campaign_from_entry(k, e, live.get(k), self.now)
                          for k, e in (self.published or {}).items()}
        self.aliases = {}
        self.posts: list[SimPost] = []
        self.alerts: list[planner.AlertSignal] = []
        self.results: list[planner.PlanResult] = []

    def has_art(self, r):
        key = image_cache_key(r.get("image") or "")
        return bool(key) and key in set(self.media.get("images", []))

    def tick(self, now: datetime | None = None) -> list[SimPost]:
        if now is not None:
            self.now = now
        built = build(self.snapshot, RecordsContext(now=self.now, known_windows=self.known_windows))
        res = planner.plan(built.records, self.campaigns, self.aliases, now=self.now,
                           data_at=self.data_at or self.now, has_art=self.has_art, cfg=self.cfg)
        self.results.append(res)
        self.alerts += [a for a in res.alerts if a.active]
        for a in res.aliases:
            self.aliases[a.alias] = a.campaign_id
        for cid in res.seen_live:
            if cid in self.campaigns:
                self.campaigns[cid].last_seen_live_at = db.ts(self.now)
        out = []
        for intent in res.intents:
            items = [(i.campaign_id, i.record) for i in intent.items]
            if len(items) == 1:
                cap = channel_caption(intent.kind, items[0][1], built.category_urls)
            else:
                cap = album_caption(items, intent.kind, intent.group, built.category_urls)
            post = SimPost(self.now, intent.kind, [i.record["set_id"] for i in intent.items], cap)
            out.append(post)
            planner.apply_sent(self.campaigns, intent, self.now,
                               lambda it: store.Campaign(id=it.campaign_id, title=it.record["title"],
                                                         grp=it.record.get("group"),
                                                         from_orphan=bool((it.record.get("window") or {})
                                                                          .get("from_orphan_event"))))
        for cid in res.cleanup:
            self.campaigns.pop(cid, None)
            for a in [a for a, t in self.aliases.items() if t == cid]:
                del self.aliases[a]
        self.posts += out
        return out

    def run(self, start, step: timedelta, span: timedelta):
        n = len(self.posts)
        t = timedelta(0)
        while t <= span:
            self.tick(start + t)
            t += step
        return self.posts[n:]
