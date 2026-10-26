"""Отправка заливки (plan.json от build.py) в каналы на других языках.

Без звука (disable_notification), строго по хронологии русского канала, пост за
постом: сначала в английский, потом в испанский. Прогресс пишется в progress.json —
после обрыва запуск продолжается с места остановки, дублей нет.
  python send.py OUT_DIR [en,es]
"""
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import env_file  # noqa: E402

CHATS = {"en": "TELEGRAM_CHANNEL_EN", "es": "TELEGRAM_CHANNEL_ES"}
PER_PHOTO = 1.6                  # с на фото: ≤ ~20 сообщений в минуту на канал с запасом


def call(cli, api, method, data, files=None):
    for _ in range(8):
        try:
            r = cli.post(f"{api}/{method}", data=data, files=files).json()
        except httpx.HTTPError as e:
            print("сеть:", e)
            time.sleep(5)
            continue
        if r.get("ok"):
            return r["result"]
        if r.get("error_code") == 429:
            wait = r.get("parameters", {}).get("retry_after", 10) + 1
            print(f"429 — жду {wait} с")
            time.sleep(wait)
            continue
        raise RuntimeError(f"{method}: {r}")
    raise RuntimeError(f"{method}: не удалось после повторов")


def send_part(cli, api, chat, part):
    media = part["media"]
    files, opened = {}, []
    if len(media) == 1:
        it = media[0]
        data = {"chat_id": chat, "disable_notification": "true"}
        if it.get("caption"):
            data.update(caption=it["caption"], parse_mode="HTML")
        if part.get("buttons"):
            data["reply_markup"] = json.dumps({"inline_keyboard": part["buttons"]}, ensure_ascii=False)
        if it.get("path"):
            f = open(it["path"], "rb"); opened.append(f)
            files["photo"] = (Path(it["path"]).name, f, "image/png")
        else:
            data["photo"] = it["file_id"]
        res = call(cli, api, "sendPhoto", data, files or None)
        ids = [res["message_id"]]
    else:
        items = []
        for k, it in enumerate(media):
            x = {"type": "photo"}
            if it.get("path"):
                f = open(it["path"], "rb"); opened.append(f)
                files[f"p{k}"] = (Path(it["path"]).name, f, "image/png")
                x["media"] = f"attach://p{k}"
            else:
                x["media"] = it["file_id"]
            if it.get("caption"):
                x.update(caption=it["caption"], parse_mode="HTML")
            items.append(x)
        data = {"chat_id": chat, "disable_notification": "true",
                "media": json.dumps(items, ensure_ascii=False)}
        res = call(cli, api, "sendMediaGroup", data, files or None)
        ids = [m["message_id"] for m in res]
    for f in opened:
        f.close()
    return ids


def main(out_dir, chans="en,es"):
    out_dir = Path(out_dir)
    env = env_file()
    # Во время заливки живая публикация новых каналов выключена: строки env
    # закомментированы «#PAUSED_REPLAY KEY=…» — id берём и оттуда.
    paused = {}
    for line in open("/etc/twitch-badges/env", encoding="utf-8"):
        if line.startswith("#PAUSED_REPLAY ") and "=" in line:
            k, v = line[len("#PAUSED_REPLAY "):].strip().split("=", 1)
            paused[k] = v.strip()
    api = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}"
    plan = json.load(open(out_dir / "plan.json", encoding="utf-8"))
    prog_path = out_dir / "progress.json"
    prog = json.load(open(prog_path)) if prog_path.exists() else {"en": {}, "es": {}}
    cli = httpx.Client(timeout=120)
    chans = chans.split(",")
    for n, post in enumerate(plan):
        key = str(post["ru_ids"][0])
        for ch in chans:
            if key in prog[ch]:
                continue
            chat = env.get(CHATS[ch]) or paused.get(CHATS[ch])
            if not chat:
                raise SystemExit(f"нет {CHATS[ch]} в env")
            ids = send_part(cli, api, chat, post["parts"][ch])
            prog[ch][key] = ids
            json.dump(prog, open(prog_path, "w"))
            time.sleep(PER_PHOTO * len(post["parts"][ch]["media"]))
        if n % 20 == 0:
            print(f"{n + 1}/{len(plan)}", flush=True)
    print("готово:", {ch: len(prog[ch]) for ch in chans})


if __name__ == "__main__":
    main(sys.argv[1], *(sys.argv[2:3] or []))
