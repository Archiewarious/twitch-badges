"""Фейковый Telegram Bot API: подмена сетевого слоя PTB (BaseRequest).

Настоящий telegram.Bot ходит сюда вместо api.telegram.org, поэтому PTB сам
превращает ответы в свои исключения (RetryAfter, BadRequest, Forbidden…), как в
жизни. Сбои задаются очередью:

    tg = FakeTelegram(); bot = tg.bot()
    tg.fail("sendPhoto", "lost_response")      # доставить, но ответ потерять

Виды сбоев:
  lost_response — запрос выполнен, клиент получил ReadTimeout (TimedOut ← httpx.ReadTimeout)
  connect_error — запрос не ушёл (NetworkError ← httpx.ConnectError)
  pool_timeout  — запрос не ушёл (TimedOut ← httpx.PoolTimeout)
  429:N         — RetryAfter(N), ничего не выполнено
  400:текст     — BadRequest
  403           — Forbidden
  502_after     — выполнено, но ответ 502 (NetworkError без причины httpx)
"""
from __future__ import annotations

import html
import itertools
import json
import re
import time
from dataclasses import dataclass, field

import httpx
from telegram import Bot
from telegram.error import NetworkError, TimedOut
from telegram.request import BaseRequest

TOKEN = "123456:TEST-fake-token-not-real"
_file_ids = itertools.count(1)


def plain(caption_html: str | None) -> str | None:
    """Как Telegram хранит подпись: без тегов, с раскрытыми сущностями."""
    if caption_html is None:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", caption_html))


@dataclass
class Chat:
    id: int
    type: str = "channel"
    title: str = "chat"
    protected: bool = False
    bot_is_admin: bool = True
    messages: dict = field(default_factory=dict)
    next_id: int = 100


class FakeTelegram:
    def __init__(self, channel_id=-1001, storage_id=-1002, owner_id=42):
        self.channel_id, self.storage_id, self.owner_id = channel_id, storage_id, owner_id
        self.chats = {channel_id: Chat(channel_id, title="channel"),
                      storage_id: Chat(storage_id, title="storage"),
                      owner_id: Chat(owner_id, type="private", title="owner")}
        self.faults: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict]] = []
        self.now = time.time

    # ── управление ──
    def fail(self, method: str, fault: str, times: int = 1):
        self.faults += [(method, fault)] * times

    def bot(self, **kw) -> Bot:
        return Bot(TOKEN, request=FakeRequest(self), get_updates_request=FakeRequest(self), **kw)

    def channel_posts(self):
        return [m for _, m in sorted(self.chats[self.channel_id].messages.items())]

    def posts_count(self):
        """Постов в канале (альбом — один пост)."""
        seen, n = set(), 0
        for m in self.channel_posts():
            g = m.get("media_group_id")
            if g and g in seen:
                continue
            seen.add(g)
            n += 1
        return n

    # ── сервер ──
    def _chat(self, chat_id):
        chat = self.chats.get(int(chat_id))
        if chat is None:
            raise _ApiError(400, "Bad Request: chat not found")
        if chat.type == "channel" and not chat.bot_is_admin:
            raise _ApiError(403, "Forbidden: bot is not a member of the channel chat")
        return chat

    def _new_message(self, chat, **extra):
        mid = chat.next_id
        chat.next_id += 1
        msg = {"message_id": mid, "date": int(self.now()),
               "chat": {"id": chat.id, "type": chat.type, "title": chat.title}, **extra}
        chat.messages[mid] = msg
        return msg

    def _photo(self):
        n = next(_file_ids)
        return [{"file_id": f"FILE{n}", "file_unique_id": f"U{n}", "width": 800, "height": 450}]

    def handle(self, method, p):
        if method == "getMe":
            return {"id": 123456, "is_bot": True, "first_name": "Test", "username": "TestBot",
                    "can_join_groups": True, "can_read_all_group_messages": False,
                    "supports_inline_queries": True}
        if method == "getChat":
            chat = self._chat(p["chat_id"])
            return {"id": chat.id, "type": chat.type, "title": chat.title,
                    "has_protected_content": chat.protected, "accent_color_id": 0,
                    "max_reaction_count": 11, "accepted_gift_types": {
                        "unlimited_gifts": False, "limited_gifts": False,
                        "unique_gifts": False, "premium_subscription": False}}
        if method == "sendMessage":
            chat = self._chat(p["chat_id"])
            return self._new_message(chat, text=plain(p.get("text")))
        if method in ("sendPhoto", "sendDocument"):
            chat = self._chat(p["chat_id"])
            cap = p.get("caption")
            if cap is not None and len(plain(cap).encode("utf-16-le")) // 2 > 1024:
                raise _ApiError(400, "Bad Request: message caption is too long")
            key = "photo" if method == "sendPhoto" else "document"
            val = self._photo() if key == "photo" else {"file_id": f"DOC{next(_file_ids)}",
                                                        "file_unique_id": "D"}
            return self._new_message(chat, caption=plain(cap), **{key: val},
                                     _media=[p.get("photo") or p.get("document")])
        if method == "sendMediaGroup":
            chat = self._chat(p["chat_id"])
            media = p["media"] if isinstance(p["media"], list) else json.loads(p["media"])
            gid = f"g{next(_file_ids)}"
            out = []
            for m in media:
                cap = m.get("caption")
                if cap is not None and len(plain(cap)) > 1024:
                    raise _ApiError(400, "Bad Request: message caption is too long")
                out.append(self._new_message(chat, caption=plain(cap), photo=self._photo(),
                                             media_group_id=gid, _media=[m.get("media")]))
            return out
        if method == "forwardMessage":
            src = self._chat(p["from_chat_id"])
            dst = self._chat(p["chat_id"])
            if src.protected:
                raise _ApiError(400, "Bad Request: message has protected content and can't be forwarded")
            orig = src.messages.get(int(p["message_id"]))
            if orig is None:
                raise _ApiError(400, "Bad Request: message to forward not found")
            fwd = {k: v for k, v in orig.items() if k not in ("message_id", "date", "chat", "_media")}
            fwd["forward_origin"] = {"type": "channel", "date": orig["date"],
                                     "chat": {"id": src.id, "type": "channel", "title": src.title},
                                     "message_id": orig["message_id"]}
            return self._new_message(dst, **fwd)
        if method == "deleteMessage":
            chat = self._chat(p["chat_id"])
            if chat.messages.pop(int(p["message_id"]), None) is None:
                raise _ApiError(400, "Bad Request: message to delete not found")
            return True
        raise _ApiError(404, f"Not Found: method {method}")


