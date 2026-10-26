"""Каналы на разных языках (27.10.2026): русский — исходный, английский (США) и
испанский (Испания + Латинская Америка) — дубли с теми же решениями планировщика,
но своим текстом, часовым поясом и тихими часами.

Locale — всё, что отличает канал: язык подписей, пояс дат, ссылки в подвале,
префикс id кампаний в БД (у русского его нет: его данные не трогаем).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Locale:
    ch: str                     # ключ канала: ru / en / es
    lang: str
    tz: str                     # IANA-пояс для дат в постах и тихих часов
    tz_label: str               # подпись пояса после времени
    channel_handle: str
    bot_in_footer: bool         # бот отвечает только по-русски — в чужие каналы не зовём
    months: tuple
    card_pill: dict             # {"free": текст, "paid": текст} на карточке

    @property
    def channel_url(self) -> str:
        return f"https://t.me/{self.channel_handle.lstrip('@')}"

    @property
    def prefix(self) -> str:
        """Префикс id кампаний этого канала в БД ("" у русского)."""
        return "" if self.ch == "ru" else f"{self.ch}:"

    def local(self, dt):
        if self.tz == "Europe/Moscow":
            # Как раньше: МСК = UTC+3 без перевода часов (побайтно прежние подписи).
            return dt.astimezone(timezone(timedelta(hours=3)))
        return dt.astimezone(ZoneInfo(self.tz))


RU = Locale("ru", "ru", "Europe/Moscow", "МСК", "@TwitchInfoRadar", True,
            ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"),
            {"free": "БЕСПЛАТНО", "paid": "ПЛАТНО"})

EN = Locale("en", "en", "America/New_York", "ET", "@TwitchDropsUSA", False,
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
            {"free": "FREE", "paid": "PAID"})

ES = Locale("es", "es", "America/Mexico_City", "hora CDMX", "@TwitchDropsES", False,
            ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre"),
            {"free": "GRATIS", "paid": "DE PAGO"})

LOCALES = {loc.ch: loc for loc in (RU, EN, ES)}
