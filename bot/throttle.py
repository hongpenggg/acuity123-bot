"""One outbound budget for every Telegram call, and a retry when Telegram says
slow down.

Telegram accepts roughly thirty messages a second from a bot across all chats,
and answers anything over that with HTTP 429 (TelegramRetryAfter). Before this
module only `sender.safe_send` and `sender._upload_note` caught that; the forty-odd
`m.answer(...)` replies in handlers.py, the verdict edits and the callback
answers did not, so under load a 429 went straight to the error handler and the
student got silence: a /quizme with no card, an answer recorded with no verdict.
A load test at 200 concurrent students reproduced exactly that (see
docs/LOADTEST.md).

FloodGuard is an aiogram request middleware, so it sits under every call the bot
makes without any handler having to know about it. It does two things:

* Queues sends first come, first served, held just under Telegram's limit, so a
  busy moment waits a fraction of a second here instead of being refused there.
  Message-creating calls always go through the queue. Edits join it the first
  time Telegram rate-limits one (see EDITS). Callback answers and the rest never
  do, but are still retried.
* Retries a 429 after the wait Telegram asks for, and pauses the whole queue
  for that long, because a 429 means the bot as a whole is over, not just the
  call that drew it.

Scheduled fan-outs cannot crowd students out. A fan-out marks its sends with
`BULK`, and those are held to BULK_RATE of the budget, so a Monday push to every
subscriber leaves the rest for replies to whoever is using the bot right then.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import (CopyMessage, EditMessageCaption, EditMessageMedia,
                             EditMessageReplyMarkup, EditMessageText, ForwardMessage,
                             SendAnimation, SendAudio, SendDocument, SendMediaGroup,
                             SendMessage, SendPhoto, SendVideo, SendVoice)

log = logging.getLogger(__name__)

#: Set (per task) by the scheduled fan-outs, so their sends yield to students.
BULK: contextvars.ContextVar[bool] = contextvars.ContextVar("bulk_send", default=False)

#: Calls that create a message, which is what Telegram's ~30/s limit counts.
BUDGETED = (SendMessage, SendDocument, SendPhoto, SendVideo, SendAudio, SendVoice,
            SendAnimation, SendMediaGroup, CopyMessage, ForwardMessage)
#: Edits. Telegram does not say whether they count against the same limit, and
#: they are most of what a quiz does (every verdict and explanation is one), so
#: budgeting them up front would roughly halve capacity on a guess. Instead they
#: join the budget the first time Telegram answers one with a 429.
EDITS = (EditMessageText, EditMessageReplyMarkup, EditMessageCaption, EditMessageMedia)

#: Comfortably under Telegram's ~30/s, with a small burst. The most that can go
#: out in any one-second window is RATE + BURST, so keep the sum under 30.
RATE = 24.0
BURST = 4
#: The most of that budget a scheduled fan-out may use, so students' replies
#: always have at least RATE - BULK_RATE a second to themselves.
BULK_RATE = 12.0
#: Retries per call. Telegram's waits are short in practice; anything longer
#: than MAX_WAIT is a real outage and is better surfaced than slept through.
ATTEMPTS = 4
MAX_WAIT = 30.0


class Budget:
    """When each budgeted call may go out, handed out strictly first come, first
    served.

    Each caller books the next free slot on a shared timeline spaced 1/rate
    apart, then sleeps until it. Booking is a couple of arithmetic operations
    with no await in between, so it is atomic on the event loop and nobody can
    jump the queue: under load every request waits about as long as the queue in
    front of it, rather than some going straight through while others wait
    minutes. `burst` lets a quiet bot send a few at once.

    Bulk sends (a scheduled fan-out) also book a slot on their own, slower
    timeline first, so however large a Monday push is it can never take more
    than `bulk_rate` of the budget: replies to students always keep the rest.
    """

    def __init__(self, rate: float = RATE, burst: int = BURST,
                 bulk_rate: float = BULK_RATE) -> None:
        self.rate = rate
        self.burst = burst
        self.bulk_rate = bulk_rate
        self._next = 0.0
        self._next_bulk = 0.0
        self._paused_until = 0.0

    @staticmethod
    def _book(next_free: float, now: float, rate: float, burst: int) -> tuple[float, float]:
        slot = max(next_free, now - (burst - 1) / rate)
        return slot, slot + 1 / rate

    async def take(self, bulk: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if bulk:
            slot, self._next_bulk = self._book(self._next_bulk, loop.time(),
                                               self.bulk_rate, 1)
            delay = slot - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        now = loop.time()
        slot, self._next = self._book(max(self._next, self._paused_until), now,
                                      self.rate, self.burst)
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Telegram said wait: push every later slot back by that much."""
        until = asyncio.get_running_loop().time() + seconds
        self._paused_until = max(self._paused_until, until)


class FloodGuard(BaseRequestMiddleware):
    def __init__(self, budget: Budget | None = None, attempts: int = ATTEMPTS,
                 max_wait: float = MAX_WAIT) -> None:
        self.budget = budget or Budget()
        self.attempts = attempts
        self.max_wait = max_wait
        self.retries = 0
        self.edits_budgeted = False

    async def __call__(self, make_request, bot, method):
        is_edit = isinstance(method, EDITS)
        for attempt in range(self.attempts):
            if isinstance(method, BUDGETED) or (is_edit and self.edits_budgeted):
                await self.budget.take(bulk=BULK.get())
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if is_edit and not self.edits_budgeted:
                    self.edits_budgeted = True
                    log.warning("Telegram rate-limited an edit, so edits now share "
                                "the outbound budget")
                if attempt == self.attempts - 1 or exc.retry_after > self.max_wait:
                    raise
                self.retries += 1
                log.warning("flood control on %s, retrying in %ss (attempt %s of %s)",
                            type(method).__name__, exc.retry_after, attempt + 2,
                            self.attempts)
                self.budget.pause(exc.retry_after)
                await asyncio.sleep(exc.retry_after)
        raise AssertionError("unreachable")  # pragma: no cover
