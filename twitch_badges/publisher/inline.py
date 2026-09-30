"""Inline-выдача (@InfoTwitchBot в любом чате): сетка карточек по file_id (Q1).

Раньше — список Article с картинкой по URL сайта на Латвии. Теперь —
InlineQueryResultCachedPhoto: карточка уже в Telegram, на ней название и цена,
подпись и кнопки — как у старого inline. Чистая функция: бот превращает
результат в объекты PTB."""
from __future__ import annotations

import hashlib

from ..domain.text import strip_accents
from .captions import fit_channel_caption, inline_caption, inline_desc, is_shown, newest_key, \
    tg_len, twitch_buttons, CAPTION_LIMIT

FREE_WORDS = {"free", "бесплатно", "бесплатные", "беспл"}
PAID_WORDS = {"paid", "платно", "платные", "плат"}
SOON_WORDS = {"soon", "скоро"}
MAX_RESULTS = 40


def parse_query(raw: str):
    q = (raw or "").strip().lower()
    if q in SOON_WORDS:
        return "upcoming", None, ""
    if q in FREE_WORDS:
        return "active", "free", ""
    if q in PAID_WORDS:
        return "active", "paid", ""
    return "active", None, q


def results(records, query: str, *, now, file_id_for, urls=None) -> list[dict]:
    """[{id, photo_file_id, title, description, caption, buttons}] — у каждой записи
    должна быть карточка в Telegram; без неё запись пропускается."""
    status, cost, search = parse_query(query)
    pool = [r for r in records if is_shown(r, now) and r["status"] == status
            and (cost is None or r.get("cost") == cost)]
    if search:
        q = strip_accents(search)
        pool = [r for r in pool if q in strip_accents(r["title"].lower())
                or q in strip_accents((r.get("group") or "").lower())]
    out = []
    for r in sorted(pool, key=newest_key, reverse=True):
        fid = file_id_for(r)
        if not fid:
            continue
        cap = inline_caption(r, urls)
        if tg_len(cap) > CAPTION_LIMIT:
            cap = fit_channel_caption("inline", r, urls)
        prefix = "🎁 " if r["status"] == "active" else "⏳ "
        out.append({
            # id стабилен для записи и версии карточки: Telegram кэширует выдачу
            "id": hashlib.sha1(f"{r['set_id']}:{fid}".encode()).hexdigest()[:32],
            "photo_file_id": fid, "title": prefix + r["title"], "description": inline_desc(r),
            "caption": cap, "buttons": twitch_buttons(r)})
        if len(out) >= MAX_RESULTS:
            break
    return out
