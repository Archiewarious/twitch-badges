"""Карточки в Telegram: загрузка в служебный канал и кэш file_id (D2, Q1).

Посты и inline отправляются по file_id — Telegram ничего не скачивает с
внешнего сайта (раньше — с Латвии, с «Wrong type of the web page content»).
Ключ кэша — (card_key, sha256 содержимого): изменилась карточка — новая загрузка."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import InputFile
from telegram.error import RetryAfter, TelegramError

from . import db
from .cards import sha256_file

log = logging.getLogger(__name__)

UPLOAD_PAUSE = 2.0


class MediaNotReady(Exception):
    """У карточки ещё нет file_id — пост подождёт следующего тика."""


class MediaStore:
    def __init__(self, conn, bot, *, storage_chat_id, cards_dir: Path,
                 sleep=asyncio.sleep, clock=db.utcnow, delete_after_upload=False):
        self.conn, self.bot = conn, bot
        self.storage_chat_id = storage_chat_id
        # Служебного канала нет — грузим в личку владельца и сразу удаляем
        # сообщение: file_id остаётся рабочим и после удаления.
        self.delete_after_upload = delete_after_upload
        self.cards_dir = Path(cards_dir)
        self.sleep, self.clock = sleep, clock

    def _sha(self, key):
        return sha256_file(self.cards_dir / f"{key}.png")

    def file_id(self, key) -> str | None:
        """file_id текущей версии карточки; None — не загружена (или файла нет)."""
        sha = self._sha(key)
        if not sha:
            return None
        row = self.conn.execute("SELECT file_id FROM media WHERE card_key=? AND sha256=?",
                                (key, sha)).fetchone()
        return row[0] if row else None

    def media_for(self, key):
        fid = self.file_id(key)
        if not fid:
            raise MediaNotReady(key)
        return fid

    def missing(self, keys) -> list[str]:
        return [k for k in dict.fromkeys(keys) if self._sha(k) and not self.file_id(k)]

    async def ensure(self, keys) -> dict:
        """Загрузить недостающие карточки (не чаще раза в 2 с). {key: file_id|ошибка}."""
        out = {}
        todo = self.missing(keys)
        if todo and not self.storage_chat_id:
            log.error("служебный канал не настроен — карточки загрузить некуда")
            return {k: "нет служебного канала" for k in todo}
        for n, key in enumerate(todo):
            if n:
                await self.sleep(UPLOAD_PAUSE)
            path = self.cards_dir / f"{key}.png"
            data = path.read_bytes()
            sha = self._sha(key)
            try:
                msg = await self.bot.send_photo(
                    chat_id=self.storage_chat_id, photo=InputFile(data, filename=f"{key}.png"),
                    caption=f"card {key} {sha[:12]}", disable_notification=True,
                    read_timeout=60, write_timeout=60)
            except RetryAfter as e:
                out[key] = f"429: {e}"
                break
            except TelegramError as e:
                out[key] = f"{e.__class__.__name__}: {e}"
                log.warning("карточка %s не загрузилась: %s", key, e)
                continue
            best = max(msg.photo, key=lambda p: p.width * p.height)
            if self.delete_after_upload:
                try:
                    await self.bot.delete_message(chat_id=self.storage_chat_id,
                                                  message_id=msg.message_id)
                except TelegramError as e:
                    log.warning("не удалил загруженную карточку %s: %s", key, e)
            with db.tx(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO media(card_key, sha256, file_id, file_unique_id, uploaded_at) "
                    "VALUES(?, ?, ?, ?, ?)", (key, sha, best.file_id, best.file_unique_id,
                                              db.ts(self.clock())))
            out[key] = best.file_id
        return out

    def prune(self, keep_keys):
        """Забыть file_id карточек, которых больше нет. Сами сообщения в
        служебном канале остаются — это архив."""
        keep = set(keep_keys)
        with db.tx(self.conn):
            for (key,) in self.conn.execute("SELECT DISTINCT card_key FROM media").fetchall():
                if key not in keep:
                    self.conn.execute("DELETE FROM media WHERE card_key=?", (key,))
