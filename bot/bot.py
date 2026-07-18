#!/usr/bin/env python3
"""
Telegram-бот Twitch Drops Info (@InfoTwitchBot).

Две функции:
1. INLINE + команды: по запросу отдаёт актуальные бейджи Twitch карточками
   (пре-рендер data/cards/<uuid>.png) с кнопками-диплинками прямо на Twitch.
2. АВТОПУБЛИКАЦИЯ в канал: job каждые PUBLISH_INTERVAL сек диффит актуальные
   бейджи против data/published.json и постит в канал жизненный цикл каждого
   бейджа — появился → стартовал (можно получать) → последний день.

Бот НЕ дёргает StreamDatabase — читает локальный data/streamdb_latest.json
(его готовит fetch_streamdb.py + generate_site.py + render_cards.py по таймеру
refresh.sh). Картинки/карточки отдаёт по публичному URL сайта.
Бот — единственный писатель published.json (ноль гонок).
"""
import asyncio
import html
import json
import logging
import os
import re
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# На этом хосте IPv6-egress сломан: у api.telegram.org есть AAAA и хост имеет
# IPv6-адрес, но SYN к IPv6 виснет (таймаут httpx на этом срабатывает ненадёжно).
# httpx/httpcore резолвят через socket.getaddrinfo — форсируем IPv4-only на уровне
# сокета (проверено: IPv6 к Telegram недоступен). Иначе при переезолве бот может
# уйти в IPv6 и молча повиснуть на polling.
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_v4(host, port, family=0, *args, **kwargs):  # noqa: E305
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
socket.getaddrinfo = _getaddrinfo_v4

BOT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BOT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
from telegram import (  # noqa: E402
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputMediaPhoto,
    InputTextMessageContent,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import TelegramError  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import fetch_streamdb as collector  # noqa: E402
import generate_site as site  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip() or None
SITE_URL = "https://example.invalid"
BOT_USERNAME = "InfoTwitchBot"
CHANNEL_URL = "https://t.me/TwitchInfoRadar"
CHANNEL_HANDLE = "@TwitchInfoRadar"

RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]

DATA_FILE = PROJECT_ROOT / "data" / "streamdb_latest.json"
IMAGES_DIR = PROJECT_ROOT / "data" / "images"
CARDS_DIR = PROJECT_ROOT / "data" / "cards"
PUBLISHED_FILE = PROJECT_ROOT / "data" / "published.json"

