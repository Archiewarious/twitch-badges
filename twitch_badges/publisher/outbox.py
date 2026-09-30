"""Outbox: надёжная отправка постов в канал (D5).

Порядок для каждого поста:
  1. enqueue — ОДНА транзакция: строка outbox (pending) + стадии кампаний.
     Стадия (кампания, стадия) уникальна в БД — второй раз её не займёшь.
  2. send — строка «sending» (commit) → запрос в Telegram → «sent» + знание
     читателя (commit). Альбом из нескольких частей фиксируется по частям.
  3. Ошибка классифицируется по причине:
       запрос не ушёл (connect/pool)  → retry с backoff;
       429                            → retry через retry_after;
       403                            → retry раз в 30 мин + алерт channel-forbidden;
       400                            → failed + алерт post-failed (наш баг);
       ответ потерян / 5xx / обрыв    → unknown → сверка.
  4. Сверка unknown: пересылаем сообщения канала last_id+1… в служебный канал и
     ищем свой пост по времени и первым строкам подписи. Нашли — sent, не нашли —
     отправляем заново. Защита контента или нет служебного канала — алерт
     post-unknown с командами /sent и /resend.
Процесс, убитый посреди отправки, оставляет строку «sending»: при старте она
становится unknown и проходит сверку — дубля нет.
"""
from __future__ import annotations

import asyncio
import json
import logging
import html as _html
import re
import warnings
from dataclasses import dataclass
from datetime import timedelta

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from .. import db, store
from ..domain.catalog import image_cache_key
from .captions import CAPTION_LIMIT, album_caption, channel_buttons, fit_album_caption, \
    fit_channel_caption, tg_len
from .planner import ENDING, AlertSignal, Intent

log = logging.getLogger(__name__)

SEND_READ_TIMEOUT = 60
PART_PAUSE = 2.0                # между частями альбома
RETRY_BASE = 30                 # с, первый повтор «запрос не ушёл»
RETRY_MAX = 1800
FORBIDDEN_RETRY = 1800
EXPIRE_AFTER = timedelta(hours=6)    # неотправленное дольше — перепланировать заново
RECONCILE_WINDOW = 10
RECONCILE_SLACK = 120           # с: часы Telegram и наши могут расходиться


# ── превращение намерения в пост ──

def card_key(r):
    """Ключ карточки значка (как у render_cards): card_key или uuid картинки."""
    return r.get("card_key") or image_cache_key(r.get("image") or "")


def render(intent: Intent, urls: dict) -> dict:
    """Intent → payload: части поста с готовыми подписями и медиа.
    Логика та же, что у старых post_badge/post_album: одиночный пост — фото с
    кнопками; группа — альбом(ы) по ≤10 и подпись ≤1024 (альбом без кнопок)."""
    items = [(i.campaign_id, i.record) for i in intent.items]
    parts = []
    if len(items) == 1:
        cid, r = items[0]
        parts.append({"type": "photo", "media": [card_key(r)], "campaigns": [cid],
                      "caption": fit_channel_caption(intent.kind, r, urls),
                      "buttons": channel_buttons(r)})
    else:
        i = 0
        group = None if intent.kind == ENDING else intent.group
        while i < len(items):
            n = min(10, len(items) - i)
            while n > 1 and tg_len(album_caption(items[i:i + n], intent.kind, group, urls)) > CAPTION_LIMIT:
                n -= 1
            chunk, i = items[i:i + n], i + n
            parts.append({"type": "photo" if n == 1 else "album",
                          "media": [card_key(r) for _, r in chunk],
                          "campaigns": [c for c, _ in chunk],
                          "caption": fit_album_caption(chunk, intent.kind, group, urls),
                          "buttons": None})
    return {"kind": intent.kind, "group": intent.group, "parts": parts, "done": {},
            "items": [{"campaign_id": it.campaign_id, "set_id": it.record["set_id"],
                       "title": it.record.get("title"), "grp": it.record.get("group"),
                       "from_orphan": bool((it.record.get("window") or {}).get("from_orphan_event")),
                       "stages": it.stages} for it in intent.items]}


