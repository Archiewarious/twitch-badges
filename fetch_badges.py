#!/usr/bin/env python3
"""
Собирает список глобальных Twitch-бейджей через Helix API,
сохраняет снапшот и показывает, что появилось/исчезло/изменилось
по сравнению с предыдущим запуском.

Всё, что нужно, лежит в этой же папке: .env с credentials,
data/ со снапшотами и changelog.md.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_FILE = DATA_DIR / "latest.json"
CHANGELOG_FILE = ROOT / "changelog.md"
TOKEN_CACHE = DATA_DIR / ".token_cache.json"

TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_BADGES_URL = "https://api.twitch.tv/helix/chat/badges/global"


def get_app_token(client_id: str, client_secret: str) -> str:
    if TOKEN_CACHE.exists():
        cached = json.loads(TOKEN_CACHE.read_text())
        if cached.get("expires_at", 0) > datetime.now(timezone.utc).timestamp() + 60:
            return cached["access_token"]

    resp = requests.post(
        TWITCH_OAUTH_URL,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    expires_at = datetime.now(timezone.utc).timestamp() + payload["expires_in"]
    TOKEN_CACHE.write_text(
        json.dumps({"access_token": payload["access_token"], "expires_at": expires_at})
    )
    return payload["access_token"]


def fetch_global_badges(client_id: str, token: str) -> dict:
    resp = requests.get(
        TWITCH_BADGES_URL,
        headers={
            "Client-Id": client_id,
            "Authorization": f"Bearer {token}",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def normalize(raw: dict) -> dict:
    """set_id -> version_id -> version details, for easy diffing."""
    out = {}
    for badge_set in raw.get("data", []):
        set_id = badge_set["set_id"]
        out[set_id] = {}
        for version in badge_set.get("versions", []):
            out[set_id][version["id"]] = {
                "title": version.get("title", ""),
                "description": version.get("description", ""),
                "click_url": version.get("click_url", ""),
                "image_url_4x": version.get("image_url_4x", ""),
            }
    return out


import re  # noqa: E402

# «...to a streamer in the ELDEN RING category» — Twitch пишет это шаблонно, и
# для 56 значков из 357 это ЕДИНСТВЕННОЕ указание, где значок получать: у SD для
# них нет ни категорий, ни каналов. Без разбора пост выходил «Подписка или гифт»
# без единого слова о месте (так было у Sorcerer Rogier ELDEN RING).
CATEGORY_IN_DESC_RE = re.compile(r"\bin the\s+(.+?)\s+category\b", re.I)
# «watching /PlaqueBoyMax during…» — канал прямо в описании.
CHANNEL_IN_DESC_RE = re.compile(r"(?:^|\s)/([A-Za-z0-9_]{3,25})\b")


# «The Festering Bloody Finger badge will be available…» — SD называет значок в
# тексте события ещё до того, как заведёт его сам. Читателю нужно именно это имя,
# а не название квестлайна: искать он будет значок.
BADGE_NAME_IN_DESC_RE = re.compile(
    r"\bThe\s+(.{2,60}?)\s+badge(?:s)?\s+(?:will\s+be|is|are|was|were)\b")


def badge_name_from_description(desc: str):
    """Имя значка из текста события, иначе None."""
    m = BADGE_NAME_IN_DESC_RE.search(desc or "")
    if not m:
        return None
    name = m.group(1).strip()
    # «Twitch global chat badges for Pichu, Bulbasaur…» — перечисление, не имя.
    return None if "," in name or len(name.split()) > 6 else name


def category_from_description(desc: str):
    """Название категории Twitch из описания значка, иначе None."""
    m = CATEGORY_IN_DESC_RE.search(desc or "")
    return m.group(1).strip() if m else None


def channel_from_description(desc: str):
    """Логин канала из описания значка («/PlaqueBoyMax»), иначе None."""
    m = CHANNEL_IN_DESC_RE.search(desc or "")
    return m.group(1) if m else None


def credentials_present() -> bool:
    """Ключи Twitch реально заданы (а не заглушки из .env.example)."""
    cid = (os.environ.get("TWITCH_CLIENT_ID") or "").strip()
    secret = (os.environ.get("TWITCH_CLIENT_SECRET") or "").strip()
    return bool(cid and secret
                and not cid.startswith("your_") and not secret.startswith("your_"))


def try_collect_info() -> dict:
    """{set_id: {title, description, click_url}} из Helix — ВТОРОЙ источник рядом
    со StreamDatabase. Нужен там, где SD пуст: у свежих кампаний он честно пишет
    «We don't yet know if this badge is earned by subscribing or watching», тогда
    как Twitch у себя уже отдаёт описание значка (напр. «This badge was earned
    during the Pokémon First Partners Collection campaign»), а нередко и само
    условие («...by watching X for 60 minutes»).

    Никогда не бросает: нет ключей или Helix недоступен → {} и работаем на одном
    SD, как раньше. Публичный badges.twitch.tv для этого больше не годится —
    Twitch его убрал, хост не резолвится."""
    load_dotenv(ROOT / ".env")
    if not credentials_present():
        return {}
    try:
        token = get_app_token(os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"])
        raw = fetch_global_badges(os.environ["TWITCH_CLIENT_ID"], token)
    except Exception as e:                       # сеть, 401, смена схемы — не наша беда
        print(f"Helix недоступен ({e}) — работаю только на StreamDatabase", file=sys.stderr)
        return {}
    out = {}
    for badge_set in raw.get("data", []):
        versions = badge_set.get("versions") or []
        if not badge_set.get("set_id") or not versions:
            continue
        v = versions[0]                          # тиры отличаются порогом, не смыслом
        info = {
            "title": (v.get("title") or "").strip(),
            "description": (v.get("description") or "").strip(),
            "click_url": (v.get("click_url") or "").strip(),
        }
        if info["description"] or info["click_url"]:
            out[badge_set["set_id"]] = info
    return out


def diff(old: dict, new: dict) -> list[str]:
    lines = []
    old_sets, new_sets = set(old), set(new)

    for set_id in sorted(new_sets - old_sets):
        for version_id, info in new[set_id].items():
            lines.append(f"+ NEW SET  {set_id}/{version_id} — {info['title']}")

    for set_id in sorted(old_sets - new_sets):
        for version_id, info in old[set_id].items():
            lines.append(f"- REMOVED SET  {set_id}/{version_id} — {info['title']}")

    for set_id in sorted(old_sets & new_sets):
        old_versions, new_versions = old[set_id], new[set_id]
        for version_id in sorted(set(new_versions) - set(old_versions)):
            info = new_versions[version_id]
            lines.append(f"+ NEW VERSION  {set_id}/{version_id} — {info['title']}")
        for version_id in sorted(set(old_versions) - set(new_versions)):
            info = old_versions[version_id]
            lines.append(f"- REMOVED VERSION  {set_id}/{version_id} — {info['title']}")
        for version_id in sorted(set(new_versions) & set(old_versions)):
            if old_versions[version_id] != new_versions[version_id]:
                lines.append(f"~ CHANGED  {set_id}/{version_id} — {new_versions[version_id]['title']}")

    return lines


def main() -> int:
    load_dotenv(ROOT / ".env")
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Нет TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET.\n"
            "Скопируй .env.example в .env и заполни (см. README.md).",
            file=sys.stderr,
        )
        return 1

    DATA_DIR.mkdir(exist_ok=True)

    token = get_app_token(client_id, client_secret)
    raw = fetch_global_badges(client_id, token)
    new_snapshot = normalize(raw)

    old_snapshot = {}
    if LATEST_FILE.exists():
        old_snapshot = json.loads(LATEST_FILE.read_text())

    changes = diff(old_snapshot, new_snapshot)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timestamped = DATA_DIR / f"{now.replace(':', '-')}.json"
    timestamped.write_text(json.dumps(new_snapshot, indent=2, ensure_ascii=False))
    LATEST_FILE.write_text(json.dumps(new_snapshot, indent=2, ensure_ascii=False))

    total_badges = sum(len(v) for v in new_snapshot.values())

    if changes:
        report = f"## {now}\n" + "\n".join(changes) + "\n"
        with CHANGELOG_FILE.open("a") as f:
            f.write(report + "\n")
        print(f"Изменения обнаружены ({len(changes)}):")
        print("\n".join(changes))
    else:
        print(f"[{now}] Изменений нет. Всего бейджей: {total_badges}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
