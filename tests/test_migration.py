"""C04: БД, перенос состояния, экспорт для отката, целостность."""
import json
import os
import sqlite3

import pytest
from conftest import T0, load_fixture

import harness
from twitch_badges import db, state, store
from twitch_badges.__main__ import main as cli


def make_data_dir(tmp_path, fx=None, published=None):
    fx = fx or load_fixture()
    d = tmp_path / "old-data"
    (d / "alerts").mkdir(parents=True)
    (d / "streamdb_latest.json").write_text(json.dumps(fx["snapshot"]))
    ts = T0.timestamp() - 600
    os.utime(d / "streamdb_latest.json", (ts, ts))
    (d / "published.json").write_text(json.dumps(published if published is not None else fx["published"]))
    (d / "known_windows.json").write_text(json.dumps(fx["known_windows"]))
    (d / "category_urls.json").write_text(json.dumps({"Art": {"url": "https://www.twitch.tv/directory/category/art", "name": "Art"}}))
    (d / "monitor_state.json").write_text(json.dumps({"incomplete_since": {}, "blindspots": []}))
    (d / "alerts" / ".lock").touch()
    (d / "alerts" / "burst").write_text(str(int(T0.timestamp()) - 3600))
    return d


def migrated(tmp_path, name="tb.sqlite3", **kw):
    d = make_data_dir(tmp_path, **kw)
    path = tmp_path / name
    summary = state.migrate_dir(d, path, now=T0)
    return d, path, summary


def test_migrate_fixture(tmp_path):
    d, path, summary = migrated(tmp_path)
    before = {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}
    assert summary["campaigns"] == 49 and summary["aliases"] == 0
    conn = db.open_db(path)
    assert db.integrity(conn) == []
    camps = store.load_campaigns(conn)
    assert set(camps) == set(load_fixture()["published"])
    assert all("announce" in c.stages for c in camps.values())
    # стадии по флагам: started 40, ending 15, dates 3, cond 1
    count = lambda st: sum(st in c.stages for c in camps.values())  # noqa: E731
    assert (count("started"), count("ending"), count("dates"), count("cond")) == (40, 15, 3, 1)
    # живые записи: окно из ТЕКУЩИХ данных, а не сохранённый end (у 19 он пустой)
    wm = camps["wolf-medallion"]
    assert (wm.known_start, wm.known_end, wm.known_cost) == ("2026-09-29T10:00:00Z", "2026-10-27T08:59:00Z", "paid")
    assert db.kv_get(conn, "alerts", "x") is None
    assert [tuple(r) for r in conn.execute("SELECT key, active FROM alerts")] == [("burst", 1)]
    assert len(db.kv_all(conn, "known_windows")) == len(load_fixture()["known_windows"])
    snap = store.current_snapshot(conn)
    assert len(snap.data["badges"]) == 523 and snap.signature == "migrated"
    # исходники не тронуты
    assert before == {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}


def test_condition_knowledge(tmp_path):
    fx = load_fixture()
    fx["published"]["ampersand"].update(cond_vague=True, cond_known=False)
    fx["published"]["chains"].update(cond_vague=True, cond_known=True)
    _, path, _ = migrated(tmp_path, fx=fx)
    camps = store.load_campaigns(db.open_db(path))
    assert camps["ampersand"].known_condition is None
    assert camps["chains"].known_condition and "cond" in camps["chains"].stages
    assert camps["dron-e"].known_condition                     # условие было в анонсе


def test_superseded_orphan_becomes_alias(tmp_path):
    fx = load_fixture()
    pub = fx["published"]
    pub["test-fest"] = {"set_id": "test-fest", "title": "Test Fest", "group": "Test Fest",
                        "appeared": True, "started": True, "from_orphan": True, "superseded": True,
                        "start": "2026-09-29T10:00:00Z", "end": "2026-10-27T09:00:00Z"}
    pub["lonely-orphan"] = {"set_id": "lonely-orphan", "title": "Lonely", "appeared": True,
                            "from_orphan": True, "superseded": True,
                            "start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"}
    _, path, summary = migrated(tmp_path, fx=fx)
    conn = db.open_db(path)
    assert store.aliases(conn) == {"test-fest": "wolf-medallion"}
    camps = store.load_campaigns(conn)
    assert "test-fest" not in camps and camps["wolf-medallion"].aliases == {"test-fest"}
    assert camps["lonely-orphan"].stages == set()
    exported = state.export_state(conn)
    assert exported["test-fest"]["superseded"] and exported["test-fest"]["from_orphan"]


