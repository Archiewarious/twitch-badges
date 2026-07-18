#!/usr/bin/env python3
"""
Разовый бэкфилл канала: постит ВСЕ текущие актуальные бейджи от самого старого
к самому новому (по дате появления) и записывает их в published.json.

После него ставим PUBLISH_ENABLED=true и перезапускаем бота — он увидит, что все
текущие бейджи уже опубликованы (в state), и будет постить только НОВЫЕ.

Можно запускать при работающем боте: отправка фото не конфликтует с поллингом
(конфликт 409 бывает только у двух поллеров getUpdates, не у send_photo).
Публикацию в боте на время бэкфилла держим выключенной (PUBLISH_ENABLED=false),
чтобы не было гонки за published.json.
"""
import asyncio

from telegram import Bot
from telegram.constants import ParseMode

import bot as b


async def main():
    if not b.CHANNEL_ID:
        print("Нет TELEGRAM_CHANNEL_ID — нечего заполнять.")
        return

    records = b.get_records(force=True)
    shown = [r for r in records if b.is_shown(r)]
    shown.sort(key=lambda r: r.get("first_seen") or "")  # старые первыми

    state = b.load_state() or {}
    tgbot = Bot(b.TOKEN)
    posted = skipped = 0

    async with tgbot:
        for r in shown:
            key = b.dedup_key(r)
            if key in state:
                skipped += 1
                continue
            url = b.card_url(r)
            if not url:
                print("нет картинки, пропуск:", r["title"])
                continue
            kind = "appeared_active" if r["status"] == "active" else "appeared_upcoming"
            try:
                await tgbot.send_photo(
                    chat_id=b.CHANNEL_ID,
                    photo=url,
                    caption=b.channel_caption(kind, r),
                    parse_mode=ParseMode.HTML,
                    reply_markup=b.channel_buttons(r),
                )
            except Exception as e:
                print("ОШИБКА постинга", r["title"], "→", e)
                continue
            state[key] = b.make_entry(r)
            b.save_state(state)          # пишем после каждого — устойчиво к обрыву
            posted += 1
            print(f"[{posted}] {r.get('first_seen', '?')[:10]}  {r['title']}")
            await asyncio.sleep(1.3)     # мягкий rate-limit канала

    print(f"\nГотово: {posted} опубликовано, {skipped} уже были в state.")


if __name__ == "__main__":
    asyncio.run(main())
