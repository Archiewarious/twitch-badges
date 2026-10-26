"""После заливки истории: каналы на других языках «помнят» то же, что русский.

Кампании, стадии и алиасы русского канала копируются с префиксом en:/es: —
планировщик дальше ведёт новые каналы синхронно с русским и не повторяет то,
что уже вышло в заливке. last_message_id каналов — по progress.json (для сверки).
  python sync_state.py OUT_DIR
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = "/var/lib/twitch-badges/twitch_badges.sqlite3"


def main(out_dir):
    prog = json.load(open(Path(out_dir) / "progress.json"))
    conn = sqlite3.connect(DB, isolation_level=None, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(campaigns)")]
    conn.execute("BEGIN IMMEDIATE")
    try:
        ru = [r for r in conn.execute("SELECT * FROM campaigns")
              if not r[0].startswith(("en:", "es:"))]
        for p in ("en:", "es:"):
            for r in ru:
                row = dict(zip(cols, r))
                row["id"] = p + row["id"]
                conn.execute(f"INSERT OR IGNORE INTO campaigns({', '.join(cols)}) "
                             f"VALUES({', '.join('?' * len(cols))})", [row[c] for c in cols])
            conn.execute("INSERT OR IGNORE INTO stages(campaign_id, stage, outbox_id, created_at) "
                         "SELECT ? || campaign_id, stage, NULL, created_at FROM stages "
                         "WHERE campaign_id NOT LIKE 'en:%' AND campaign_id NOT LIKE 'es:%'", (p,))
            conn.execute("INSERT OR IGNORE INTO aliases(alias, campaign_id, reason, created_at) "
                         "SELECT ? || alias, ? || campaign_id, reason, created_at FROM aliases "
                         "WHERE alias NOT LIKE 'en:%' AND alias NOT LIKE 'es:%'", (p, p))
        for ch in ("en", "es"):
            ids = [i for v in prog.get(ch, {}).values() for i in v]
            if ids:
                conn.execute("INSERT INTO kv(ns, key, value, updated_at) VALUES('channel', ?, ?, ?) "
                             "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, "
                             "updated_at=excluded.updated_at",
                             (f"last_message_id:{ch}", json.dumps(max(ids)), now))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    for p in ("en:", "es:"):
        n = conn.execute("SELECT count(*) FROM campaigns WHERE id LIKE ?", (p + "%",)).fetchone()[0]
        s = conn.execute("SELECT count(*) FROM stages WHERE campaign_id LIKE ?", (p + "%",)).fetchone()[0]
        print(p, "кампаний", n, "стадий", s)


if __name__ == "__main__":
    main(sys.argv[1])