PUBLISH_INTERVAL = 600   # проверяем базу раз в 10 минут (появления постим по одному за тик)
# Отдельный рубильник постинга: даже с заданным CHANNEL_ID пост не пойдёт,
# пока PUBLISH_ENABLED не true. Так можно донастроить, не постя раньше времени.
PUBLISH_ENABLED = os.environ.get("PUBLISH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx пишет URL с токеном — глушим
log = logging.getLogger("twitch-drops-bot")

COST_EMOJI = {"free": "🟢", "paid": "🟠"}
FREE_WORDS = {"free", "бесплатно", "бесплатные", "беспл"}
PAID_WORDS = {"paid", "платно", "платные", "плат"}
SOON_WORDS = {"soon", "скоро"}

_cache = {"records": None, "mtime": None}


# ────────────────────────── данные ──────────────────────────

def get_records(force: bool = False):
    """Локальный снапшот; пересчёт только если файл обновился (по mtime)."""
    if not DATA_FILE.exists():
        raise RuntimeError(f"{DATA_FILE} не найден — запусти fetch_streamdb.py")
    mtime = DATA_FILE.stat().st_mtime
    if force or _cache["records"] is None or mtime != _cache["mtime"]:
        snapshot = json.loads(DATA_FILE.read_text())
        _cache["records"] = site.build_records(snapshot)
        _cache["mtime"] = mtime
        log.info("records reloaded (%d, snapshot %s)",
                  len(_cache["records"]), snapshot.get("fetched_at"))
    return _cache["records"]


def esc(s):
    return html.escape(str(s))


# Telegram кэширует inline-картинки по URL. uuid карточки завязан на значок и не
# меняется при редизайне — поэтому версионируем URL, чтобы после смены дизайна
# Telegram перезабрал новую карточку. Бампать при каждом изменении вида карточки.
CARD_VERSION = 7


def card_url(r):
    """URL пре-рендер-карточки; фоллбэк на сырой значок, если карточки ещё нет."""
    key = collector.image_cache_key(r["image"])
    if not key:
        return None
    if (CARDS_DIR / f"{key}.png").exists():
        return f"{SITE_URL}/cards/{key}.png?v={CARD_VERSION}"
    if (IMAGES_DIR / f"{key}.png").exists():
        return f"{SITE_URL}/badges/{key}.png"
    return None


def is_shown(r):
    """Бот показывает только актуальные не-технические бейджи
    (без модератора/битсов/сабов — это роли, а не собираемые дропы)."""
    return r["status"] in ("active", "upcoming") and r.get("group") != "__permanent__"


def fmt_dt(dt):
    """День+месяц+время в МСК: '25 июля 14:59'."""
    m = dt + timedelta(hours=3)
    return f"{m.day} {RU_MONTHS_GEN[m.month - 1]} {m.strftime('%H:%M')}"


def status_word(r):
    """Явный статус (для inline; в канале статус несёт заголовок цикла)."""
    if r["status"] == "active":
        return "✅ <b>Доступен сейчас</b>"
    return "📣 <b>Анонс</b> — ещё нельзя получить"


def window_line(r):
    """Полное окно с датой и временем (МСК). Неизвестно — так и пишем."""
    w = r.get("window") or {}
    s, e = w.get("start"), w.get("end")
    if s and e:
        return f"📅 С {fmt_dt(s)} до {fmt_dt(e)} (МСК)"
    if e:
        return f"📅 До {fmt_dt(e)} (МСК)"
    if s:
        return f"📅 С {fmt_dt(s)} (МСК)"
    return "📅 Даты пока неизвестны"


def short_cond(cond):
    """Сжимаем длинные условия — они занимают кучу места (см. анализ:
    'Оформить подписку 1 уровня или подарить подписку 1 уровня' встречается у 14/23)."""
    c = (cond or "").strip()
    low = c.lower()
    if "подписку" in low and "подарить" in low:
        return "Подписка или гифт"
    if low.startswith("смотреть эфир"):
        return c.replace("Смотреть эфир", "Смотреть")
    m = re.search(r"билет twitchcon на (.+)$", low)
    if m:
        return f"Билет TwitchCon ({m.group(1)})"
    return c


def watch_target(r):
    """Куда вести за значком (kind, label, url):
    - категория из данных → директория игры на Twitch;
    - автономная ссылка со страницы бейджа (событие/категория, EWC и т.п.) —
      Twitch сам показывает участников в эфире;
    - иначе (не нашлась ссылка) → без ссылки, только текст."""
    w = r.get("window") or {}
    href = w.get("category_href")
    game = w.get("game")
    # Валидируем URL: один относительный/битый href в inline-кнопке роняет ВЕСЬ
    # inline-ответ (Button_url_invalid). Берём только абсолютные https-ссылки.
    if href and game and str(href).startswith("https://"):
        return ("category", game, href)
    tl = w.get("twitch_link")
    if tl and str(tl.get("url") or "").startswith("https://"):
        kind = "event" if "/directory/event/" in tl["url"] else "category"
        return (kind, tl.get("label") or "на Twitch", tl["url"])
    return (None, None, None)


def how_short(r):
    """Компактно: как получить + куда идти за значком."""
    cond = short_cond(r.get("condition")) or "условие не определено"
    kind, label, url = watch_target(r)
    if url:
        prep = {"category": "в категории", "event": "на каналах события"}[kind]
        return f'📍 {esc(cond)} {prep} <a href="{esc(url)}">{esc(label)}</a>'
    # Каналовый бейдж без курируемой ссылки — просто чёткий текст, без битых ссылок.
    if (r.get("window") or {}).get("channel_count"):
        grp = r.get("group") or ""
        tail = f' события «{esc(grp)}»' if grp and grp != "__permanent__" else ""
        return f"📍 {esc(cond)} — у участвующих стримеров{tail}"
    return f"📍 {esc(cond)}"


def footer_line():
    return (f'📱 <a href="{CHANNEL_URL}">{CHANNEL_HANDLE}</a> · '
            f'<a href="https://t.me/{BOT_USERNAME}">@{BOT_USERNAME}</a> в любом чате')


def build_caption(top_lines, r):
    """Подпись: верхние строки (заголовок/статус/окно) → как получить → футер."""
    parts = [ln for ln in top_lines if ln]
    parts += ["", how_short(r), "", footer_line()]
    return "\n".join(parts)


def watch_button(r):
    """Кнопка за значком: категория/событие на Twitch (если ссылка есть)."""
    kind, label, url = watch_target(r)
    if not url:
        return None
    return InlineKeyboardButton(f"▶️ Смотреть: {label}", url=url)


def twitch_buttons(r):
    """Кнопки inline-сообщения: смотреть (категория/дропы) + наш канал."""
    rows = []
    wb = watch_button(r)
    if wb:
        rows.append([wb])
    rows.append([InlineKeyboardButton("📱 Наш канал", url=CHANNEL_URL)])
    return InlineKeyboardMarkup(rows)


# ──────────────────── личка с ботом = редирект (без прямого диалога) ────────────────────

def redirect_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Show badges in a chat", switch_inline_query="")],
        [InlineKeyboardButton("📣 Channel", url=CHANNEL_URL)],
    ])


