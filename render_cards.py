#!/usr/bin/env python3
"""
Пре-рендер карточек бейджей 640×360 для inline-выдачи и постов в канал.

Значки Twitch отдаются CDN максимум в 72px с прозрачным фоном — крупно и красиво
их можно показать только на подложке-карточке (что тут и делаем): значок на светлом
чипе + название + пилюля бесплатно/платно + статус, на тёмном фоне с цветным акцентом.
В карточку НЕ запекаем даты/«осталось N дней» (меняется, а Telegram кэширует по URL).

Только актуальные (active+upcoming) и НЕ технические (не __permanent__) бейджи —
модератора/битсы/сабы в боте не показываем. Карточки → data/cards/<uuid>.png.
"""
import colorsys
import json
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import fetch_streamdb as collector
import generate_site as site

ROOT = Path(__file__).resolve().parent
IMAGES_DIR = ROOT / "data" / "images"
CARDS_DIR = ROOT / "data" / "cards"
FONT_FILE = ROOT / "assets" / "Manrope-Bold.ttf"

# Квадрат: Telegram в inline-сетке режет широкие картинки по бокам (значок слева
# пропадает). Квадрат с центрированным значком влезает в ячейку целиком.
W, H = 640, 640

NEUTRAL_BG = (14, 20, 33)      # фон для серых значков (нет доминирующего цвета)
NEUTRAL_GLOW = (34, 46, 72)
LIGHT_CHIP = (236, 239, 245)   # для тёмных значков
DARK_CHIP = (33, 43, 66)       # для светлых значков (чтобы белые кольца не сливались)


def badge_luminance(icon):
    """Средняя яркость непрозрачных пикселей значка (0–255, взвешено по альфе)."""
    small = icon.convert("RGBA").resize((32, 32), Image.LANCZOS)
    px = small.load()
    tot = cnt = 0
    for y in range(32):
        for x in range(32):
            r, g, b, a = px[x, y]
            if a > 40:
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                tot += lum * a
                cnt += a
    return (tot / cnt) if cnt else 128.0


def chip_for(lum):
    """Светлый значок → тёмный чип, тёмный значок → светлый чип (контраст под любой)."""
    return DARK_CHIP if lum > 140 else LIGHT_CHIP


