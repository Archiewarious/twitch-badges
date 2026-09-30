"""CLI: python -m twitch_badges <команда>.

    migrate --from DIR       перенести состояние старой установки в новую БД
    export-state --to FILE   БД → published.json старого формата (откат)
    doctor                   целостность БД и инварианты
    collect [--force]        один прогон сбора (его запускает tb-collector.timer)
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
        return 1 if res.action == "failed" else 0
    finally:
        lock.release()


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
    p = sub.add_parser("collect")
    p.add_argument("--db")
    p.add_argument("--force", action="store_true")
    p.add_argument("--deadline", type=float, default=480)
    args = ap.parse_args(argv)
    cfg = config.load(env_file=Path(args.env_file) if args.env_file else None)
    try:
        return {"migrate": cmd_migrate, "export-state": cmd_export, "doctor": cmd_doctor,
                "collect": cmd_collect}[args.cmd](args, cfg)
    except db.DbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