def _norm(dump):
    for rows in dump.values():
        for r in rows:
            for k in ("created_at", "updated_at", "first_at", "last_sent_at", "committed_at"):
                r.pop(k, None)
    dump.pop("meta")
    return dump


def test_round_trip(tmp_path):
    """migrate(export(migrate(x))) == migrate(x)."""
    d, path1, _ = migrated(tmp_path, "a.sqlite3")
    exported = state.export_state(db.open_db(path1))
    d2 = tmp_path / "second"
    import shutil
    shutil.copytree(d, d2, dirs_exist_ok=True)
    os.utime(d2 / "streamdb_latest.json", (T0.timestamp() - 600,) * 2)
    (d2 / "published.json").write_text(json.dumps(exported))
    path2 = tmp_path / "b.sqlite3"
    state.migrate_dir(d2, path2, now=T0)
    assert _norm(db.dump(db.open_db(path1))) == _norm(db.dump(db.open_db(path2)))


def test_export_keeps_legacy_quiet(tmp_path, legacy):
    """Откат: старый бот на экспортированном состоянии ничего не постит."""
    fx = load_fixture()
    _, path, _ = migrated(tmp_path)
    fx["published"] = state.export_state(db.open_db(path))
    sim = legacy(**fx)
    assert sim.tick() == []
    posts = sim.run(T0, harness.timedelta(hours=6), harness.timedelta(days=3))
    ref = legacy(**load_fixture()).run(T0, harness.timedelta(hours=6), harness.timedelta(days=3))
    assert [p.head for p in posts] == [p.head for p in ref]


# ── отказ без засева ──

def test_missing_db_is_error(tmp_path):
    with pytest.raises(db.DbError, match="засева"):
        db.open_db(tmp_path / "nope.sqlite3")
    assert not (tmp_path / "nope.sqlite3").exists()


def test_corrupt_db_is_error(tmp_path):
    _, path, _ = migrated(tmp_path)
    raw = bytearray(path.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)
    path.write_bytes(bytes(raw))
    with pytest.raises(db.DbError):
        db.open_db(path)
    assert cli(["doctor", "--db", str(path)]) == 1


def test_garbage_file_is_error(tmp_path):
    p = tmp_path / "x.sqlite3"
    p.write_text("not a database")
    with pytest.raises(db.DbError):
        db.open_db(p)


def test_schema_mismatch_is_error(tmp_path):
    _, path, _ = migrated(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(db.DbError, match="версия схемы"):
        db.open_db(path)


def test_migrate_refuses_existing_db(tmp_path):
    d, path, _ = migrated(tmp_path)
    with pytest.raises(db.DbError, match="уже существует"):
        state.migrate_dir(d, path, now=T0)


def test_migrate_refuses_broken_source(tmp_path):
    d = make_data_dir(tmp_path)
    (d / "published.json").write_text("{broken")
    with pytest.raises(ValueError):
        state.migrate_dir(d, tmp_path / "x.sqlite3", now=T0)
    assert not (tmp_path / "x.sqlite3").exists()


def test_stage_unique(tmp_path):
    _, path, _ = migrated(tmp_path)
    conn = db.open_db(path)
    with db.tx(conn):
        assert store.add_stage(conn, "wolf-medallion", "extended:2026-11-10") is True
        assert store.add_stage(conn, "wolf-medallion", "extended:2026-11-10") is False
        assert store.add_stage(conn, "wolf-medallion", "announce") is False


def test_cli(tmp_path):
    d = make_data_dir(tmp_path)
    path = tmp_path / "cli.sqlite3"
    assert cli(["migrate", "--from", str(d), "--db", str(path)]) == 0
    assert cli(["doctor", "--db", str(path)]) == 0
    out = tmp_path / "published.json"
    assert cli(["export-state", "--to", str(out), "--db", str(path)]) == 0
    assert len(json.loads(out.read_text())) == 49
    assert cli(["doctor", "--db", str(tmp_path / "none.sqlite3")]) == 1
