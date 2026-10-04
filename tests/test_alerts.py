"""C10: алерты — тексты, дедуп, повтор, восстановление; мониторы; shell-скрипты."""
import asyncio
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import mutations as m
import pytest
from conftest import REPO, T0, load_fixture

from twitch_badges import alerts, db, monitors
from twitch_badges.domain.records import RecordsContext, build_records
from twitch_badges.publisher.captions import watch_target
from twitch_badges.sources import sd_parse


@pytest.fixture
def conn(tmp_path):
    return db.create(tmp_path / "a.sqlite3", T0)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def test_render_has_four_answers():
    t = alerts.render("channel-forbidden", "бот не может писать в канал", "детали")
    for part in ("бот не может писать в канал", "детали", "Влияние на канал:",
                 "Чья сторона:", "Что делать:", "channel-forbidden"):
        assert part in t
    assert all(k in alerts.CATALOG for k in (
        "collector-failing", "format-drift", "post-failed", "channel-forbidden", "post-unknown",
        "telegram-unreachable", "posting-paused-stale", "burst", "anomalies", "blindspots",
        "helix-auth", "db-integrity"))


def test_sender_dedup_repeat_recovery(conn):
    sent, clock = [], Clock()

    async def send(text):
        sent.append(text)
    s = alerts.AlertSender(conn, send, clock)
    with db.tx(conn):
        alerts.raise_alert(conn, "burst", "7 групп", "…", clock.now)
    assert asyncio.run(s.flush()) == 1 and sent[-1].startswith("🔴")
    with db.tx(conn):
        alerts.raise_alert(conn, "burst", "8 групп", "…", clock.now)    # тот же инцидент
    clock.now += timedelta(hours=1)
    assert asyncio.run(s.flush()) == 0
    clock.now += timedelta(hours=6)
    assert asyncio.run(s.flush()) == 1 and sent[-1].startswith("🔁 Всё ещё") and "8 групп" in sent[-1]
    with db.tx(conn):
        alerts.clear_alert(conn, "burst", "очередь в норме", clock.now)
    assert asyncio.run(s.flush()) == 1 and sent[-1].startswith("✅") and "очередь в норме" in sent[-1]
    assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0
    assert asyncio.run(s.flush()) == 0


def test_cleared_before_sent_is_silent(conn):
    sent = []

    async def send(text):
        sent.append(text)
    with db.tx(conn):
        alerts.raise_alert(conn, "anomalies", "x")
        alerts.clear_alert(conn, "anomalies", "ok")
    assert asyncio.run(alerts.AlertSender(conn, send).flush()) == 0 and sent == []


def test_send_failure_retried(conn):
    calls = []

    async def send(text):
        calls.append(text)
        if len(calls) == 1:
            raise OSError("net")
    s = alerts.AlertSender(conn, send)
    with db.tx(conn):
        alerts.raise_alert(conn, "db-integrity", "x")
    assert asyncio.run(s.flush()) == 0
    assert asyncio.run(s.flush()) == 1 and len(calls) == 2


# ── мониторы ──

def _recs(snap, now=T0):
    return build_records(snap, RecordsContext(now=now))


def test_blindspots_and_anomalies_quiet_on_fixture(conn):
    snap = load_fixture()["snapshot"]
    recs = _recs(snap)
    assert monitors.blindspots(conn, snap, recs, T0) == {}
    assert monitors.anomalies(conn, recs, T0) == []
    assert alerts.active(conn) == {}


def test_blindspot_reported_once(conn):
    snap = load_fixture()["snapshot"]
    snap["badges"].append(m.catalog_badge("test-silent", "Test Silent", added_at=T0 - m.days(2)))
    recs = _recs(snap)
    assert set(monitors.blindspots(conn, snap, recs, T0)) == {"test-silent"}
    assert "Test Silent" in alerts.active(conn)["blindspots"]["body"]
    with db.tx(conn):
        conn.execute("UPDATE alerts SET last_sent_at=?", (db.ts(T0),))
    monitors.blindspots(conn, snap, recs, T0 + m.hours(3))
    assert alerts.active(conn)["blindspots"]["last_sent_at"] == db.ts(T0)   # не переподнята
    assert monitors.blindspots(conn, snap, recs, T0, ignore={"test-silent"}) == {}


