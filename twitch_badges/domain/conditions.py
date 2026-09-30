"""Условия получения и цена: из структурных полей SD, текста и описаний Twitch."""
import re

from .text import plural, ru_duration_minutes, tier_ru, _subs_ru

def _step_ru(step):
    """Один шаг из steps → русская формулировка. Незнакомый тип → None."""
    t = step.get("type")
    if t == "watch":
        mins = step.get("watch_minutes")
        days = step.get("watch_days")
        text = f"смотреть эфир {ru_duration_minutes(mins)}" if mins else "смотреть эфир"
        if days and days > 1:
            # Ключевая деталь, которой нет в плоских полях: у покемонов нужно
            # именно 3 РАЗНЫХ дня, а не 60 минут подряд.
            text += f" в {days} {plural(days, 'разный день', 'разных дня', 'разных дней')}"
        return text
    if t == "subscription":
        return f"оформить {_subs_ru(step.get('subscription_amount'))} {tier_ru(step.get('subscription_tier'))}"
    if t == "subscription_gift":
        return f"подарить {_subs_ru(step.get('subscription_gift_amount'))} {tier_ru(step.get('subscription_gift_tier'))}"
    if t == "twitchcon":
        days = step.get("twitchcon_days")
        return (f"купить билет на офлайн-мероприятие TwitchCon ({days} "
                f"{plural(days, 'день', 'дня', 'дней')})" if days
                else "купить билет на офлайн-мероприятие TwitchCon")
    if t == "bits":
        amount = step.get("bits_amount")
        return f"потратить {amount} Bits" if amount and amount > 1 else "потратить Bits"
    if t == "clip":
        return "создать клип"
    if t == "turbo":
        return "оформить подписку Turbo"
    return None


PAID_STEP_TYPES = {"subscription", "subscription_gift", "bits", "twitchcon", "turbo"}


def cost_from_steps(steps, fallback):
    """Стоимость по steps: нужен ли хоть на одном этапе платёж.

    Точнее списка costs у SD: там перечислены стоимости ВСЕЙ кампании, поэтому у
    Bulbasaur выходило «free, paid» (в наборе есть и бесплатный Pichu), хотя сам
    значок требует подписку. Строка «free, paid» не совпадает ни с одной меткой,
    и пилюля цены просто не отображалась."""
    if not steps:
        return fallback
    types = {(st or {}).get("type") for stage in steps for st in (stage or [])}
    if not types:
        return fallback
    return "paid" if types & PAID_STEP_TYPES else "free"


def describe_steps_ru(steps):
    """Условие из структурного steps — самого точного, что даёт StreamDatabase.

    Формат: внешний список — ЭТАПЫ (нужно выполнить все, по порядку), внутренний —
    АЛЬТЕРНАТИВЫ внутри этапа. Так у Bulbasaur лежит
    [[подписка, гифт], [смотреть 20 мин × 3 дня]] = «оформить или подарить подписку,
    затем смотреть...». Плоские поля availability этой структуры не передают: из них
    выходило просто «смотреть эфир 20 минут», без «в 3 разных дня» и без порядка.

    Незнакомый тип шага делает ВЕСЬ разбор недействительным (None), чтобы не
    выдать читателю половину условия за целое — пусть лучше сработает фолбэк на
    describe_condition_ru."""
    if not steps:
        return None
    stages = []
    for stage in steps:
        alts = []
        for step in stage or []:
            text = _step_ru(step or {})
            if text is None:
                return None
            alts.append(text)
        if alts:
            stages.append(" или ".join(alts))
    if not stages:
        return None
    text = ", затем ".join(stages)
    return text[0].upper() + text[1:]


def av_objectives(av):
    """Структурное условие записи availability.

    SD переименовал поле: до сентября 2026 — steps, с 13.09 — objectives (форма
    та же: этапы × альтернативы). Проверка формата поймала пропажу steps сразу,
    но разбор уже тихо обеднел: у Pichu пропало «в 3 разных дня», у costumed
    Pikachu условие собиралось из плоских полей. Читаем оба имени."""
    av = av or {}
    return av.get("objectives") or av.get("steps")