async def redirect(update, context):
    """Любое обращение к боту в личке → как пользоваться (инлайн) + ссылка на канал.
    Прямого диалога/списков в личке нет — бот работает инлайном и через канал."""
    msg = update.effective_message
    if not msg:
        return
    # Логируем chat_id личных обращений — так владелец узнаёт свой ALERT_CHAT_ID
    # (для алертинга), и видно кто пишет боту напрямую.
    log.info("DM от chat_id=%s (@%s)", msg.chat_id,
             getattr(update.effective_user, "username", None))
    await msg.reply_html(
        f"Type <code>@{BOT_USERNAME}</code> in any chat to show Twitch badge drops.\n\n"
        f'📣 Updates: <a href="{CHANNEL_URL}">{CHANNEL_HANDLE}</a>',
        reply_markup=redirect_markup(),
        disable_web_page_preview=True,
    )


# ────────────────────────── inline ──────────────────────────

def newest_key(r):
    """Сортировка от самых новых к старым (по дате появления бейджа)."""
    return r.get("first_seen") or ""


def inline_header(r):
    """Стоимость — в тексте: цветной кружок + слово."""
    e = COST_EMOJI.get(r.get("cost"), "⚪")
    w = {"paid": "платно", "free": "бесплатно"}.get(r.get("cost"), "")
    tail = f" — {w}" if w else ""
    return f'{e} <b>{esc(r["title"])}</b>{tail}'


def inline_caption(r):
    return build_caption([inline_header(r), status_word(r), window_line(r)], r)


def inline_desc(r):
    """Короткая строка под названием в списке результатов."""
    cond = short_cond(r.get("condition")) or ""
    w = r.get("window") or {}
    if r["status"] == "active" and w.get("end"):
        return f"✅ до {fmt_dt(w['end'])} · {cond}"
    if r["status"] == "upcoming" and w.get("start"):
        return f"📣 старт {fmt_dt(w['start'])} · {cond}"
    return f"📣 анонс · {cond}" if r["status"] == "upcoming" else cond


