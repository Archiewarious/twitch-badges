"""Тексты постов в канал и inline — перенос из bot/bot.py без изменения поведения.

Чистые функции: запись → HTML-подпись. Кнопки — данными
([[{"text", "url"}]]), в объекты Telegram их превращает бот.
urls — ссылки на категории после канонизации (Built.category_urls): раньше
подписи брали их из глобала generate_site.
"""
import html
import re
from datetime import timedelta

from ..domain.categories import _cat_key

BOT_USERNAME = "InfoTwitchBot"


CHANNEL_URL = "https://t.me/TwitchInfoRadar"


CHANNEL_HANDLE = "@TwitchInfoRadar"


RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]


COST_EMOJI = {"free": "🟢", "paid": "🟠"}


# Тихие часы (МСК): в этом окне НЕ шлём несрочные посты («последний день» и анонсы,
# до старта которых >сутки) — придерживаем до утра, чтобы не будить ночью. Срочное
# («доступен сейчас», «стартовало», анонс со стартом <сутки) идёт в любое время.
# START==END → тихие часы выключены.
UPCOMING_HORIZON_DAYS = 45   # анонсы дальше этого горизонта не показываем


CAPTION_LIMIT = 1024     # лимит Telegram на подпись к медиа


def esc(s):
    return html.escape(str(s))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def dedup_key(r):
    # Только стабильный set_id. НЕ включаем group: StreamDatabase иногда переименовывает
    # событие («Summer Drop Fest» ↔ «Summer Drops Fest»), и если бы group был в ключе,
    # бейдж считался бы новым при каждом переименовании → повторный пост в канал.
    return r["set_id"]


def is_shown(r, now):
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
        if (w["start"] - now).days > UPCOMING_HORIZON_DAYS:
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
    return f"📅 {window_text(r)}"


def window_text(r):
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
        return f"С {fmt_dt(s, st)} до {fmt_dt(e, et)}{tail}{note}"
    if e:
        return f"До {fmt_dt(e, et)}{tail}{note}"
    if s:
        return f"С {fmt_dt(s, st)}{tail}{note}"
    return "Даты пока неизвестны"


STAGE_SEP = ", затем "


# «Оформить подписку 1 уровня или подарить подписку 1 уровня» → «подписка или
# гифт». Сворачиваем ТОЛЬКО эту часть: условие бывает длиннее («…или смотреть
# эфир 20 минут в 3 разных дня (шанс 1 из 3)»), и прежнее правило «есть
# подписку и подарить — значит вся строка про подписку» выкидывало остальное.
SUB_OR_GIFT_RE = re.compile(
    r"оформить (?:\d+ )?подписк\w* (?:\d+ уровня|неизвестного уровня)"
    r" или подарить (?:\d+ )?подписк\w* (?:\d+ уровня|неизвестного уровня)"
    r"|оформить или подарить подписку", re.I)


def _short_stage(c):
    """Сжимает ОДИН этап условия, не теряя его хвост."""
    low = c.lower()
    m = re.search(r"билет на офлайн-мероприятие twitchcon \(([^)]+)\)", low)
    if m:
        return f"билет на TwitchCon — офлайн, {m.group(1)}"  # явно: не Twitch-дроп
    if "офлайн-мероприятие twitchcon" in low:
        return "билет на TwitchCon — офлайн-мероприятие"
    out = SUB_OR_GIFT_RE.sub("подписка или гифт", c)
    # «подписка или гифт или смотреть…» — три альтернативы через два «или»
    # читаются коряво; перечисление через запятую яснее.
    out = re.sub(r"(?i)подписка или гифт или ", "подписка, гифт или ", out)
    out = re.sub(r"(?i)смотреть эфир", "смотреть", out)
    return out


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


def categories_line(r, urls=None):
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
    # Каждое имя — ссылка на директорию Twitch, если она у нас подтверждена.
    # Без этого читателю приходилось искать категорию руками.
    def link(name):
        url = category_url_for(urls, name)
        return f'<a href="{esc(url)}">{esc(name)}</a>' if url else esc(name)
    head = ", ".join(link(c) for c in cats[:3])
    if len(cats) > 3:
        return (f"в {len(cats)} {plural_cat(len(cats))} — {head} и другие")
    return f"в категориях {head}"


