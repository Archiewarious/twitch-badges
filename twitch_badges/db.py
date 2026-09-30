"""SQLite: соединение, схема, миграции и проверка целостности.

Один файл БД на установку. Писателей два — сбор (snapshots, kv, runs) и бот
(всё остальное); для WAL с busy_timeout это штатно.

Главное правило: бот НИКОГДА не создаёт БД сам. Нет файла, битый файл или
чужая версия схемы — это отказ и алерт db-integrity, а не «холодный старт»
с засевом (так старый бот молча помечал всё текущее опубликованным, B3).
Создаёт БД только `migrate` (или `init` для чистой установки).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE snapshots(
  id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, committed_at TEXT NOT NULL,
  signature TEXT NOT NULL, payload BLOB NOT NULL);

CREATE TABLE campaigns(
  id TEXT PRIMARY KEY, title TEXT, grp TEXT, from_orphan INTEGER NOT NULL DEFAULT 0,
  known_start TEXT, known_end TEXT, known_vague INTEGER, known_condition TEXT, known_cost TEXT,
  first_posted_at TEXT, last_posted_at TEXT, last_seen_live_at TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE aliases(alias TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id), reason TEXT, created_at TEXT NOT NULL);

CREATE TABLE outbox(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, grp TEXT,
  payload TEXT NOT NULL,
  knowledge TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','unknown','retry','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, sending_at TEXT, sent_at TEXT,
  message_ids TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE stages(
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  stage TEXT NOT NULL,
  outbox_id INTEGER REFERENCES outbox(id),
  created_at TEXT NOT NULL, PRIMARY KEY(campaign_id, stage));

CREATE TABLE media(card_key TEXT NOT NULL, sha256 TEXT NOT NULL, file_id TEXT NOT NULL,
  file_unique_id TEXT, uploaded_at TEXT NOT NULL, PRIMARY KEY(card_key, sha256));

CREATE TABLE kv(ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(ns, key));

CREATE TABLE runs(id INTEGER PRIMARY KEY, job TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, ok INTEGER, error_kind TEXT, error TEXT, stats TEXT);

CREATE TABLE alerts(key TEXT PRIMARY KEY, active INTEGER NOT NULL, subject TEXT, body TEXT,
  first_at TEXT, last_sent_at TEXT, cleared_at TEXT);

CREATE INDEX outbox_status ON outbox(status, next_attempt_at);
CREATE INDEX aliases_campaign ON aliases(campaign_id);
"""

STAGES_FIXED = {"announce", "started", "ending", "dates", "cond"}
OUTBOX_OPEN = ("pending", "sending", "retry", "unknown")


class DbError(RuntimeError):
    """БД нельзя использовать: нет файла, битая, чужая схема. Никакого засева."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: транзакции только явные (BEGIN IMMEDIATE в tx()).
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class tx:
    """Транзакция на запись: with tx(conn): ... (BEGIN IMMEDIATE — без гонок писателей)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


def create(path: Path, now: datetime | None = None) -> sqlite3.Connection:
    """Новая пустая БД. Файл не должен существовать."""
    path = Path(path)
    if path.exists():
        raise DbError(f"{path} уже существует — не перезаписываю")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    # executescript сам коммитит незакрытую транзакцию, поэтому BEGIN/COMMIT — в скрипте
    conn.executescript(
        "BEGIN;" + SCHEMA_V1 +
        f"INSERT INTO meta VALUES('schema_version', '{SCHEMA_VERSION}');"
        f"INSERT INTO meta VALUES('created_at', '{ts(now or utcnow())}');COMMIT;")
    return conn


def open_db(path: Path, check: bool = True) -> sqlite3.Connection:
    """Открыть существующую БД. Нет файла / чужая схема / битая → DbError."""
    path = Path(path)
    if not path.exists():
        raise DbError(f"БД {path} не найдена. Постинг остановлен: создать её может только "
                      "`python -m twitch_badges migrate` (засева «с нуля» нет)")
    try:
        conn = _connect(path)
        ver = get_meta(conn, "schema_version")
    except sqlite3.DatabaseError as e:
        raise DbError(f"БД {path} не читается: {e}") from e
    if ver != str(SCHEMA_VERSION):
        conn.close()
        raise DbError(f"БД {path}: версия схемы {ver!r}, код ждёт {SCHEMA_VERSION}")
    if check:
        problems = integrity(conn, quick=True)
        if problems:
            conn.close()
            raise DbError(f"БД {path} повреждена: {'; '.join(problems[:5])}")
    return conn


def integrity(conn, quick: bool = False) -> list[str]:
    """Проблемы БД (пусто — всё в порядке). quick — только то, что дёшево."""
    out = []
    try:
        res = [r[0] for r in conn.execute(
            "PRAGMA quick_check" if quick else "PRAGMA integrity_check")]
    except sqlite3.DatabaseError as e:
        return [f"integrity_check: {e}"]
    if res != ["ok"]:
        out += [f"integrity_check: {x}" for x in res[:10]]
    try:
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            out.append(f"foreign_key_check: {len(fk)} нарушений, например {tuple(fk[0])}")
        if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
            out.append("meta.schema_version не совпадает")
        if quick:
            return out
        n = conn.execute("SELECT count(*) FROM aliases a JOIN campaigns c ON c.id = a.alias").fetchone()[0]
        if n:
            out.append(f"{n} алиасов совпадают с id кампаний")
        n = conn.execute("SELECT count(*) FROM aliases a JOIN aliases b ON b.alias = a.campaign_id").fetchone()[0]
        if n:
            out.append(f"{n} алиасов указывают на алиас (цепочка)")
        bad = [r[0] for r in conn.execute("SELECT DISTINCT stage FROM stages")
               if r[0] not in STAGES_FIXED and not r[0].startswith("extended:")]
        if bad:
            out.append(f"неизвестные стадии: {bad[:5]}")
        n = conn.execute("SELECT count(*) FROM outbox WHERE status='sent' AND message_ids IS NULL").fetchone()[0]
        if n:
            out.append(f"{n} отправленных строк outbox без message_ids")
    except sqlite3.DatabaseError as e:
        out.append(f"проверка инвариантов: {e}")
    return out


# ── meta / kv ──

def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def kv_get(conn, ns, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
    return json.loads(row[0]) if row else default


def kv_all(conn, ns) -> dict:
    return {r[0]: json.loads(r[1]) for r in
            conn.execute("SELECT key, value FROM kv WHERE ns=? ORDER BY key", (ns,))}


def kv_set(conn, ns, key, value, now=None):
    conn.execute("INSERT INTO kv(ns, key, value, updated_at) VALUES(?, ?, ?, ?) "
                 "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (ns, key, json.dumps(value, ensure_ascii=False, sort_keys=True), ts(now or utcnow())))


def kv_replace_ns(conn, ns, mapping: dict, now=None):
    """Заменить пространство kv целиком (память окон, кэш категорий)."""
    conn.execute("DELETE FROM kv WHERE ns=?", (ns,))
    for k, v in mapping.items():
        kv_set(conn, ns, k, v, now)


def dump(conn) -> dict:
    """Всё содержимое БД (кроме payload снапшотов) — для тестов и отладки."""
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        sel = ", ".join("length(payload)" if c == "payload" else c for c in cols)
        out[name] = [dict(zip(cols, r)) for r in
                     conn.execute(f"SELECT {sel} FROM {name} ORDER BY 1, 2")]
    return out
