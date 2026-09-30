"""Ссылки на категории Twitch через публичный GQL (тот же, что у twitch.tv).

Кэш — kv(category_urls): найдено → {"url", "name", "at"}; «нет такой» →
{"missing": true, "at", "tries"} и повтор через 7 дней (раньше — 4 попытки и
навсегда «нет»). Сбой сети кэш не портит.

GQL — неофициальный API на публичном web Client-Id (F11): если Twitch его
закроет, ссылки на новые категории перестанут находиться — это ловит проверка
формата (доля «категория без ссылки»), а старые останутся в кэше."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta

from .http import Http, HttpError

log = logging.getLogger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
DIRECTORY = "https://www.twitch.tv/directory/category/"
UNKNOWN_GAME_RE = re.compile(r"Unknown Game \(ID: (\d+)\)")
BATCH = 25
NEGATIVE_TTL = timedelta(days=7)


def _words(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return set(re.findall(r"[a-z0-9]+", re.sub(r"[™®©'’]", "", s)))


class Categories:
    def __init__(self, http: Http):
        self.http = http

    def _post(self, payload):
        resp = self.http.post(GQL_URL, headers={"Client-Id": GQL_CLIENT_ID}, json=payload)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError(f"GQL не JSON: {e}") from e

    def _search(self, name):
        try:
            data = self._post({"query": "query($q:String!){searchCategories(query:$q,first:3)"
                                        "{edges{node{name slug}}}}", "variables": {"q": name}})
        except HttpError:
            return None
        edges = (((data.get("data") or {}).get("searchCategories") or {}).get("edges") or [])
        want = _words(name)
        for e in edges:
            node = e.get("node") or {}
            got = _words(node.get("name"))
            small = min(want, got, key=len)
            if small and len(small) >= 2 and (want <= got or got <= want):
                return node
        return None

    def lookup(self, names) -> dict:
        """{имя: {"url","name"} | None}. Сеть → HttpError (это не ответ «нет»)."""
        parts = []
        for i, n in enumerate(names):
            m = UNKNOWN_GAME_RE.fullmatch(n)
            clean = re.sub(r"[™®©]", "", n)
            arg = f"id:{json.dumps(m.group(1))}" if m else f"name:{json.dumps(clean)}"
            parts.append(f"g{i}:game({arg}){{name slug}}")
        data = self._post({"query": "query{" + " ".join(parts) + "}"}).get("data")
        if not isinstance(data, dict):
            raise HttpError("GQL без data")
        out = {}
        for i, n in enumerate(names):
            g = data.get(f"g{i}")
            if not (g and g.get("slug")) and not UNKNOWN_GAME_RE.fullmatch(n):
                g = self._search(n)
            out[n] = ({"url": DIRECTORY + g["slug"], "name": g.get("name") or n}
                      if g and g.get("slug") else None)
        return out


def _due(entry, now) -> bool:
    """Спросить ли Twitch про это имя."""
    if isinstance(entry, dict) and entry.get("url"):
        return False
    if isinstance(entry, dict) and entry.get("missing"):
        try:
            return now - datetime.fromisoformat(entry["at"]) >= NEGATIVE_TTL
        except (KeyError, TypeError, ValueError):
            return True
    return True                      # нет записи или старый формат (число попыток)


def resolve(names, cache: dict, lookup, now):
    """(новый кэш, {имя: url}, {имя: имя у Twitch}, ошибка|None)."""
    cache = dict(cache or {})
    todo = [n for n in sorted(set(names)) if n and _due(cache.get(n), now)]
    error = None
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            found = lookup(batch)
        except HttpError as e:
            error = str(e)
            log.warning("GQL категорий недоступен (%s) — ссылки из кэша", e)
            break
        for n, v in found.items():
            if v:
                cache[n] = {**v, "at": now.isoformat()}
            else:
                prev = cache.get(n)
                tries = (prev.get("tries", 0) if isinstance(prev, dict) else
                         prev if isinstance(prev, int) else 0) + 1
                cache[n] = {"missing": True, "at": now.isoformat(), "tries": tries}
    good = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("url")}
    return cache, {k: v["url"] for k, v in good.items()}, \
        {k: v.get("name") or k for k, v in good.items()}, error
