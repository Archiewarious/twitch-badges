"""Бэкап БД: онлайн-копия SQLite (backup API), 14 последних локально."""
from __future__ import annotations

import sqlite3
from pathlib import Path

KEEP = 14


def backup(conn, backup_dir: Path, now) -> Path:
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"twitch_badges-{now:%Y%m%d-%H%M}.sqlite3"
    tmp = path.with_suffix(".tmp")
    dst = sqlite3.connect(str(tmp))
    try:
        conn.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        dst.close()
    if not ok:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("копия БД не прошла integrity_check")
    tmp.replace(path)
    old = sorted(backup_dir.glob("twitch_badges-*.sqlite3"))[:-KEEP]
    for p in old:
        p.unlink()
    return path
