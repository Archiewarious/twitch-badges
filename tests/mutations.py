"""Мутации снапшота SD: как источник заводит и меняет кампании.

Каждая функция меняет снапшот НА МЕСТЕ и повторяет то, что SD реально делает
(формат 30.09.2026: даты и цена прямо в каталоге, availability — в событиях).
Возвращают ключ картинки значка, если он появился: старая логика не постит без
файла арта, и тест кладёт его сам.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

CATEGORY = {"id": "491931", "name": "Arc Raiders"}      # есть в category_urls фикстуры
WATCH_30 = [[{"type": "watch", "watch_minutes": 30}]]
SUB_OR_GIFT = [[{"type": "subscription"}, {"type": "subscription_gift"}]]
BITS = [[{"type": "bits", "bits": 500}]]


def image_key(set_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-badge:{set_id}"))


def image_url(set_id: str) -> str:
    return f"https://static-cdn.jtvnw.net/badges/v1/{image_key(set_id)}/3"


def sd_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def sd_time(dt: datetime | None, catalog: bool = False) -> str:
    """Каталог отдаёт HH:MM:SS.mmm, события и availability — HH:MM (приложение A)."""
    if not dt:
        return ""
    return dt.strftime("%H:%M:%S.000") if catalog else dt.strftime("%H:%M")


def _flags(objectives):
    types = {s["type"] for stage in objectives or [] for s in stage}
    watch = next((s.get("watch_minutes") for stage in objectives or [] for s in stage
                  if s["type"] == "watch"), None)
    return {"subscription": "subscription" in types,
            "subscription_gift": "subscription_gift" in types,
            "bits": "bits" in types, "watch": "watch" in types,
            "watch_minutes": watch, "stream": False, "clip": False,
            "twitchcon": False, "turbo": False}


def availability(set_id, start, end, objectives=WATCH_30, cost="free", categories=(CATEGORY,)):
    av = {"_id": f"av-{set_id}", "docModel": "TwitchGlobalBadge", "hidden": False,
          "time_limited": True, "categories": [dict(c) for c in categories],
          "start_at_date": sd_date(start), "start_at_time": sd_time(start),
          "end_at_date": sd_date(end), "end_at_time": sd_time(end),
          "objectives": objectives, "costs": [cost] if cost else [], "cost": cost,
          "upvote_count": 0, "downvote_count": 0, "channel_count": 0}
    if objectives is not None:
        av.update(_flags(objectives))
    return av


def catalog_badge(set_id, title, *, added_at, start=None, end=None, cost="free"):
    b = {"_id": f"cat-{set_id}",
         "current": {"set_id": set_id, "version": {"id": "1", "title": title,
                                                   "image_url_4x": image_url(set_id)}},
         "added": True, "user_count": 0, "removed": False,
         "added_at": added_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "cost": cost, "system": False, "cancelled": False}
    if start or end:
        b.update(time_limited=True,
                 start_at_date=sd_date(start), start_at_time=sd_time(start, catalog=True),
                 end_at_date=sd_date(end), end_at_time=sd_time(end, catalog=True))
    return b


def find_badge(snap, set_id):
    return next(b for b in snap["badges"] if b["current"]["set_id"] == set_id)


def find_event(snap, title):
    return next(e for e in snap["events"] if e["title"] == title)


def new_campaign(snap, event_title, badges, *, start, end, added_at,
                 objectives=WATCH_30, cost="free", in_event=True, catalog_dates=True):
    """Новая кампания: значки в каталоге (с датами, как в новом формате) и
    событие с availability у каждого. badges: [(set_id, title)]."""
    ev_badges = []
    for set_id, title in badges:
        snap["badges"].append(catalog_badge(
            set_id, title, added_at=added_at, cost=cost,
            start=start if catalog_dates else None, end=end if catalog_dates else None))
        ev_badges.append({"_id": f"cat-{set_id}",
                          "current": snap["badges"][-1]["current"],
                          "availability": [availability(set_id, start, end, objectives, cost)]})
    if in_event:
        snap["events"].append({
            "_id": f"ev-{event_title}", "title": event_title, "content": "",
            "start_at_date": sd_date(start), "start_at_time": sd_time(start),
            "end_at_date": sd_date(end), "end_at_time": sd_time(end),
            "twitch_global_badges": ev_badges})
    return [image_key(s) for s, _ in badges]


def new_badge(snap, set_id, title, **kw):
    return new_campaign(snap, kw.pop("event_title", title), [(set_id, title)], **kw)[0]


def set_window(snap, set_id, start=..., end=...):
    """Сдвинуть окно значка везде, где SD его хранит: каталог, availability
    событий, страница значка. start/end=... — не трогать."""
    b = find_badge(snap, set_id)
    for dt, f in ((start, "start"), (end, "end")):
        if dt is ...:
            continue
        if f"{f}_at_date" in b or dt:
            b[f"{f}_at_date"], b[f"{f}_at_time"] = sd_date(dt), sd_time(dt, catalog=True)
    avs = [av for ev in snap["events"] for eb in ev.get("twitch_global_badges") or []
           if eb["current"]["set_id"] == set_id for av in eb.get("availability") or []]
    avs += (snap.get("page_availability") or {}).get(set_id, [])
    for av in avs:
        for dt, f in ((start, "start"), (end, "end")):
            if dt is not ...:
                av[f"{f}_at_date"], av[f"{f}_at_time"] = sd_date(dt), sd_time(dt)


def clear_condition(snap, set_id):
    """SD завёл значок, а как получить — ещё не написал."""
    for ev in snap["events"]:
        for eb in ev.get("twitch_global_badges") or []:
            if eb["current"]["set_id"] == set_id:
                for av in eb.get("availability") or []:
                    av.update(objectives=None, costs=[], cost=None, **_flags(None))
                    av.pop("objective", None)
    (snap.get("helix") or {}).pop(set_id, None)


def orphan_event(snap, title, *, start, end, content=None):
    """Событие с датами, у которого значка ещё нет ни в каталоге, ни в событии."""
    snap["events"].append({
        "_id": f"ev-{title}", "title": title,
        "content": content or "A badge will be available for subscribing or gifting a sub.",
        "start_at_date": sd_date(start), "start_at_time": sd_time(start),
        "end_at_date": sd_date(end), "end_at_time": sd_time(end),
        "twitch_global_badges": []})


def attach_to_event(snap, title, set_id, **av_kw):
    """Значок появился и SD привязал его к уже существующему событию."""
    ev = find_event(snap, title)
    b = find_badge(snap, set_id)
    ev["twitch_global_badges"].append({
        "_id": b["_id"], "current": b["current"],
        "availability": [availability(set_id, **av_kw)]})


def no_date_badge(snap, set_id, title, *, added_at, description):
    """Значок без дат: SD знает только added_at, условие есть лишь у Twitch (Helix)."""
    snap["badges"].append(catalog_badge(set_id, title, added_at=added_at, cost=None))
    snap.setdefault("helix", {})[set_id] = {
        "title": title, "description": description, "click_url": "",
        "image_url_4x": image_url(set_id)}
    return image_key(set_id)


def bits_page_badge(snap, set_id, title, *, start, end, added_at):
    """Значок за Bits, про который SD знает только текст страницы (kind=bits)."""
    snap["badges"].append(catalog_badge(set_id, title, added_at=added_at, cost=None))
    snap.setdefault("page_info", {})[set_id] = {
        "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M"),
        "start_time_known": True, "end_time_known": True, "kind": "bits",
        "watch_minutes": None, "unconfirmed": False, "too_late": False}
    return image_key(set_id)


# ── смена формата (для проверок формата) ──

def old_time_format(snap):
    """Каталог снова отдаёт время как HH:MM."""
    for b in snap["badges"]:
        for f in ("start_at_time", "end_at_time"):
            if b.get(f):
                b[f] = b[f][:5]


def drop_added_at(snap):
    for b in snap["badges"]:
        b.pop("added_at", None)


def drop_catalog_dates(snap):
    for b in snap["badges"]:
        for f in ("start_at_date", "start_at_time", "end_at_date", "end_at_time"):
            b.pop(f, None)


def rename_catalog_time(snap, fmt="%H.%M"):
    """Время в каталоге в незнакомом виде."""
    for b in snap["badges"]:
        for f in ("start_at_time", "end_at_time"):
            if b.get(f):
                b[f] = datetime.strptime(b[f][:5], "%H:%M").strftime(fmt)


def hours(n):
    return timedelta(hours=n)


def days(n):
    return timedelta(days=n)
