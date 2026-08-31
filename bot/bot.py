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
from telegram.error import Conflict, NetworkError, TelegramError  # noqa: E402
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
HEARTBEAT_FILE = PROJECT_ROOT / "data" / "bot_alive"

# Проверяем базу раз в 2 минуты. Работа тут дешёвая: читаем ЛОКАЛЬНЫЙ снапшот и
# только по изменению mtime пересчитываем записи (см. get_records) — сеть трогаем
# лишь когда реально есть что публиковать. Раньше стояло 10 минут, и свежие данные
# просто лежали в файле, добавляя задержки к опросу StreamDatabase.
PUBLISH_INTERVAL = 120
# Тихие часы (МСК): в этом окне НЕ шлём несрочные посты («последний день» и анонсы,
# до старта которых >сутки) — придерживаем до утра, чтобы не будить ночью. Срочное
# («доступен сейчас», «стартовало», анонс со стартом <сутки) идёт в любое время.
# START==END → тихие часы выключены.
UPCOMING_HORIZON_DAYS = 45   # анонсы дальше этого горизонта не показываем
MAX_GROUPS_PER_TICK = 5      # больше за один тик = аномалия, анонсы стоп + алерт
ENDING_WINDOW = 86400        # «последний день» — конец окна ближе этого (24ч)
QUIET_START = int(os.environ.get("QUIET_HOURS_START", "23"))  # с 23:00 МСК
QUIET_END = int(os.environ.get("QUIET_HOURS_END", "9"))       # до 09:00 МСК
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
# Окно и порог для Conflict-ошибок: одиночная — штатный артефакт перезапуска.
CONFLICT_WINDOW = 600
CONFLICT_ALERT_AFTER = 3
_conflicts = []
_publish_fail_streak = 0   # тиков подряд, когда постить было что, но ничего не ушло


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
CARD_VERSION = 10


def card_url(r):
    """URL пре-рендер-карточки; фоллбэк на сырой значок, если карточки ещё нет.

    card_key — записи без значка (кампания анонсирована, Twitch арт не выложил):
    имя файла выводить не из чего, поэтому ключ задаётся явно. Фоллбэк на сырой
    значок для них не работает — картинки нет — но карточка с названием есть."""
    img_key = collector.image_cache_key(r["image"])
    card_key = r.get("card_key") or img_key
    if card_key and (CARDS_DIR / f"{card_key}.png").exists():
        return f"{SITE_URL}/cards/{card_key}.png?v={CARD_VERSION}"
    if img_key and (IMAGES_DIR / f"{img_key}.png").exists():
        return f"{SITE_URL}/badges/{img_key}.png"
    return None


def is_shown(r):
    """Бот показывает только актуальные не-технические бейджи, которые реально
    можно получить действием НА Twitch (смотреть/оформить подписку/потратить биты).
    Исключены: постоянные роли (модератор/битсы/сабы) и билеты на офлайн-мероприятия
    (TwitchCon и т.п. — нужно физически купить билет и поехать, это не Twitch Drop).
    Сайт-каталог их всё равно показывает (там уместно), скрыто только в боте/канале."""
    if r["status"] not in ("active", "upcoming") or r.get("group") == "__permanent__":
        return False
    w = r.get("window") or {}
    if w.get("offline_event"):
        return False
    # Слишком далёкий анонс — не новость (FFXIV JP стартует через 99 дней).
    # Появится в боте, когда приблизится. Сайт-каталог горизонт не применяет.
    if r["status"] == "upcoming" and w.get("start"):
        if (w["start"] - datetime.now(timezone.utc)).days > UPCOMING_HORIZON_DAYS:
            return False
    return True


def fmt_dt(dt, with_time=True):
    """День+месяц+время в МСК: '25 июля 14:59'. with_time=False → только дата
    (когда источник времени не указал — не выдумываем точность, которой нет)."""
    m = dt + timedelta(hours=3)
    base = f"{m.day} {RU_MONTHS_GEN[m.month - 1]}"
    return f"{base} {m.strftime('%H:%M')}" if with_time else base


def status_word(r):
    """Явный статус (для inline; в канале статус несёт заголовок цикла)."""
    if r["status"] == "active":
        return "✅ <b>Доступен сейчас</b>"
    return "📣 <b>Анонс</b> — ещё нельзя получить"


def time_known(w, field):
    """Знаем ли РЕАЛЬНЫЙ час у края окна. dates_coarse — окно из дат события или
    каталога, где часов нет вовсе (parse_dt подставил 00:00, показывать нельзя).
    from_page — разбор текста SD, там час бывает не указан («X:X UTC»)."""
    if w.get("dates_coarse"):
        return False
    if not w.get("from_page"):
        return True
    return bool(w.get(f"{field}_time_known"))


def window_vague(w):
    """Окно приблизительное: SD сам пишет «даты не подтверждены» либо не знает
    часа. Такие анонсы позже уточняются — см. announce-кинд dates_confirmed."""
    if not w:
        return True          # дат нет вовсе — как появятся, пришлём уточнение
    if w.get("dates_unconfirmed"):
        return True
    if w.get("start") and not time_known(w, "start"):
        return True
    if w.get("end") and not time_known(w, "end"):
        return True
    return False


