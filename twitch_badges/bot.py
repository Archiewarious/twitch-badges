"""Процесс бота @InfoTwitchBot (D3, C09): единственный, кто пишет в Telegram.

  · inline — сетка карточек по file_id (publisher/inline.py);
  · личка: владельцу (ALERT_CHAT_ID) — команды, остальным — как пользоваться;
  · публикация раз в 2 минуты: план → outbox → канал;
  · карточки → служебный канал (file_id), бэкап БД раз в сутки;
  · мониторы и отправка алертов (отдельным ботом, если задан ALERT_BOT_TOKEN);
  · systemd: Type=notify, READY=1 после старта, WATCHDOG=1 из джобы цикла —
    пинг доказывает, что цикл жив, и не зависит от Telegram (иначе при лежащем
    Telegram был бы бесконечный рестарт);
  · heartbeat-bot для watchdog.sh; flock — второй экземпляр не стартует.
Битая или отсутствующая БД — не «холодный старт», а режим отказа: алерт
db-integrity раз в 6 ч, ничего не постим.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultCachedPhoto,
                      InlineQueryResultsButton, Update)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import Conflict, TelegramError
from telegram.ext import (Application, ChatMemberHandler, ContextTypes, InlineQueryHandler,
                          MessageHandler, filters)
from telegram.request import HTTPXRequest

from . import alerts, backup, config, db, monitors, owner, sdnotify, store
from .domain.records import RecordsContext, build
from .lock import AlreadyRunning, InstanceLock
from .media import MediaStore
from .publisher import inline, planner
from .publisher.captions import BOT_USERNAME, CHANNEL_HANDLE, CHANNEL_URL, is_shown
from .publisher.outbox import Outbox, card_key
from .publisher.service import art_checker, tick

log = logging.getLogger("twitch_badges.bot")

PUBLISH_EVERY = 120
MEDIA_PER_RUN = 30
CONFLICT_WINDOW, CONFLICT_ALERT_AFTER = 600, 3


def make_request(read_timeout=15.0):
    return HTTPXRequest(connection_pool_size=8, connect_timeout=10.0, read_timeout=read_timeout,
                        write_timeout=30.0, pool_timeout=5.0)


class BotApp:
    def __init__(self, cfg: config.Config, conn):
        self.cfg, self.conn = cfg, conn
        self.app = None
        self.outbox = self.media = None
        self.alert_bot = None
        self._records_key = None
        self._records = None
        self._conflicts: list[float] = []

    # ── данные ──
    def storage_chat_id(self):
        return self.cfg.storage_chat_id or db.kv_get(self.conn, "channel", "storage_chat_id")

    def records(self):
        """Записи текущего снапшота на эту минуту (inline спрашивает часто)."""
        snap = store.current_snapshot(self.conn)
        if snap is None:
            return [], {}
        now = db.utcnow()
        key = (snap.id, now.strftime("%Y%m%d%H%M"))
        if key != self._records_key:
            built = build(snap.data, RecordsContext(
                now=now, known_windows=db.kv_all(self.conn, "known_windows"),
                overrides=db.kv_get(self.conn, "overrides", "data", {}) or {}))
            self._records, self._records_key = (built.records, built.category_urls), key
        return self._records

    def alert(self, sig):
        alerts.apply_signal(self.conn, sig)

    # ── сборка приложения ──
    def build(self, request=None, updates_request=None) -> Application:
        """request — подмена сетевого слоя (тесты: fake_telegram.FakeRequest)."""
        cfg = self.cfg
        b = (Application.builder().token(cfg.bot_token).request(request or make_request())
             .get_updates_request(updates_request or make_request(read_timeout=40.0))
             .post_init(self.post_init).post_shutdown(self.post_shutdown))
        if cfg.telegram_api_base:
            b = b.base_url(cfg.telegram_api_base)
        self.app = app = b.build()
        app.add_handler(InlineQueryHandler(self.on_inline))
        app.add_handler(ChatMemberHandler(self.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE, self.on_private))
        app.add_error_handler(self.on_error)
        return app

    async def post_init(self, app: Application):
        cfg = self.cfg
        if cfg.alert_bot_token and cfg.alert_bot_token != cfg.bot_token:
            from telegram import Bot
            self.alert_bot = Bot(cfg.alert_bot_token, request=make_request())
            await self.alert_bot.initialize()
        self.media = MediaStore(self.conn, app.bot, storage_chat_id=self.storage_chat_id(),
                                cards_dir=cfg.cards_dir)
        self.outbox = Outbox(self.conn, app.bot, channel_id=cfg.channel_id,
                             storage_chat_id=self.storage_chat_id(),
                             media_for=self.media.media_for, alert=self.alert)
        if "kill_after_send" in cfg.faults:           # фаза 3, сценарий T4
            def die(*a, **k):
                log.error("TB_FAULTS=kill_after_send — выход сразу после отправки")
                os._exit(9)
            self.outbox._part_done = die
        self.outbox.recover()
        if cfg.channel_id:
            try:
                chat = await app.bot.get_chat(cfg.channel_id)
                with db.tx(self.conn):
                    db.kv_set(self.conn, "channel", "protected", bool(chat.has_protected_content))
                if chat.has_protected_content:
                    log.warning("у канала включена защита контента — сверка пересылкой невозможна, "
                                "неясные посты решает владелец (/sent, /resend)")
            except TelegramError as e:
                log.warning("getChat канала не удался: %s", e)
        jq = app.job_queue
        wd = sdnotify.watchdog_interval()
        jq.run_repeating(self.job_heartbeat, interval=min(60, wd or 60), first=1)
        jq.run_repeating(self.job_publish, interval=PUBLISH_EVERY, first=10)
        jq.run_repeating(self.job_alerts, interval=60, first=20)
        jq.run_repeating(self.job_health, interval=300, first=30)
        jq.run_repeating(self.job_anomalies, interval=6 * 3600, first=300)
        jq.run_repeating(self.job_blindspots, interval=3 * 3600, first=600)
        jq.run_daily(self.job_backup, time=datetime.strptime("03:30", "%H:%M").time()
                     .replace(tzinfo=timezone.utc))
        if "block_loop" in cfg.faults:                # фаза 3, сценарий T10
            jq.run_once(self.job_block, when=30)
        sdnotify.notify("READY=1\nSTATUS=бот запущен")
        log.info("бот запущен: канал %s, публикация %s, служебный канал %s",
                 cfg.channel_id or "—", "вкл" if cfg.publish_enabled else "выкл",
                 self.storage_chat_id() or "—")

    async def post_shutdown(self, app):
        sdnotify.notify("STOPPING=1")
        if self.alert_bot:
            await self.alert_bot.shutdown()

    # ── джобы ──
    async def job_heartbeat(self, ctx):
        """Пинг systemd и файл для watchdog.sh — ТОЛЬКО из цикла событий: завис
        цикл — пинги прекращаются, и systemd перезапускает процесс."""
        sdnotify.notify("WATCHDOG=1")
        try:
            (self.cfg.data_dir / "heartbeat-bot").touch()
        except OSError:
            log.exception("не смог обновить heartbeat")

    async def job_block(self, ctx):
        log.error("TB_FAULTS=block_loop — блокирую цикл событий")
        time.sleep(10 ** 6)

    async def job_publish(self, ctx):
        cfg = self.cfg
        now = db.utcnow()
        try:
            recs, _ = self.records()
            keys = [card_key(r) for r in recs if is_shown(r, now)]
            if self.storage_chat_id():
                self.media.storage_chat_id = self.outbox.storage_chat_id = self.storage_chat_id()
                await self.media.ensure([k for k in keys if k][:MEDIA_PER_RUN])
            if not (cfg.channel_id and cfg.publish_enabled):
                return
            await tick(self.conn, self.outbox, now=now,
                       has_art=art_checker(cfg.images_dir),
                       cfg=planner.PlanConfig(quiet_start=cfg.quiet_start, quiet_end=cfg.quiet_end),
                       media=self.media)
        except db.DbError as e:
            log.error("БД: %s", e)
        except Exception:
            log.exception("тик публикации упал")

    async def job_alerts(self, ctx):
        chat = self.cfg.alert_chat_id
        if not chat:
            return
        bot = self.alert_bot or self.app.bot

        async def send(text):
            await bot.send_message(chat_id=chat, text=text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
        await alerts.AlertSender(self.conn, send).flush()

    async def job_health(self, ctx):
        now = db.utcnow()
        monitors.collector_health(self.conn, now)
        try:
            await self.app.bot.get_me()
            monitors.telegram_health(self.conn, now, ok=True)
        except TelegramError as e:
            monitors.telegram_health(self.conn, now, ok=False, error=str(e)[:200])
        problems = db.integrity(self.conn, quick=True)
        with db.tx(self.conn):
            if problems:
                alerts.raise_alert(self.conn, "db-integrity", "база данных повреждена",
                                   "; ".join(problems[:5]), now)
            else:
                alerts.clear_alert(self.conn, "db-integrity", "база данных в порядке", now)

    async def job_anomalies(self, ctx):
        recs, _ = self.records()
        monitors.anomalies(self.conn, recs, db.utcnow())

    async def job_blindspots(self, ctx):
        snap = store.current_snapshot(self.conn)
        if snap:
            recs, _ = self.records()
            monitors.blindspots(self.conn, snap.data, recs, db.utcnow(),
                                monitors.load_ignore(self.cfg.data_dir / "ignore.txt"))

    async def job_backup(self, ctx):
        now = db.utcnow()
        try:
            path = backup.backup(self.conn, self.cfg.data_dir / "backups", now)
        except Exception:
            log.exception("бэкап БД не удался")
            return
        chat = self.storage_chat_id()
        if chat:
            try:
                with open(path, "rb") as f:
                    await self.app.bot.send_document(chat_id=chat, document=f, filename=path.name,
                                                     caption=f"бэкап БД {now:%Y-%m-%d}",
                                                     read_timeout=120, write_timeout=120)
            except TelegramError as e:
                log.warning("бэкап в служебный канал не ушёл: %s", e)

    # ── обработчики ──
    async def on_inline(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.inline_query
        try:
            recs, urls = self.records()
            items = inline.results(recs, q.query, now=db.utcnow(), urls=urls,
                                   file_id_for=lambda r: self.media.file_id(card_key(r)))
            res = [InlineQueryResultCachedPhoto(
                id=it["id"], photo_file_id=it["photo_file_id"], title=it["title"],
                description=it["description"], caption=it["caption"], parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"])
                                                    for b in row] for row in it["buttons"]]))
                for it in items]
            await q.answer(res, cache_time=60, is_personal=False,
                           button=InlineQueryResultsButton(text="🤖 Открыть бота",
                                                           start_parameter="start"))
        except TelegramError:
            log.exception("inline: ответ не принят — отвечаю пустым")
            try:
                await q.answer([], cache_time=30)
            except TelegramError:
                pass

    async def on_private(self, update: Update, ctx):
        msg = update.effective_message
        if not msg:
            return
        if self.cfg.alert_chat_id and str(msg.chat_id) == str(self.cfg.alert_chat_id) \
                and (msg.text or "").startswith("/") and not msg.text.startswith("/start"):
            reply = owner.handle(self.conn, self.outbox, msg.text, db.utcnow())
            await msg.reply_text(reply)
            return
        log.debug("личное сообщение от chat_id=%s", msg.chat_id)
        await msg.reply_html(
            f"Type <code>@{BOT_USERNAME}</code> in any chat to show Twitch badge drops.\n\n"
            f'📣 Updates: <a href="{CHANNEL_URL}">{CHANNEL_HANDLE}</a>',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Show badges in a chat", switch_inline_query="")],
                [InlineKeyboardButton("📣 Channel", url=CHANNEL_URL)]]),
            disable_web_page_preview=True)

    async def on_my_chat_member(self, update: Update, ctx):
        """Q3: бота сделали админом приватного канала — это служебный канал.
        Запоминаем, только если добавил владелец и канал не основной."""
        cm = update.my_chat_member
        if not cm or cm.chat.type != ChatType.CHANNEL:
            return
        if str(cm.chat.id) == str(self.cfg.channel_id) or self.cfg.storage_chat_id:
            return
        if cm.new_chat_member.status != ChatMemberStatus.ADMINISTRATOR:
            return
        if not self.cfg.alert_chat_id or str(cm.from_user.id) != str(self.cfg.alert_chat_id):
            log.warning("бота добавили в канал %s не владелец — игнорирую", cm.chat.id)
            return
        with db.tx(self.conn):
            db.kv_set(self.conn, "channel", "storage_chat_id", cm.chat.id)
        log.info("служебный канал: %s (%s)", cm.chat.title, cm.chat.id)
        try:
            await ctx.bot.send_message(chat_id=self.cfg.alert_chat_id,
                                       text=f"✅ Служебный канал запомнен: {cm.chat.title}. "
                                            "Сюда бот складывает карточки и бэкапы.")
        except TelegramError as e:
            log.warning("не смог подтвердить владельцу служебный канал: %s", e)

    async def on_error(self, update, ctx: ContextTypes.DEFAULT_TYPE):
        err = ctx.error
        if isinstance(err, Conflict):
            now = time.time()
            self._conflicts = [t for t in self._conflicts if now - t < CONFLICT_WINDOW] + [now]
            if len(self._conflicts) >= CONFLICT_ALERT_AFTER:
                with db.tx(self.conn):
                    alerts.raise_alert(self.conn, "bot-conflict", "запущен второй экземпляр бота",
                                       "Telegram отдаёт обновления другому процессу с тем же токеном. "
                                       "Остановить старую установку: sudo systemctl stop twitch-badges-bot")
            return
        log.error("ошибка обработки: %s", err, exc_info=err)


async def degraded(cfg: config.Config, err: Exception):
    """БД не открывается: не постим, раз в 6 ч сообщаем владельцу, пингуем watchdog."""
    from telegram import Bot
    sdnotify.notify("READY=1\nSTATUS=БД недоступна — постинг остановлен")
    token = cfg.alert_bot_token or cfg.bot_token
    last = 0.0
    while True:
        sdnotify.notify("WATCHDOG=1")
        if cfg.alert_chat_id and token and time.time() - last > 6 * 3600:
            try:
                async with Bot(token, request=make_request()) as b:
                    await b.send_message(chat_id=cfg.alert_chat_id, parse_mode=ParseMode.HTML,
                                         text=alerts.render("db-integrity", "база данных недоступна",
                                                            str(err)))
                last = time.time()
            except Exception as e:  # noqa: BLE001
                log.warning("алерт db-integrity не ушёл: %s", e)
        await asyncio.sleep(60)


def main(cfg: config.Config | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # в URL токен
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # F10
    cfg = cfg or config.load()
    if not cfg.bot_token:
        log.error("TELEGRAM_BOT_TOKEN не задан")
        return 2
    try:
        lock = InstanceLock(cfg.data_dir / "bot.lock").acquire()
    except AlreadyRunning as e:
        log.error("%s", e)
        return 3
    try:
        try:
            conn = db.open_db(cfg.db_path)
        except db.DbError as e:
            log.error("%s", e)
            asyncio.run(degraded(cfg, e))
            return 4
        app = BotApp(cfg, conn).build()
        app.run_polling(allowed_updates=["message", "inline_query", "my_chat_member"],
                        drop_pending_updates=True)
        return 0
    finally:
        lock.release()

