"""Блокировка экземпляра: второй бот/сбор на тех же данных не стартует (A6)."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """with InstanceLock(data_dir / "bot.lock"): ...  — без ожидания: занято → AlreadyRunning."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(f"{self.path}: уже запущен другой экземпляр") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return self

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
