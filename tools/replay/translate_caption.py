"""Перевод готовых подписей русского канала (всех форматов с июля 2026) на
английский и испанский — для заливки истории в новые каналы.

Подпись разбирается построчно: заголовок, цена и имя значка, условие с местом
(категория, стример, событие), даты, подвал. Даты с временем — МСК; пересчитываются
в пояс канала. Строка, которую не удалось перевести, остаётся русской — вызывающий
обязан это проверить (CYR) и не отправлять такой пост.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from twitch_badges.publisher.translate import CYRILLIC as CYR, translate_condition

MSK = timezone(timedelta(hours=3))
RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"]
MON = {m: i + 1 for i, m in enumerate(RU_MONTHS)}


# ── сущности Telegram → HTML ──

def to_html(text, entities):
    """caption + caption_entities → HTML (смещения в UTF-16)."""
    if not text:
        return ""
    u = text.encode("utf-16-le")
    marks = []
    for e in entities or []:
        s, n = e["offset"], e["length"]
        if e["type"] == "bold":
            marks.append((s, 1, "<b>")); marks.append((s + n, 0, "</b>"))
        elif e["type"] == "text_link":
            marks.append((s, 1, f'<a href="{html.escape(e["url"], quote=True)}">'))
            marks.append((s + n, 0, "</a>"))
        elif e["type"] == "italic":
            marks.append((s, 1, "<i>")); marks.append((s + n, 0, "</i>"))
    marks.sort(key=lambda x: (x[0], x[1]))
    out, pos = [], 0
    for off, _, tag in marks:
        out.append(html.escape(u[pos * 2:off * 2].decode("utf-16-le"), quote=False))
        out.append(tag)
        pos = off
    out.append(html.escape(u[pos * 2:].decode("utf-16-le"), quote=False))
    return "".join(out)


# ── словари ──

T = {
    "en": {
        "heads": {
            "🎁 Можно получить уже сейчас!": "🎁 Available right now!",
            "🎁 Новый значок — можно получить уже сейчас!": "🎁 New badge — available right now!",
            "⚡ Доступно сейчас — но ненадолго!": "⚡ Available now — but not for long!",
            "📅 Скоро новый значок": "📅 New badge coming soon",
            "📣 Скоро новый значок": "📣 New badge coming soon",
            "📅 Скоро новые значки": "📅 New badges coming soon",
            "⏰ Уточнили время": "⏰ Time confirmed",
            "📝 Стало известно, как получить": "📝 Now we know how to get it",
            "▶️ Стартовало — можно получать сейчас!": "▶️ Started — get it now!",
            "⏳ Последний день! Успей получить": "⏳ Last day! Get it while you can",
            "⏳ Последний день — успей получить!": "⏳ Last day — get them while you can!",
            "🆕 Новый значок — сроки пока неизвестны": "🆕 New badge — dates TBA",
            "🆕 Новые значки — сроки пока неизвестны": "🆕 New badges — dates TBA",
            "🔁 Продлили — ещё можно получить!": "🔁 Extended — still available!",
            "🔁 Продлили — только для тех, кто начал до 1 октября":
                "🔁 Extended — only for those who started before October 1",
            "🏁 Раздача завершилась": "🏁 The drop has ended",
            "❓ Как получить": "❓ How to get it",
            "📅 Когда": "📅 When",
        },
        "paid": "Paid badge", "free": "Free badge", "badge": "Badge",
        "new_paid": "New paid badge", "new_free": "New free badge", "new": "New badge",
        "n_badges": lambda n: f"{n} badge" + ("s" if n != 1 else ""),
        "no_more": "no longer available",
        "tba": "Dates TBA", "unconfirmed": " · exact dates TBA",
        "from_to": "From {s} to {e}", "until": "Until {e}", "from": "From {s}",
        "start_until": "Starts {s}, until {e}", "start": "Starts {s}",
        "watch_until": "Finish by {e}",
        "cond_tba": "Conditions TBA",
        "in_cat": "in the {l} category", "to_streamer_in_cat": "to any streamer in the {l} category",
        "at_streamer": "on {l}", "event_channels": "on the event's channels: {l}",
        "participating": "at participating streamers", "participating_ev": "at participating streamers of the «{g}» event",
        "official": "official site",
        "see_channel": "watch {l}", "see_cat": "see drops in the {l} category",
        "see_event": "see the event's channels: {l}", "details": "details — {l}",
        "cats_few": "in categories {h}", "cats_many": "in {n} categories — {h} and more",
        "art": "🖼 The image shows the {y} badge: Twitch hasn't revealed the final art yet",
        "starters": {
            "Новым участникам уже нельзя. Купить Great Ball (подписка или гифт 1 уровня) больше нельзя, а первый просмотр нужно было сделать до 1 октября. Кто успел — досмотрите 20 минут ещё в 2 разных дня (значок из Great Ball случайный, шанс 1 из 3).":
                "No longer available to new participants: Great Balls (tier 1 sub or gift) are no longer sold, and the first watch had to happen before October 1. If you started in time, watch 20 minutes on 2 more different days (the Great Ball badge is random, 1 in 3 chance).",
            "Новым участникам уже нельзя. Первый просмотр (20 минут) нужно было сделать до 1 октября. Кто успел — досмотрите 20 минут ещё в 2 разных дня.":
                "No longer available to new participants: the first watch (20 minutes) had to happen before October 1. If you started in time, watch 20 minutes on 2 more different days.",
        },
    },
    "es": {
        "heads": {
            "🎁 Можно получить уже сейчас!": "🎁 ¡Ya disponible!",
            "🎁 Новый значок — можно получить уже сейчас!": "🎁 Nueva insignia — ¡ya disponible!",
            "⚡ Доступно сейчас — но ненадолго!": "⚡ Disponible ahora — ¡pero por poco tiempo!",
            "📅 Скоро новый значок": "📅 Pronto: nueva insignia",
            "📣 Скоро новый значок": "📣 Pronto: nueva insignia",
            "📅 Скоро новые значки": "📅 Pronto: nuevas insignias",
            "⏰ Уточнили время": "⏰ Horario confirmado",
            "📝 Стало известно, как получить": "📝 Ya sabemos cómo conseguirla",
            "▶️ Стартовало — можно получать сейчас!": "▶️ ¡Ya empezó — consíguela ahora!",
            "⏳ Последний день! Успей получить": "⏳ ¡Último día! Consíguela ya",
            "⏳ Последний день — успей получить!": "⏳ ¡Último día — consíguelas ya!",
            "🆕 Новый значок — сроки пока неизвестны": "🆕 Nueva insignia — fechas por confirmar",
            "🆕 Новые значки — сроки пока неизвестны": "🆕 Nuevas insignias — fechas por confirmar",
            "🔁 Продлили — ещё можно получить!": "🔁 Ampliado — ¡aún disponible!",
            "🔁 Продлили — только для тех, кто начал до 1 октября":
                "🔁 Ampliado — solo para quienes empezaron antes del 1 de octubre",
            "🏁 Раздача завершилась": "🏁 La campaña terminó",
            "❓ Как получить": "❓ Cómo conseguirla",
            "📅 Когда": "📅 Cuándo",
        },
        "paid": "Insignia de pago", "free": "Insignia gratis", "badge": "Insignia",
        "new_paid": "Nueva insignia de pago", "new_free": "Nueva insignia gratis", "new": "Nueva insignia",
        "n_badges": lambda n: f"{n} insignia" + ("s" if n != 1 else ""),
        "no_more": "ya no se puede conseguir",
        "tba": "Fechas por confirmar", "unconfirmed": " · fechas exactas por confirmar",
        "from_to": "Del {s} al {e}", "until": "Hasta el {e}", "from": "Desde el {s}",
        "start_until": "Empieza el {s}, hasta el {e}", "start": "Empieza el {s}",
        "watch_until": "Termina antes del {e}",
        "cond_tba": "Condiciones por confirmar",
        "in_cat": "en la categoría {l}", "to_streamer_in_cat": "a cualquier streamer en la categoría {l}",
        "at_streamer": "con el streamer {l}", "event_channels": "en los canales del evento: {l}",
        "participating": "con los streamers participantes",
        "participating_ev": "con los streamers participantes del evento «{g}»",
        "official": "sitio oficial",
        "see_channel": "mira el canal {l}", "see_cat": "mira los drops en la categoría {l}",
        "see_event": "mira los canales del evento: {l}", "details": "detalles — {l}",
        "cats_few": "en las categorías {h}", "cats_many": "en {n} categorías — {h} y más",
        "art": "🖼 La imagen muestra la insignia de {y}: Twitch aún no ha mostrado el arte final",
        "starters": {
            "Новым участникам уже нельзя. Купить Great Ball (подписка или гифт 1 уровня) больше нельзя, а первый просмотр нужно было сделать до 1 октября. Кто успел — досмотрите 20 минут ещё в 2 разных дня (значок из Great Ball случайный, шанс 1 из 3).":
                "Ya no está disponible para nuevos participantes: las Great Ball (suscripción o regalo de nivel 1) ya no se venden y la primera visualización debía hacerse antes del 1 de octubre. Si empezaste a tiempo, mira 20 minutos en 2 días distintos más (la insignia de la Great Ball es aleatoria, probabilidad de 1 entre 3).",
            "Новым участникам уже нельзя. Первый просмотр (20 минут) нужно было сделать до 1 октября. Кто успел — досмотрите 20 минут ещё в 2 разных дня.":
                "Ya no está disponible para nuevos participantes: la primera visualización (20 minutos) debía hacerse antes del 1 de octubre. Si empezaste a tiempo, mira 20 minutos en 2 días distintos más.",
        },
    },
}


# ── даты ──

DATE_RE = re.compile(r"(\d{1,2}) (" + "|".join(RU_MONTHS) + r")(?: (\d{1,2}):(\d{2}))?")


class Dates:
    def __init__(self, lang, tz, tz_label, msg_dt):
        self.lang, self.tz, self.tz_label, self.msg_dt = lang, tz, tz_label, msg_dt
        self.months = (["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                       if lang == "en" else
                       ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                        "septiembre", "octubre", "noviembre", "diciembre"])

    def _year(self, month, day):
        y = self.msg_dt.year
        d = datetime(y, month, day, tzinfo=timezone.utc)
        if (d - self.msg_dt).days < -200:
            return y + 1
        if (d - self.msg_dt).days > 200:
            return y - 1
        return y

    def fmt(self, m):
        """Один «D месяц[ HH:MM]» МСК → текст в поясе канала."""
        day, mon = int(m.group(1)), MON[m.group(2)]
        y = self._year(mon, day)
        if m.group(3) is None:
            return self._date(datetime(y, mon, day)), False
        dt = datetime(y, mon, day, int(m.group(3)), int(m.group(4)), tzinfo=MSK)
        loc = dt.astimezone(ZoneInfo(self.tz))
        utc = dt.astimezone(timezone.utc)
        u = f"{utc:%H:%M}" if utc.date() == loc.date() else (
            f"{self._date(utc)}, {utc:%H:%M}")
        if self.lang == "en":
            hh = loc.hour % 12 or 12
            local = f"{self._date(loc)}, {hh}:{loc.minute:02d} {'AM' if loc.hour < 12 else 'PM'}"
        else:
            local = f"{self._date(loc)}, {loc.strftime('%H:%M')}"
        # как в живых постах: местное с подписью пояса + UTC (27.10.2026)
        return f"{local} {self.tz_label} ({u} UTC)", True

    def _date(self, d):
        if self.lang == "en":
            return f"{self.months[d.month - 1]} {d.day}"
        return f"{d.day} de {self.months[d.month - 1]}"

    def line(self, body):
        """Тело строки дат (без «📅 ») → перевод или None."""
        t = T[self.lang]
        note = ""
        if body.endswith(" · точные даты уточняются"):
            body, note = body[: -len(" · точные даты уточняются")], t["unconfirmed"]
        body = body.replace(" (МСК)", "")
        if body == "Даты пока неизвестны":
            return t["tba"] + note
        ds = list(DATE_RE.finditer(body))
        shape = DATE_RE.sub("D", body)
        v = [self.fmt(m)[0] for m in ds]
        tail = ""
        forms = {"С D до D": "from_to", "До D": "until", "С D": "from",
                 "Старт D, до D": "start_until", "Старт D": "start", "Досмотреть до D": "watch_until"}
        key = forms.get(shape)
        if not key:
            return None
        names = {"from_to": ("s", "e"), "until": ("e",), "from": ("s",), "start_until": ("s", "e"),
                 "start": ("s",), "watch_until": ("e",)}[key]
        return t[key].format(**dict(zip(names, v))) + tail + note


# ── условие с местом ──

A = r'(<a href="[^"]+">[^<]*</a>)'


def _cond(ru, lang):
    tr = translate_condition(ru, lang)
    if tr is None:
        extra = {
            "Билет TwitchCon (1 день)": ("TwitchCon ticket (1 day)", "Entrada para TwitchCon (1 día)"),
            "Билет TwitchCon (3 дня)": ("TwitchCon ticket (3 days)", "Entrada para TwitchCon (3 días)"),
            "Смотреть трансляцию запуска на канале NASA": ("Watch the launch stream on the NASA channel",
                                                           "Ver la transmisión del lanzamiento en el canal de la NASA"),
        }
        if ru in extra:
            return extra[ru][0 if lang == "en" else 1]
    return tr


def how_line(s, lang):
    """Условие + где (весь хвост строки после «📍 » или под «Как получить»)."""
    t = T[lang]
    if s in t["starters"]:
        return t["starters"][s]
    rules = [
        (rf"^Условия уточняются · смотри канал {A}$", lambda m: f'{t["cond_tba"]} · {t["see_channel"].format(l=m[1])}'),
        (rf"^Условия уточняются · смотри дропы в категории {A}$", lambda m: f'{t["cond_tba"]} · {t["see_cat"].format(l=m[1])}'),
        (rf"^Условия уточняются · смотри каналы события {A}$", lambda m: f'{t["cond_tba"]} · {t["see_event"].format(l=m[1])}'),
        (r'^Условия уточняются · подробности — <a href="([^"]+)">официальный сайт</a>$',
         lambda m: t["cond_tba"] + " · " + t["details"].format(
             l='<a href="' + m[1] + '">' + t["official"] + "</a>")),
        (rf"^Условия уточняются · подробности — {A}$", lambda m: f'{t["cond_tba"]} · {t["details"].format(l=m[1])}'),
        (r"^Условия уточняются · у участвующих стримеров события «(.+)»$",
         lambda m: f'{t["cond_tba"]} · {t["participating_ev"].format(g=m[1])}'),
        (r"^Условия уточняются · у участвующих стримеров$", lambda m: f'{t["cond_tba"]} · {t["participating"]}'),
        (r"^Условия уточняются · (в .+)$", lambda m: f'{t["cond_tba"]} · {_cats(m[1], lang)}'),
        (r"^Условия уточняются$", lambda m: t["cond_tba"]),
        (rf"^(.+?) любому стримеру в категории {A}$",
         lambda m: _j(_cond(m[1], lang), t["to_streamer_in_cat"].format(l=m[2]))),
        (r"^(.+?) любому стримеру в категории (.+)$",
         lambda m: _j(_cond(m[1], lang), t["to_streamer_in_cat"].format(l=m[2]))),
        (rf"^(.+?) в категории {A}$", lambda m: _j(_cond(m[1], lang), t["in_cat"].format(l=m[2]))),
        (r"^(.+?) в категории (.+)$", lambda m: _j(_cond(m[1], lang), t["in_cat"].format(l=m[2]))),
        (rf"^(.+?) у стримера {A}$", lambda m: _j(_cond(m[1], lang), t["at_streamer"].format(l=m[2]))),
        (rf"^(.+?) на каналах события {A}$", lambda m: _j(_cond(m[1], lang), t["event_channels"].format(l=m[2]))),
        (r'^(.+?) — <a href="([^"]+)">официальный сайт</a>$',
         lambda m: _j(_cond(m[1], lang), f'— <a href="{m[2]}">{t["official"]}</a>')),
        (r"^(.+?) — у участвующих стримеров события «(.+)»$",
         lambda m: _j(_cond(m[1], lang), "— " + t["participating_ev"].format(g=m[2]))),
        (r"^(.+?) — у участвующих стримеров$", lambda m: _j(_cond(m[1], lang), "— " + t["participating"])),
        (r'^(.+?) — <a href="([^"]+)">у участвующих стримеров</a>$',
         lambda m: _j(_cond(m[1], lang), f'— <a href="{m[2]}">{t["participating"]}</a>')),
        (r"^(.+?) (в \d+ категори\w+ — .+)$", lambda m: _j(_cond(m[1], lang), _cats(m[2], lang))),
        (r"^(.+?) (в категориях .+)$", lambda m: _j(_cond(m[1], lang), _cats(m[2], lang))),
        (r"^(.+)$", lambda m: _cond(m[1], lang)),
    ]
    for rx, fn in rules:
        mm = re.match(rx, s)
        if mm:
            out = fn(mm)
            if out:
                return out
    return None


def _j(cond, where):
    return f"{cond} {where}" if cond else None


def _cats(s, lang):
    t = T[lang]
    m = re.match(r"^в (\d+) категори\w+ — (.+) и другие$", s)
    if m:
        return t["cats_many"].format(n=m[1], h=m[2])
    m = re.match(r"^в категориях (.+)$", s)
    if m:
        return t["cats_few"].format(h=m[1])
    return s


# ── вся подпись ──

def translate(caption_html, lang, tz, tz_label, msg_dt, channel_url, channel_handle):
    t = T[lang]
    dates = Dates(lang, tz, tz_label, msg_dt)
    out = []
    lines = caption_html.split("\n")
    in_how = in_when = False
    for ln in lines:
        s = ln.strip()
        plain = re.sub(r"<[^>]+>", "", s)
        if not s:
            out.append("")
            in_how = in_when = False if not s else in_how
            continue
        # подвал
        if "@TwitchInfoRadar" in plain:
            out.append(f'📱 <a href="{channel_url}">{channel_handle}</a>')
            continue
        # заголовки и разделы
        bare = plain
        if bare in t["heads"]:
            out.append(f"<b>{t['heads'][bare]}</b>" if "<b>" in s else t["heads"][bare])
            in_how, in_when = bare == "❓ Как получить", bare == "📅 Когда"
            continue
        if in_when:
            r = dates.line(plain)
            out.append(r if r is not None else s)
            in_when = False
            continue
        if in_how:
            r = how_line(s, lang)
            out.append(r if r is not None else s)
            in_how = False
            continue
        if s.startswith("📅 "):
            r = dates.line(plain[2:].strip())
            out.append("📅 " + r if r is not None else s)
            continue
        if s.startswith("📍 "):
            r = how_line(s[2:].strip(), lang)
            out.append("📍 " + r if r is not None else s)
            continue
        # «🟠 Платный значок <b>X</b>», «Новый бесплатный значок <b>X</b>», «Платный значок X»
        m = re.match(r"^((?:🟢|🟠|🏷) )?(Новый )?(платный|бесплатный|Платный|Бесплатный)? ?[Зз]начок (.+)$", s)
        if m:
            kind = (m[3] or "").lower()
            if m[2]:
                word = t["new_paid"] if kind == "платный" else t["new_free"] if kind == "бесплатный" else t["new"]
            else:
                word = t["paid"] if kind == "платный" else t["free"] if kind == "бесплатный" else t["badge"]
            out.append(f"{m[1] or ''}{word} {m[4]}")
            continue
        # «<b>Событие</b> — N значков[ — больше не получить]»
        m = re.match(r"^(.+) — (\d+) значк\w*( — больше не получить)?$", s)
        if m:
            tail = f" — {t['no_more']}" if m[3] else ""
            out.append(f"{m[1]} — {t['n_badges'](int(m[2]))}{tail}")
            continue
        m = re.match(r"^(.+) — больше не получить$", s)
        if m:
            out.append(f"{m[1]} — {t['no_more']}")
            continue
        m = re.match(r"^🖼 На картинке — значок (\d{4}) года: финальный арт Twitch ещё не показал$", plain)
        if m:
            out.append(t["art"].format(y=m[1]))
            continue
        out.append(s)                     # имя значка, «• Имя» и т.п. — как есть
    text = "\n".join(out).strip()
    if lang == "en":
        text = re.sub(r"\bthe (<a [^>]+>)?(?=The )", r"\1", text)
    return text