async def inline_query(update, context):
    raw = update.inline_query.query.strip()
    query = raw.lower()
    try:
        records = get_records()
    except Exception:
        log.exception("get_records failed in inline")
        await update.inline_query.answer([], cache_time=10)
        return

    if query in SOON_WORDS:
        status, cost_filter, search = "upcoming", None, ""
    elif query in FREE_WORDS:
        status, cost_filter, search = "active", "free", ""
    elif query in PAID_WORDS:
        status, cost_filter, search = "active", "paid", ""
    else:
        status, cost_filter, search = "active", None, query

    pool = [r for r in records
            if is_shown(r) and r["status"] == status
            and (cost_filter is None or r.get("cost") == cost_filter)]
    if search:
        q = site.strip_accents(search)
        pool = [r for r in pool if q in site.strip_accents(r["title"].lower())
                or q in site.strip_accents((r.get("group") or "").lower())]

    # Article-результаты показываются вертикальным СПИСКОМ (Photo — сеткой).
    # Картинку-карточку в отправленном сообщении показываем крупным link-preview.
    results = []
    for r in sorted(pool, key=newest_key, reverse=True)[:40]:
        url = card_url(r)
        prefix = "🎁 " if r["status"] == "active" else "⏳ "
        lpo = (LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=True)
               if url else LinkPreviewOptions(is_disabled=True))
        results.append(InlineQueryResultArticle(
            id=str(uuid4()),
            title=prefix + r["title"],
            description=inline_desc(r),
            thumbnail_url=url,
            input_message_content=InputTextMessageContent(
                inline_caption(r),
                parse_mode=ParseMode.HTML,
                link_preview_options=lpo,
            ),
            reply_markup=twitch_buttons(r),
        ))

    if not results:
        results.append(InlineQueryResultArticle(
            id="none",
            title="Ничего не найдено",
            description="Попробуй: free, paid, soon — или название бейджа",
            input_message_content=InputTextMessageContent("Бейджи не найдены."),
        ))

    try:
        await update.inline_query.answer(
            results, cache_time=60, is_personal=False,
            button=InlineQueryResultsButton(text="🤖 Открыть бота", start_parameter="start"),
        )
    except TelegramError:
        # Один битый результат (напр. невалидная кнопка) не должен ронять всю выдачу —
        # отвечаем пустым с кэшем, чтобы не спамить Telegram ретраями.
        log.exception("inline answer failed, отвечаю пустым")
        try:
            await update.inline_query.answer([], cache_time=30)
        except TelegramError:
            pass


# ────────────────────────── автопубликация в канал ──────────────────────────

def dedup_key(r):
    return f"{r['set_id']}|{r.get('group') or ''}"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def make_entry(r):
    w = r.get("window") or {}
    return {
        "set_id": r["set_id"],
        "group": r.get("group"),
        "title": r["title"],
        "end": iso(w.get("end")),
        "appeared": True,
        "started": r["status"] == "active",
        "ending": False,
    }


def load_state():
    """None = холодный старт (файла нет)."""
    if not PUBLISHED_FILE.exists():
        return None
    try:
        return json.loads(PUBLISHED_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log.exception("published.json битый — считаем холодным стартом")
        return None


def save_state(state):
    collector.atomic_write_json(PUBLISHED_FILE, state)


def cost_word(r):
    return {"paid": "платный", "free": "бесплатный"}.get(r.get("cost"), "")


def channel_header(kind, r):
    name = f'<b>{esc(r["title"])}</b>'
    cw = cost_word(r)
    if kind == "appeared_active":
        return f'🎁 <b>Можно получить уже сейчас!</b>\nНовый {cw} значок {name}'.rstrip()
    if kind == "appeared_upcoming":
        head = (cw.capitalize() + " значок") if cw else "Значок"
        return f'📅 <b>Скоро новый значок</b>\n{head} {name}'
    if kind == "started":
        return f'▶️ <b>Стартовало — можно получать сейчас!</b>\n{name}'
    if kind == "ending":
        return f'⏳ <b>Последний день! Успей получить</b>\n{name}'
    return name


def channel_caption(kind, r):
    # Статус несёт заголовок жизненного цикла → в теле полное окно с временем.
    return build_caption([channel_header(kind, r), window_line(r)], r)


def channel_buttons(r):
    """Кнопки поста: смотреть (категория/дропы) + две ссылки внизу (канал и бот)."""
    rows = []
    wb = watch_button(r)
    if wb:
        rows.append([wb])
    rows.append([
        InlineKeyboardButton("📱 Наш канал", url=CHANNEL_URL),
        InlineKeyboardButton("🤖 Бот", url=f"https://t.me/{BOT_USERNAME}"),
    ])
    return InlineKeyboardMarkup(rows)


async def post_badge(context, r, kind, now=None):
    url = card_url(r)
    if not url:
        log.warning("post_badge: нет картинки для %s, пропуск", r["set_id"])
        return False
    try:
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=url,
            caption=channel_caption(kind, r),
            parse_mode=ParseMode.HTML,
            reply_markup=channel_buttons(r),
        )
        log.info("published %s (%s) -> channel", r["set_id"], kind)
        return True
    except TelegramError:
        log.exception("send_photo в канал упал для %s", r["set_id"])
        return False


