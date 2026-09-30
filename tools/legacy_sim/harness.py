"""Харнесс СТАРОЙ логики публикации (bot/bot.py + generate_site.py) для тестов.

Гоняет настоящий `publish_new` на отдельном каталоге данных с подменёнными
часами, фейковым Telegram и заглушками алертов. Сеть не трогается, боевой
токен не нужен, `monitor/alert.sh` не вызывается.

    sim = LegacySim(workdir, snapshot=..., published=..., known_windows=...,
                    media={"images": [...], "cards": [...]}, now=T0)
    posts = sim.tick()                     # один тик publish_new
    posts = sim.run(T0, timedelta(hours=1), timedelta(days=40))

Модуль бота импортируется один раз на процесс; каждый LegacySim перенастраивает
его глобальные пути и сбрасывает кэш, поэтому экземпляры можно создавать
последовательно (но не использовать одновременно).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime as _dt, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Всё окружение задаём явно ДО импорта бота: он читает его на уровне модуля.
# Значения из чужого .env (если харнесс запустят в прод-папке) не перекроют эти.
_ENV = {
    "TELEGRAM_BOT_TOKEN": "0:legacy-sim",
    "TELEGRAM_CHANNEL_ID": "-1000000000000",
    "PUBLISH_ENABLED": "true",
    "QUIET_HOURS_START": "0",
    "QUIET_HOURS_END": "0",
    "SITE_URL": "http://legacy-sim.invalid",
    "ALERT_CHAT_ID": "",
}


class _Clock:
    value = _dt(2026, 9, 30, tzinfo=timezone.utc)


class FrozenDatetime(_dt):
    """datetime, у которого now() — часы харнесса."""

    @classmethod
    def now(cls, tz=None):
        v = _Clock.value
        return v.astimezone(tz) if tz else v.replace(tzinfo=None)


def _import_legacy():
    os.environ.update(_ENV)
    for p in (str(REPO), str(REPO / "bot")):
        if p not in sys.path:
            sys.path.insert(0, p)
    with contextlib.redirect_stderr(io.StringIO()):
        import bot as b  # noqa: PLC0415
    b.datetime = FrozenDatetime
    b.site.datetime = FrozenDatetime
    return b


bot = _import_legacy()
site = bot.site


@dataclass
class Post:
    at: _dt
    kind: str                 # photo | album
    media: list[str]
    caption: str
    buttons: bool = False

    @property
    def lines(self) -> list[str]:
        """Непустые строки подписи без HTML."""
        return [re.sub(r"<[^>]+>", "", s) for s in self.caption.split("\n") if s.strip()]

    @property
    def head(self) -> str:
        return " / ".join(self.lines[:2])


@dataclass
class Alert:
    at: _dt
    key: str
    subject: str
    cleared: bool = False


class _FakeBot:
    def __init__(self, sim):
        self.sim = sim

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        self.sim.posts.append(Post(_Clock.value, "photo", [photo], caption or "",
                                   buttons=reply_markup is not None))

    async def send_media_group(self, chat_id, media, **kw):
        self.sim.posts.append(Post(_Clock.value, "album", [m.media for m in media],
                                   media[0].caption or ""))
        return []


class _Ctx:
    pass


@dataclass
class LegacySim:
    workdir: Path
    snapshot: dict
    published: dict | None
    known_windows: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)       # {"images": [key], "cards": [key]}
    overrides: dict | None = None
    now: _dt = field(default_factory=lambda: _Clock.value)

    def __post_init__(self):
        self.workdir = Path(self.workdir)
        d = self.workdir
        for sub in ("images", "cards"):
            (d / sub).mkdir(parents=True, exist_ok=True)
            for key in self.media.get(sub, []):
                (d / sub / f"{key}.png").touch()
        self.posts: list[Post] = []
        self.alerts: list[Alert] = []
        (d / "known_windows.json").write_text(json.dumps(self.known_windows))
        (d / "overrides.json").write_text(json.dumps(self.overrides or {}))
        if self.published is not None:
            (d / "published.json").write_text(json.dumps(self.published))
        self._configure()
        self.set_now(self.now)
        self.set_snapshot(self.snapshot)

    # ── настройка модуля бота на этот каталог ──
    def _configure(self):
        d = self.workdir
        bot.DATA_FILE = d / "streamdb_latest.json"
        bot.IMAGES_DIR = d / "images"
        bot.CARDS_DIR = d / "cards"
        bot.PUBLISHED_FILE = d / "published.json"
        bot.HEARTBEAT_FILE = d / "bot_alive"
        bot.MONITOR_STATE = d / "monitor_state.json"
        bot.IGNORE_FILE = d / "ignore.txt"
        site.KNOWN_WINDOWS_FILE = d / "known_windows.json"
        site.OVERRIDES_FILE = d / "overrides.json"
        site.IMAGES_DIR = d / "images"
        bot._cache.update(records=None, mtime=None)
        bot._publish_fail_streak = 0

        async def fake_alert(key, subject, body=""):
            self.alerts.append(Alert(_Clock.value, key, subject))

        async def fake_clear(key, note=""):
            self.alerts.append(Alert(_Clock.value, key, note, cleared=True))

        bot.send_alert, bot.clear_alert = fake_alert, fake_clear
        self._ctx = _Ctx()
        self._ctx.bot = _FakeBot(self)

    def set_now(self, now: _dt):
        _Clock.value = now
        self.now = now

    def set_snapshot(self, snapshot: dict):
        """Новый снапшот «только что закоммичен» (mtime = текущие часы)."""
        self.snapshot = snapshot
        bot.DATA_FILE.write_text(json.dumps(snapshot))
        self.touch_snapshot()

    def touch_snapshot(self, at: _dt | None = None):
        ts = (at or _Clock.value).timestamp()
        os.utime(bot.DATA_FILE, (ts, ts))
        bot._cache.update(records=None, mtime=None)

    def records(self) -> list[dict]:
        bot._cache.update(records=None, mtime=None)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return bot.get_records()

    def shown(self) -> list[dict]:
        return [r for r in self.records() if bot.is_shown(r)]

    @property
    def state(self) -> dict | None:
        p = bot.PUBLISHED_FILE
        return json.loads(p.read_text()) if p.exists() else None

    def tick(self, now: _dt | None = None, fresh: bool = True) -> list[Post]:
        """Один проход publish_new. fresh=True — снапшот свежий (mtime = now)."""
        if now is not None:
            self.set_now(now)
        if fresh:
            self.touch_snapshot()
        n = len(self.posts)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(self._publish())
        return self.posts[n:]

    async def _publish(self):
        # Паузы между частями альбома (asyncio.sleep(2)) — пропускаем, но только
        # на время тика: asyncio общий для всего процесса.
        real_sleep = asyncio.sleep

        async def no_sleep(*a, **k):
            await real_sleep(0)

        asyncio.sleep = no_sleep
        try:
            await bot.publish_new(self._ctx)
        finally:
            asyncio.sleep = real_sleep

    def monitor(self, job: str = "check_blindspots") -> list[Alert]:
        """Один прогон монитора бота (check_blindspots | check_anomalies)."""
        n = len(self.alerts)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(getattr(bot, job)(self._ctx))
        return self.alerts[n:]

    def run(self, start: _dt, step: timedelta, span: timedelta) -> list[Post]:
        """Прокрутка времени на неизменных данных: тик на каждом шаге."""
        n = len(self.posts)
        t = timedelta(0)
        while t <= span:
            self.tick(start + t)
            t += step
        return self.posts[n:]


def load_data_dir(data: Path) -> dict:
    """Аргументы LegacySim из настоящего каталога data/ (только чтение)."""
    data = Path(data)

    def rd(name, default):
        p = data / name
        return json.loads(p.read_text()) if p.exists() else default

    return {
        "snapshot": rd("streamdb_latest.json", {}),
        "published": rd("published.json", None),
        "known_windows": rd("known_windows.json", {}),
        "media": {d: [p.stem for p in (data / d).glob("*.png")] for d in ("images", "cards")},
    }
