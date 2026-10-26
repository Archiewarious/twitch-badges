"""Тексты постов в канал и inline — перенос из bot/bot.py без изменения поведения.

Чистые функции: запись → HTML-подпись. Кнопки — данными
([[{"text", "url"}]]), в объекты Telegram их превращает бот.
urls — ссылки на категории после канонизации (Built.category_urls): раньше
подписи брали их из глобала generate_site.
"""
import html
import re
from datetime import timedelta, timezone

from ..domain.categories import _cat_key
from .locale import RU, Locale
from .translate import translate_condition

BOT_USERNAME = "InfoTwitchBot"


CHANNEL_URL = "https://t.me/TwitchInfoRadar"


CHANNEL_HANDLE = "@TwitchInfoRadar"


RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]


COST_EMOJI = {"free": "🟢", "paid": "🟠"}


# Тексты постов на английском и испанском (русские — прямо в функциях, как были).
TXT = {
    "en": {
        "tba": "Dates TBA", "dates_unconfirmed": " · exact dates TBA",
        "from_to": "From {s} to {e}", "until": "Until {e}", "from": "From {s}",
        "cond_tba": "Conditions TBA",
        "where": {"category": "see drops in the", "event": "see the event's channels",
                  "channel": "watch", "external": "details —"},
        "prep": {"category": "in the", "event": "on the event's channels:", "channel": "on",
                 "external": "—"},
        "cat_tail": {"category": " category"},
        "in_cat": "{c} in the {g} category",
        "streamers": "at participating streamers{g}", "grp_tail": " of the «{g}» event",
        "to_streamer": " to any streamer",
        "cats_many": "in {n} categories — {head} and more", "cats_few": "in categories {head}",
        "how": "❓ <b>How to get it</b>", "when": "📅 <b>When</b>",
        "badge": "Badge", "badge_cost": {"paid": "Paid badge", "free": "Free badge"},
        "art": "🖼 The image shows the {y} badge: Twitch hasn't revealed the final art yet",
        "official": "official site", "on_twitch": "on Twitch",
        "btn_watch": "▶️ Watch: {l}", "btn_site": "🔗 Official site", "btn_channel": "📱 Our channel",
        "badges": ("badge", "badges"),
        "heads": {
            "appeared_active": "🎁 <b>New badge — available right now!</b>",
            "active_short": "⚡ <b>Available now — but not for long!</b>",
            "appeared_upcoming": "📣 <b>New badge coming soon</b>",
            "dates_confirmed": "⏰ <b>Time confirmed</b>",
            "cond_confirmed": "📝 <b>Now we know how to get it</b>",
            "started": "▶️ <b>Started — get it now!</b>",
            "ending": "⏳ <b>Last day! Get it while you can</b>",
            "appeared_nodates": "🆕 <b>New badge — dates TBA</b>",
            "extended": "🔁 <b>Extended — still available!</b>",
        },
        "album": {
            "appeared_active": "🎁 <b>Available right now!</b>",
            "active_short": "⚡ <b>Available now — but not for long!</b>",
            "appeared_upcoming": "📅 <b>New badges coming soon</b>",
            "dates_confirmed": "⏰ <b>Time confirmed</b>",
            "cond_confirmed": "📝 <b>Now we know how to get them</b>",
            "started": "▶️ <b>Started — get them now!</b>",
            "appeared_nodates": "🆕 <b>New badges — dates TBA</b>",
            "extended": "🔁 <b>Extended — still available!</b>",
            "ending": "⏳ <b>Last day — get them while you can!</b>",
        },
    },
    "es": {
        "tba": "Fechas por confirmar", "dates_unconfirmed": " · fechas exactas por confirmar",
        "from_to": "Del {s} al {e}", "until": "Hasta el {e}", "from": "Desde el {s}",
        "cond_tba": "Condiciones por confirmar",
        "where": {"category": "mira los drops en la categoría", "event": "mira los canales del evento",
                  "channel": "mira el canal", "external": "detalles —"},
        "prep": {"category": "en la categoría", "event": "en los canales del evento:",
                 "channel": "con el streamer", "external": "—"},
        "cat_tail": {},
        "in_cat": "{c} en la categoría {g}",
        "streamers": "con los streamers participantes{g}", "grp_tail": " del evento «{g}»",
        "to_streamer": " a cualquier streamer",
        "cats_many": "en {n} categorías — {head} y más", "cats_few": "en las categorías {head}",
        "how": "❓ <b>Cómo conseguirla</b>", "when": "📅 <b>Cuándo</b>",
        "badge": "Insignia", "badge_cost": {"paid": "Insignia de pago", "free": "Insignia gratis"},
        "art": "🖼 La imagen muestra la insignia de {y}: Twitch aún no ha mostrado el arte final",
        "official": "sitio oficial", "on_twitch": "en Twitch",
        "btn_watch": "▶️ Ver: {l}", "btn_site": "🔗 Sitio oficial", "btn_channel": "📱 Nuestro canal",
        "badges": ("insignia", "insignias"),
        "heads": {
            "appeared_active": "🎁 <b>Nueva insignia — ¡ya disponible!</b>",
            "active_short": "⚡ <b>Disponible ahora — ¡pero por poco tiempo!</b>",
            "appeared_upcoming": "📣 <b>Pronto: nueva insignia</b>",
            "dates_confirmed": "⏰ <b>Horario confirmado</b>",
            "cond_confirmed": "📝 <b>Ya sabemos cómo conseguirla</b>",
            "started": "▶️ <b>¡Ya empezó — consíguela ahora!</b>",
            "ending": "⏳ <b>¡Último día! Consíguela ya</b>",
            "appeared_nodates": "🆕 <b>Nueva insignia — fechas por confirmar</b>",
            "extended": "🔁 <b>Ampliado — ¡aún disponible!</b>",
        },
        "album": {
            "appeared_active": "🎁 <b>¡Ya disponibles!</b>",
            "active_short": "⚡ <b>Disponibles ahora — ¡pero por poco tiempo!</b>",
            "appeared_upcoming": "📅 <b>Pronto: nuevas insignias</b>",
            "dates_confirmed": "⏰ <b>Horario confirmado</b>",
            "cond_confirmed": "📝 <b>Ya sabemos cómo conseguirlas</b>",
            "started": "▶️ <b>¡Ya empezó — consíguelas ahora!</b>",
            "appeared_nodates": "🆕 <b>Nuevas insignias — fechas por confirmar</b>",
            "extended": "🔁 <b>Ampliado — ¡aún disponibles!</b>",
            "ending": "⏳ <b>¡Último día — consíguelas ya!</b>",
        },
    },
}


