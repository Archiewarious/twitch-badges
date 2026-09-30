"""Конфигурация из окружения и env-файла (KEY=VALUE, без исполнения bash)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TRUE = {"1", "true", "yes", "on"}


def parse_env_file(path: Path) -> dict:
    """KEY=VALUE построчно. # — комментарий, кавычки вокруг значения снимаются.
    Ничего не исполняется (старый alert.sh делал `source .env`)."""
    out = {}
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key.isidentifier():
            out[key] = val
    return out


@dataclass(frozen=True)
class Config:
    data_dir: Path
    bot_token: str = ""
    channel_id: str = ""
    storage_chat_id: str = ""
    alert_chat_id: str = ""
    alert_bot_token: str = ""
    publish_enabled: bool = False
    quiet_start: int = 0
    quiet_end: int = 0
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    deadman_url: str = ""
    telegram_api_base: str = ""
    sd_base_url: str = "https://www.streamdatabase.com"
    faults: frozenset = frozenset()
    overrides_file: Path = Path(__file__).resolve().parent.parent / "manual" / "overrides.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "twitch_badges.sqlite3"

    @property
    def cards_dir(self) -> Path:
        return self.data_dir / "cards"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"


def load(env: dict | None = None, env_file: Path | None = None) -> Config:
    """Окружение процесса поверх env-файла (TB_ENV_FILE)."""
    env = dict(os.environ if env is None else env)
    path = env_file or env.get("TB_ENV_FILE")
    if path:
        env = {**parse_env_file(Path(path)), **env}
    g = env.get

    def num(key, default):
        try:
            return int(g(key, default))
        except (TypeError, ValueError):
            return default

    return Config(
        data_dir=Path(g("DATA_DIR") or "data-tb").resolve(),
        bot_token=g("TELEGRAM_BOT_TOKEN", ""),
        channel_id=(g("TELEGRAM_CHANNEL_ID") or "").strip(),
        storage_chat_id=(g("TELEGRAM_STORAGE_CHAT_ID") or "").strip(),
        alert_chat_id=(g("ALERT_CHAT_ID") or "").strip(),
        alert_bot_token=g("ALERT_BOT_TOKEN", ""),
        publish_enabled=(g("PUBLISH_ENABLED") or "").strip().lower() in TRUE,
        quiet_start=num("QUIET_HOURS_START", 0),
        quiet_end=num("QUIET_HOURS_END", 0),
        twitch_client_id=g("TWITCH_CLIENT_ID", ""),
        twitch_client_secret=g("TWITCH_CLIENT_SECRET", ""),
        deadman_url=g("DEADMAN_URL", ""),
        telegram_api_base=g("TELEGRAM_API_BASE", ""),
        sd_base_url=(g("SD_BASE_URL") or "https://www.streamdatabase.com").rstrip("/"),
        faults=frozenset(x for x in (g("TB_FAULTS") or "").split(",") if x),
        **({"overrides_file": Path(g("OVERRIDES_FILE"))} if g("OVERRIDES_FILE") else {}),
    )
