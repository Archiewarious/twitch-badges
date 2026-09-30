"""sd_notify без зависимостей: READY=1, WATCHDOG=1, STATUS=… (Type=notify)."""
from __future__ import annotations

import os
import socket


def notify(state: str) -> bool:
    """Отправить состояние systemd. Вне systemd (нет NOTIFY_SOCKET) — ничего."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError:
        return False


def watchdog_interval() -> float | None:
    """Половина WatchdogSec (как советует systemd), None — watchdog выключен."""
    usec = os.environ.get("WATCHDOG_USEC")
    try:
        return int(usec) / 2e6 if usec else None
    except ValueError:
        return None
