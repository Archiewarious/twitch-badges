"""Перевод условия «как получить» с русского на английский и испанский.

Условие собирает наш же код из конечного набора фраз (conditions.py, short_cond),
поэтому переводим по шаблонам, а не «машинным переводом». Осталась хоть одна
незнакомая русская буква — возвращаем None: пост честно скажет «условия
уточняются», а не покажет полурусскую строку. Так бывает с текстом, вписанным
руками в manual/overrides.json.
"""
from __future__ import annotations

import re

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

_H = r"(\d+) (?:часов|часа|час)"
_M = r"(\d+) (?:минуту|минуты|минут)"
DURATION = rf"(?:{_H}(?: {_M})?|{_M})"


def _duration(m, lang, base=1):
    """Группы base..base+2 — (часы, минуты при часах, минуты без часов)."""
    h, hm, mm = m.group(base), m.group(base + 1), m.group(base + 2)
    mins = int(hm) if hm else (int(mm) if mm else 0)
    hours = int(h) if h else 0
    out = []
    if lang == "en":
        if hours:
            out.append(f"{hours} hour" + ("s" if hours != 1 else ""))
        if mins:
            out.append(f"{mins} minute" + ("s" if mins != 1 else ""))
    else:
        if hours:
            out.append(f"{hours} hora" + ("s" if hours != 1 else ""))
        if mins:
            out.append(f"{mins} minuto" + ("s" if mins != 1 else ""))
    return " ".join(out)


def _days(n, lang):
    n = int(n)
    if lang == "en":
        return f"{n} day" + ("s" if n != 1 else "")
    return f"{n} día" + ("s" if n != 1 else "")


def _subs(amount, tier, verb, lang):
    """verb: sub | gift. amount/tier — строки или None («неизвестного уровня»)."""
    n = int(amount) if amount else 1
    if lang == "en":
        tier_s = f"tier {tier} " if tier else ""
        if n == 1:
            return f"subscribe ({tier_s.strip()})" if verb == "sub" and tier else \
                ("subscribe" if verb == "sub" else f"gift a {tier_s}sub")
        return f"{'get' if verb == 'sub' else 'gift'} {n} {tier_s}subs"
    nivel = f" de nivel {tier}" if tier else ""
    what = f"{n} suscripciones{nivel}" if n > 1 else f"una suscripción{nivel}"
    return f"suscribirse con {what}" if verb == "sub" else f"regalar {what}"


def _rules(lang):
    en = lang == "en"
    T = (lambda e, s: e if en else s)
    return [
        # TwitchCon — офлайн (короткая и полная формы)
        (rf"(?:купить )?билет на (?:офлайн-мероприятие )?TwitchCon(?: — офлайн)?,? \(?(\d+) (?:день|дня|дней)\)?",
         lambda m: T(f"TwitchCon ticket — in-person event, {_days(m.group(1), lang)}",
                     f"entrada para TwitchCon — evento presencial, {_days(m.group(1), lang)}")),
        (r"(?:купить )?билет на (?:офлайн-мероприятие )?TwitchCon(?: — офлайн-мероприятие)?",
         lambda m: T("TwitchCon ticket — in-person event", "entrada para TwitchCon — evento presencial")),
        # подписка / гифт (короткие формы short_cond)
        (r"подписка, гифт или ", lambda m: T("sub, gift sub or ", "suscripción, regalo de suscripción o ")),
        (r"подписка или гифт", lambda m: T("sub or gift sub", "suscripción o regalo de suscripción")),
        (r"оформить или подарить подписку", lambda m: T("subscribe or gift a sub",
                                                         "suscribirse o regalar una suscripción")),
        # полные формы из describe_condition_ru
        (r"оформить (?:(\d+) )?подписк\w* (?:(\d+) уровня|неизвестного уровня)",
         lambda m: _subs(m.group(1), m.group(2), "sub", lang)),
        (r"подарить (?:(\d+) )?подписк\w* (?:(\d+) уровня|неизвестного уровня)",
         lambda m: _subs(m.group(1), m.group(2), "gift", lang)),
        (r"оформить подписку Turbo", lambda m: T("get Twitch Turbo", "suscribirse a Twitch Turbo")),
        # просмотр
        (rf"смотреть(?: эфир| трансляцию)? {DURATION} в (\d+) (?:разный день|разных дня|разных дней)",
         lambda m: T(f"watch for {_duration(m, lang)} on {_days(m.group(4), lang).replace('day', 'different day')}",
                     f"ver {_duration(m, lang)} en {m.group(4)} días distintos")),
        (rf"смотреть(?: эфир| трансляцию)? {DURATION}",
         lambda m: T(f"watch for {_duration(m, lang)}", f"ver {_duration(m, lang)}")),
        (r"смотреть(?: эфир| трансляцию)?", lambda m: T("watch the stream", "ver la transmisión")),
        # стримерские шаги
        (rf"провести стрим {DURATION}", lambda m: T(f"stream for {_duration(m, lang)}",
                                                    f"transmitir {_duration(m, lang)}")),
        (r"провести стрим", lambda m: T("go live", "transmitir en directo")),
        (r"состоять в DJ-программе Twitch", lambda m: T("be in the Twitch DJ Program",
                                                        "estar en el Programa de DJ de Twitch")),
        (r"создать клип и поделиться им", lambda m: T("create a clip and share it",
                                                      "crear un clip y compartirlo")),
        (r"создать клип", lambda m: T("create a clip", "crear un clip")),
        # Bits
        (r"потратить (\d+) Bits", lambda m: T(f"spend {m.group(1)} Bits", f"usar {m.group(1)} Bits")),
        (r"потратить Bits", lambda m: T("spend Bits", "usar Bits")),
        # прочее
        (r"иметь предыдущий значок эволюции", lambda m: T("own the previous evolution badge",
                                                          "tener la insignia de la evolución anterior")),
        (r"купить билет", lambda m: T("buy a ticket", "comprar una entrada")),
        (r"\(шанс 1 из (\d+)\)", lambda m: T(f"(1 in {m.group(1)} chance)",
                                              f"(probabilidad de 1 entre {m.group(1)})")),
        # связки — последними: фразы выше их уже съели
        (r", затем ", lambda m: T(", then ", ", luego ")),
        (r" или ", lambda m: T(" or ", " o ")),
        (r" и ", lambda m: T(" and ", " y ")),
    ]


_COMPILED = {}


def translate_condition(ru: str | None, lang: str) -> str | None:
    """Русское условие (уже свёрнутое short_cond) → тот же смысл на lang."""
    if not ru:
        return None
    if lang == "ru":
        return ru
    rules = _COMPILED.get(lang)
    if rules is None:
        rules = _COMPILED[lang] = [(re.compile(p, re.I), f) for p, f in _rules(lang)]
    out = ru
    for rx, fn in rules:
        out = rx.sub(fn, out)
    if CYRILLIC.search(out):
        return None
    out = out.strip()
    return out[0].upper() + out[1:] if out else None