def window_line(r):
    """Полное окно с датой и временем (МСК). Неизвестно — так и пишем.
    Для окон, разобранных из текста описания (from_page), время показываем только
    если оно там реально было — иначе 00:00 было бы выдуманной точностью."""
    w = r.get("window") or {}
    s, e = w.get("start"), w.get("end")
    st = time_known(w, "start")
    et = time_known(w, "end")
    tail = " (МСК)" if (st or et) else ""
    note = " · точные даты уточняются" if w.get("dates_unconfirmed") else ""
    if s and e:
        return f"📅 С {fmt_dt(s, st)} до {fmt_dt(e, et)}{tail}{note}"
    if e:
        return f"📅 До {fmt_dt(e, et)}{tail}{note}"
    if s:
        return f"📅 С {fmt_dt(s, st)}{tail}{note}"
    return "📅 Даты пока неизвестны"


STAGE_SEP = ", затем "


def _short_stage(c):
    """Сжимает ОДИН этап условия."""
    low = c.lower()
    if "подписку" in low and "подарить" in low:
        return "подписка или гифт"
    if low.startswith("смотреть эфир"):
        return "смотреть" + c[len("смотреть эфир"):]
    m = re.search(r"билет на офлайн-мероприятие twitchcon \(([^)]+)\)", low)
    if m:
        return f"билет на TwitchCon — офлайн, {m.group(1)}"  # явно: не Twitch-дроп
    if "офлайн-мероприятие twitchcon" in low:
        return "билет на TwitchCon — офлайн-мероприятие"
    return c


def short_cond(cond, manual=False):
    """Сжимаем длинные условия — они занимают кучу места (см. анализ:
    'Оформить подписку 1 уровня или подарить подписку 1 уровня' встречается у 14/23).

    Сжимаем КАЖДЫЙ ЭТАП отдельно. Условие из steps бывает многоступенчатым
    («оформить или подарить подписку, ЗАТЕМ смотреть эфир 20 минут в 3 разных
    дня»), и правило про подписку срабатывало на всей строке разом, выкидывая
    требование смотреть эфир — читатель терял половину условия.

    manual=True — текст вписан руками в manual/overrides.json, там человек уже
    сформулировал ровно то, что хотел сказать; не трогаем."""
    c = (cond or "").strip()
    if manual or not c:
        return c
    out = STAGE_SEP.join(_short_stage(part) for part in c.split(STAGE_SEP))
    return out[0].upper() + out[1:]


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
    url = str(tl.get("url") or "") if tl else ""
    if url.startswith("https://"):
        if "/directory/event/" in url:
            return ("event", tl.get("label") or "на Twitch", url)
        if "/directory/category/" in url:
            return ("category", tl.get("label") or "на Twitch", url)
        m = re.match(r"https://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)/?$", url)
        if m:                       # канал конкретного стримера (La Velada → ibai)
            return ("channel", m.group(1), url)
        # Не twitch.tv вообще (напр. официальный сайт офлайн-мероприятия типа
        # TwitchCon) — не приписываем "категория"/"событие", это неверно.
        # Метка со страницы StreamDatabase часто на английском/с опечатками —
        # не тащим её в кнопку/текст поста, ставим нейтральную формулировку.
        return ("external", "официальный сайт", url)
    return (None, None, None)


# Не игровые категории Twitch: под них попадает почти любой стрим, поэтому в
# перечислении они полезнее конкретных игр.
GENERIC_CATEGORIES = {
    "Just Chatting", "Music", "Art", "DJs", "Sports", "Talk Shows & Podcasts",
    "Special Events", "Makers & Crafting", "Co-working & Studying",
    "Animals, Aquariums, and Zoos", "Science & Technology", "Food & Drink",
    "Travel & Outdoors", "ASMR", "Beauty & Body Art", "Fitness & Health",
    "Software and Game Development",
}


def categories_line(r):
    """«в 20 категориях — Pokémon GO, Just Chatting и другие» для значков, что
    выдаются во МНОЖЕСТВЕ категорий. Одну ссылку туда ставить нельзя (читатель
    решит, что нужна именно она), но и молчать нельзя: раньше пост о покемонах
    вообще не говорил, где их получать."""
    cats = (r.get("window") or {}).get("categories") or []
    if len(cats) < 2:
        return None
    # Общие категории вперёд: для читателя «подходит Just Chatting» — куда более
    # полезный факт, чем три малоизвестные игры франшизы, которые SD ставит
    # первыми. У покемонов из 20 категорий половина именно такие.
    cats = sorted(cats, key=lambda c: (c not in GENERIC_CATEGORIES, cats.index(c)))
    head = ", ".join(esc(c) for c in cats[:3])
    if len(cats) > 3:
        return (f"в {len(cats)} {plural_cat(len(cats))} — {head} и другие")
    return f"в категориях {head}"


def plural_cat(n):
    if n % 10 == 1 and n % 100 != 11:
        return "категории"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "категориях"
    return "категориях"