def test_anomaly_after_24h(conn):
    snap = load_fixture()["snapshot"]
    m.new_badge(snap, "test-nc", "Test NC", start=T0 - m.hours(1), end=T0 + m.days(9),
                added_at=T0 - m.hours(2))
    m.clear_condition(snap, "test-nc")
    ev = m.find_event(snap, "Test NC")
    ev["twitch_global_badges"][0]["availability"][0]["categories"] = []
    assert monitors.anomalies(conn, _recs(snap), T0) == []
    issues = monitors.anomalies(conn, _recs(snap, T0 + m.hours(25)), T0 + m.hours(25))
    assert len(issues) == 1 and "Test NC" in issues[0] and "anomalies" in alerts.active(conn)


def _channel_badge(snap, set_id, title, *, categories=(), channels=0, logins=None):
    """Значок «смотри эфир 1 час» с каналами раздачи (после trim_channels)."""
    m.new_badge(snap, set_id, title, start=T0 - m.hours(1), end=T0 + m.days(9),
                added_at=T0 - m.hours(2))
    av = m.find_event(snap, title)["twitch_global_badges"][0]["availability"][0]
    av["categories"] = [{"id": str(i), "name": n} for i, n in enumerate(categories)]
    av["channel_count"] = channels
    if logins:
        av["channel_logins"] = logins
    return av


def _anomalies_after_day(conn, snap):
    monitors.anomalies(conn, _recs(snap), T0)
    later = T0 + m.hours(25)
    return monitors.anomalies(conn, _recs(snap, later), later)


def test_anomaly_quiet_when_categories_listed(conn):
    """Yellow Party Hat (04.10.2026): 4 канала и 3 категории — пост перечислял
    категории со ссылками, а монитор кричал «ни условия, ни ссылки»."""
    snap = load_fixture()["snapshot"]
    _channel_badge(snap, "test-yph", "Test YPH", channels=4,
                   categories=("RuneScape", "Old School RuneScape", "RuneScape: Dragonwilds"))
    assert _anomalies_after_day(conn, snap) == []


def test_single_channel_gives_link(conn):
    snap = load_fixture()["snapshot"]
    _channel_badge(snap, "test-one", "Test One", channels=1, logins=["oldschoolrs"])
    r = next(r for r in _recs(snap) if r["set_id"] == "test-one")
    assert watch_target(r) == ("channel", "oldschoolrs", "https://www.twitch.tv/oldschoolrs")
    assert _anomalies_after_day(conn, snap) == []


def test_anomaly_text_when_condition_known(conn):
    snap = load_fixture()["snapshot"]
    _channel_badge(snap, "test-many", "Test Many", channels=5)
    issues = _anomalies_after_day(conn, snap)
    assert len(issues) == 1 and "условие есть, но неизвестно, где смотреть" in issues[0]


def test_trim_channels_keeps_short_lists():
    chan = lambda login: {"user": {"login": login}}  # noqa: E731
    events = [{"twitch_global_badges": [{"availability": [
        {"channels": [chan("oldschoolrs")]},
        {"channels": [chan(f"s{i}") for i in range(4)]},
    ]}]}]
    one, many = sd_parse.trim_channels(events)[0]["twitch_global_badges"][0]["availability"]
    assert one == {"channel_count": 1, "channel_logins": ["oldschoolrs"]}
    assert many == {"channel_count": 4}


def test_collector_and_telegram_health(conn):
    with db.tx(conn):
        db.kv_set(conn, "collector", "state", {"last_ok_at": T0.isoformat(),
                                               "last_error": {"kind": "source_http", "error": "503"}})
    monitors.collector_health(conn, T0 + m.hours(2))
    assert "collector-failing" not in alerts.active(conn)
    monitors.collector_health(conn, T0 + m.hours(4))
    assert "503" in alerts.active(conn)["collector-failing"]["body"]
    monitors.telegram_health(conn, T0, ok=True)
    monitors.telegram_health(conn, T0 + timedelta(minutes=10), ok=False, error="timeout")
    assert "telegram-unreachable" not in alerts.active(conn)
    monitors.telegram_health(conn, T0 + timedelta(minutes=16), ok=False, error="timeout")
    assert "telegram-unreachable" in alerts.active(conn)
    monitors.telegram_health(conn, T0 + timedelta(minutes=20), ok=True)
    assert "telegram-unreachable" not in alerts.active(conn)


# ── shell ──

