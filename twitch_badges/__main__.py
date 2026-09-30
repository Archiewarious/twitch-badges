"""CLI: python -m twitch_badges <команда>.

    migrate --from DIR       перенести состояние старой установки в новую БД
    export-state --to FILE   БД → published.json старого формата (откат)
    doctor                   целостность БД и инварианты
    collect [--force]        один прогон сбора (его запускает tb-collector.timer)
    bot                      процесс бота (tb-bot.service)
    plan --dry-run           что бот опубликовал бы сейчас (ничего не отправляет)
    status                   состояние: данные, очередь, тревоги
    backup                   копия БД в DATA_DIR/backups
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, db
from .state import export_state, migrate_dir

REPO = Path(__file__).resolve().parent.parent


def _db_path(args, cfg):
    return Path(args.db) if getattr(args, "db", None) else cfg.db_path


def cmd_migrate(args, cfg):
    ov_path = Path(args.overrides) if args.overrides else REPO / "manual" / "overrides.json"
    try:
        overrides = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    except ValueError as e:
        print(f"overrides не прочитан ({e})", file=sys.stderr)
        return 1
    summary = migrate_dir(Path(getattr(args, "from")), _db_path(args, cfg), overrides=overrides)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_export(args, cfg):
    conn = db.open_db(_db_path(args, cfg))
    data = export_state(conn)
    conn.close()
    out = Path(args.to)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(out)
    print(f"{len(data)} записей → {out}")
    return 0


def cmd_doctor(args, cfg):
    try:
        conn = db.open_db(_db_path(args, cfg), check=False)
    except db.DbError as e:
        print(f"ПЛОХО: {e}")
        return 1
    problems = db.integrity(conn)
    conn.close()
    if problems:
        print("ПЛОХО:\n  " + "\n  ".join(problems))
        return 1
    print("ok")
    return 0


def make_sources(cfg, http, conn):
    """Настоящие источники: SD, Helix (если есть ключи), GQL, CDN картинок."""
    from .sources import gql
    from .sources.helix import Helix, credentials_ok
    from .sources.streamdb import StreamDB
    from .collector import Sources

    helix = None
    if credentials_ok(cfg.twitch_client_id, cfg.twitch_client_secret):
        def token_set(v):
            with db.tx(conn):
                if v is None:
                    conn.execute("DELETE FROM kv WHERE ns='helix_token'")
                else:
                    db.kv_set(conn, "helix_token", "app", v)
        helix = Helix(http, cfg.twitch_client_id, cfg.twitch_client_secret,
                      lambda: db.kv_get(conn, "helix_token", "app"), token_set, db.utcnow)

    def fetch_image(url):
        r = http.get(url, ok_404=True)
        return r.status_code, r.headers.get("content-type"), r.content

    return Sources(sd=StreamDB(http, cfg.sd_base_url), helix=helix,
                   categories_lookup=gql.Categories(http).lookup, fetch_image=fetch_image)


def cmd_collect(args, cfg):
    import logging
    from .collector import run_once
    from .lock import AlreadyRunning, InstanceLock
    from .sources.http import Http

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        lock = InstanceLock(cfg.data_dir / "collector.lock").acquire()
    except AlreadyRunning:
        print("предыдущий сбор ещё идёт — пропускаю")
        return 0
    try:
        conn = db.open_db(_db_path(args, cfg))
        http = Http(deadline=args.deadline)
        try:
            res = run_once(conn, make_sources(cfg, http, conn), now=db.utcnow(),
                           images_dir=cfg.images_dir, cards_dir=cfg.cards_dir,
                           overrides_path=cfg.overrides_file, force=args.force)
        finally:
            http.close()
        print(json.dumps({"action": res.action, "reason": res.reason}, ensure_ascii=False))
        if res.action != "failed":
            (cfg.data_dir / "heartbeat-collector").touch()     # для watchdog.sh
        return 1 if res.action == "failed" else 0
    finally:
        lock.release()


def cmd_bot(args, cfg):
    from .bot import main as bot_main
    return bot_main(cfg)


def cmd_plan(args, cfg):
    from . import store
    from .domain.records import RecordsContext, build
    from .publisher import planner
    from .publisher.service import art_checker

    conn = db.open_db(_db_path(args, cfg))
    snap = store.current_snapshot(conn)
    if snap is None:
        print("снапшотов нет")
        return 1
    now = db.utcnow()
    built = build(snap.data, RecordsContext(
        now=now, known_windows=db.kv_all(conn, "known_windows"),
        overrides=db.kv_get(conn, "overrides", "data", {}) or {}))
    busy = {r[0] for r in conn.execute(
        "SELECT DISTINCT s.campaign_id FROM stages s JOIN outbox o ON o.id=s.outbox_id "
        "WHERE o.status != 'sent'")}
    res = planner.plan(built.records, store.load_campaigns(conn), store.aliases(conn), now=now,
                       data_at=snap.committed, has_art=art_checker(cfg.images_dir),
                       cfg=planner.PlanConfig(quiet_start=cfg.quiet_start, quiet_end=cfg.quiet_end),
                       busy=busy)
    print(f"данные: {snap.committed_at}; постов на этом тике: {len(res.intents)}")
    for it in res.intents:
        print(f"  {it.kind}: {', '.join(it.campaign_ids)}" + (f" ({it.group})" if it.group else ""))
    for a in res.aliases:
        print(f"  алиас {a.alias} → {a.campaign_id}: {a.reason}")
    held = [h for h in res.held if h[2] != "нет арта"]
    for cid, kind, why in held:
        print(f"  придержан {cid} {kind or ''}: {why}")
    for sig in res.alerts:
        if sig.active:
            print(f"  тревога {sig.key}: {sig.subject}")
    return 0


def cmd_status(args, cfg):
    from .owner import status_text
    conn = db.open_db(_db_path(args, cfg))
    print(status_text(conn, db.utcnow()))
    return 0


def cmd_backup(args, cfg):
    from .backup import backup
    conn = db.open_db(_db_path(args, cfg))
    print(backup(conn, cfg.data_dir / "backups", db.utcnow()))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m twitch_badges")
    ap.add_argument("--env-file", help="env-файл KEY=VALUE (по умолчанию TB_ENV_FILE)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("migrate")
    p.add_argument("--from", required=True, help="каталог data/ старой установки (только чтение)")
    p.add_argument("--db")
    p.add_argument("--overrides")
    p = sub.add_parser("export-state")
    p.add_argument("--to", required=True)
    p.add_argument("--db")
    p = sub.add_parser("doctor")
    p.add_argument("--db")
    sub.add_parser("bot")
    for name in ("plan", "status", "backup"):
        p = sub.add_parser(name)
        p.add_argument("--db")
        if name == "plan":
            p.add_argument("--dry-run", action="store_true", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--db")
    p.add_argument("--force", action="store_true")
    p.add_argument("--deadline", type=float, default=480)
    args = ap.parse_args(argv)
    cfg = config.load(env_file=Path(args.env_file) if args.env_file else None)
    try:
        return {"migrate": cmd_migrate, "export-state": cmd_export, "doctor": cmd_doctor,
                "collect": cmd_collect, "bot": cmd_bot, "plan": cmd_plan, "status": cmd_status,
                "backup": cmd_backup}[args.cmd](args, cfg)
    except db.DbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
