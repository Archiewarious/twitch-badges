"""Перенос состояния старого бота в БД и обратно (приложение B плана).

migrate: data/ старой установки → новая БД. Исходные файлы только читаются.
export:  БД → published.json в прежнем формате (для отката на старого бота).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, store
from .domain.records import RecordsContext, build
from .domain.windows import clean_overrides
from .publisher.captions import dedup_key, is_shown, iso

MIGRATED_CONDITION = "<migrated>"
ORPHAN_MATCH = timedelta(days=2)


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def live_records(snapshot: dict, now: datetime, known_windows: dict, overrides: dict):
    """{set_id: запись} того, что сейчас показывается (как live в старом publish_new)."""
    built = build(snapshot, RecordsContext(now=now, known_windows=known_windows,
                                           overrides=overrides))
    live = {}
    for r in built.records:
        if not is_shown(r, now):
            continue
        key = dedup_key(r)
        if key not in live or r["status"] == "active":
            live[key] = r
    return live, built


def _dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None
    except ValueError:
        return None


def _windows_match(a_start, a_end, b_start, b_end):
    """Окна совпадают: начало ±2 дня и конец ±2 дня (или конца нет у обоих)."""
    a_start, a_end, b_start, b_end = map(_dt, (a_start, a_end, b_start, b_end))
    if not a_start or not b_start or abs(a_start - b_start) > ORPHAN_MATCH:
        return False
    if a_end is None and b_end is None:
        return True
    return bool(a_end and b_end and abs(a_end - b_end) <= ORPHAN_MATCH)


def campaign_from_entry(key: str, e: dict, live_r: dict | None, now: datetime) -> store.Campaign:
    c = store.Campaign(id=key, title=e.get("title"), grp=e.get("group"),
                       from_orphan=bool(e.get("from_orphan")), last_seen_live_at=db.ts(now))
    w = (live_r or {}).get("window") or {}
    if live_r:
        # Окно из ТЕКУЩЕЙ записи: сохранённый end мог устареть, и сравнение с ним
        # дало бы ложное «Продлили».
        c.known_start, c.known_end = iso(w.get("start")), iso(w.get("end"))
        c.known_cost = live_r.get("cost")
    else:
        c.known_start, c.known_end = e.get("start"), e.get("end")
    c.known_vague = bool(e.get("dates_vague")) and not e.get("dates_confirmed")
    cond_unknown = bool(e.get("cond_vague")) and not e.get("cond_known")
    if not cond_unknown:
        c.known_condition = (live_r or {}).get("condition") or MIGRATED_CONDITION
    if e.get("appeared", True):
        c.stages.add("announce")
    for flag, stage in (("started", "started"), ("ending", "ending"),
                        ("dates_confirmed", "dates"), ("cond_known", "cond")):
        if e.get(flag):
            c.stages.add(stage)
    return c


def migrate_state(conn, data_dir: Path, *, now: datetime, overrides: dict | None = None):
    """Перенос в уже созданную пустую БД. Возвращает сводку."""
    data_dir = Path(data_dir)
    published = _read_json(data_dir / "published.json", None)
    if not isinstance(published, dict):
        raise db.DbError(f"{data_dir}/published.json нет или он битый — переносить нечего")
    snapshot = _read_json(data_dir / "streamdb_latest.json", None)
    if not isinstance(snapshot, dict) or not snapshot.get("badges"):
        raise db.DbError(f"{data_dir}/streamdb_latest.json нет или пуст")
    known_windows = _read_json(data_dir / "known_windows.json", {})
    overrides = clean_overrides(overrides or {})
    live, _ = live_records(snapshot, now, known_windows, overrides)

    campaigns = {k: campaign_from_entry(k, e, live.get(k), now) for k, e in published.items()
                 if isinstance(e, dict)}
    # Орфаны, которые уже заместил настоящий значок: алиас на него, если нашли.
    alias_of = {}
    for k, e in published.items():
        if not (isinstance(e, dict) and e.get("from_orphan") and e.get("superseded")):
            continue
        target = next((t for t, te in sorted(published.items(), key=lambda kv: kv[0] not in live)
                       if t != k and isinstance(te, dict) and not te.get("from_orphan")
                       and _windows_match(e.get("start"), e.get("end"), te.get("start"), te.get("end"))),
                      None)
        if target:
            alias_of[k] = target
        else:
            campaigns[k].stages.clear()           # запись без стадий (приложение B)

    mtime = datetime.fromtimestamp((data_dir / "streamdb_latest.json").stat().st_mtime, timezone.utc)
    with db.tx(conn):
        for c in campaigns.values():
            store.upsert_campaign(conn, c, now)
            for st in sorted(c.stages):
                store.add_stage(conn, c.id, st, None, now)
        for alias, target in sorted(alias_of.items()):
            store.add_alias(conn, alias, target, "migrated: superseded orphan", now)
        db.kv_replace_ns(conn, "known_windows", known_windows, now)
        db.kv_replace_ns(conn, "category_urls", _read_json(data_dir / "category_urls.json", {}), now)
        mon = _read_json(data_dir / "monitor_state.json", {})
        db.kv_replace_ns(conn, "monitor", mon if isinstance(mon, dict) else {}, now)
        for p in sorted((data_dir / "alerts").glob("*")) if (data_dir / "alerts").is_dir() else []:
            if p.name.startswith("."):
                continue
            raw = p.read_text().strip()
            last = datetime.fromtimestamp(int(raw) if raw.isdigit() else p.stat().st_mtime,
                                          timezone.utc)
            conn.execute("INSERT OR REPLACE INTO alerts(key, active, subject, first_at, last_sent_at) "
                         "VALUES(?, 1, ?, ?, ?)", (p.name, "перенесено из data/alerts",
                                                   db.ts(last), db.ts(last)))
        store.put_snapshot(conn, snapshot, signature="migrated", committed_at=mtime)
        db.set_meta(conn, "migrated_from", str(data_dir))
        db.set_meta(conn, "migrated_at", db.ts(now))
    return {"campaigns": len(campaigns) - len(alias_of), "aliases": len(alias_of),
            "stages": sum(len(c.stages) for k, c in campaigns.items() if k not in alias_of),
            "live": len(live), "known_windows": len(known_windows)}


def export_state(conn) -> dict:
    """БД → published.json старого бота. Алиасы — как заменённые орфаны."""
    out = {}
    camps = store.load_campaigns(conn)
    for c in sorted(camps.values(), key=lambda c: c.id):
        dc = "dates" in c.stages
        ck = "cond" in c.stages
        e = {"set_id": c.id, "group": c.grp, "title": c.title, "end": c.known_end,
             "appeared": "announce" in c.stages, "started": "started" in c.stages,
             "ending": "ending" in c.stages,
             "dates_vague": bool(c.known_vague) or dc,
             "cond_vague": c.known_condition is None or ck,
             "from_orphan": c.from_orphan, "start": c.known_start}
        if dc:
            e["dates_confirmed"] = True
        if ck:
            e["cond_known"] = True
        out[c.id] = e
        for a in sorted(c.aliases):
            out[a] = {"set_id": a, "group": c.grp, "title": c.title, "end": c.known_end,
                      "appeared": True, "started": "started" in c.stages,
                      "ending": "ending" in c.stages, "dates_vague": False,
                      "cond_vague": False, "from_orphan": True, "superseded": True,
                      "start": c.known_start}
    return out


def migrate_dir(data_dir: Path, db_path: Path, *, now=None, overrides=None) -> dict:
    """Создать БД и перенести в неё состояние. Существующую БД не трогает."""
    now = now or db.utcnow()
    conn = db.create(db_path, now)
    try:
        summary = migrate_state(conn, data_dir, now=now, overrides=overrides)
    except BaseException:
        conn.close()
        Path(db_path).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        raise
    problems = db.integrity(conn)
    conn.close()
    if problems:
        raise db.DbError("после миграции: " + "; ".join(problems))
    return summary

