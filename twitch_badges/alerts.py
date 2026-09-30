"""Алерты: состояние в БД (таблица alerts) — общая очередь для сбора и бота.

Сбор и бот только поднимают/снимают тревоги; отправкой (дедуп, повтор раз в
6 ч, сообщение о восстановлении, отдельный токен) занимается бот (C10)."""
from __future__ import annotations

from . import db


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
