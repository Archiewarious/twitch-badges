"""Сборка заливки истории русского канала в каналы на других языках.

Вход: ru.json (harvest.py). Выход: plan.json — посты по порядку, у каждого части
(одиночное фото или альбом) с переведёнными подписями и картинками:
  - карточки без текста (июль–август) — тот же file_id, что в русском канале;
  - карточки с плашкой цены — перерисованы на языке канала (значок ищем по
    названию в каталоге SD); не нашли — плашка закрашивается и рисуется заново.
Запуск от имени сервиса (нужны БД, картинки и токен):
  python build.py ru.json OUT_DIR
"""
import io
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, "/opt/twitch-badges/current")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from translate_caption import CYR, to_html, translate  # noqa: E402

from twitch_badges import store  # noqa: E402
from twitch_badges.cards import COST_PILL, W, _font, render_card  # noqa: E402
from twitch_badges.domain.catalog import image_cache_key  # noqa: E402
from twitch_badges.publisher.locale import EN, ES  # noqa: E402

DB = "/var/lib/twitch-badges/twitch_badges.sqlite3"
IMAGES = Path("/var/lib/twitch-badges/images")
PILL_Y = 150 + 330 + 42            # draw_cost_pill: chip_y + chip_s + 42
CHANNEL_URL = {"en": EN.channel_url, "es": ES.channel_url}
BTN = {"en": ("▶️ Watch: {l}", "📱 Our channel"), "es": ("▶️ Ver: {l}", "📱 Nuestro canal")}


def env_file(path="/etc/twitch-badges/env"):
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def group_posts(msgs):
    """Альбом = подряд идущие сообщения с одной и той же датой."""
    posts = []
    for m in msgs:
        if posts and posts[-1][-1]["date"] == m["date"] and posts[-1][-1]["id"] == m["id"] - 1:
            posts[-1].append(m)
        else:
            posts.append([m])
    return posts


def titles_for(post):
    """Название значка на каждом фото поста (по порядку) или None."""
    html0 = to_html(post[0]["caption"] or "", post[0]["entities"])
    if all(m["caption"] for m in post):                 # подпись у каждого фото
        return [_first_title(to_html(m["caption"], m["entities"])) for m in post]
    if len(post) == 1:
        return [_first_title(html0)]
    items = re.findall(r"^• (.+)$", html0, re.M)
    if not items:
        lines = html0.split("\n")
        items = [re.sub(r"</?b>", "", ln) for ln in lines[2:]
                 if re.fullmatch(r"<b>[^<]+</b>", ln.strip())]
    return (items + [None] * len(post))[:len(post)]


def _first_title(h):
    for ln in h.split("\n")[1:3] if "\n" in h else [h]:
        m = re.search(r"<b>([^<]+)</b>", ln)
        if m:
            return m.group(1)
    m = re.match(r"([^\n<]+)", h)
    return m.group(1).strip() if m else None


