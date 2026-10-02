"""StreamDatabase (Next.js _next/data): buildId, каталог, события, страницы значков."""
from __future__ import annotations

import logging
import random
import re

from .http import Http, HttpError
from .sd_parse import find_badge_list

log = logging.getLogger(__name__)


class SourceFormatError(RuntimeError):
    """Ответ SD не той формы: нет buildId, pageProps, пустой каталог…"""


class StreamDB:
    def __init__(self, http: Http, base_url="https://www.streamdatabase.com",
                 page_pause=(0.3, 0.5), sleep=None):
        self.http, self.base = http, base_url.rstrip("/")
        self.page_pause, self.sleep = page_pause, sleep or http.sleep
        self.build_id = None
        self.page_errors: list[str] = []

    def fetch_build_id(self) -> str:
        text = self.http.get(self.base + "/").text
        m = re.search(r'"buildId":"([^"]+)"', text)
        if not m:
            raise SourceFormatError("нет buildId на главной StreamDatabase — сменилась вёрстка")
        self.build_id = m.group(1)
        return self.build_id

    def next_data(self, path: str) -> dict:
        """Данные страницы. На 404 — один повтор со свежим buildId (SD выкатился)."""
        if not self.build_id:
            self.fetch_build_id()
        for attempt in (0, 1):
            resp = self.http.get(f"{self.base}/_next/data/{self.build_id}/{path}.json", ok_404=True)
            if resp.status_code != 404:
                try:
                    return resp.json()
                except ValueError as e:
                    raise SourceFormatError(f"{path}: не JSON ({e})") from e
            if attempt == 0:
                old = self.build_id
                self.fetch_build_id()
                log.info("buildId устарел (%s → %s)", old, self.build_id)
        raise HttpError(f"{path}: 404 и со свежим buildId", 404)

    def catalog(self) -> list:
        data = self.next_data("twitch/global-badges")
        if "pageProps" not in data:
            raise SourceFormatError("нет pageProps в ответе каталога")
        badges = find_badge_list(data["pageProps"]) or []
        if not badges:
            raise SourceFormatError("пустой список значков — сменилась вёрстка каталога")
        return badges

    def events(self) -> list:
        pp = self.next_data("events").get("pageProps") or {}
        events = pp.get("initialEvents") or pp.get("initialData") or []
        if not events:
            raise SourceFormatError("пустой список событий — сменилась структура")
        return events

    def badge_page(self, set_id: str):
        """Объект значка со страницы или None (ошибка запоминается в page_errors:
        раньше их глотали, и данные молча беднели)."""
        if self.page_pause:
            self.sleep(random.uniform(*self.page_pause))
        try:
            data = self.next_data(f"twitch/global-badges/{set_id}/1")
        except (HttpError, SourceFormatError) as e:
            self.page_errors.append(f"{set_id}: {e}")
            return None
        pp = data.get("pageProps") or {}
        badge = dict(pp.get("twitchGlobalBadge") or {})
        # 02.10.2026 SD вынес описание и availability из значка на уровень страницы
        # (pageProps.contexts / pageProps.availabilities): читаем оба места. Непустой
        # список страницы главнее: у части значков внутри остался пустой старый
        # availability: [] (FFXIV Fan Festival), а данные уже снаружи.
        for key, page_key in (("contexts", "contexts"), ("availability", "availabilities")):
            if pp.get(page_key) or (key not in badge and page_key in pp):
                badge[key] = pp[page_key]
        return badge
