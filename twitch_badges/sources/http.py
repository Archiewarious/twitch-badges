"""Общий HTTP-клиент источников: таймауты, ограниченные повторы, потолок
Retry-After, дедлайн прогона (C4).

Раньше запрос с ретраями мог длиться ~2 минуты, Retry-After не ограничивался,
а IPv4 форсировался подменой socket на весь процесс (F7). Здесь IPv4 — через
local_address транспорта, только для наших запросов."""
from __future__ import annotations

import logging
import random
import time

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; twitch-badges-tracker/2.0)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class DeadlineExceeded(RuntimeError):
    """Прогон сбора исчерпал своё время — выходим, не коммитя половину."""


class HttpError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self, client: httpx.Client | None = None, *, retries: int = 3,
                 retry_after_cap: float = 60, backoff: float = 2.0, deadline: float | None = None,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.client = client or httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.retries, self.retry_after_cap, self.backoff = retries, retry_after_cap, backoff
        self.sleep, self.monotonic = sleep, monotonic
        self.deadline_at = monotonic() + deadline if deadline else None
        self.requests = 0

    def left(self) -> float | None:
        return None if self.deadline_at is None else self.deadline_at - self.monotonic()

    def _check_deadline(self, need=0.0):
        left = self.left()
        if left is not None and left <= need:
            raise DeadlineExceeded("дедлайн прогона исчерпан")

    def request(self, method, url, *, ok_404=False, **kw) -> httpx.Response:
        """Ответ 2xx (или 404 при ok_404). Сеть и 429/5xx — повтор с backoff;
        исчерпали — HttpError. Другие 4xx — HttpError сразу."""
        last = None
        for attempt in range(self.retries + 1):
            self._check_deadline()
            self.requests += 1
            try:
                resp = self.client.request(method, url, **kw)
            except httpx.HTTPError as e:
                last = HttpError(f"{method} {url}: {e.__class__.__name__}: {e}")
                delay = self.backoff * 2 ** attempt
            else:
                if resp.status_code < 300 or (ok_404 and resp.status_code == 404):
                    return resp
                last = HttpError(f"{method} {url}: HTTP {resp.status_code}", resp.status_code)
                if resp.status_code not in RETRY_STATUSES:
                    raise last
                delay = self.backoff * 2 ** attempt
                ra = resp.headers.get("Retry-After")
                if ra and ra.strip().isdigit():
                    delay = float(ra)
                delay = min(delay, self.retry_after_cap)
            if attempt == self.retries:
                break
            delay *= random.uniform(0.9, 1.1)
            self._check_deadline(delay)
            log.info("повтор через %.1f с: %s", delay, last)
            self.sleep(delay)
        raise last

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def close(self):
        self.client.close()
