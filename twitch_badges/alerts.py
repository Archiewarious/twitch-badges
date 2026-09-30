"""Алерты владельцу: каталог, состояние в БД, отправка (D7, C10).

Сбор и бот только поднимают и снимают тревоги (raise_alert / clear_alert) —
таблица alerts служит общей очередью. Отправляет бот (AlertSender):
  · новая тревога — сразу;
  · всё ещё активна — напоминание раз в 6 часов;
  · снята после того, как о ней сообщили, — «✅ восстановлено».
Токен — отдельный ALERT_BOT_TOKEN (Q8): тревога дойдёт, даже если основной
бот отозван или упал. Без него — основным токеном, как раньше.

Каждый текст отвечает на четыре вопроса: что случилось, как это влияет на
канал, чья это сторона (источник / Telegram / мы) и что делать.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import timedelta

from . import db

log = logging.getLogger(__name__)

REPEAT = timedelta(hours=6)


@dataclass(frozen=True)
class Kind:
    impact: str
    side: str
    action: str


CATALOG = {
    "collector-failing": Kind("новые значки и изменения не попадают в канал",
                              "обычно источник (StreamDatabase), иногда мы",
                              "если SD лежит — ждать; иначе смотреть journalctl -u tb-collector"),
    "format-drift": Kind("часть значков может не попасть в канал или выйти без дат/условия",
                         "источник сменил формат", "нужна правка кода сбора"),
    "post-failed": Kind("этот пост не уйдёт, пока ошибку не исправят",
                        "мы (ошибка бота)", "исправить и выкатить; бот повторит пост один раз сам"),
    "channel-forbidden": Kind("посты в канал не уходят", "права в Telegram",
                              "вернуть боту права администратора канала — посты догонят сами"),
    "post-unknown": Kind("возможен дубль или пропуск одного поста", "Telegram (потерян ответ)",
                         "проверить канал и ответить боту /sent <id> или /resend <id>"),
    "telegram-unreachable": Kind("посты и inline не работают", "Telegram или сеть сервера",
                                 "обычно ждать; бот сам догонит"),
    "posting-paused-stale": Kind("анонсы не уходят, пока данные не обновятся",
                                 "как у collector-failing", "смотреть collector-failing"),
    "burst": Kind("очередь постов растёт: по одной группе раз в 2 минуты",
                  "источник (массовое изменение)", "если это мусор — /pause в личке бота"),
    "anomalies": Kind("значок выдаётся, а в посте нечего сказать «как получить»",
                      "источник (пробел данных)", "ничего; при желании — manual/overrides.json"),
    "blindspots": Kind("значок есть на Twitch, но в канал не идёт",
                       "источник (нет дат)", "ничего; если это нормально — добавить в ignore"),
    "helix-auth": Kind("описания и арт Twitch не обновляются", "ключи приложения Twitch",
                       "проверить TWITCH_CLIENT_ID/SECRET на dev.twitch.tv"),
    "overrides-invalid": Kind("ручные данные для этих значков не применяются",
                              "мы (ошибка в файле)", "исправить manual/overrides.json"),
    "db-integrity": Kind("постинг остановлен", "мы (база данных)",
                         "восстановить из бэкапа: README, раздел «Восстановление»"),
}
DEFAULT_KIND = Kind("—", "—", "—")


def raise_alert(conn, key, subject, body="", now=None):
    """Поднять тревогу (идемпотентно: текст обновляется, first_at — нет)."""
    now_s = db.ts(now or db.utcnow())
    conn.execute(
        "INSERT INTO alerts(key, active, subject, body, first_at) VALUES(?, 1, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET subject=excluded.subject, body=excluded.body, "
        "first_at=CASE WHEN alerts.active=1 THEN alerts.first_at ELSE excluded.first_at END, "
        "last_sent_at=CASE WHEN alerts.active=1 THEN alerts.last_sent_at ELSE NULL END, "
        "active=1, cleared_at=NULL", (key, subject, body, now_s))


def clear_alert(conn, key, note="", now=None):
    """Снять тревогу. Бот отправит «восстановлено», если о ней сообщал."""
    conn.execute("UPDATE alerts SET active=0, cleared_at=?, body=CASE WHEN ?='' THEN body ELSE ? END "
                 "WHERE key=? AND active=1", (db.ts(now or db.utcnow()), note, note, key))


def active(conn) -> dict:
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM alerts WHERE active=1")}


def apply_signal(conn, sig, now=None):
    """planner.AlertSignal → raise/clear (в своей транзакции)."""
    with db.tx(conn):
        if sig.active:
            raise_alert(conn, sig.key, sig.subject, sig.body, now)
        else:
            clear_alert(conn, sig.key, sig.subject, now)


def render(key, subject, body, *, repeat=False, since=None) -> str:
    k = CATALOG.get(key, DEFAULT_KIND)
    head = "🔁 Всё ещё: " if repeat else "🔴 "
    lines = [f"{head}<b>{html.escape(subject)}</b>"]
    if body:
        lines += ["", html.escape(body)]
    lines += ["", f"<b>Влияние на канал:</b> {html.escape(k.impact)}",
              f"<b>Чья сторона:</b> {html.escape(k.side)}",
              f"<b>Что делать:</b> {html.escape(k.action)}"]
    if since:
        lines.append(f"<i>С {since}</i>")
    lines.append(f"<code>{html.escape(key)}</code>")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def render_recovery(key, note) -> str:
    return f"✅ <b>Восстановлено</b>: {html.escape(note or key)}\n<code>{html.escape(key)}</code>"


class AlertSender:
    """send(text) — корутина отправки владельцу (отдельным ботом, если он есть)."""

    def __init__(self, conn, send, clock=db.utcnow):
        self.conn, self.send, self.clock = conn, send, clock

    async def flush(self) -> int:
        now = self.clock()
        sent = 0
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM alerts ORDER BY key")]
        for r in rows:
            if r["active"]:
                last = db.parse_ts(r["last_sent_at"])
                if last and now - last < REPEAT:
                    continue
                text = render(r["key"], r["subject"] or r["key"], r["body"] or "",
                              repeat=last is not None, since=r["first_at"])
            elif r["last_sent_at"]:
                text = render_recovery(r["key"], r["body"])
            else:
                with db.tx(self.conn):          # снята раньше, чем о ней сообщили
                    self.conn.execute("DELETE FROM alerts WHERE key=? AND active=0", (r["key"],))
                continue
            try:
                await self.send(text)
            except Exception as e:  # noqa: BLE001 — сеть: попробуем на следующем проходе
                log.warning("алерт %s не отправлен: %s", r["key"], e)
                return sent
            sent += 1
            with db.tx(self.conn):
                if r["active"]:
                    self.conn.execute("UPDATE alerts SET last_sent_at=? WHERE key=?",
                                      (db.ts(now), r["key"]))
                else:
                    self.conn.execute("DELETE FROM alerts WHERE key=? AND active=0", (r["key"],))
        return sent
