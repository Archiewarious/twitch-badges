"""Выгрузка истории русского канала (26.10.2026, заливка каналов на других языках).

Бот не умеет читать историю канала, поэтому каждое сообщение пересылается
владельцу в личку без звука, запоминается (подпись с разметкой, дата, альбом)
и сразу удаляется. Запуск от имени сервиса:
  python tools/replay/harvest.py OUT.json
"""
import json
import sys
import time

import httpx


def env_file(path="/etc/twitch-badges/env"):
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def main(out_path):
    env = env_file()
    api = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}"
    src, owner = env["TELEGRAM_CHANNEL_ID"], env["ALERT_CHAT_ID"]
    cli = httpx.Client(timeout=30)
    msgs, misses, mid = [], 0, 0
    while misses < 40:
        mid += 1
        r = cli.post(f"{api}/forwardMessage", json={"chat_id": owner, "from_chat_id": src,
                                                    "message_id": mid, "disable_notification": True}).json()
        if not r.get("ok"):
            if r.get("error_code") == 429:
                time.sleep(r.get("parameters", {}).get("retry_after", 5) + 1)
                mid -= 1
                continue
            misses += 1
            continue
        misses = 0
        m = r["result"]
        photo = max(m.get("photo") or [{}], key=lambda p: p.get("width", 0) * p.get("height", 0))
        msgs.append({"id": mid, "date": (m.get("forward_origin") or {}).get("date") or m.get("forward_date"),
                     "media_group_id": m.get("media_group_id"),
                     "caption": m.get("caption") or m.get("text"),
                     "entities": m.get("caption_entities") or m.get("entities") or [],
                     "photo": bool(m.get("photo")), "photo_file_id": photo.get("file_id")})
        cli.post(f"{api}/deleteMessage", json={"chat_id": owner, "message_id": m["message_id"]})
        time.sleep(0.4)
    json.dump(msgs, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"сообщений: {len(msgs)}, последний id: {msgs[-1]['id'] if msgs else None}")


if __name__ == "__main__":
    main(sys.argv[1])