def album_ending_caption(r, header=False):
    """Компактная подпись под фото в альбоме «последний день»: имя + дедлайн + как."""
    lines = []
    if header:
        lines.append("⏳ <b>Последний день — успей получить!</b>\n")
    lines.append(f"<b>{esc(r['title'])}</b>")
    w = r.get("window") or {}
    if w.get("end"):
        lines.append(f"📅 До {fmt_dt(w['end'])} (МСК)")
    lines.append(how_short(r))
    return "\n".join(lines)


async def post_ending_album(context, ending):
    """Все бейджи, у которых сегодня последний день — ОДНИМ постом-альбомом
    (все картинки + инструкции), а не N постов. Chunk по 10 (лимит media group).
    ending: список (key, r). Возвращает set успешно опубликованных key."""
    posted = set()
    for start in range(0, len(ending), 10):
        chunk = ending[start:start + 10]
        media = []
        keys = []
        for i, (key, r) in enumerate(chunk):
            url = card_url(r)
            if not url:
                continue
            media.append(InputMediaPhoto(
                media=url, caption=album_ending_caption(r, header=(i == 0)),
                parse_mode=ParseMode.HTML))
            keys.append(key)
        if not media:
            continue
        try:
            if len(media) == 1:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID, photo=media[0].media,
                    caption=media[0].caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
            posted.update(keys)
            log.info("published ending album of %d -> channel", len(media))
        except TelegramError:
            log.exception("ending album (chunk) в канал упал")
        await asyncio.sleep(2)
    return posted


