"""A stand-in for the Telegram Bot API, plugged in as an aiogram session.

The bot's own code runs unmodified; only the HTTP layer is replaced. It models
the parts of Telegram that matter under load:

* network latency per call, and aiohttp's 100-connection cap;
* flood control: a global send budget and a per-chat budget, answered with
  TelegramRetryAfter exactly as the real API does with HTTP 429;
* the validation Telegram does on every message (HTML parse errors, 4096-char
  cap, 64-byte callback data, "message is not modified", editing a message that
  does not exist, answering a callback twice or too late);
* blocked users (TelegramForbiddenError).

It also records everything the bot sent, so the harness can act like a client:
read the latest card, tap its buttons, and check what each student received.
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, DeleteWebhook,
                             EditMessageReplyMarkup, EditMessageText, GetMe,
                             SendDocument, SendMessage, SetMyCommands)
from aiogram.types import Chat, Document, Message, MessageEntity, User

BOT_ID = 8000000001
BOT_USER = User(id=BOT_ID, is_bot=True, first_name="LKC OphSoc Bot", username="lkceyebot")


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class _Html(HTMLParser):
    TAGS = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic",
            "u": "underline", "ins": "underline", "s": "strikethrough",
            "code": "code", "pre": "pre", "a": "text_link",
            "tg-spoiler": "spoiler", "blockquote": "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos = 0
        self.stack: list[tuple[str, int, dict]] = []
        self.entities: list[dict] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS:
            self.errors.append(f"Unsupported start tag \"{tag}\"")
        self.stack.append((tag, self.pos, dict(attrs)))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append(f"Unmatched end tag \"{tag}\"")
            return
        name, start, attrs = self.stack.pop()
        if self.pos > start and name in self.TAGS:
            ent = {"type": self.TAGS[name], "offset": start, "length": self.pos - start}
            if name == "a":
                ent["url"] = attrs.get("href") or "https://example.org"
            self.entities.append(ent)

    def handle_data(self, data):
        self.parts.append(data)
        self.pos += u16(data)


_BARE_LT = re.compile(r"<(?!/?[a-zA-Z][a-zA-Z-]*[\s>/])")


def parse_html(html: str) -> tuple[str, list[dict]]:
    """Telegram's HTML parse mode, strictly enough to reject what Telegram rejects."""
    if _BARE_LT.search(html):
        raise ValueError("Can't parse entities: unexpected '<' that is not part of a tag")
    p = _Html()
    p.feed(html)
    p.close()
    if p.errors:
        raise ValueError("Can't parse entities: " + p.errors[0])
    if p.stack:
        raise ValueError(f"Can't parse entities: can't find end tag corresponding to start tag \"{p.stack[-1][0]}\"")
    return "".join(p.parts), sorted(p.entities, key=lambda e: e["offset"])


@dataclass
class Stored:
    mid: int
    text: str
    entities: list[dict]
    markup: object | None
    kind: str = "text"            # text | document | user
    sent_at: float = 0.0
    html: str | None = None


@dataclass
class Config:
    latency: tuple[float, float] = (0.12, 0.25)   # seconds per API round trip
    connections: int = 100                         # aiohttp TCPConnector limit
    limits: bool = True
    global_per_sec: int = 30                       # sends+edits across all chats
    chat_per_sec: int = 4                          # sends to one private chat
    retry_after: int = 2
    callback_timeout: float = 15.0                 # Telegram stops accepting answers
    upload_bytes_per_sec: float = 5_000_000
    edits_count: bool = False                      # edits use the global budget too


@dataclass
class Stats:
    calls: collections.Counter = field(default_factory=collections.Counter)
    flood_global: int = 0
    flood_chat: int = 0
    forbidden: int = 0
    bad_request: collections.Counter = field(default_factory=collections.Counter)
    accepted_by_second: collections.Counter = field(default_factory=collections.Counter)
    max_inflight: int = 0