def plural_cat(n):
    if n % 10 == 1 and n % 100 != 11:
        return "категории"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "категориях"
    return "категориях"


def how_short(r, urls=None):
    """Компактно, одной строкой (inline и альбомы)."""
    return f"📍 {how_text(r, urls)}"


def _to_streamer(cond):
    """«Подписка или гифт» → «…любому стримеру»: без этого читатель не понимает,
    что подписываться можно на кого угодно в категории, а не на конкретный канал."""
    if re.search(r"(?i)(подписка|гифт)$", cond):
        return f"{cond} любому стримеру"
    return cond


def how_text(r, urls=None):
    """Как получить + куда идти за значком."""
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
            return f'Условия уточняются · {where} <a href="{esc(url)}">{esc(label)}</a>'
        many = categories_line(r, urls)
        if many:
            return f"Условия уточняются · {many}"
        if (r.get("window") or {}).get("channel_count"):
            return f"Условия уточняются · у участвующих стримеров{grp_tail}"
        return "Условия уточняются"

    game = (r.get("window") or {}).get("game")
    if url and not (kind == "external" and game):
        prep = {"category": "в категории", "event": "на каналах события",
                "channel": "у стримера", "external": "—"}[kind]
        c = _to_streamer(cond) if kind == "category" else cond
        return f'{esc(c)} {prep} <a href="{esc(url)}">{esc(label)}</a>'
    # Категория известна, а проверенной ссылки на неё нет (Twitch называет её
    # иначе — «Tom Clancy\'s Rainbow Six Siege»). Тогда пишем категорию текстом:
    # вести читателя вместо неё на страницу магазина — обман, значок там не дают.
    if game:
        return f"{esc(_to_streamer(cond))} в категории {esc(game)}"
    many = categories_line(r, urls)
    if many:
        return f"{esc(cond)} {many}"
    # Каналовый бейдж без курируемой ссылки — просто чёткий текст, без битых ссылок.
    if (r.get("window") or {}).get("channel_count"):
        return f"{esc(cond)} — у участвующих стримеров{grp_tail}"
    return esc(cond)


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


def build_caption(top_lines, r, urls=None):
    """Подпись: верхние строки (заголовок/статус/окно) → как получить → футер."""
    parts = [ln for ln in top_lines if ln]
    parts += ["", how_short(r, urls)]
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
    return {"text": text, "url": url}


def twitch_buttons(r):
    """Кнопки inline-сообщения: смотреть (категория/дропы) + наш канал."""
    rows = []
    wb = watch_button(r)
    if wb:
        rows.append([wb])
    rows.append([{"text": "📱 Наш канал", "url": CHANNEL_URL}])
    return rows


def newest_key(r):
    """Сортировка от самых новых к старым (по дате появления бейджа)."""
    return r.get("first_seen") or ""


def inline_header(r):
    """Стоимость — в тексте: цветной кружок + слово."""
    e = COST_EMOJI.get(r.get("cost"), "⚪")
    w = {"paid": "платно", "free": "бесплатно"}.get(r.get("cost"), "")
    tail = f" — {w}" if w else ""
    return f'{e} <b>{esc(r["title"])}</b>{tail}'


def inline_caption(r, urls=None):
    return build_caption([inline_header(r), status_word(r), window_line(r)], r, urls)


def inline_desc(r):
    """Короткая строка под названием в списке результатов."""
    cond = short_cond(r.get("condition"), (r.get("window") or {}).get("from_manual")) or ""
    w = r.get("window") or {}
    if r["status"] == "active" and w.get("end"):
        return f"✅ до {fmt_dt(w['end'])} · {cond}"
    if r["status"] == "upcoming" and w.get("start"):
        return f"📣 старт {fmt_dt(w['start'])} · {cond}"
    return f"📣 анонс · {cond}" if r["status"] == "upcoming" else cond


def cost_word(r):
    return {"paid": "платный", "free": "бесплатный"}.get(r.get("cost"), "")