def localize(r, loc: Locale = RU):
    """Запись для поста на языке loc: условие переведено заранее (и помечено),
    чтобы укорачивание длинной подписи (_variants) резало уже перевод."""
    if loc.lang == "ru" or r.get("_localized"):
        return r
    w = r.get("window") or {}
    cond = translate_condition(short_cond(r.get("condition"), w.get("from_manual")), loc.lang)
    return dict(r, condition=cond, _localized=True)


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
    # Вход закрыт: обязательный этап условия просрочен — новичок значок не
    # получит, хотя окно кампании ещё открыто (покемоны-стартеры, 20.10.2026).
    if w.get("entry_until") and now > w["entry_until"]:
        return False
    # Слишком далёкий анонс — не новость (FFXIV JP стартует через 99 дней).
    # Появится в боте, когда приблизится. Сайт-каталог горизонт не применяет.
    if r["status"] == "upcoming" and w.get("start"):
        if (w["start"] - now).days > UPCOMING_HORIZON_DAYS:
            return False
    return True


def fmt_dt(dt, with_time=True, loc: Locale = RU):
    """День+месяц+время в поясе канала: '25 июля 14:59'. with_time=False → только
    дата (когда источник времени не указал — не выдумываем точность, которой нет)."""
    if loc.lang == "ru":
        m = dt + timedelta(hours=3)
        base = f"{m.day} {RU_MONTHS_GEN[m.month - 1]}"
        return f"{base} {m.strftime('%H:%M')}" if with_time else base
    m = loc.local(dt)
    if loc.lang == "en":
        base = f"{loc.months[m.month - 1]} {m.day}"
        if not with_time:
            return base
        hh = m.hour % 12 or 12
        return f"{base}, {hh}:{m.minute:02d} {'AM' if m.hour < 12 else 'PM'}"
    base = f"{m.day} de {loc.months[m.month - 1]}"
    return f"{base}, {m.strftime('%H:%M')}" if with_time else base


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


