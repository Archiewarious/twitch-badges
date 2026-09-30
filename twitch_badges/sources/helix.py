"""Twitch Helix: описания и арт значков — второй источник рядом с SD.

Токен приложения хранится в БД (kv helix_token). На 401 — сброс токена и одна
попытка с новым (C5): раньше 401 или битый кэш выключали Helix до истечения
токена — до ~60 дней, молча."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .http import Http, HttpError

log = logging.getLogger(__name__)

OAUTH_URL = "https://id.twitch.tv/oauth2/token"
BADGES_URL = "https://api.twitch.tv/helix/chat/badges/global"


class HelixAuthError(RuntimeError):
    pass


def credentials_ok(client_id, secret) -> bool:
    return bool(client_id and secret and not client_id.startswith("your_")
                and not secret.startswith("your_"))


class Helix:
    def __init__(self, http: Http, client_id: str, secret: str, token_get, token_set, clock):
        self.http, self.client_id, self.secret = http, client_id, secret
        self.token_get, self.token_set, self.clock = token_get, token_set, clock

    def _new_token(self) -> str:
        resp = self.http.post(OAUTH_URL, params={"client_id": self.client_id,
                                                 "client_secret": self.secret,
                                                 "grant_type": "client_credentials"})
        p = resp.json()
        expires = self.clock() + timedelta(seconds=int(p["expires_in"]))
        self.token_set({"access_token": p["access_token"], "expires_at": expires.isoformat()})
        return p["access_token"]

    def _token(self) -> str:
        t = self.token_get() or {}
        try:
            ok = t.get("access_token") and datetime.fromisoformat(t["expires_at"]) > \
                self.clock() + timedelta(minutes=5)
        except (KeyError, ValueError, TypeError):
            ok = False
        return t["access_token"] if ok else self._new_token()

    def _badges(self, token):
        return self.http.get(BADGES_URL, headers={"Client-Id": self.client_id,
                                                  "Authorization": f"Bearer {token}"})

    def collect(self) -> dict:
        """{set_id: {title, description, click_url, image_url_4x}}. HelixAuthError
        — ключи не принимают даже после нового токена; HttpError — сеть."""
        try:
            resp = self._badges(self._token())
        except HttpError as e:
            if e.status not in (400, 401, 403):
                raise
            log.warning("Helix %s — сбрасываю токен и пробую заново", e.status)
            self.token_set(None)
            try:
                resp = self._badges(self._new_token())
            except HttpError as e2:
                if e2.status in (400, 401, 403):
                    raise HelixAuthError(str(e2)) from e2
                raise
        out = {}
        for badge_set in resp.json().get("data", []):
            versions = badge_set.get("versions") or []
            if not badge_set.get("set_id") or not versions:
                continue
            v = versions[0]
            info = {"title": (v.get("title") or "").strip(),
                    "description": (v.get("description") or "").strip(),
                    "click_url": (v.get("click_url") or "").strip(),
                    "image_url_4x": (v.get("image_url_4x") or "").strip()}
            if info["description"] or info["click_url"] or info["image_url_4x"]:
                out[badge_set["set_id"]] = info
        return out
