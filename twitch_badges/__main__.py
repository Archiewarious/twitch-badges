"""CLI: python -m twitch_badges <команда>.

    migrate --from DIR       перенести состояние старой установки в новую БД
    export-state --to FILE   БД → published.json старого формата (откат)
    doctor                   целостность БД и инварианты
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
    args = ap.parse_args(argv)
    cfg = config.load(env_file=Path(args.env_file) if args.env_file else None)
    try:
        return {"migrate": cmd_migrate, "export-state": cmd_export, "doctor": cmd_doctor}[args.cmd](args, cfg)
    except db.DbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