class FakeTelegram:
    def __init__(self, cfg: Config | None = None, seed: int = 1):
        self.cfg = cfg or Config()
        self.rng = random.Random(seed)
        self.chats: dict[int, dict[int, Stored]] = collections.defaultdict(dict)
        self._ids: dict[int, itertools.count] = collections.defaultdict(lambda: itertools.count(1))
        self.blocked: set[int] = set()
        self.stats = Stats()
        self._global_window: collections.deque[float] = collections.deque()
        self._chat_window: dict[int, collections.deque[float]] = collections.defaultdict(collections.deque)
        self._conn = asyncio.Semaphore(self.cfg.connections)
        self._inflight = 0
        self.t0 = time.monotonic()
        # callback id -> creation time; answers recorded separately
        self.callbacks: dict[str, float] = {}
        self.cb_answers: dict[str, list[tuple[float, str | None, bool]]] = collections.defaultdict(list)
        self.commands_set: list = []

    # ------------------------------------------------------------- client side
    def next_id(self, chat_id: int) -> int:
        return next(self._ids[chat_id])

    def user_message(self, chat_id: int, text: str) -> int:
        mid = self.next_id(chat_id)
        self.chats[chat_id][mid] = Stored(mid, text, [], None, "user", time.monotonic())
        return mid

    def bot_messages(self, chat_id: int, after: int = 0) -> list[Stored]:
        return [m for mid, m in sorted(self.chats[chat_id].items())
                if mid > after and m.kind != "user"]

    def as_message(self, chat_id: int, mid: int, user: User) -> Message:
        s = self.chats[chat_id][mid]
        return Message(message_id=mid, date=datetime.now(timezone.utc),
                       chat=Chat(id=chat_id, type="private"), from_user=BOT_USER,
                       text=s.text if s.kind == "text" else None,
                       caption=s.text if s.kind == "document" else None,
                       entities=[MessageEntity(**e) for e in s.entities] or None,
                       reply_markup=s.markup)

    # ------------------------------------------------------------- flood model
    def _flood_check(self, method, chat_id: int | None, counts_for_chat: bool) -> None:
        if not self.cfg.limits:
            return
        now = time.monotonic()
        gw = self._global_window
        while gw and now - gw[0] > 1.0:
            gw.popleft()
        if len(gw) >= self.cfg.global_per_sec:
            self.stats.flood_global += 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests: retry after",
                                     retry_after=self.cfg.retry_after)
        if chat_id is not None and counts_for_chat:
            cw = self._chat_window[chat_id]
            while cw and now - cw[0] > 1.0:
                cw.popleft()
            if len(cw) >= self.cfg.chat_per_sec:
                self.stats.flood_chat += 1
                raise TelegramRetryAfter(method=method, message="Too Many Requests: retry after",
                                         retry_after=1)
            cw.append(now)
        gw.append(now)

    def _bad(self, method, msg: str):
        self.stats.bad_request[msg.split(":")[0][:60]] += 1
        raise TelegramBadRequest(method=method, message=f"Bad Request: {msg}")

    def _render(self, method, text: str, parse_mode, entities=None) -> tuple[str, list[dict]]:
        if parse_mode and str(parse_mode).upper() == "HTML":
            try:
                plain, ents = parse_html(text)
            except ValueError as exc:
                self._bad(method, str(exc))
        else:
            plain, ents = text, [e.model_dump(exclude_none=True) for e in (entities or [])]
        if u16(plain) > 4096:
            self._bad(method, "message is too long")
        if not plain.strip():
            self._bad(method, "message text is empty")
        return plain, ents

    def _check_markup(self, method, markup):
        if markup is None:
            return
        for row in getattr(markup, "inline_keyboard", []) or []:
            for b in row:
                if b.callback_data is not None and len(b.callback_data.encode()) > 64:
                    self._bad(method, "BUTTON_DATA_INVALID")

    # ------------------------------------------------------------ API dispatch
    async def handle(self, bot, method):
        name = type(method).__name__
        self.stats.calls[name] += 1
        async with self._conn:
            self._inflight += 1
            self.stats.max_inflight = max(self.stats.max_inflight, self._inflight)
            try:
                lo, hi = self.cfg.latency
                await asyncio.sleep(self.rng.uniform(lo, hi))
                if isinstance(method, SendDocument):
                    await asyncio.sleep(60_000 / self.cfg.upload_bytes_per_sec)
                return self._apply(bot, method, name)
            finally:
                self._inflight -= 1

    def _apply(self, bot, method, name):
        now = time.monotonic()
        if isinstance(method, (SendMessage, SendDocument)):
            chat_id = int(method.chat_id)
            if chat_id in self.blocked:
                self.stats.forbidden += 1
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
            self._flood_check(method, chat_id, counts_for_chat=True)
            self._check_markup(method, method.reply_markup)
            if isinstance(method, SendMessage):
                text, ents = self._render(method, method.text, method.parse_mode, method.entities)
                kind = "text"
            else:
                text, ents = (self._render(method, method.caption, method.parse_mode)
                              if method.caption else ("", []))
                kind = "document"
            mid = self.next_id(chat_id)
            self.chats[chat_id][mid] = Stored(mid, text, ents, method.reply_markup, kind, now,
                                              html=getattr(method, "text", None) or method.caption)
            self.stats.accepted_by_second[int(now - self.t0)] += 1
            msg = Message(message_id=mid, date=datetime.now(timezone.utc),
                          chat=Chat(id=chat_id, type="private"), from_user=BOT_USER,
                          text=text if kind == "text" else None,
                          caption=text if kind == "document" and text else None,
                          document=Document(file_id=f"f{mid}", file_unique_id=f"u{mid}",
                                            file_name="sheet.pdf") if kind == "document" else None,
                          entities=[MessageEntity(**e) for e in ents] or None if kind == "text" else None,
                          reply_markup=method.reply_markup)
            return msg.as_(bot)

        if isinstance(method, EditMessageText):
            chat_id, mid = int(method.chat_id), int(method.message_id)
            if self.cfg.edits_count:
                self._flood_check(method, chat_id, counts_for_chat=False)
            s = self.chats[chat_id].get(mid)
            if s is None or s.kind == "user":
                self._bad(method, "message to edit not found")
            self._check_markup(method, method.reply_markup)
            text, ents = self._render(method, method.text, method.parse_mode, method.entities)
            same_markup = (method.reply_markup.model_dump() if method.reply_markup else None) == \
                          (s.markup.model_dump() if s.markup else None)
            if text == s.text and ents == s.entities and same_markup:
                self._bad(method, "message is not modified")
            s.text, s.entities, s.markup, s.html = text, ents, method.reply_markup, method.text
            return Message(message_id=mid, date=datetime.now(timezone.utc),
                           chat=Chat(id=chat_id, type="private"), from_user=BOT_USER,
                           text=text, entities=[MessageEntity(**e) for e in ents] or None,
                           reply_markup=method.reply_markup).as_(bot)

        if isinstance(method, EditMessageReplyMarkup):
            chat_id, mid = int(method.chat_id), int(method.message_id)
            s = self.chats[chat_id].get(mid)
            if s is None:
                self._bad(method, "message to edit not found")
            same = (method.reply_markup.model_dump() if method.reply_markup else None) == \
                   (s.markup.model_dump() if s.markup else None)
            if same:
                self._bad(method, "message is not modified")
            s.markup = method.reply_markup
            return True

        if isinstance(method, AnswerCallbackQuery):
            qid = method.callback_query_id
            created = self.callbacks.get(qid)
            self.cb_answers[qid].append((now, method.text, bool(method.show_alert)))
            if created is None or len(self.cb_answers[qid]) > 1:
                self._bad(method, "query is too old and response timeout expired or query ID is invalid")
            if now - created > self.cfg.callback_timeout:
                self._bad(method, "query is too old and response timeout expired or query ID is invalid")
            return True

        if isinstance(method, SetMyCommands):
            self.commands_set.append(method)
            return True
        if isinstance(method, (DeleteWebhook, DeleteMessage)):
            return True
        if isinstance(method, GetMe):
            return BOT_USER
        self.stats.calls[f"UNHANDLED:{name}"] += 1
        return True


class FakeSession(BaseSession):
    def __init__(self, tg: FakeTelegram):
        super().__init__()
        self.tg = tg

    async def make_request(self, bot, method, timeout=None):
        return await self.tg.handle(bot, method)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        if False:
            yield b""

    async def close(self):
        return None