def stamp(dt, with_time, loc: Locale = RU):
    """Момент окна: местное время с подписью пояса + UTC (27.10.2026, просьба
    владельца: «UTC плюс местное, с подписью»). Без часа — только дата.
    Если в UTC уже другой день — в скобках и дата."""
    if not with_time:
        return fmt_dt(dt, False, loc)
    utc = dt.astimezone(timezone.utc)
    local = (dt + timedelta(hours=3)) if loc.lang == "ru" else loc.local(dt)
    same_day = local.date() == utc.date()
    if loc.lang == "ru":
        u = f"{utc:%H:%M}" if same_day else f"{utc.day} {RU_MONTHS_GEN[utc.month - 1]} {utc:%H:%M}"
    elif loc.lang == "en":
        u = f"{utc:%H:%M}" if same_day else f"{loc.months[utc.month - 1]} {utc.day}, {utc:%H:%M}"
    else:
        u = f"{utc:%H:%M}" if same_day else f"{utc.day} de {loc.months[utc.month - 1]}, {utc:%H:%M}"
    return f"{fmt_dt(dt, True, loc)} {loc.tz_label} ({u} UTC)"


def window_line(r, loc: Locale = RU):
    return f"📅 {window_text(r, loc)}"


def window_text(r, loc: Locale = RU):
    """Полное окно с датой и временем (МСК). Неизвестно — так и пишем.
    Для окон, разобранных из текста описания (from_page), время показываем только
    если оно там реально было — иначе 00:00 было бы выдуманной точностью."""
    w = r.get("window") or {}
    s, e = w.get("start"), w.get("end")
    st = time_known(w, "start")
    et = time_known(w, "end")
    if loc.lang != "ru":
        t = TXT[loc.lang]
        note = t["dates_unconfirmed"] if w.get("dates_unconfirmed") else ""
        if s and e:
            return t["from_to"].format(s=stamp(s, st, loc), e=stamp(e, et, loc)) + note
        if e:
            return t["until"].format(e=stamp(e, et, loc)) + note
        if s:
            return t["from"].format(s=stamp(s, st, loc)) + note
        return t["tba"]
    note = " · точные даты уточняются" if w.get("dates_unconfirmed") else ""
    if s and e:
        return f"С {stamp(s, st)} до {stamp(e, et)}{note}"
    if e:
        return f"До {stamp(e, et)}{note}"
    if s:
        return f"С {stamp(s, st)}{note}"
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


def watch_target(r, loc: Locale = RU):
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
    # Ссылка на сам источник (streamdatabase.com) — не «куда идти»: она могла
    # попасть в снапшот до фильтра в extract_link_from_text.
    if "streamdatabase.com" in url:
        url = ""
    if url.startswith("https://"):
        on_twitch = "на Twitch" if loc.lang == "ru" else TXT[loc.lang]["on_twitch"]
        if "/directory/event/" in url:
            return ("event", tl.get("label") or on_twitch, url)
        if "/directory/category/" in url:
            return ("category", tl.get("label") or on_twitch, url)
        m = re.match(r"https://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)/?$", url)
        if m:                       # канал конкретного стримера (La Velada → ibai)
            return ("channel", m.group(1), url)
        # Не twitch.tv вообще (напр. официальный сайт офлайн-мероприятия типа
        # TwitchCon) — не приписываем "категория"/"событие", это неверно.
        # Метка со страницы StreamDatabase часто на английском/с опечатками —
        # не тащим её в кнопку/текст поста, ставим нейтральную формулировку.
        return ("external", "официальный сайт" if loc.lang == "ru" else TXT[loc.lang]["official"], url)
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


def categories_line(r, urls=None, loc: Locale = RU):
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
    if loc.lang != "ru":
        t = TXT[loc.lang]
        return (t["cats_many"].format(n=len(cats), head=head) if len(cats) > 3
                else t["cats_few"].format(head=head))
    if len(cats) > 3:
        return (f"в {len(cats)} {plural_cat(len(cats))} — {head} и другие")
    return f"в категориях {head}"


def plural_cat(n):
    if n % 10 == 1 and n % 100 != 11:
        return "категории"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "категориях"
    return "категориях"


def how_short(r, urls=None, loc: Locale = RU):
    """Компактно, одной строкой (inline и альбомы)."""
    return f"📍 {how_text(r, urls, loc)}"


def _to_streamer(cond, loc: Locale = RU):
    """«Подписка или гифт» → «…любому стримеру»: без этого читатель не понимает,
    что подписываться можно на кого угодно в категории, а не на конкретный канал."""
    if loc.lang == "ru":
        if re.search(r"(?i)(подписка|гифт)$", cond):
            return f"{cond} любому стримеру"
        return cond
    if re.search(r"(?i)(sub|suscripción)$", cond):
        return cond + TXT[loc.lang]["to_streamer"]
    return cond


def how_text(r, urls=None, loc: Locale = RU):
    """Как получить + куда идти за значком."""
    if loc.lang != "ru":
        return _how_text_l10n(localize(r, loc), urls, loc)
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


