"""Время StreamDatabase (UTC) и конец окна."""
import re
from datetime import datetime, timezone

SD_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?$")


def parse_dt(date_s, time_s):
    """Дата и время SD в UTC. Время бывает HH:MM (события, availability),
    HH:MM:SS.mmm и HH:MM:SS (каталог с ~27.09.2026), пусто — полночь.
    Незнакомый вид — None: лучше «даты нет», чем выдуманная."""
    if not date_s:
        return None
    m = SD_TIME_RE.match((time_s or "00:00").strip())
    if not m:
        return None
    try:
        d = datetime.strptime(date_s, "%Y-%m-%d")
        return d.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                         second=int(m.group(3) or 0), tzinfo=timezone.utc)
    except ValueError:
        return None


def effective_end(w):
    """Конец окна с поправкой на грубые даты. У dates_coarse день указан без часа,
    parse_dt подставляет 00:00 — но «до 26 июля» значит конец ТОГО дня, а не его
    начало. Без поправки бейдж считался бы завершённым на сутки раньше, теряя весь
    последний день. Единый источник правды: тем же пользуется бот в publish_new."""
    e = w.get("end")
    if e and w.get("dates_coarse"):
        return e.replace(hour=23, minute=59, second=59)
    return e