class _ApiError(Exception):
    def __init__(self, code, description, retry_after=None):
        self.code, self.description, self.retry_after = code, description, retry_after


class FakeRequest(BaseRequest):
    def __init__(self, server: FakeTelegram):
        self.server = server

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        srv = self.server
        srv.calls.append((api, params))
        fault = None
        for i, (m, f) in enumerate(srv.faults):
            if m == api:
                fault = f
                del srv.faults[i]
                break
        if fault == "connect_error":
            raise NetworkError("httpx.ConnectError: fake") from httpx.ConnectError("fake")
        if fault == "pool_timeout":
            raise TimedOut("Pool timeout") from httpx.PoolTimeout("fake")
        if fault and fault.startswith("429"):
            sec = int(fault.split(":")[1]) if ":" in fault else 3
            return 429, json.dumps({"ok": False, "error_code": 429,
                                    "description": f"Too Many Requests: retry after {sec}",
                                    "parameters": {"retry_after": sec}}).encode()
        if fault and fault.startswith("400"):
            return 400, json.dumps({"ok": False, "error_code": 400,
                                    "description": fault.partition(":")[2] or "Bad Request"}).encode()
        if fault == "403":
            return 403, json.dumps({"ok": False, "error_code": 403,
                                    "description": "Forbidden: bot was kicked"}).encode()
        try:
            result = srv.handle(api, params)
        except _ApiError as e:
            return e.code, json.dumps({"ok": False, "error_code": e.code,
                                       "description": e.description}).encode()
        if fault == "lost_response":
            raise TimedOut() from httpx.ReadTimeout("fake")
        if fault == "502_after":
            return 502, b'{"ok": false, "error_code": 502, "description": "Bad Gateway"}'

        def clean(x):
            if isinstance(x, dict):
                return {k: clean(v) for k, v in x.items() if not k.startswith("_")}
            if isinstance(x, list):
                return [clean(v) for v in x]
            return x
        return 200, json.dumps({"ok": True, "result": clean(result)}).encode()
