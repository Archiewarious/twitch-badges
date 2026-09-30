"""Команды владельца в личке бота (только ALERT_CHAT_ID)."""
from __future__ import annotations

from . import store
from .publisher.service import paused, set_paused

HELP = ("Команды:\n/status — состояние\n/pause — остановить посты\n/resume — возобновить\n"
        "/sent <id> — пост #id в канале есть\n/resend <id> — поста #id нет, отправить заново")


def handle(conn, outbox, text: str, now) -> str:
    parts = (text or "").strip().split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else None
    if cmd == "/pause":
        set_paused(conn, True, now)
        return "⏸ Посты остановлены. /resume — возобновить."
    if cmd == "/resume":
        set_paused(conn, False, now)
        return "▶️ Посты возобновлены."
    if cmd in ("/sent", "/resend"):
        if not (arg and arg.lstrip("#").isdigit()):
            return f"Нужен номер поста: {cmd} 12"
        rid = int(arg.lstrip("#"))
        if not conn.execute("SELECT 1 FROM outbox WHERE id=?", (rid,)).fetchone():
            return f"Поста #{rid} нет"
        return outbox.mark_sent(rid) if cmd == "/sent" else outbox.resend(rid)
    if cmd == "/status":
        return status_text(conn, now)
    return HELP


def status_text(conn, now) -> str:
    snap = store.current_snapshot(conn)
    lines = [f"Посты: {'⏸ на паузе' if paused(conn) else '▶️ идут'}"]
    if snap:
        age = (now - snap.committed).total_seconds() / 3600
        lines.append(f"Данные: снапшот {snap.committed_at}, {age:.1f} ч назад")
    else:
        lines.append("Данные: снапшотов нет")
    rows = conn.execute("SELECT status, count(*) FROM outbox GROUP BY status").fetchall()
    lines.append("Outbox: " + (", ".join(f"{s} {n}" for s, n in rows) or "пусто"))
    stuck = conn.execute("SELECT id, kind, status, last_error FROM outbox WHERE status IN "
                         "('unknown','failed','retry') ORDER BY id DESC LIMIT 5").fetchall()
    for r in stuck:
        lines.append(f"  #{r[0]} {r[1]} {r[2]}: {(r[3] or '')[:80]}")
    last = conn.execute("SELECT id, kind, sent_at FROM outbox WHERE status='sent' "
                        "ORDER BY sent_at DESC LIMIT 1").fetchone()
    if last:
        lines.append(f"Последний пост: #{last[0]} {last[1]} {last[2]}")
    active = [r[0] for r in conn.execute("SELECT key FROM alerts WHERE active=1 ORDER BY key")]
    lines.append("Тревоги: " + (", ".join(active) or "нет"))
    lines.append(f"Кампаний в памяти: {conn.execute('SELECT count(*) FROM campaigns').fetchone()[0]}")
    return "\n".join(lines)