def _how_text_l10n(r, urls, loc: Locale):
    """how_text для не-русских каналов: та же логика, условие уже переведено."""
    t = TXT[loc.lang]
    cond = r.get("condition") or ""
    kind, label, url = watch_target(r, loc)
    grp = r.get("group") or ""
    grp_tail = t["grp_tail"].format(g=esc(grp)) if grp and grp != "__permanent__" else ""
    w = r.get("window") or {}

    def link(text, href, tail=""):
        return f'<a href="{esc(href)}">{esc(text)}</a>{tail}'

    if not cond:
        if url:
            return (f'{t["cond_tba"]} · {t["where"][kind]} '
                    f'{link(label, url, t["cat_tail"].get(kind, ""))}')
        many = categories_line(r, urls, loc)
        if many:
            return f'{t["cond_tba"]} · {many}'
        if w.get("channel_count"):
            return f'{t["cond_tba"]} · {t["streamers"].format(g=grp_tail)}'
        return t["cond_tba"]
    game = w.get("game")
    if url and not (kind == "external" and game):
        c = _to_streamer(cond, loc) if kind == "category" else cond
        return f'{esc(c)} {t["prep"][kind]} {link(label, url, t["cat_tail"].get(kind, ""))}'
    if game:
        return t["in_cat"].format(c=esc(_to_streamer(cond, loc)), g=esc(game))
    many = categories_line(r, urls, loc)
    if many:
        return f"{esc(cond)} {many}"
    if w.get("channel_count"):
        return f'{esc(cond)} — {t["streamers"].format(g=grp_tail)}'
    return esc(cond)


def footer_line(loc: Locale = RU):
    if loc.lang != "ru":
        return f'📱 <a href="{loc.channel_url}">{loc.channel_handle}</a>'
    return _footer_ru()


def _footer_ru():
    return (f'📱 <a href="{CHANNEL_URL}">{CHANNEL_HANDLE}</a> · '
            f'<a href="https://t.me/{BOT_USERNAME}">@{BOT_USERNAME}</a> в любом чате')


def art_disclaimer(r, loc: Locale = RU):
    """Для кампаний, анонсированных до появления самого значка, на карточке стоит
    арт прошлогоднего выпуска (см. add_orphan_event_records). Молчать об этом
    нельзя: читатель примет заглушку за финальный дизайн."""
    prev = r.get("art_placeholder_from")
    if not prev:
        return None
    year = prev.rsplit("-", 1)[-1]
    if loc.lang != "ru":
        return TXT[loc.lang]["art"].format(y=year)
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


def watch_button(r, loc: Locale = RU):
    """Кнопка за значком: категория/событие на Twitch, или внешний сайт (если ссылка есть)."""
    kind, label, url = watch_target(r, loc)
    if not url:
        return None
    if loc.lang != "ru":
        t = TXT[loc.lang]
        return {"text": t["btn_site"] if kind == "external" else t["btn_watch"].format(l=label),
                "url": url}
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


def channel_header(kind, r, loc: Locale = RU):
    """Две строки: что случилось + какой значок. Цена — прямо в имени: это
    первое, что читатель хочет знать, решая, читать ли дальше."""
    if loc.lang != "ru":
        t = TXT[loc.lang]
        word = t["badge_cost"].get(r.get("cost"), t["badge"])
        name = f"{COST_EMOJI.get(r.get('cost'), '🏷')} {word} <b>{esc(r['title'])}</b>"
        head = t["heads"].get(kind)
        return f"{head}\n{name}" if head else name
    cw = cost_word(r)
    badge = f"{COST_EMOJI.get(r.get('cost'), '🏷')} {cw.capitalize() + ' значок' if cw else 'Значок'}"
    name = f"{badge} <b>{esc(r['title'])}</b>"
    head = CHANNEL_HEADS.get(kind)
    return f"{head}\n{name}" if head else name


def channel_caption(kind, r, urls=None, loc: Locale = RU):
    """Пост в канал — разделами с заголовками: «как получить» и «когда» читаются
    отдельно и находятся взглядом, а не выковыриваются из сплошного абзаца."""
    if loc.lang != "ru":
        t = TXT[loc.lang]
        parts = [channel_header(kind, r, loc), "", t["how"], how_text(r, urls, loc), "",
                 t["when"], window_text(r, loc)]
        note = art_disclaimer(r, loc)
        if note:
            parts += ["", note]
        parts += ["", footer_line(loc)]
        return "\n".join(parts)
    parts = [channel_header(kind, r), "",
             "❓ <b>Как получить</b>", how_text(r, urls), "",
             "📅 <b>Когда</b>", window_text(r)]
    note = art_disclaimer(r)
    if note:
        parts += ["", note]
    parts += ["", footer_line()]
    return "\n".join(parts)