def how_short(r):
    """Компактно: как получить + куда идти за значком."""
    cond = short_cond(r.get("condition"), (r.get("window") or {}).get("from_manual"))
    kind, label, url = watch_target(r)
    grp = r.get("group") or ""
    grp_tail = f' события «{esc(grp)}»' if grp and grp != "__permanent__" else ""

    if not cond:
        # Условие SD не публикует (обычно окно взято из дат события/каталога).
        # Не выдумываем текст — отправляем к первоисточнику.
        if url:
            where = {"category": "смотри дропы в категории",
                     "event": "смотри каналы события",
                     "channel": "смотри канал",
                     "external": "подробности —"}[kind]
            return f'📍 Условия уточняются · {where} <a href="{esc(url)}">{esc(label)}</a>'
        many = categories_line(r)
        if many:
            return f"📍 Условия уточняются · {many}"
        if (r.get("window") or {}).get("channel_count"):
            return f"📍 Условия уточняются · у участвующих стримеров{grp_tail}"
        return "📍 Условия уточняются"

    if url:
        prep = {"category": "в категории", "event": "на каналах события",
                "channel": "у стримера", "external": "—"}[kind]
        return f'📍 {esc(cond)} {prep} <a href="{esc(url)}">{esc(label)}</a>'
    many = categories_line(r)
    if many:
        return f"📍 {esc(cond)} {many}"
    # Каналовый бейдж без курируемой ссылки — просто чёткий текст, без битых ссылок.
    if (r.get("window") or {}).get("channel_count"):
        return f"📍 {esc(cond)} — у участвующих стримеров{grp_tail}"
    return f"📍 {esc(cond)}"


def footer_line():
    return (f'📱 <a href="{CHANNEL_URL}">{CHANNEL_HANDLE}</a> · '
            f'<a href="https://t.me/{BOT_USERNAME}">@{BOT_USERNAME}</a> в любом чате')


def art_disclaimer(r):
    """Для кампаний, анонсированных до появления самого значка, на карточке стоит
    арт прошлогоднего выпуска (см. add_orphan_event_records). Молчать об этом
    нельзя: читатель примет заглушку за финальный дизайн."""
    prev = r.get("art_placeholder_from")
    if not prev:
        return None
    year = prev.rsplit("-", 1)[-1]
    return f"🖼 На картинке — значок {year} года: финальный арт Twitch ещё не показал"


def build_caption(top_lines, r):
    """Подпись: верхние строки (заголовок/статус/окно) → как получить → футер."""
    parts = [ln for ln in top_lines if ln]
    parts += ["", how_short(r)]
    note = art_disclaimer(r)
    if note:
        parts += ["", note]
    parts += ["", footer_line()]
    return "\n".join(parts)


def watch_button(r):
    """Кнопка за значком: категория/событие на Twitch, или внешний сайт (если ссылка есть)."""
    kind, label, url = watch_target(r)
    if not url:
        return None
    text = "🔗 Официальный сайт" if kind == "external" else f"▶️ Смотреть: {label}"
    return InlineKeyboardButton(text, url=url)


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
    cond = short_cond(r.get("condition"), (r.get("window") or {}).get("from_manual")) or ""
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
    # Только стабильный set_id. НЕ включаем group: StreamDatabase иногда переименовывает
    # событие («Summer Drop Fest» ↔ «Summer Drops Fest»), и если бы group был в ключе,
    # бейдж считался бы новым при каждом переименовании → повторный пост в канал.
    return r["set_id"]


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
        # анонс ушёл с приблизительными датами → потом стоит прислать уточнение
        "dates_vague": window_vague(w),
        # ...и то же самое про условие: анонс без «как получить» требует
        # продолжения, когда SD/Twitch наконец скажут, что делать.
        "cond_vague": not r.get("condition"),
        # Запись синтезирована из события, у которого ещё нет значка. Когда
        # настоящий значок появится, он придёт под ДРУГИМ set_id — по нему и
        # нужно будет узнать, что это та же кампания (см. supersedes_orphan).
        "from_orphan": bool(w.get("from_orphan_event")),
        "start": iso(w.get("start")),
    }


ORPHAN_MATCH_DAYS = 2