async def publish_new(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID or not PUBLISH_ENABLED:
        return
    try:
        records = get_records()
    except Exception:
        log.exception("get_records failed in publish_new")
        return
    # Staleness-стоп-кран: если refresh перестал обновлять данные (SD сменил формат/лёг),
    # latest.json замирает → НЕ постим неверный жизненный цикл («последний день» и т.п.)
    # по протухшему окну. Watchdog отдельно заалертит о самой заморозке.
    try:
        age_h = (datetime.now(timezone.utc).timestamp() - DATA_FILE.stat().st_mtime) / 3600
    except OSError:
        return
    if age_h > 6:
        log.error("данные не обновлялись %.1f ч — постинг остановлен (StreamDatabase?)", age_h)
        return
    now = datetime.now(timezone.utc)

    live = {}
    for r in records:
        if not is_shown(r):  # только актуальные не-технические
            continue
        key = dedup_key(r)
        if key not in live or r["status"] == "active":
            live[key] = r

    state = load_state()
    if state is None:  # холодный старт — засеять без постинга
        save_state({k: make_entry(r) for k, r in live.items()})
        log.info("publish cold-start: seeded %d, no posts", len(live))
        return

    announce, ending = [], []   # announce: появления + старты (по одному за тик); ending: альбом
    for key, r in live.items():
        w = r.get("window") or {}
        if key not in state:
            kind = "appeared_active" if r["status"] == "active" else "appeared_upcoming"
            announce.append((key, r, kind))
        else:
            st = state[key]
            if r["status"] == "active" and not st.get("started"):
                announce.append((key, r, "started"))
            elif (r["status"] == "active" and not st.get("ending") and w.get("end")
                  and 0 <= (w["end"] - now).total_seconds() <= 86400):
                ending.append((key, r))

    # ПОЯВЛЕНИЯ/СТАРТЫ: по ОДНОМУ за тик — так они расходятся во времени (интервал =
    # PUBLISH_INTERVAL, 10 мин), а не валятся пачкой. Разные ивенты = разные посты
    # естественно. Остальные — на следующие тики (диф пересчитается и возьмёт следующий).
    if announce:
        key, r, kind = announce[0]
        try:
            if await post_badge(context, r, kind):
                if kind == "started":
                    state[key]["started"] = True
                else:
                    state[key] = make_entry(r)
                save_state(state)
        except Exception:
            log.exception("publish_new: пропускаю анонс %s (%s)", key, kind)

    # ПОСЛЕДНИЙ ДЕНЬ: все, кто вошёл в 24ч-окно на этом тике — ОДНИМ альбомом
    # (все картинки + инструкции), а не N постов.
    if ending:
        posted = await post_ending_album(context, ending)
        if posted:
            for key, r in ending:
                if key in posted:
                    state[key]["ending"] = True
                    state[key]["end"] = iso((r.get("window") or {}).get("end"))
            save_state(state)

    # GC: убрать записи, чьё окно закрылось >7 дней назад
    cutoff = now - timedelta(days=7)
    changed = False
    for key in list(state):
        end = state[key].get("end")
        if end:
            try:
                if datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < cutoff:
                    del state[key]
                    changed = True
            except ValueError:
                pass
    if changed:
        save_state(state)


# ──────────────────── уведомления владельцу (через alert.sh) ────────────────────

ALERT_SCRIPT = PROJECT_ROOT / "monitor" / "alert.sh"


async def send_alert(key, subject, body=""):
    """Уведомить владельца через alert.sh (дедуп/антиспам/recovery, curl -4).
    Никогда не роняет вызывающий код."""
    if not ALERT_SCRIPT.exists():
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            str(ALERT_SCRIPT), key, subject, body,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception:
        log.exception("send_alert(%s) не удался", key)


async def clear_alert(key, note=""):
    if not ALERT_SCRIPT.exists():
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            str(ALERT_SCRIPT), "--clear", key, note,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception:
        pass


async def check_anomalies(context: ContextTypes.DEFAULT_TYPE):
    """Ищем «непонятные» актуальные бейджи и зовём владельца (то, что стоит
    добавить/поправить руками): условие, которое мы не умеем описать, или каналовый
    бейдж, для которого не нашли ссылку на событие/категорию. Один агрегат, с recovery."""
    try:
        records = [r for r in get_records() if is_shown(r)]
    except Exception:
        return
    issues = []
    for r in records:
        w = r.get("window") or {}
        if not r.get("condition"):
            issues.append(f"❓ {r['title']}: неизвестное условие (SD добавил новый тип?)")
        elif w.get("channel_count") and not w.get("category_href") and not w.get("twitch_link"):
            issues.append(f"🔗 {r['title']}: не нашёл ссылку на событие/категорию")
    if issues:
        body = "\n".join(issues[:15])
        if len(issues) > 15:
            body += f"\n…и ещё {len(issues) - 15}"
        await send_alert("anomalies", f"{len(issues)} бейдж(ей) требуют внимания", body)
    else:
        await clear_alert("anomalies", "все бейджи распознаны")


# ────────────────────────── запуск ──────────────────────────

async def on_error(update, context):
    """Единая точка наблюдаемости: любое непойманное исключение в хендлере/джобе
    логируется с трейсбеком и шлётся владельцу (дедуплено), но бот НЕ падает."""
    err = context.error
    log.error("необработанное исключение в хендлере", exc_info=err)
    await send_alert(
        "bot-exception", "необработанное исключение в боте",
        f"{type(err).__name__}: {str(err)[:300]}\nСмотри: journalctl -u twitch-badges-bot.service -n 50")


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    # Инлайн — основная фича. В личке — только редирект (без прямого диалога).
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, redirect))
    app.add_error_handler(on_error)

    # Скан «непонятных» бейджей раз в 6ч (независимо от постинга) — предупреждает
    # владельца о новом типе условия / нерешённой ссылке события.
    if app.job_queue:
        app.job_queue.run_repeating(check_anomalies, interval=6 * 3600, first=120)

    if CHANNEL_ID and PUBLISH_ENABLED:
        app.job_queue.run_repeating(publish_new, interval=PUBLISH_INTERVAL, first=30)
        log.info("auto-publish ENABLED -> channel %s", CHANNEL_ID)
    elif CHANNEL_ID:
        log.warning("канал задан (%s), но PUBLISH_ENABLED выкл — постинг НЕ идёт", CHANNEL_ID)
    else:
        log.warning("TELEGRAM_CHANNEL_ID не задан — автопубликация выключена")

    log.info("Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