def _stub_bin(tmp_path, systemctl_active=True, restarts=0, curl_ok=True):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "curl").write_text(f"""#!/bin/bash
printf '%s\\n' "$@" >> "{tmp_path}/curl.args"
if [[ " $* " == *" -K - "* ]]; then cat >> "{tmp_path}/curl.stdin"; fi
exit {0 if curl_ok else 7}
""")
    (b / "systemctl").write_text(f"""#!/bin/bash
case "$1" in
  is-active) exit {0 if systemctl_active else 3} ;;
  show) echo {restarts} ;;
esac
""")
    (b / "hostname").write_text("#!/bin/bash\necho testhost\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _sh(tmp_path, script, *args, env=None, **stub):
    b = _stub_bin(tmp_path, **stub)
    e = {"PATH": f"{b}:/usr/bin:/bin", "TB_ENV_FILE": str(tmp_path / "env"),
         "ALERT_STATE_DIR": str(tmp_path / "st"), "WATCHDOG_PROBE_SLEEP": "0", **(env or {})}
    return subprocess.run([str(REPO / "monitor" / script), *args], env=e, capture_output=True,
                          text=True, timeout=60)


def _envfile(tmp_path, extra=""):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "env").write_text(
        f'DATA_DIR={tmp_path / "data"}\nALERT_BOT_TOKEN="111:SECRET-TOKEN"\nALERT_CHAT_ID=42\n'
        f'EVIL=$(touch {tmp_path}/pwned)\n{extra}')


def test_alert_sh_dry_run_dedup(tmp_path):
    _envfile(tmp_path)
    env = {"ALERT_DRY_RUN": "1"}
    r = _sh(tmp_path, "alert.sh", "k1", "тема", "тело", env=env)
    assert r.returncode == 0 and "DRY-RUN alert: [testhost] 🔴: тема" in r.stdout
    assert _sh(tmp_path, "alert.sh", "k1", "тема", env=env).stdout == ""
    assert "Восстановлено: k1" in _sh(tmp_path, "alert.sh", "--clear", "k1", env=env).stdout
    assert _sh(tmp_path, "alert.sh", "--clear", "k1", env=env).stdout == ""
    assert not (tmp_path / "pwned").exists()                 # env-файл не исполняется


def test_alert_sh_token_via_stdin(tmp_path):
    _envfile(tmp_path)
    r = _sh(tmp_path, "alert.sh", "k2", "тема")
    assert r.returncode == 0, r.stderr
    args = (tmp_path / "curl.args").read_text()
    assert "SECRET-TOKEN" not in args and "chat_id=42" in args
    assert "bot111:SECRET-TOKEN/sendMessage" in (tmp_path / "curl.stdin").read_text()
    assert not (tmp_path / "pwned").exists()


def test_watchdog_sh(tmp_path):
    _envfile(tmp_path, "DEADMAN_URL=https://hc-ping.invalid/uuid\n")
    data = tmp_path / "data"
    (data / "heartbeat-bot").touch()
    (data / "heartbeat-collector").touch()
    env = {"ALERT_DRY_RUN": "1"}
    r = _sh(tmp_path, "watchdog.sh", env=env)
    assert r.returncode == 0 and "DRY-RUN alert" not in r.stdout, r.stdout + r.stderr
    old = T0.timestamp() - 3600 * 5
    os.utime(data / "heartbeat-bot", (old, old))
    os.utime(data / "heartbeat-collector", (old, old))
    r = _sh(tmp_path, "watchdog.sh", env=env)
    assert "цикл молчит" in r.stdout and "сбор данных не отрабатывает" in r.stdout
    r = _sh(tmp_path, "watchdog.sh", env={**env, "ALERT_RENOTIFY": "0"}, systemctl_active=False)
    assert "бот не работает" in r.stdout
    r = _sh(tmp_path, "watchdog.sh", env=env, restarts=10, curl_ok=False)
    assert "Telegram с сервера недоступен" in r.stdout


def test_shellcheck():
    import shutil
    import sys
    sc = shutil.which("shellcheck") or str(Path(sys.prefix) / "bin" / "shellcheck")
    if not Path(sc).exists():
        pytest.skip("shellcheck не установлен")
    scripts = [*(REPO / "monitor").glob("*.sh"), *(REPO / "deploy").glob("*.sh")]
    r = subprocess.run([sc, *map(str, scripts)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