def plain_lines(caption_html, n=2):
    text = _html.unescape(re.sub(r"<[^>]+>", "", caption_html or ""))
    return [ln.strip() for ln in text.split("\n") if ln.strip()][:n]


# ── классификация ошибок ──

NOT_SENT, RATE, FORBIDDEN, BAD, UNKNOWN = "not_sent", "rate_limited", "forbidden", "bad_request", "unknown"


def classify_error(exc: BaseException) -> tuple[str, float]:
    """(вид, через сколько секунд повторить)."""
    if isinstance(exc, RetryAfter):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # PTB 22: int сейчас, timedelta потом
            ra = exc.retry_after
        return RATE, float(ra.total_seconds() if isinstance(ra, timedelta) else ra)
    if isinstance(exc, Forbidden):
        return FORBIDDEN, FORBIDDEN_RETRY
    if isinstance(exc, BadRequest):
        return BAD, 0
    if isinstance(exc, (TimedOut, NetworkError)):
        cause = exc.__cause__
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return NOT_SENT, RETRY_BASE
        return UNKNOWN, 0
    if isinstance(exc, TelegramError):
        return UNKNOWN, 0
    return UNKNOWN, 0


# ── строка outbox ──

@dataclass
class Row:
    id: int
    kind: str
    status: str
    payload: dict
    knowledge: dict
    attempts: int
    sending_at: str | None
    created_at: str

    @classmethod
    def load(cls, conn, rid):
        r = conn.execute("SELECT * FROM outbox WHERE id=?", (rid,)).fetchone()
        return cls(r["id"], r["kind"], r["status"], json.loads(r["payload"]),
                   json.loads(r["knowledge"]), r["attempts"], r["sending_at"], r["created_at"])


