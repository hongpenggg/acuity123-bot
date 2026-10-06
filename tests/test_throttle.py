"""The outbound budget and the 429 retry in bot/throttle.py.

The load test in docs/LOADTEST.md is what these protect: before FloodGuard, a
429 on any reply that did not go through sender.safe_send ended in silence for
the student, and at 200 concurrent students that was hundreds of commands.
"""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from bot import throttle
from bot.throttle import Budget, FloodGuard


def send(chat=1):
    return SendMessage(chat_id=chat, text="hi")


@pytest.mark.asyncio
async def test_budget_is_first_come_first_served_and_under_the_limit():
    budget = Budget(rate=24, burst=4)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    order, stamps = [], []

    async def one(i):
        await budget.take()
        order.append(i)
        stamps.append(loop.time() - t0)

    await asyncio.gather(*(one(i) for i in range(40)))

    assert order == list(range(40)), "nobody jumps the queue"
    busiest = max(sum(1 for t in stamps if s <= t < s + 1) for s in stamps)
    assert busiest <= 28, f"{busiest} sends in one second, Telegram allows ~30"


@pytest.mark.asyncio
async def test_a_broadcast_cannot_crowd_out_students():
    budget = Budget(rate=24, burst=4, bulk_rate=12)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    done = []

    async def one(kind):
        await budget.take(bulk=kind == "bulk")
        done.append((loop.time() - t0, kind))

    await asyncio.gather(*(one("bulk") for _ in range(40)),
                         *(one("student") for _ in range(20)))

    students = [t for t, k in done if k == "student"]
    assert max(students) < 1.5, "students waited behind the whole broadcast"
    bulk_first_2s = sum(1 for t, k in done if k == "bulk" and t < 2.0)
    assert bulk_first_2s <= 12 * 2 + 1


class Flaky:
    """make_request that answers 429 a given number of times, then succeeds."""

    def __init__(self, failures, retry_after=0):
        self.failures = failures
        self.retry_after = retry_after
        self.calls = 0

    async def __call__(self, bot, method):
        self.calls += 1
        if self.calls <= self.failures:
            raise TelegramRetryAfter(method=method, message="Too Many Requests",
                                     retry_after=self.retry_after)
        return "ok"


@pytest.mark.asyncio
async def test_guard_retries_a_429_and_the_reply_still_arrives():
    guard = FloodGuard(Budget(rate=1000, burst=10))
    flaky = Flaky(failures=2)

    assert await guard(flaky, SimpleNamespace(), send()) == "ok"
    assert flaky.calls == 3
    assert guard.retries == 2


@pytest.mark.asyncio
async def test_guard_gives_up_after_its_attempts():
    guard = FloodGuard(Budget(rate=1000, burst=10), attempts=3)
    flaky = Flaky(failures=10)

    with pytest.raises(TelegramRetryAfter):
        await guard(flaky, SimpleNamespace(), send())
    assert flaky.calls == 3


@pytest.mark.asyncio
async def test_guard_does_not_sleep_through_an_outage():
    guard = FloodGuard(Budget(rate=1000, burst=10), max_wait=30)
    flaky = Flaky(failures=1, retry_after=600)

    with pytest.raises(TelegramRetryAfter):
        await guard(flaky, SimpleNamespace(), send())
    assert flaky.calls == 1


@pytest.mark.asyncio
async def test_only_message_sends_spend_the_budget(monkeypatch):
    spent = []

    class Counting(Budget):
        async def take(self, bulk=False):
            spent.append(bulk)

    guard = FloodGuard(Counting())

    async def ok(bot, method):
        return True

    await guard(ok, SimpleNamespace(), send())
    await guard(ok, SimpleNamespace(),
                EditMessageText(chat_id=1, message_id=2, text="x"))
    await guard(ok, SimpleNamespace(), AnswerCallbackQuery(callback_query_id="1"))
    assert spent == [False]

    token = throttle.BULK.set(True)
    try:
        await guard(ok, SimpleNamespace(), send())
    finally:
        throttle.BULK.reset(token)
    assert spent == [False, True]


@pytest.mark.asyncio
async def test_a_failed_command_is_answered_rather_than_ignored():
    """main.on_error used to reply only to button taps; a command that failed
    left the student looking at nothing."""
    from aiogram.types import Chat, ErrorEvent, Message, Update, User
    from datetime import datetime, timezone

    from bot import main

    replies = []

    class Recording(Message):
        async def answer(self, text, **kw):  # type: ignore[override]
            replies.append(text)

    msg = Recording(message_id=1, date=datetime.now(timezone.utc),
                    chat=Chat(id=1, type="private"),
                    from_user=User(id=1, is_bot=False, first_name="S"), text="/quizme")
    event = ErrorEvent(update=Update(update_id=1, message=msg),
                       exception=RuntimeError("boom"))

    await main.on_error(event)

    assert replies == ["Something went wrong. Please try again."]


@pytest.mark.asyncio
async def test_edits_join_the_budget_once_telegram_limits_one():
    spent = []

    class Counting(Budget):
        async def take(self, bulk=False):
            spent.append("token")

    guard = FloodGuard(Counting())
    edit = EditMessageText(chat_id=1, message_id=2, text="x")

    async def ok(bot, method):
        return True

    await guard(ok, SimpleNamespace(), edit)
    assert spent == [], "edits are free until Telegram says otherwise"

    await guard(Flaky(failures=1), SimpleNamespace(), edit)
    assert guard.edits_budgeted
    assert spent == ["token"], "the retry itself already waits its turn"

    await guard(ok, SimpleNamespace(), edit)
    assert spent == ["token", "token"]