def supersedes_orphan(r, state):
    """Ключ уже анонсированной ОРФАН-записи, которую этот значок замещает.

    Мы анонсируем кампанию до появления значка (LEGO Harley Quinn вышел за
    четверо суток до того, как SD завёл сам значок). Потом значок появляется под
    своим set_id — «harley-mayhem», — и без этой проверки в канал уходит второй
    пост о той же раздаче. Так и случилось 31.08.2026.

    Связать их по группе нельзя: у орфана она из названия события («LEGO Harley
    Quinn»), у значка — из кампании SD («LEGO® Batman™: Legacy of the Dark
    Knight»). Надёжный признак — ОКНО: у обоих совпали и начало, и конец
    (расхождение в пределах суток, пока SD уточняет часы)."""
    w = r.get("window") or {}
    start, end = w.get("start"), w.get("end")
    if not start and not end:
        return None
    for key, st in state.items():
        if not st.get("from_orphan") or st.get("superseded"):
            continue
        for field, mine in (("start", start), ("end", end)):
            theirs = st.get(field)
            if not mine or not theirs:
                return None                      # без обеих дат не рискуем
            try:
                other = datetime.strptime(theirs, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
            if abs((mine - other).total_seconds()) > ORPHAN_MATCH_DAYS * 86400:
                break
        else:
            return key
    return None


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
        head = f"Новый {cw} значок" if cw else "Новый значок"   # без cw был двойной пробел
        return f'🎁 <b>Можно получить уже сейчас!</b>\n{head} {name}'
    if kind == "active_short":
        head = f"{cw.capitalize()} значок" if cw else "Значок"
        return f'⚡ <b>Доступно сейчас — но ненадолго!</b>\n{head} {name}'
    if kind == "appeared_upcoming":
        head = (cw.capitalize() + " значок") if cw else "Значок"
        return f'📅 <b>Скоро новый значок</b>\n{head} {name}'
    if kind == "dates_confirmed":
        return f'⏰ <b>Уточнили время</b>\n{name}'
    if kind == "cond_confirmed":
        return f'📝 <b>Стало известно, как получить</b>\n{name}'
    if kind == "started":
        return f'▶️ <b>Стартовало — можно получать сейчас!</b>\n{name}'
    if kind == "ending":
        return f'⏳ <b>Последний день! Успей получить</b>\n{name}'
    if kind == "finished":
        return f'🏁 <b>Раздача завершилась</b>\n{name} — больше не получить'
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


CAPTION_LIMIT = 1024     # лимит Telegram на подпись к медиа


def plural_badges(n):
    if n % 10 == 1 and n % 100 != 11:
        return "значок"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "значка"
    return "значков"


def album_header(kind, group, n):
    """Шапка поста-альбома. group — название события (у «последнего дня» его нет:
    туда попадают разные бейджи, объединённые только датой)."""
    tail = ""
    if group:
        tail = f'\n<b>{esc(group)}</b> — {n} {plural_badges(n)}'
    if kind == "appeared_active":
        return f'🎁 <b>Можно получить уже сейчас!</b>{tail}'
    if kind == "active_short":
        return f'⚡ <b>Доступно сейчас — но ненадолго!</b>{tail}'
    if kind == "appeared_upcoming":
        return f'📅 <b>Скоро новые значки</b>{tail}'
    if kind == "dates_confirmed":
        return f'⏰ <b>Уточнили время</b>{tail}'
    if kind == "cond_confirmed":
        return f'📝 <b>Стало известно, как получить</b>{tail}'
    if kind == "started":
        return f'▶️ <b>Стартовало — можно получать сейчас!</b>{tail}'
    if kind == "finished":
        return f'🏁 <b>Раздача завершилась</b>{tail} — больше не получить'
    return f'⏳ <b>Последний день — успей получить!</b>{tail}'


def _meta_sig(r):
    """Подпись «окно + условие» — чтобы понять, одинаковы ли они у всей группы."""
    w = r.get("window") or {}
    return (iso(w.get("start")), iso(w.get("end")), bool(w.get("dates_coarse")),
            bool(w.get("dates_unconfirmed")), how_short(r))


def album_caption(items, kind, group):
    """ОДНА подпись на весь альбом: Telegram под медиагруппой показывает только
    первую, остальные видны лишь при открытии фото — значит весь текст в неё.
    Если даты и условие у всех совпадают (типичный случай — тиры одного события),
    пишем их один раз, а бейджи перечисляем именами."""
    parts = [album_header(kind, group, len(items))]
    if len({_meta_sig(r) for _, r in items}) == 1:
        r0 = items[0][1]
        parts += [window_line(r0), how_short(r0), ""]
        parts += [f"• {esc(r['title'])}" for _, r in items]
    else:
        parts.append("")
        for _, r in items:
            parts += [f"<b>{esc(r['title'])}</b>", window_line(r), how_short(r), ""]
        parts.pop()
    parts += ["", footer_line()]
    return "\n".join(parts)


async def post_album(context, items, kind, group=None):
    """N бейджей одним постом-альбомом (все картинки + инструкции), а не N постов.
    Альбомы Telegram не поддерживают inline-кнопки → ссылки идут текстом.
    items: список (key, r). Возвращает set успешно опубликованных key."""
    items = [(k, r) for k, r in items if card_url(r)]
    posted, i = set(), 0
    while i < len(items):
        # жадно набираем чанк: и медиа (≤10), и подпись (≤1024) должны влезть
        n = min(10, len(items) - i)
        while n > 1 and len(album_caption(items[i:i + n], kind, group)) > CAPTION_LIMIT:
            n -= 1
        chunk, i = items[i:i + n], i + n
        caption = album_caption(chunk, kind, group)
        try:
            if len(chunk) == 1:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID, photo=card_url(chunk[0][1]),
                    caption=caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_media_group(chat_id=CHANNEL_ID, media=[
                    InputMediaPhoto(media=card_url(r),
                                    caption=caption if j == 0 else None,
                                    parse_mode=ParseMode.HTML if j == 0 else None)
                    for j, (_, r) in enumerate(chunk)])
            posted.update(k for k, _ in chunk)
            log.info("published album %s x%d -> channel", kind, len(chunk))
        except TelegramError:
            log.exception("альбом (%s) в канал упал", kind)
        await asyncio.sleep(2)
    return posted


def in_quiet_hours(now):
    """Сейчас тихие часы (по МСК)? START==END → выключено."""
    if QUIET_START == QUIET_END:
        return False
    h = (now + timedelta(hours=3)).hour  # UTC → МСК
    if QUIET_START < QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END  # окно через полночь (23→9)


# Конец окна с поправкой на грубые даты — единый источник в generate_site,
# чтобы логика жизненного цикла не разъехалась между сайтом и ботом.
effective_end = site.effective_end


def night_hold(kind, r, now):
    """Придержать ли пост в тихие часы (несрочное). Срочное — всегда сразу."""
    if kind == "ending":
        return True
    if kind in ("dates_confirmed", "cond_confirmed", "finished"):
        return True          # уточнение и «раздача закрылась» подождут до утра
    if kind == "appeared_upcoming":
        s = (r.get("window") or {}).get("start")
        return bool(s) and (s - now).total_seconds() > ENDING_WINDOW  # до старта >суток
    return False


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
        end = effective_end(w)
        active = r["status"] == "active"
        # «Короткий» бейдж: как только он активен, конец УЖЕ в пределах 24ч —
        # «стартовало» и «последний день» совпали бы во времени. Шлём ОДИН пост
        # «доступно, но ненадолго» вместо двух почти одинаковых (La Velada: старт и
        # «последний день» приходили с разницей в часы для суточного окна).
        short = active and end and 0 <= (end - now).total_seconds() <= ENDING_WINDOW
        if key not in state:
            orphan = supersedes_orphan(r, state)
            if orphan:
                # Об этой кампании уже рассказывали — «появился» не шлём, просто
                # заводим запись, чтобы дальше жить обычным циклом.
                log.info("%s замещает орфан %s — повторный анонс не шлю", key, orphan)
                state[key] = make_entry(r)
                state[orphan]["superseded"] = True
                save_state(state)
            elif short:
                announce.append((key, r, "active_short"))
            else:
                announce.append((key, r, "appeared_active" if active else "appeared_upcoming"))
        else:
            st = state[key]
            if active and not st.get("started"):
                announce.append((key, r, "active_short" if short else "started"))
            elif (r["status"] == "upcoming" and st.get("dates_vague")
                  and not st.get("dates_confirmed") and not window_vague(w)
                  and w.get("start")
                  and (w["start"] - now).total_seconds() > ENDING_WINDOW):
                # Анонсировали «точные даты уточняются», а SD дозаполнил час —
                # говорим читателю. Только пока значок ещё не стартовал (после
                # старта точное окно уходит в посте «стартовало») И пока до старта
                # больше суток: иначе уточнение и «стартовало» встанут рядом —
                # это ровно тот дубль, что был у La Velada.
                announce.append((key, r, "dates_confirmed"))
            elif (st.get("cond_vague") and not st.get("cond_known")
                  and r.get("condition")
                  and not (end and (end - now).total_seconds() <= ENDING_WINDOW)):
                # Анонс ушёл с «Условия уточняются» (SD заводит значок раньше, чем
                # модератор напишет описание — так было у nasa-roman), а теперь
                # условие известно. Молчать нельзя: у читателя остался пост, из
                # которого непонятно, что делать. Не шлём, если значок уже на
                # последнем дне — там рядом встанет «успей получить».
                announce.append((key, r, "cond_confirmed"))
            elif active and not st.get("ending") and short:
                ending.append((key, r))

    # ЗАВЕРШЕНИЕ РАЗДАЧИ. Обычный цикл выше идёт по live — а закрывшийся значок
    # туда не попадает (is_shown уже False), поэтому конец кампании оставался
    # незамеченным: последним словом в канале было «последний день», и читатель
    # не знал, всё ли ещё можно успеть. Идём по state — там ровно то, о чём мы
    # уже рассказывали.
    by_id = {r["set_id"]: r for r in records}
    for key, st in state.items():
        if st.get("finished") or not st.get("end"):
            continue
        try:
            end = datetime.strptime(st["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # Только СВЕЖЕ закрывшиеся. Без окна первый же запуск после появления
        # этой логики вывалил бы в канал десяток «раздача завершилась» про
        # кампании недельной давности. GC чистит state через 7 дней, так что
        # двух суток хватает с запасом.
        if not (timedelta(0) < now - end <= timedelta(days=2)):
            continue
        r = by_id.get(key)
        if r:
            announce.append((key, r, "finished"))

    night = in_quiet_hours(now)

    # ПОЯВЛЕНИЯ/СТАРТЫ: по ОДНОЙ ГРУППЕ за тик — так они расходятся во времени
    # (интервал = PUBLISH_INTERVAL), а не валятся пачкой. Ночью несрочные анонсы
    # (старт >суток) придерживаем до утра — если тихие часы включены (сейчас нет:
    # QUIET_HOURS_START==END в .env, владелец хочет новости немедленно).
    ready = [item for item in announce if not (night and night_hold(item[2], item[1], now))]

    # Бейджи одного события (напр. 6 тиров EWC Co-Streamer) — ОДИН пост-альбом,
    # а не 6 постов подряд. Разные события — разные посты, по одному за тик.
    buckets = {}
    for key, r, kind in ready:
        g = (r.get("window") or {}).get("group") or r.get("group") or ""
        if g and g != "__permanent__":
            gk = g
        elif not r.get("window") and r.get("first_seen"):
            # У бейджей без дат события нет вообще, поэтому группы тоже нет. Но
            # SD заводит их пачками (3 значка Audible пришли одним пакетом) —
            # шлём одним альбомом по дню появления, а не тремя постами подряд.
            gk = f"\x00nodate:{r['first_seen'][:10]}"
        else:
            gk = f"\x00solo:{key}"
        buckets.setdefault((gk, kind), []).append((key, r))

    # Аномально много новых групп разом = SD что-то переколбасил (массовое
    # переименование/ре-импорт) либо бот долго лежал и очередь накопилась.
    # Публикацию НЕ замораживаем: очередь и так течёт по одной группе за тик
    # (максимум 6 постов в час), а заморозка была самоподдерживающейся — ключи не
    # попадали в published.json, поэтому на следующем тике групп было ровно
    # столько же, и снятия не наступало никогда. Просто предупреждаем и даём
    # рычаг остановки прямо в тексте.
    if len(buckets) > MAX_GROUPS_PER_TICK:
        names = ", ".join(sorted({gk for gk, _ in buckets if not gk.startswith("\x00solo:")})) or "—"
        log.warning("много новых групп (%d) — публикую по одной за тик", len(buckets))
        await send_alert(
            "burst", f"{len(buckets)} новых групп значков в очереди",
            f"Группы: {names}\n\n"
            "Публикую по одной за 10 минут (не залпом). Если это мусор от "
            "StreamDatabase и постить не надо — останови немедленно:\n"
            "  sed -i 's/^PUBLISH_ENABLED=true/PUBLISH_ENABLED=false/' "
            f"{PROJECT_ROOT}/.env && sudo systemctl restart twitch-badges-bot.service")
    elif buckets:
        await clear_alert("burst", "очередь анонсов вернулась в норму")

    posted_anything = False
    attempted = False          # реально ли пытались отправить хоть один пост
    for (gk, kind), items in sorted(buckets.items()):
        attempted = True
        try:
            if len(items) == 1:
                key, r = items[0]
                done = {key} if await post_badge(context, r, kind) else set()
            else:
                # \x00… — синтетические ключи (одиночка / бездатная пачка),
                # это не название события и в шапку поста не идёт.
                done = await post_album(context, items, kind,
                                        None if gk.startswith("\x00") else gk)
        except Exception:
            # Битая группа не должна навсегда затыкать очередь — пробуем следующую.
            log.exception("publish_new: пропускаю группу %s (%s)", gk, kind)
            continue
        if not done:
            continue
        for key, r in items:
            if key not in done:
                continue
            if kind == "started":
                state[key]["started"] = True
            elif kind == "dates_confirmed":
                state[key]["dates_confirmed"] = True
            elif kind == "cond_confirmed":
                state[key]["cond_known"] = True
            elif kind == "finished":
                state[key]["finished"] = True
                state[key]["dates_vague"] = False
                state[key]["end"] = iso(effective_end(r.get("window") or {}))
            elif kind == "active_short":
                # Один пост закрывает и «стартовало», и «последний день».
                e = state.get(key) or make_entry(r)
                e["started"] = True
                e["ending"] = True
                e["end"] = iso(effective_end(r.get("window") or {}))
                state[key] = e
            else:
                state[key] = make_entry(r)
        save_state(state)
        posted_anything = True
        break   # один пост за тик

    # ПОСЛЕДНИЙ ДЕНЬ: все, кто вошёл в 24ч-окно на этом тике — ОДНИМ альбомом.
    # Ночью придерживаем до утра (не срочно — весь день впереди). 24ч-окно шире тихих
    # часов, так что утренний тик всё равно застанет бейдж «в последний день».
    if ending and night:
        log.info("тихие часы: %d 'последний день' придержаны до утра", len(ending))
    elif len(ending) == 1:
        # Один «последний день» → обычный пост с кнопками (▶️ смотреть, 📱 наш канал, 🤖 бот).
        key, r = ending[0]
        attempted = True
        try:
            if await post_badge(context, r, "ending"):
                state[key]["ending"] = True
                state[key]["end"] = iso((r.get("window") or {}).get("end"))
                save_state(state)
                posted_anything = True
        except Exception:
            log.exception("publish_new: пропускаю 'последний день' %s", key)
    elif ending:
        # Несколько сразу → альбом (кнопок нет — лимит Telegram, ссылка на канал в тексте).
        attempted = True
        posted = await post_album(context, ending, "ending")
        if posted:
            for key, r in ending:
                if key in posted:
                    state[key]["ending"] = True
                    state[key]["end"] = iso((r.get("window") or {}).get("end"))
            save_state(state)
            posted_anything = True

    # КАНАЛ УМЕР? Публикация — единственная нога без страховки: post_badge и
    # post_album глушат TelegramError и возвращают False, тик заканчивается
    # «успешно», бот active, данные свежие, диск в порядке — все проверки зелёные,
    # а канал молчит неделями. Так уже было 21 июля: 9 тиков подряд падали на
    # «Wrong type of the web page content», и никто об этом не узнал.
    # Порог в два тика (20 мин), чтобы одиночный сетевой блип не будил зря.
    #
    # Считаем ТОЛЬКО реальные попытки (attempted), а не «было что постить». Иначе
    # придержанный ночью «последний день» (ending есть, но публикация отложена до
    # утра) выглядел как отказ канала — так 24.07 в 03:13 пришла ложная тревога,
    # снявшаяся ровно в 06:03 UTC, когда тихие часы кончились и бейджи ушли.
    global _publish_fail_streak
    if attempted:
        if posted_anything:
            _publish_fail_streak = 0
            await clear_alert("channel-dead", "публикация в канал снова работает")
        else:
            _publish_fail_streak += 1
            if _publish_fail_streak >= 2:
                await send_alert(
                    "channel-dead",
                    f"не могу опубликовать в канал ({_publish_fail_streak} тика подряд)",
                    "Было что постить, но ни один пост не ушёл. Обычные причины: бота "
                    "исключили из канала или сняли право постинга; TELEGRAM_CHANNEL_ID "
                    "устарел (канал стал супергруппой); Telegram не может забрать "
                    "картинку с сайта.\n"
                    "Смотреть: journalctl -u twitch-badges-bot.service -n 50 | grep -i 'канал\\|channel'")

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
        _, _, url = watch_target(r)
        # Будим владельца только если пост получается БЕСПОЛЕЗНЫМ: ни условия, ни
        # ссылки — читателю некуда идти. Если условия нет, но ссылка есть, пост
        # честно говорит «условия уточняются · подробности — <ссылка>», и это
        # штатная ситуация: у SD для части бейджей условия нет в принципе.
        # Иначе один активный ивент (EWC до 23 августа) слал бы алерт каждые 6ч
        # весь месяц и обесценил алертинг целиком.
        if not r.get("condition") and not url:
            issues.append(f"❓ {r['title']}: ни условия, ни ссылки — пост будет пустым")
        elif w.get("channel_count") and not url:
            issues.append(f"🔗 {r['title']}: не нашёл ссылку на событие/категорию")
    if issues:
        body = "\n".join(issues[:15])
        if len(issues) > 15:
            body += f"\n…и ещё {len(issues) - 15}"
        await send_alert("anomalies", f"{len(issues)} бейдж(ей) требуют внимания", body)
    else:
        await clear_alert("anomalies", "все бейджи распознаны")


BLINDSPOT_DAYS = 30      # «недавно добавлен» — окно, в котором молчание подозрительно
# Нижняя граница: StreamDatabase сначала заводит значок в каталоге, а описание с
# датами дописывает позже — обычно в пределах часа. Попадём скрейпом в эту щель —
# данных нет ни у нас, ни у SD, и алертить не о чем: следующий refresh (раз в
# 30 мин) подхватит сам. Так было с Dead by Daylight Pride Icon 27.07: значок
# заведён в 20:14, снапшот снят в 20:16, а к 20:43 у SD уже всё было.
BLINDSPOT_GRACE_HOURS = 3
IGNORE_FILE = PROJECT_ROOT / "monitor" / "ignore.txt"


def load_ignore():
    """set_id, о которых владелец больше не хочет слышать (по одному в строке)."""
    try:
        return {ln.split("#")[0].strip() for ln in IGNORE_FILE.read_text().splitlines()
                if ln.split("#")[0].strip()}
    except OSError:
        return set()


MONITOR_STATE = PROJECT_ROOT / "data" / "monitor_state.json"


def load_monitor_state():
    """Память мониторов между запусками: о чём уже сообщали. Битый/отсутствующий
    файл — не повод падать, просто считаем, что не сообщали ни о чём."""
    try:
        st = json.loads(MONITOR_STATE.read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_monitor_state(st):
    try:
        tmp = MONITOR_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        tmp.replace(MONITOR_STATE)
    except OSError:
        log.exception("не смог сохранить %s", MONITOR_STATE)


def hidden_reason(r):
    """Осознанная причина не показывать бейдж. Есть причина — алерт не нужен."""
    if r is None:
        return None                      # записи вообще нет — это и есть дыра
    w = r.get("window") or {}
    if r.get("group") == "__permanent__":
        return "постоянный"
    if w.get("offline_event"):
        return "офлайн-мероприятие"
    if w.get("too_late"):
        return "добавлен уже после окончания"
    if r["status"] == "upcoming" and w.get("start"):
        return "анонс за горизонтом"
    if r["status"] == "ended" and w.get("end"):
        return "окно закрылось"
    return None                          # причины нет → не знаем, что с ним делать


async def check_blindspots(context: ContextTypes.DEFAULT_TYPE):
    """СЛЕПАЯ ЗОНА: бейдж уже живёт на Twitch и добавлен недавно, но бот его не
    показывает — и ни по одной осознанной причине (постоянный / офлайн / поздно /
    далёкий анонс / окно закрылось). Обычно это значит «у SD нет дат, и мы не знаем,
    как его классифицировать».

    Именно этой проверки не хватало: check_anomalies перебирает только is_shown(),
    поэтому невидимый бейдж не мог вызвать алерт в принципе — EWC и La Velada
    молчали неделями, и никто об этом не узнал."""
    try:
        snapshot = json.loads(DATA_FILE.read_text())
        by_id = {r["set_id"]: r for r in get_records()}
    except Exception:
        log.exception("check_blindspots: не смог прочитать данные")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=BLINDSPOT_DAYS)
    grace = datetime.now(timezone.utc) - timedelta(hours=BLINDSPOT_GRACE_HOURS)
    ignore = load_ignore()
    issues = {}
    for badge in snapshot.get("badges", []):
        cur = badge.get("current") or {}
        sid = cur.get("set_id")
        if not sid or not badge.get("added") or sid in ignore:
            continue
        if sid in site.MANUAL_SET_IDS:          # вручную разобранные категории
            continue
        ts = collector._badge_added_at(badge)
        if not ts:
            continue
        try:
            added = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if added < cutoff or added > grace:
            continue      # слишком старый — или слишком свежий, SD ещё дозаполняет
        r = by_id.get(sid)
        if r and is_shown(r):
            continue
        if hidden_reason(r):
            continue
        title = (cur.get("version") or {}).get("title") or sid
        what = "нет записи в каталоге" if r is None else "нет дат — не знаю, как классифицировать"
        issues[sid] = f"❓ {title} ({sid})\n   на Twitch с {added:%d.%m}, в канал не идёт: {what}"

    # Сообщаем про каждый значок ОДИН раз, а не каждые 3 часа все 30 дней подряд.
    # Иначе один значок, который владелец починить не может (у SD просто нет
    # данных), даст под сотню одинаковых сообщений и обесценит алертинг целиком —
    # ровно та мина, что сегодня сработала в check_anomalies.
    st = load_monitor_state()
    reported = set(st.get("blindspots", []))
    fresh = [text for sid, text in issues.items() if sid not in reported]

    if set(issues) != reported:
        st["blindspots"] = sorted(issues)
        save_monitor_state(st)

    if fresh:
        body = "\n".join(fresh[:10])
        if len(fresh) > 10:
            body += f"\n…и ещё {len(fresh) - 10}"
        body += f"\n\nЕсли это нормально — впиши set_id в {IGNORE_FILE}"
        await send_alert("blindspots", f"{len(fresh)} значк(ов) живут на Twitch, но молчат", body)
    elif not issues:
        await clear_alert("blindspots", "слепых зон нет — все свежие значки разобраны")


async def heartbeat(context: ContextTypes.DEFAULT_TYPE):
    """Доказательство жизни: бот не просто «active» в systemd, а реально
    разговаривает с Telegram API. Зависший event loop, дедлок в httpx или снова
    сломавшийся IPv6 оставляют юнит active; данные при этом обновляет ОТДЕЛЬНЫЙ
    refresh, диск в порядке — все три проверки watchdog зелёные, а продукт лежит
    на 100%. Файл трогаем ТОЛЬКО после успешного ответа API, иначе отметка
    означала бы «джоба вызвалась», а не «сеть жива»."""
    try:
        await context.bot.get_me()
    except Exception:
        log.warning("heartbeat: Telegram не ответил", exc_info=True)
        return
    try:
        HEARTBEAT_FILE.touch()
    except OSError:
        log.exception("heartbeat: не смог обновить %s", HEARTBEAT_FILE)


# ────────────────────────── запуск ──────────────────────────

async def on_error(update, context):
    """Единая точка наблюдаемости: любое непойманное исключение в хендлере/джобе
    логируется с трейсбеком и шлётся владельцу (дедуплено), но бот НЕ падает."""
    err = context.error
    log.error("необработанное исключение в хендлере", exc_info=err)
    # Обрыв/таймаут сети — рядовое событие на этом хосте, PTB сам ретраит.
    # Будить владельца ради него нельзя: это шум, который заглушит настоящие
    # исключения. Настоящая потеря связи всё равно всплывёт через heartbeat.
    if isinstance(err, NetworkError):
        return
    # Conflict — «кто-то ещё опрашивает тем же токеном». Одиночный вспыхивает
    # штатно: при перезапуске старое соединение getUpdates какое-то время живёт
    # на стороне Telegram, и новое застаёт его. PTB переживает это сам, бот
    # продолжает работать — будить владельца незачем. А вот ПОВТОРЯЮЩИЙСЯ
    # означает реально вторую живую копию: вот тогда и зовём.
    if isinstance(err, Conflict):
        now = datetime.now(timezone.utc)
        _conflicts.append(now)
        while _conflicts and (now - _conflicts[0]).total_seconds() > CONFLICT_WINDOW:
            _conflicts.pop(0)
        if len(_conflicts) < CONFLICT_ALERT_AFTER:
            log.warning("Conflict %d/%d за %d мин — считаю разовым, молчу",
                        len(_conflicts), CONFLICT_ALERT_AFTER, CONFLICT_WINDOW // 60)
            return
        await send_alert(
            "bot-conflict",
            f"бот опрашивается ДВАЖДЫ ({len(_conflicts)} конфликта за "
            f"{CONFLICT_WINDOW // 60} мин)",
            "Тем же токеном пользуется вторая копия — посты и ответы будут теряться "
            "случайным образом.\n"
            "Найти:   ps -eo pid,etimes,cmd | grep bot.py\n"
            "Обычные причины: бот запущен ещё где-то (другой сервер/ноутбук) либо "
            "остался процесс после ручного запуска.")
        return
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
        # Слепая зона — раз в 3ч: значок живёт на Twitch, а бот молчит и не знает почему.
        app.job_queue.run_repeating(check_blindspots, interval=3 * 3600, first=180)
        # Пульс раз в 5 мин — watchdog по возрасту data/bot_alive отличает
        # «бот работает» от «юнит active, но бот повис».
        app.job_queue.run_repeating(heartbeat, interval=300, first=15)

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