class Outbox:
    """Исполнитель outbox на одной БД и одном telegram.Bot."""

    def __init__(self, conn, bot, *, channel_id, storage_chat_id=None, media_for=None,
                 alert=None, sleep=asyncio.sleep, clock=db.utcnow):
        self.conn, self.bot = conn, bot
        self.channel_id, self.storage_chat_id = channel_id, storage_chat_id or None
        self.media_for = media_for or (lambda key: key)
        self.alert = alert or (lambda sig: None)
        self.sleep, self.clock = sleep, clock

    # ── постановка ──
    def enqueue(self, intent: Intent, urls: dict) -> int | None:
        """Строка outbox + стадии в одной транзакции. Пункты, чьи стадии уже
        заняты (пост уже был), выбрасываются. None — ставить нечего."""
        now = self.clock()
        with db.tx(self.conn):
            keep = []
            for it in intent.items:
                taken = self.conn.execute(
                    f"SELECT 1 FROM stages WHERE campaign_id=? AND stage IN "
                    f"({','.join('?' * len(it.stages))})", (it.campaign_id, *it.stages)).fetchone()
                if taken:
                    log.warning("стадии %s у %s уже заняты — пропускаю", it.stages, it.campaign_id)
                    continue
                keep.append(it)
            if not keep:
                return None
            intent = Intent(intent.kind, intent.group, keep, intent.sort_key)
            payload = render(intent, urls)
            knowledge = {it.campaign_id: it.knowledge for it in keep}
            cur = self.conn.execute(
                "INSERT INTO outbox(kind, grp, payload, knowledge, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'pending', ?, ?)",
                (intent.kind, intent.group, json.dumps(payload, ensure_ascii=False),
                 json.dumps(knowledge, ensure_ascii=False), db.ts(now), db.ts(now)))
            rid = cur.lastrowid
            for it in keep:
                if not self.conn.execute("SELECT 1 FROM campaigns WHERE id=?", (it.campaign_id,)).fetchone():
                    r = it.record
                    store.upsert_campaign(self.conn, store.Campaign(
                        id=it.campaign_id, title=r.get("title"), grp=r.get("group"),
                        from_orphan=bool((r.get("window") or {}).get("from_orphan_event")),
                        last_seen_live_at=db.ts(now)), now)
                for st in it.stages:
                    store.add_stage(self.conn, it.campaign_id, st, rid, now)
        return rid

    # ── восстановление при старте ──
    def recover(self):
        """Строки «sending» от убитого процесса → unknown (их проверит сверка).
        Упавшие с 400 — один повтор после перезапуска (обычно это выкатка фикса)."""
        now = db.ts(self.clock())
        with db.tx(self.conn):
            n = self.conn.execute("UPDATE outbox SET status='unknown', updated_at=? "
                                  "WHERE status='sending'", (now,)).rowcount
            m = self.conn.execute("UPDATE outbox SET status='retry', next_attempt_at=?, updated_at=? "
                                  "WHERE status='failed' AND last_error LIKE 'bad_request:%' "
                                  "AND attempts < 3", (now, now)).rowcount
        if n or m:
            log.warning("outbox: %d незавершённых отправок → сверка, %d упавших → повтор", n, m)

    # ── исполнение ──
    def due(self):
        now = db.ts(self.clock())
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM outbox WHERE status='pending' "
            "OR (status IN ('retry','unknown') AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
            "ORDER BY id", (now,))]

    def open_rows(self):
        return self.conn.execute("SELECT count(*) FROM outbox WHERE status IN "
                                 "('pending','sending','retry','unknown')").fetchone()[0]

    async def process(self) -> dict:
        """Пройти очередь. Возвращает счётчики по исходам."""
        stats = {"sent": 0, "retry": 0, "unknown": 0, "failed": 0}
        self.expire()
        for rid in self.due():
            self._last_kind = None
            outcome = await self.run(rid)
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome in ("retry", "unknown") and self._last_kind in (NOT_SENT, RATE, FORBIDDEN):
                break           # Telegram недоступен или просит подождать — не долбим дальше
        return stats

    _last_kind = None

    def expire(self):
        """Не отправленное за 6 ч — отменить и освободить стадии: план пересчитают
        по свежим данным, а не отправят устаревший текст."""
        cutoff = db.ts(self.clock() - EXPIRE_AFTER)
        rows = self.conn.execute("SELECT id, payload FROM outbox WHERE status IN ('pending','retry') "
                                 "AND created_at < ?", (cutoff,)).fetchall()
        for rid, payload in rows:
            if json.loads(payload).get("done"):
                continue                     # часть уже в канале — довести до конца
            with db.tx(self.conn):
                self.conn.execute("UPDATE outbox SET status='failed', last_error='expired', "
                                  "updated_at=? WHERE id=?", (db.ts(self.clock()), rid))
                self.conn.execute("DELETE FROM stages WHERE outbox_id=?", (rid,))
            log.warning("outbox %d: не ушёл за %s — отменён, стадии освобождены", rid, EXPIRE_AFTER)

    async def run(self, rid) -> str:
        row = Row.load(self.conn, rid)
        if row.status == "unknown":
            res = await self.reconcile(row)
            if res != "resend":
                return res
            row = Row.load(self.conn, rid)
        parts = row.payload["parts"]
        for idx, part in enumerate(parts):
            if str(idx) in row.payload["done"]:
                continue
            self._set(rid, status="sending", sending_at=db.ts(self.clock()),
                      attempts=row.attempts + 1)
            row.attempts += 1
            try:
                ids = await self._send_part(part)
            except TelegramError as e:
                return self._on_error(row, idx, e)
            self._part_done(row, idx, ids)
            if idx < len(parts) - 1:
                await self.sleep(PART_PAUSE)
        self._finish(row)
        return "sent"

    async def _send_part(self, part) -> list[int]:
        media = [self.media_for(k) for k in part["media"]]
        if part["type"] == "photo":
            markup = None
            if part.get("buttons"):
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"]) for b in row]
                                               for row in part["buttons"]])
            msg = await self.bot.send_photo(
                chat_id=self.channel_id, photo=media[0], caption=part["caption"],
                parse_mode=ParseMode.HTML, reply_markup=markup,
                read_timeout=SEND_READ_TIMEOUT, write_timeout=SEND_READ_TIMEOUT)
            ids = [msg.message_id]
        else:
            msgs = await self.bot.send_media_group(
                chat_id=self.channel_id,
                media=[InputMediaPhoto(media=m, caption=part["caption"] if j == 0 else None,
                                       parse_mode=ParseMode.HTML if j == 0 else None)
                       for j, m in enumerate(media)],
                read_timeout=SEND_READ_TIMEOUT, write_timeout=SEND_READ_TIMEOUT)
            ids = [m.message_id for m in msgs]
        return ids

    # ── запись результатов ──
    def _set(self, rid, **fields):
        fields["updated_at"] = db.ts(self.clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        with db.tx(self.conn):
            self.conn.execute(f"UPDATE outbox SET {cols} WHERE id=?", (*fields.values(), rid))

    def _note_last_id(self, mid):
        """Последний id канала, до которого всё сверено. Внутри транзакции записи
        части: иначе после убийства процесса сверка начала бы искать ПОСЛЕ поста."""
        last = db.kv_get(self.conn, "channel", "last_message_id") or 0
        if mid > last:
            db.kv_set(self.conn, "channel", "last_message_id", mid, self.clock())

    def _part_done(self, row: Row, idx: int, ids: list[int]):
        """Часть в канале: запомнить id и сразу обновить знание читателя о её кампаниях."""
        now = self.clock()
        row.payload["done"][str(idx)] = ids
        with db.tx(self.conn):
            self.conn.execute("UPDATE outbox SET payload=?, message_ids=?, updated_at=? WHERE id=?",
                              (json.dumps(row.payload, ensure_ascii=False),
                               json.dumps(row.payload["done"]), db.ts(now), row.id))
            for cid in row.payload["parts"][idx]["campaigns"]:
                k = row.knowledge.get(cid) or {}
                self.conn.execute(
                    "UPDATE campaigns SET known_start=?, known_end=?, known_vague=?, known_condition=?, "
                    "known_cost=?, first_posted_at=COALESCE(first_posted_at, ?), last_posted_at=?, "
                    "last_seen_live_at=?, updated_at=? WHERE id=?",
                    (k.get("start"), k.get("end"), None if k.get("vague") is None else int(k["vague"]),
                     k.get("condition"), k.get("cost"), db.ts(now), db.ts(now), db.ts(now),
                     db.ts(now), cid))
            db.kv_set(self.conn, "telegram", "last_ok_at", db.ts(now), now)
            if ids:
                self._note_last_id(max(ids))
        log.info("outbox %d: часть %d/%d в канале (%s)", row.id, idx + 1,
                 len(row.payload["parts"]), ids)

    def _finish(self, row: Row):
        self._set(row.id, status="sent", sent_at=db.ts(self.clock()), last_error=None,
                  next_attempt_at=None)
        self.alert(AlertSignal("post-failed", False, "посты снова уходят"))
        self.alert(AlertSignal("channel-forbidden", False, "права в канале вернулись"))
        log.info("outbox %d (%s) отправлен", row.id, row.kind)

    def _on_error(self, row: Row, idx: int, e: TelegramError) -> str:
        kind, delay = classify_error(e)
        self._last_kind = kind
        now = self.clock()
        err = f"{kind}: {e}"[:500]
        with db.tx(self.conn):
            db.kv_set(self.conn, "telegram", "last_error", {"at": db.ts(now), "error": err}, now)
        if kind == UNKNOWN:
            self._set(row.id, status="unknown", last_error=err)
            log.warning("outbox %d: судьба части %d неизвестна (%s) — сверка", row.id, idx, e)
            return "unknown"
        if kind == BAD:
            self._set(row.id, status="failed", last_error=err)
            self.alert(AlertSignal(
                "post-failed", True, "Telegram отклонил пост — это ошибка бота",
                f"Пост #{row.id} ({row.kind}): {e}\nОн не уйдёт, пока ошибку не исправят. "
                "После выкатки исправления бот повторит его один раз сам."))
            log.error("outbox %d: 400 — %s", row.id, e)
            return "failed"
        if kind == FORBIDDEN:
            self.alert(AlertSignal(
                "channel-forbidden", True, "бот не может писать в канал",
                "Бота убрали из канала или сняли право публиковать. Верни боту права "
                "администратора с правом «Публикация сообщений» — посты догонят сами."))
        else:
            delay = min(RETRY_MAX, max(delay, RETRY_BASE * 2 ** max(0, row.attempts - 1))) \
                if kind == NOT_SENT else delay
        self._set(row.id, status="retry", last_error=err,
                  next_attempt_at=db.ts(now + timedelta(seconds=delay)))
        log.warning("outbox %d: %s — повтор через %.0f с", row.id, e, delay)
        return "retry"

    # ── сверка ──
    async def reconcile(self, row: Row) -> str:
        """Ищем в канале часть, чья судьба неизвестна. sent | resend | unknown."""
        idx = next(i for i in range(len(row.payload["parts"])) if str(i) not in row.payload["done"])
        part = row.payload["parts"][idx]
        want = plain_lines(part["caption"])
        since = db.parse_ts(row.sending_at) or db.parse_ts(row.created_at)
        last = db.kv_get(self.conn, "channel", "last_message_id")

        def stuck(why):
            self.alert(AlertSignal(
                "post-unknown", True, "не знаю, ушёл ли пост в канал",
                f"Пост #{row.id} ({row.kind}): {why}.\nПроверь канал и ответь боту в личке:\n"
                f"/sent {row.id} — если пост в канале есть\n/resend {row.id} — если его нет"))
            # Сами больше не трогаем (иначе пересылки каждые 2 минуты); напомним через 30 мин.
            self._set(row.id, status="unknown", last_error=f"stuck: {why}"[:500],
                      next_attempt_at=db.ts(self.clock() + timedelta(seconds=FORBIDDEN_RETRY)))
            return "unknown"

        if not self.storage_chat_id:
            return stuck("служебный канал не настроен, сверить нельзя")
        if last is None:
            return stuck("неизвестен номер последнего сообщения канала")
        misses = 0
        for mid in range(last + 1, last + 1 + RECONCILE_WINDOW):
            try:
                fwd = await self.bot.forward_message(chat_id=self.storage_chat_id,
                                                     from_chat_id=self.channel_id, message_id=mid)
            except BadRequest as e:
                if "not found" in str(e).lower():
                    misses += 1
                    if misses >= 3:
                        break
                    continue
                if "protected" in str(e).lower() or "can't be forwarded" in str(e).lower():
                    return stuck("у канала включена защита контента, пересылка невозможна")
                return stuck(f"пересылка не удалась: {e}")
            except TelegramError as e:
                self._set(row.id, status="unknown", last_error=f"reconcile: {e}"[:500])
                self._last_kind = NOT_SENT
                return "unknown"            # сеть — попробуем на следующем тике
            misses = 0
            try:
                await self.bot.delete_message(chat_id=self.storage_chat_id, message_id=fwd.message_id)
            except TelegramError:
                pass
            origin = getattr(fwd, "forward_origin", None)
            date = getattr(origin, "date", None)
            got = plain_lines(fwd.caption or fwd.text or "")
            if date and since and date.timestamp() >= since.timestamp() - RECONCILE_SLACK and got == want:
                n = len(part["media"]) if part["type"] == "album" else 1
                ids = list(range(mid, mid + n))
                self._part_done(row, idx, ids)
                log.warning("outbox %d: сверка нашла часть %d в канале (%s)", row.id, idx, ids)
                if len(row.payload["done"]) == len(row.payload["parts"]):
                    self._finish(row)
                    return "sent"
                self._set(row.id, status="retry", next_attempt_at=db.ts(self.clock()))
                return "resend"
        log.warning("outbox %d: в канале поста нет — отправляю заново", row.id)
        self._set(row.id, status="retry", next_attempt_at=db.ts(self.clock()))
        return "resend"

    # ── команды владельца ──
    def mark_sent(self, rid) -> str:
        row = Row.load(self.conn, rid)
        if row.status not in ("unknown", "failed", "retry"):
            return f"#{rid}: статус {row.status}, отмечать нечего"
        for idx in range(len(row.payload["parts"])):
            if str(idx) not in row.payload["done"]:
                self._part_done(row, idx, [])
        self._finish(row)
        return f"#{rid} отмечен отправленным"

    def resend(self, rid) -> str:
        row = Row.load(self.conn, rid)
        if row.status not in ("unknown", "failed"):
            return f"#{rid}: статус {row.status}, повторять нечего"
        self._set(rid, status="retry", next_attempt_at=db.ts(self.clock()), attempts=0)
        return f"#{rid} будет отправлен заново на ближайшем тике"
