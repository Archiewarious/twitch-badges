"""Картинки значков с Twitch CDN: проверка и атомарная запись (B9).

Раньше «картинкой» считался любой файл: HTML-страница ошибки проходила как арт,
карточка рисовалась без значка и уходила в канал, а битый файл не перекачивался
никогда (качались только отсутствующие)."""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image

from .cards import atomic_write, media_shown
from .domain.catalog import image_cache_key

log = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
MIN_SIDE, MAX_SIDE = 16, 2048


def check_png(data: bytes, content_type: str | None = None) -> str | None:
    """None — годится; иначе причина отказа."""
    if content_type and not content_type.lower().startswith("image/"):
        return f"Content-Type {content_type}"
    if not data:
        return "пустой ответ"
    if len(data) > MAX_BYTES:
        return f"{len(data)} байт — слишком большой"
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
    except Exception as e:  # noqa: BLE001 — Pillow бросает что угодно на мусоре
        return f"не картинка: {e.__class__.__name__}"
    if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
        return f"размер {w}×{h}"
    return None


def file_ok(path: Path) -> bool:
    try:
        return check_png(Path(path).read_bytes()) is None
    except FileNotFoundError:
        return False


def sync_images(records, images_dir: Path, fetch) -> dict:
    """images_dir = ровно картинки актуальных значков: недостающие и битые
    докачиваются, лишние удаляются. fetch(url) -> (status, content_type, bytes)."""
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    wanted = {}
    for r in records:
        if media_shown(r):
            key = image_cache_key(r.get("image") or "")
            if key:
                wanted[key] = r["image"]
    live = [r for r in records if media_shown(r) and r.get("image")]
    if live and not wanted:
        raise RuntimeError(f"ни у одного из {len(live)} актуальных значков не распознан UUID "
                           "картинки — Twitch сменил схему URL CDN. Картинки не трогаю.")
    got, bad = 0, {}
    for key, url in sorted(wanted.items()):
        path = images_dir / f"{key}.png"
        if file_ok(path):
            continue
        try:
            status, ctype, data = fetch(url)
        except Exception as e:  # noqa: BLE001 — сеть: попробуем в следующий раз
            bad[key] = f"сеть: {e.__class__.__name__}"
            continue
        why = f"HTTP {status}" if status != 200 else check_png(data, ctype)
        if why:
            bad[key] = why
            log.warning("картинка %s отклонена: %s", key, why)
            continue
        atomic_write(path, data)
        got += 1
    removed = 0
    for f in images_dir.glob("*.png"):
        if f.stem not in wanted:
            f.unlink()
            removed += 1
    return {"wanted": len(wanted), "downloaded": got, "rejected": bad, "removed": removed}