def pill_cost(img):
    """По цвету плашки на карточке: paid / free / None (плашки нет)."""
    px = img.convert("RGB").resize((W, W)).getpixel((W // 2 - 60, PILL_Y))
    for cost, (_, col) in COST_PILL.items():
        if sum(abs(a - b) for a, b in zip(px, col)) < 90:
            return cost
    return None


def _is_pill(px, col):
    return sum(abs(a - b) for a, b in zip(px, col)) < 120


def repaint_pill(img, cost, loc):
    """Плашка на языке канала поверх старой: старую стираем, заливая каждую строку
    плавным переходом между пикселями фона слева и справа от неё (фон карточки —
    мягкое свечение, переход незаметен), и рисуем новую тем же шрифтом и цветом."""
    img = img.convert("RGB").resize((W, W))
    col = COST_PILL[cost][1]
    xs = [x for x in range(W) if _is_pill(img.getpixel((x, PILL_Y)), col)]
    x0, x1 = min(xs) - 6, max(xs) + 6
    y0, y1 = PILL_Y - 30, PILL_Y + 30
    px = img.load()
    for y in range(y0, y1 + 1):
        left, right = px[x0 - 1, y], px[x1 + 1, y]
        for x in range(x0, x1 + 1):
            t = (x - x0) / max(1, x1 - x0)
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(left, right))
    d = ImageDraw.Draw(img)
    text = loc.card_pill[cost]
    font = _font(26)
    tw = d.textlength(text, font=font)
    pw, ph = tw + 56, 48
    bx, by = (W - pw) / 2, PILL_Y - ph / 2
    d.rounded_rectangle([bx, by, bx + pw, by + ph], radius=ph / 2, fill=col)
    d.text(((W - tw) / 2, by + 8), text, font=font, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def fetch_photo(cli, api, token, file_id, cache):
    """Фото из русского канала (один раз на файл; сеть бывает с обрывами)."""
    if file_id in cache:
        return cache[file_id]
    for attempt in range(6):
        try:
            f = cli.get(f"{api}/getFile", params={"file_id": file_id}).json()["result"]
            raw = cli.get(f"https://api.telegram.org/file/bot{token}/{f['file_path']}").content
            cache[file_id] = raw
            return raw
        except (httpx.HTTPError, KeyError):
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"не скачал фото {file_id}")


def main(ru_json, out_dir):
    out_dir = Path(out_dir)
    (out_dir / "cards").mkdir(parents=True, exist_ok=True)
    (out_dir / "icons").mkdir(exist_ok=True)
    env = env_file()
    token = env["TELEGRAM_BOT_TOKEN"]
    api = f"https://api.telegram.org/bot{token}"
    cli = httpx.Client(timeout=60)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    snap = store.current_snapshot(conn)
    catalog = {}
    for b in snap.data["badges"]:
        v = (b.get("current") or {}).get("version") or {}
        if v.get("title"):
            catalog.setdefault(v["title"].strip().lower(), v.get("image_url_4x"))
    msgs = json.load(open(ru_json, encoding="utf-8"))
    plan, problems, cache = [], [], {}
    for post in group_posts(msgs):
        titles = titles_for(post)
        dt = datetime.fromtimestamp(post[0]["date"], timezone.utc)
        out = {"ru_ids": [m["id"] for m in post], "date": post[0]["date"], "parts": {}}
        for loc in (EN, ES):
            media = []
            for m, title in zip(post, titles):
                img = Image.open(io.BytesIO(fetch_photo(cli, api, token, m["photo_file_id"], cache)))
                cost = pill_cost(img)
                cap = translate(to_html(m["caption"], m["entities"]), loc.lang, loc.tz, loc.tz_label,
                                dt, loc.channel_url, loc.channel_handle) if m["caption"] else None
                if cap and CYR.search(re.sub(r"<[^>]+>", "", cap)):
                    problems.append((m["id"], loc.ch, "кириллица в подписи"))
                item = {"caption": cap}
                if cost is None:
                    item["file_id"] = m["photo_file_id"]            # карточка без текста
                else:
                    png = repaint_pill(img, cost, loc)
                    path = out_dir / "cards" / f"{m['id']}.{loc.ch}.png"
                    path.write_bytes(png)
                    item["path"] = str(path)
                media.append(item)
            buttons = None
            if len(post) == 1 and media[0]["caption"]:
                links = re.findall(r'href="(https://www\.twitch\.tv/[^"]+)">([^<]+)<', media[0]["caption"])
                rows = []
                if links:
                    rows.append([{"text": BTN[loc.ch][0].format(l=links[0][1]), "url": links[0][0]}])
                rows.append([{"text": BTN[loc.ch][1], "url": CHANNEL_URL[loc.ch]}])
                buttons = rows
            out["parts"][loc.ch] = {"media": media, "buttons": buttons}
        plan.append(out)
    json.dump(plan, open(out_dir / "plan.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"постов: {len(plan)}, фото: {sum(len(p['ru_ids']) for p in plan)}")
    print(f"замечаний: {len(problems)}")
    for p in problems[:40]:
        print("  ", p)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