def channel_buttons(r, loc: Locale = RU):
    """Кнопки поста: смотреть (категория/дропы) + две ссылки внизу (канал и бот)."""
    rows = []
    wb = watch_button(r, loc)
    if wb:
        rows.append([wb])
    if loc.lang != "ru":
        # Бот отвечает только по-русски — в чужом канале зовём лишь в сам канал.
        rows.append([{"text": TXT[loc.lang]["btn_channel"], "url": loc.channel_url}])
        return rows
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


def album_header(kind, group, n, loc: Locale = RU):
    """Шапка поста-альбома. group — название события (у «последнего дня» его нет:
    туда попадают разные бейджи, объединённые только датой)."""
    if loc.lang != "ru":
        t = TXT[loc.lang]
        one, many = t["badges"]
        tail = f'\n<b>{esc(group)}</b> — {n} {one if n == 1 else many}' if group else ""
        return t["album"].get(kind, t["album"]["ending"]) + tail
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


def _meta_sig(r, urls=None, loc: Locale = RU):
    """Подпись «окно + условие» — чтобы понять, одинаковы ли они у всей группы."""
    w = r.get("window") or {}
    return (iso(w.get("start")), iso(w.get("end")), bool(w.get("dates_coarse")),
            bool(w.get("dates_unconfirmed")), how_short(r, urls, loc))


def album_caption(items, kind, group, urls=None, loc: Locale = RU):
    """ОДНА подпись на весь альбом: Telegram под медиагруппой показывает только
    первую, остальные видны лишь при открытии фото — значит весь текст в неё.
    Если даты и условие у всех совпадают (типичный случай — тиры одного события),
    пишем их один раз, а бейджи перечисляем именами."""
    items = [(k, localize(r, loc)) for k, r in items]
    parts = [album_header(kind, group, len(items), loc)]
    if len({_meta_sig(r, urls, loc) for _, r in items}) == 1:
        r0 = items[0][1]
        parts += [window_line(r0, loc), how_short(r0, urls, loc), ""]
        parts += [f"• {esc(r['title'])}" for _, r in items]
    else:
        parts.append("")
        for _, r in items:
            parts += [f"<b>{esc(r['title'])}</b>", window_line(r, loc), how_short(r, urls, loc), ""]
        parts.pop()
    parts += ["", footer_line(loc)]
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


# ── лимиты подписей (B4) ──

def tg_len(s: str) -> int:
    """Длина в единицах Telegram (UTF-16). Считаем по HTML целиком — с запасом:
    лимит применяется к тексту без тегов, а он всегда короче."""
    return len((s or "").encode("utf-16-le")) // 2


def _shorten(text, n):
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return (cut or text[:n]) + "…"


def _variants(r):
    """Запись со всё более короткими необязательными частями: сначала дисклеймер
    арта, потом хвост условия, в самом конце — название."""
    yield r
    r = dict(r, art_placeholder_from=None)
    yield r
    cond = r.get("condition") or ""
    for n in (300, 160, 80, 40):
        if len(cond) > n:
            yield dict(r, condition=_shorten(cond, n))
    r = dict(r, condition=_shorten(cond, 40) if cond else cond)
    for n in (120, 60):
        if len(r.get("title") or "") > n:
            yield dict(r, title=_shorten(r["title"], n))


def fit_channel_caption(kind, r, urls=None, limit=CAPTION_LIMIT, loc: Locale = RU):
    """Подпись одиночного поста, гарантированно не длиннее лимита. Раньше
    длинная подпись давала BadRequest на каждом тике, и пост не уходил никогда."""
    last = None
    for v in _variants(localize(r, loc)):
        last = channel_caption(kind, v, urls, loc)
        if tg_len(last) <= limit:
            return last
    raise ValueError(f"подпись {r.get('set_id')} не укладывается в {limit}: {tg_len(last)}")


def fit_album_caption(items, kind, group, urls=None, limit=CAPTION_LIMIT, loc: Locale = RU):
    """Подпись альбома; для одной записи — с укорачиванием, как у одиночного поста."""
    cap = album_caption(items, kind, group, urls, loc)
    if tg_len(cap) <= limit or len(items) != 1:
        return cap
    key, r = items[0]
    for v in _variants(localize(r, loc)):
        cap = album_caption([(key, v)], kind, group, urls, loc)
        if tg_len(cap) <= limit:
            return cap
    raise ValueError(f"подпись альбома {key} не укладывается в {limit}")
