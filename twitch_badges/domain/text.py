"""Русские тексты и нормализация строк."""
import html
import re
import unicodedata

RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def plural(n, one, few, many):
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def ru_duration_minutes(total_minutes):
    h, m = divmod(int(total_minutes), 60)
    parts = []
    if h:
        parts.append(f"{h} {plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{m} {plural(m, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts) or "0 минут"


def tier_ru(n):
    return f"{n} уровня" if n else "неизвестного уровня"


def _subs_ru(amount):
    """«подписку» / «2 подписки» — с правильным склонением. Раньше выходило
    «оформить 2 подписку»: количество подставлялось, а слово не склонялось."""
    if not amount or amount <= 1:
        return "подписку"
    return f"{amount} {plural(amount, 'подписку', 'подписки', 'подписок')}"


def esc(s):
    return html.escape(str(s), quote=True)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_alnum(s):
    """Схлопнуть строку в lowercase-алфавитно-цифровую: 'Spider-Man' → 'spiderman'."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(s or "").lower())


def _content_ngrams(text, n=4):
    """Множество склеенных подпоследовательностей слов текста (до n подряд).
    Матч имени бейджа делаем по этому множеству, а НЕ голой подстрокой: иначе
    имя цеплялось бы через границы слов — 'Indiana Jones' → 'indianajones'
    содержит 'diana', и бот опубликовал бы ложный анонс в канал."""
    words = re.findall(r"[a-z0-9]+", strip_accents(text or "").lower())
    grams = set()
    for i in range(len(words)):
        joined = ""
        for j in range(i, min(i + n, len(words))):
            joined += words[j]
            grams.add(joined)
    return grams