CHANNEL_HEADS = {
    "appeared_active": "🎁 <b>Новый значок — можно получить уже сейчас!</b>",
    "active_short": "⚡ <b>Доступно сейчас — но ненадолго!</b>",
    "appeared_upcoming": "📣 <b>Скоро новый значок</b>",
    "dates_confirmed": "⏰ <b>Уточнили время</b>",
    "cond_confirmed": "📝 <b>Стало известно, как получить</b>",
    "started": "▶️ <b>Стартовало — можно получать сейчас!</b>",
    "ending": "⏳ <b>Последний день! Успей получить</b>",
    # Q6: значок уже есть, а сроков нет нигде. Не обещаем «скоро»: раздача,
    # возможно, уже идёт. Когда даты появятся, придёт «Уточнили время».
    "appeared_nodates": "🆕 <b>Новый значок — сроки пока неизвестны</b>",
    # Q7: после «Последнего дня» кампанию продлили — исправляем устаревший пост.
    "extended": "🔁 <b>Продлили — ещё можно получить!</b>",
}


def channel_header(kind, r):
    """Две строки: что случилось + какой значок. Цена — прямо в имени: это
    первое, что читатель хочет знать, решая, читать ли дальше."""
    cw = cost_word(r)
    badge = f"{COST_EMOJI.get(r.get('cost'), '🏷')} {cw.capitalize() + ' значок' if cw else 'Значок'}"
    name = f"{badge} <b>{esc(r['title'])}</b>"
    head = CHANNEL_HEADS.get(kind)
    return f"{head}\n{name}" if head else name


def channel_caption(kind, r, urls=None):
    """Пост в канал — разделами с заголовками: «как получить» и «когда» читаются
    отдельно и находятся взглядом, а не выковыриваются из сплошного абзаца."""
    parts = [channel_header(kind, r), "",
             "❓ <b>Как получить</b>", how_text(r, urls), "",
             "📅 <b>Когда</b>", window_text(r)]
    note = art_disclaimer(r)
    if note:
        parts += ["", note]
    parts += ["", footer_line()]
    return "\n".join(parts)


def channel_buttons(r):
    """Кнопки поста: смотреть (категория/дропы) + две ссылки внизу (канал и бот)."""
    rows = []
    wb = watch_button(r)
    if wb:
        rows.append([wb])
    rows.append([
        {"text": "📱 Наш канал", "url": CHANNEL_URL},
        {"text": "🤖 Бот", "url": f"https://t.me/{BOT_USERNAME}"},
    ])
    return rows


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
    if kind == "appeared_nodates":
        return f'🆕 <b>Новые значки — сроки пока неизвестны</b>{tail}'
    if kind == "extended":
        return f'🔁 <b>Продлили — ещё можно получить!</b>{tail}'
    return f'⏳ <b>Последний день — успей получить!</b>{tail}'


def _meta_sig(r, urls=None):
    """Подпись «окно + условие» — чтобы понять, одинаковы ли они у всей группы."""
    w = r.get("window") or {}
    return (iso(w.get("start")), iso(w.get("end")), bool(w.get("dates_coarse")),
            bool(w.get("dates_unconfirmed")), how_short(r, urls))


def album_caption(items, kind, group, urls=None):
    """ОДНА подпись на весь альбом: Telegram под медиагруппой показывает только
    первую, остальные видны лишь при открытии фото — значит весь текст в неё.
    Если даты и условие у всех совпадают (типичный случай — тиры одного события),
    пишем их один раз, а бейджи перечисляем именами."""
    parts = [album_header(kind, group, len(items))]
    if len({_meta_sig(r, urls) for _, r in items}) == 1:
        r0 = items[0][1]
        parts += [window_line(r0), how_short(r0, urls), ""]
        parts += [f"• {esc(r['title'])}" for _, r in items]
    else:
        parts.append("")
        for _, r in items:
            parts += [f"<b>{esc(r['title'])}</b>", window_line(r), how_short(r, urls), ""]
        parts.pop()
    parts += ["", footer_line()]
    return "\n".join(parts)


def category_url_for(urls, name):
    """Ссылка на категорию из карты ссылок сборки (без учёта регистра и артикля)."""
    if not name:
        return None
    want = _cat_key(name)
    for k, v in (urls or {}).items():
        if _cat_key(k) == want:
            return v
    return None
