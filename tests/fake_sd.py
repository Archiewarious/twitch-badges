"""Заглушка StreamDatabase + Twitch (Helix, GQL, CDN) на httpx.MockTransport."""
from __future__ import annotations

import copy
import io
import json
import re

import httpx
from PIL import Image

from twitch_badges.sources.http import Http


def _png():
    buf = io.BytesIO()
    Image.new("RGBA", (72, 72), (120, 60, 200, 255)).save(buf, "PNG")
    return buf.getvalue()


PNG = _png()


class FakeSD:
    def __init__(self, snapshot):
        self.badges = copy.deepcopy(snapshot["badges"])
        self.events = copy.deepcopy(snapshot["events"])
        self.helix = copy.deepcopy(snapshot.get("helix") or {})
        self.page_avail = copy.deepcopy(snapshot.get("page_availability") or {})
        self.pages: dict[str, dict] = {}          # set_id -> объект значка страницы
        # inline — contexts/availability внутри twitchGlobalBadge (до 02.10.2026);
        # split — рядом с ним в pageProps (contexts/availabilities); split-stale — то же,
        # но внутри остался пустой старый availability: [] (FFXIV); bare — нигде.
        self.page_layout = "inline"
        self.build_id = "build-1"
        self.token = "tok-1"
        self.helix_401 = 0                          # сколько раз ответить 401
        self.oauth_fail = False
        self.faults: list[tuple[str, int]] = []     # (регэксп пути, статус) — по одному
        self.log: list[str] = []
        self.image_ctype = "image/png"

    # ── управление ──
    def fail(self, pattern, status, times=1):
        self.faults += [(pattern, status)] * times

    def http(self, **kw) -> Http:
        return Http(httpx.Client(transport=httpx.MockTransport(self.handle)),
                    sleep=lambda s: None, **kw)

    # ── сервер ──
    def _json(self, data, status=200):
        return httpx.Response(status, json=data)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        self.log.append(f"{request.method} {url.host}{path}")
        for i, (pat, status) in enumerate(self.faults):
            if re.search(pat, f"{url.host}{path}"):
                del self.faults[i]
                return httpx.Response(status, text="fault")
        if url.host == "www.streamdatabase.com":
            return self._sd(path)
        if url.host == "id.twitch.tv":
            if self.oauth_fail:
                return self._json({"status": 403, "message": "invalid client secret"}, 403)
            self.token = f"tok-{len(self.log)}"
            return self._json({"access_token": self.token, "expires_in": 5000000})
        if url.host == "api.twitch.tv":
            auth = request.headers.get("authorization", "")
            if self.helix_401 or auth != f"Bearer {self.token}":
                self.helix_401 = max(0, self.helix_401 - 1)
                return self._json({"error": "Unauthorized", "status": 401}, 401)
            return self._json({"data": [
                {"set_id": sid, "versions": [{"id": "1", **info}]} for sid, info in self.helix.items()]})
        if url.host == "gql.twitch.tv":
            q = json.loads(request.content)
            data = {}
            for alias, name in re.findall(r'(g\d+):game\(name:("(?:[^"\\]|\\.)*")\)', q.get("query", "")):
                n = json.loads(name)
                data[alias] = {"name": n, "slug": re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")}
            if "searchCategories" in q.get("query", ""):
                data = {"searchCategories": {"edges": []}}
            return self._json({"data": data})
        if url.host == "static-cdn.jtvnw.net":
            return httpx.Response(200, content=PNG, headers={"content-type": self.image_ctype})
        return httpx.Response(404, text="unknown host")

    def _sd(self, path):
        if path == "/":
            return httpx.Response(200, text=f'<script>{{"buildId":"{self.build_id}"}}</script>')
        m = re.match(r"^/_next/data/([^/]+)/(.+)\.json$", path)
        if not m:
            return httpx.Response(404)
        bid, page = m.groups()
        if bid != self.build_id:
            return httpx.Response(404, text="old build")
        if page == "twitch/global-badges":
            return self._json({"pageProps": {"data": [{"twitchGlobalBadge": b} for b in self.badges]}})
        if page == "events":
            return self._json({"pageProps": {"initialEvents": self.events}})
        m = re.match(r"^twitch/global-badges/([^/]+)/1$", page)
        if m:
            sid = m.group(1)
            badge = dict(self.pages.get(sid) or {"availability": self.page_avail.get(sid, []),
                                                  "contexts": []})
            if self.page_layout == "inline":
                return self._json({"pageProps": {"twitchGlobalBadge": badge}})
            ctx, avs = badge.pop("contexts", []), badge.pop("availability", [])
            if self.page_layout == "split-stale":
                badge["availability"] = []
            pp = {"twitchGlobalBadge": badge}
            if self.page_layout.startswith("split"):
                pp.update(contexts=ctx, availabilities=avs)
            return self._json({"pageProps": pp})
        return httpx.Response(404)