def describe_condition_ru(av):
    """Строит русское описание условия из структурных полей StreamDatabase,
    а не переводом английского objective — так честнее для 'unknown'-полей.

    steps приоритетнее: там есть и порядок этапов, и «в N разных дней»."""
    from_steps = describe_steps_ru(av_objectives(av))
    if from_steps:
        return from_steps
    parts = []
    sub_parts = []
    if av.get("subscription"):
        sub_parts.append(f"оформить {_subs_ru(av.get('subscription_amount'))} "
                         f"{tier_ru(av.get('subscription_tier'))}")
    if av.get("subscription_gift"):
        sub_parts.append(f"подарить {_subs_ru(av.get('subscription_gift_amount'))} "
                         f"{tier_ru(av.get('subscription_gift_tier'))}")
    if sub_parts:
        parts.append(" или ".join(sub_parts))

    if av.get("watch"):
        mins = av.get("watch_minutes")
        text = f"смотреть эфир {ru_duration_minutes(mins)}" if mins else "смотреть эфир"
        # watch_days появился плоским полем 13.09.2026 (у записей без objectives):
        # без него у Pichu и стартеров терялось «в 3 разных дня».
        days = av.get("watch_days")
        if days and days > 1:
            text += f" в {days} {plural(days, 'разный день', 'разных дня', 'разных дней')}"
        parts.append(text)

    if av.get("twitchcon"):
        days = av.get("twitchcon_days")
        # Явно "офлайн-мероприятие" — иначе можно принять за обычный Twitch-дроп
        # за просмотр стрима (условие принципиально другое: физический билет).
        parts.append(f"купить билет на офлайн-мероприятие TwitchCon ({days} "
                      f"{plural(days, 'день', 'дня', 'дней')})" if days
                      else "купить билет на офлайн-мероприятие TwitchCon")
    if av.get("bits"):
        amount = av.get("bits_amount")
        # SD ставит bits_amount=1 в смысле «сколько угодно» (SUBtember) —
        # «потратить 1 Bits» звучит нелепо, число показываем от двух.
        parts.append(f"потратить {amount} Bits" if amount and amount > 1 else "потратить Bits")
    if av.get("clip") and not av.get("watch"):
        parts.append("создать клип")
    if av.get("turbo") and not sub_parts:
        parts.append("оформить подписку Turbo")

    if not parts:
        return None
    # Как действия сочетаются, SD теперь говорит полем operator. Раньше части
    # склеивались через «;», и «смотреть эфир 20 минут; оформить подписку» у
    # Bulbasaur читалось как два шага подряд — а по данным это АЛЬТЕРНАТИВЫ.
    joiner = " и " if av.get("operator") == "and" else " или "
    text = joiner.join(parts)
    # chance_denominator — значок выпадает не всегда: у стартеров покемонов
    # «шанс 1 из 3» (случайный из трёх). Без пометки читатель ждал бы гарантию.
    chance = av.get("chance_denominator")
    if chance and chance > 1:
        text += f" (шанс 1 из {chance})"
    return text[0].upper() + text[1:]


# Условие из разобранного описания страницы бейджа (когда структурных данных нет)
PAGE_KIND_RU = {
    "sub": "Оформить или подарить подписку",
    "purchase": "Купить билет",
    "bits": "Потратить Bits",
}


# Цена по типу условия со страницы SD. Bits — платно (раньше выходило
# «бесплатно»), незнакомый тип — цена неизвестна, а не «бесплатно».
PAGE_KIND_COST = {"sub": "paid", "purchase": "paid", "bits": "paid", "watch": "free"}


def _condition_from_content(raw):
    """Условие получения из текста события — только по ОДНОЗНАЧНЫМ словам-действиям.
    Это те же примитивы, что describe_condition_ru строит из структурных полей;
    здесь их источник — прямой текст SD, а не догадка. Нет явного слова — None
    (тогда подпись честно скажет «Условия уточняются», не выдумывая)."""
    parts = []
    if re.search(r"subscri|gift", raw):
        parts.append("оформить или подарить подписку")
    if re.search(r"\bwatch", raw):
        parts.append("смотреть трансляцию")
    if re.search(r"bits|cheer", raw):
        parts.append("потратить Bits")
    # Клип-задания: «Unlock the badge by downloading and sharing an epic mid-set
    # moment from your stream to social» (Clipped That, 01.09.2026). Действие
    # однозначное, но прежние слова его не покрывали — условие выходило пустым,
    # и раз в 6 часов уходила тревога о значке, с которым всё в порядке.
    if re.search(r"\bclip\b|\bclips\b|moment from your stream", raw):
        if re.search(r"shar|post|social", raw):
            parts.append("создать клип и поделиться им")
        else:
            parts.append("создать клип")
    text = " или ".join(parts)
    return text[0].upper() + text[1:] if text else None


def condition_from_helix(desc):
    """Условие из описания значка в Twitch Helix.

    Twitch пишет их шаблонно, поэтому разбираем те же примитивы, что и
    describe_condition_ru: «watching ... for 60 minutes», «subscribing, gifting,
    or using Bits». Если шаблон не узнан — None, а НЕ сырой английский текст:
    строка вида «This badge was earned during the X campaign» условия не несёт,
    и подставлять её вместо условия — врать читателю (пусть лучше честное
    «условия уточняются»)."""
    if not desc:
        return None
    raw = desc.lower()
    parts = []
    if re.search(r"subscrib|gift", raw):
        parts.append("оформить или подарить подписку")
    m = re.search(r"watch\w*\b[^.]*?(\d+)\s*(minute|min|hour)", raw)
    if m:
        mins = int(m.group(1)) * (60 if m.group(2) == "hour" else 1)
        parts.append(f"смотреть эфир {ru_duration_minutes(mins)}")
    elif re.search(r"\bwatch", raw):
        parts.append("смотреть эфир")
    if re.search(r"bits|cheer", raw):
        parts.append("потратить Bits")
    text = " или ".join(parts)
    return text[0].upper() + text[1:] if text else None
