"""Мониторы бота: пробелы источника, слепые зоны, сбор, Telegram.

anomalies и blindspots — перенос check_anomalies/check_blindspots из bot.py
(тексты и пороги прежние, состояние — в kv monitor). Новые: collector-failing
(нет успешного сбора > 3 ч) и telegram-unreachable (нет успешных вызовов API
> 15 мин) — раньше первое видел только watchdog по файлу, второго не было."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import alerts, db
from .domain.catalog import MANUAL_SET_IDS
from .publisher.captions import is_shown, iso, watch_target
from .sources.sd_parse import _badge_added_at

log = logging.getLogger(__name__)

INCOMPLETE_ALERT_HOURS = 24
BLINDSPOT_DAYS = 30
BLINDSPOT_GRACE_HOURS = 24
COLLECTOR_FAIL_HOURS = 3
TELEGRAM_FAIL_MIN = 15


def _monitor(conn) -> dict:
    return {"incomplete_since": db.kv_get(conn, "monitor", "incomplete_since", {}) or {},
            "blindspots": db.kv_get(conn, "monitor", "blindspots", []) or []}


def anomalies(conn, records, now):
    """Выдаётся дольше суток, а сказать «как получить» нечего."""
    seen = dict(_monitor(conn)["incomplete_since"])
    issues, live_ids = [], set()
    for r in records:
        if not is_shown(r, now):
            continue
        w = r.get("window") or {}
        _, _, url = watch_target(r)
        if not ((not r.get("condition") and not url) or (w.get("channel_count") and not url)):
            continue
        live_ids.add(r["set_id"])
        first = seen.setdefault(r["set_id"], iso(now))
        if r["status"] != "active":
            continue
        try:
            since = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
        except ValueError:
            since = now
        if (now - since).total_seconds() < INCOMPLETE_ALERT_HOURS * 3600:
            continue
        hours = int((now - since).total_seconds() // 3600)
        issues.append(f"❓ {r['title']}: выдаётся уже {hours} ч, а сказать «как получить» "
                      "нечего — ни условия, ни ссылки")
    seen = {k: v for k, v in seen.items() if k in live_ids}
    with db.tx(conn):
        db.kv_set(conn, "monitor", "incomplete_since", seen, now)
        if issues:
            body = "\n".join(issues[:15]) + (f"\n…и ещё {len(issues) - 15}" if len(issues) > 15 else "")
            alerts.raise_alert(conn, "anomalies", f"{len(issues)} значк(ов) без условия", body, now)
        else:
            alerts.clear_alert(conn, "anomalies", "все значки распознаны", now)
    return issues


def hidden_reason(r):
    """Осознанная причина не показывать значок (перенос из bot.py)."""
    if r is None:
        return None
    w = r.get("window") or {}
    if r.get("group") == "__permanent__":
        return "постоянный"
    if w.get("offline_event"):
        return "офлайн-мероприятие"
    if w.get("too_late"):
        return "добавлен уже после окончания"
    if r["status"] == "upcoming" and w.get("start"):
        return "анонс за горизонтом"
    if r.get("note_kind") == "cancelled":
        return "кампания отменена"
    if r["status"] == "ended" and w.get("end"):
        return "окно закрылось"
    return None


def load_ignore(path: Path | None) -> set:
    try:
        return {ln.split("#")[0].strip() for ln in Path(path).read_text().splitlines()
                if ln.split("#")[0].strip()} if path else set()
    except OSError:
        return set()


def blindspots(conn, snapshot, records, now, ignore=frozenset()):
    """Свежий значок живёт на Twitch, а в канал не идёт — и без осознанной причины.
    Сообщаем о каждом значке один раз."""
    by_id = {r["set_id"]: r for r in records}
    cutoff = now - timedelta(days=BLINDSPOT_DAYS)
    grace = now - timedelta(hours=BLINDSPOT_GRACE_HOURS)
    issues = {}
    for badge in snapshot.get("badges", []):
        cur = badge.get("current") or {}
        sid = cur.get("set_id")
        if not sid or not badge.get("added") or sid in ignore or sid in MANUAL_SET_IDS:
            continue
        ts = _badge_added_at(badge)
        try:
            added = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except ValueError:
            added = None
        if not added or added < cutoff or added > grace:
            continue
        r = by_id.get(sid)
        if (r and is_shown(r, now)) or hidden_reason(r):
            continue
        title = (cur.get("version") or {}).get("title") or sid
        what = "нет записи в каталоге" if r is None else "нет дат — не знаю, как классифицировать"
        issues[sid] = f"❓ {title} ({sid})\n   на Twitch с {added:%d.%m}, в канал не идёт: {what}"
    reported = set(_monitor(conn)["blindspots"])
    fresh = [t for sid, t in issues.items() if sid not in reported]
    with db.tx(conn):
        db.kv_set(conn, "monitor", "blindspots", sorted(issues), now)
        if fresh:
            body = "\n".join(fresh[:10]) + (f"\n…и ещё {len(fresh) - 10}" if len(fresh) > 10 else "")
            alerts.raise_alert(conn, "blindspots",
                               f"{len(fresh)} значк(ов) живут на Twitch, но молчат", body, now)
        elif not issues:
            alerts.clear_alert(conn, "blindspots", "слепых зон нет — все свежие значки разобраны", now)
    return issues


def collector_health(conn, now):
    st = db.kv_get(conn, "collector", "state", {}) or {}
    last_ok = datetime.fromisoformat(st["last_ok_at"]) if st.get("last_ok_at") else None
    if last_ok is None:
        snap_row = conn.execute("SELECT committed_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        last_ok = db.parse_ts(snap_row[0]) if snap_row else None
    with db.tx(conn):
        if last_ok and now - last_ok > timedelta(hours=COLLECTOR_FAIL_HOURS):
            err = st.get("last_error") or {}
            hours = (now - last_ok).total_seconds() / 3600
            alerts.raise_alert(
                conn, "collector-failing", f"сбор данных не удаётся {hours:.0f} ч",
                f"Последний успешный сбор: {last_ok:%d.%m %H:%M} UTC.\n"
                f"Последняя ошибка ({err.get('kind', '?')}): {err.get('error', '—')}", now)
        elif last_ok:
            alerts.clear_alert(conn, "collector-failing", "сбор данных снова работает", now)


def telegram_health(conn, now, ok: bool, error: str = ""):
    """ok — только что был успешный вызов API."""
    with db.tx(conn):
        if ok:
            db.kv_set(conn, "telegram", "last_ok_at", db.ts(now), now)
            alerts.clear_alert(conn, "telegram-unreachable", "Telegram снова доступен", now)
            return
        last = db.parse_ts(db.kv_get(conn, "telegram", "last_ok_at"))
        if last and now - last > timedelta(minutes=TELEGRAM_FAIL_MIN):
            alerts.raise_alert(conn, "telegram-unreachable",
                               f"Telegram недоступен {int((now - last).total_seconds() // 60)} мин",
                               f"Последняя ошибка: {error or '—'}", now)