def accent_color(icon):
    """Доминирующий насыщенный цвет значка (без ИИ — квантование + вес по насыщенности).
    Возвращает (r,g,b) или None, если значок по сути серый."""
    small = icon.convert("RGBA").resize((48, 48), Image.LANCZOS)
    px = small.load()
    buckets = Counter()
    for y in range(48):
        for x in range(48):
            r, g, b, a = px[x, y]
            if a < 80:
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / mx if mx else 0
            if sat < 0.18 or mx < 40:      # серое/почти чёрное — не цвет
                continue
            # округляем в корзины 32×32×32, копим вес = насыщенность
            buckets[(r // 32, g // 32, b // 32)] += 0.3 + sat
    if not buckets:
        return None
    rk, gk, bk = buckets.most_common(1)[0][0]
    return (rk * 32 + 16, gk * 32 + 16, bk * 32 + 16)


def _hls_color(color, lightness, sat_cap):
    r, g, b = (x / 255 for x in color)
    h, _, s = colorsys.rgb_to_hls(r, g, b)
    r2, g2, b2 = colorsys.hls_to_rgb(h, lightness, min(s, sat_cap))
    return tuple(int(x * 255) for x in (r2, g2, b2))


def bg_colors(accent):
    """Из акцента значка → (тёмная база фона, цвет свечения). Приглушённо, премиально."""
    if accent is None:
        return NEUTRAL_BG, NEUTRAL_GLOW
    return _hls_color(accent, 0.11, 0.5), _hls_color(accent, 0.30, 0.62)


def soft_glow_bg(base_col, glow_col):
    """Тёмный фон base с мягким свечением glow по центру (гауссово размытие)."""
    base = Image.new("RGB", (W, H), base_col)
    mask = Image.new("L", (W, H), 0)
    rad = 270
    ImageDraw.Draw(mask).ellipse(
        [W // 2 - rad, H // 2 - rad - 10, W // 2 + rad, H // 2 + rad - 10], fill=225)
    mask = mask.filter(ImageFilter.GaussianBlur(130))
    return Image.composite(Image.new("RGB", (W, H), glow_col), base, mask)


def render_card(r, out_path):
    """Значок как app-icon по центру. Фон автоматически строится в тон значку
    (доминирующий цвет), цвет чипа — под яркость значка. Без ИИ."""
    chip_s = 360
    chip_x = (W - chip_s) // 2
    chip_y = (H - chip_s) // 2

    # Грузим значок первым — от него зависят и цвет фона, и цвет чипа
    key = collector.image_cache_key(r["image"])
    icon_path = IMAGES_DIR / f"{key}.png" if key else None
    icon = None
    chip = LIGHT_CHIP
    base_col, glow_col = NEUTRAL_BG, NEUTRAL_GLOW
    if icon_path and icon_path.exists():
        try:
            icon = Image.open(icon_path).convert("RGBA")
            chip = chip_for(badge_luminance(icon))
            base_col, glow_col = bg_colors(accent_color(icon))
        except Exception:
            icon = None

    img = soft_glow_bg(base_col, glow_col)

    # Мягкая тень под чипом
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [chip_x, chip_y + 18, chip_x + chip_s, chip_y + chip_s + 18],
        radius=76, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    img = Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")

    d = ImageDraw.Draw(img)
    d.rounded_rectangle([chip_x, chip_y, chip_x + chip_s, chip_y + chip_s], radius=76, fill=chip)
    # Тонкая обводка чипа — отделяет тёмный чип от тёмного фона
    d.rounded_rectangle([chip_x, chip_y, chip_x + chip_s, chip_y + chip_s], radius=76,
                        outline=(255, 255, 255, 30), width=2)

    if icon is not None:
        try:
            box = 224  # значок 72px → ~3x; крупнее нельзя, пойдёт мыло
            ic = icon.resize((box, box), Image.LANCZOS) if icon.width < box else icon.copy()
            ic.thumbnail((box, box), Image.LANCZOS)
            img.paste(ic, (chip_x + (chip_s - ic.width) // 2,
                           chip_y + (chip_s - ic.height) // 2), ic)
        except Exception:
            pass
    else:
        # Значка нет вовсе — кампания анонсирована, а Twitch арт ещё не выложил
        # (LEGO Harley Quinn/Joker: у SD есть точные даты и условие, объекта
        # значка нет). Раньше такие посты не уходили СОВСЕМ: post_badge молча
        # пропускает запись без картинки. Рисуем название кампании текстом —
        # честно и достаточно, чтобы анонс вышел вовремя.
        draw_title_text(d, r.get("title") or "", chip_x, chip_y, chip_s, chip)

    img.save(out_path, "PNG")


def draw_title_text(d, title, chip_x, chip_y, chip_s, chip_color):
    """Название кампании по центру чипа, с переносом по словам."""
    dark_chip = sum(chip_color[:3]) / 3 < 128
    color = (236, 239, 245) if dark_chip else (24, 30, 46)
    pad = 34
    size = 54
    while size >= 24:
        try:
            font = ImageFont.truetype(str(FONT_FILE), size)
        except OSError:                      # шрифта нет — берём встроенный
            font = ImageFont.load_default(size=size)
        words, lines, cur = title.split(), [], ""
        for wd in words:
            probe = f"{cur} {wd}".strip()
            if d.textlength(probe, font=font) <= chip_s - 2 * pad or not cur:
                cur = probe
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        line_h = int(size * 1.25)
        if len(lines) * line_h <= chip_s - 2 * pad:
            break
        size -= 6
    y = chip_y + (chip_s - len(lines) * line_h) // 2
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text((chip_x + (chip_s - w) / 2, y), ln, font=font, fill=color)
        y += line_h


# Сколько дней держим карточку значка ПОСЛЕ конца окна. Пост «раздача
# завершилась» уходит в течение двух суток после закрытия (см. bot.publish_new),
# а карточка к тому моменту уже удалялась как неактуальная — пост становился
# невозможен, и бот бесконечно пытался его отправить.
KEEP_ENDED_DAYS = 4


def is_shown(r):
    """Бот показывает только актуальные не-технические бейджи, плюс недавно
    завершившиеся — им ещё предстоит пост о завершении."""
    if r.get("group") == "__permanent__":
        return False
    if r["status"] in ("active", "upcoming"):
        return True
    end = (r.get("window") or {}).get("end")
    if end:
        from datetime import datetime, timezone, timedelta
        return datetime.now(timezone.utc) - end <= timedelta(days=KEEP_ENDED_DAYS)
    return False


def sync_cards(records):
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for r in records:
        if not is_shown(r):
            continue
        # card_key — для записей без значка (кампания анонсирована, арта ещё нет):
        # имя файла карточки не из чего вывести, берём заданный ключ.
        key = r.get("card_key") or collector.image_cache_key(r["image"])
        if not key:
            continue
        wanted.add(key)
        render_card(r, CARDS_DIR / f"{key}.png")

    # Тот же fail-close, что и в sync_images: если UUID не распознался НИ У ОДНОГО
    # показываемого бейджа, значит сломался разбор URL, а не кончились раздачи.
    # Без этого удалились бы все карточки, а refresh вышел бы с кодом 0 — молча.
    shown = [r for r in records if is_shown(r) and r.get("image")]
    if shown and not wanted:
        raise RuntimeError(
            f"ни у одного из {len(shown)} показываемых бейджей не распознан UUID картинки — "
            "похоже, Twitch сменил схему URL CDN (IMG_UUID_RE в fetch_streamdb.py). "
            "Карточки НЕ трогаю.")

    removed = 0
    for f in CARDS_DIR.glob("*.png"):
        if f.stem not in wanted:
            f.unlink()
            removed += 1
    print(f"cards: {len(wanted)} отрендерено, {removed} удалено")
    return wanted


def main():
    snapshot = json.loads(site.input_data_file().read_text())
    records = site.build_records(snapshot)
    sync_cards(records)


if __name__ == "__main__":
    main()
