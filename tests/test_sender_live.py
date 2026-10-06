"""`send_question_for_level` against a real bank: which card it picks next.

`db.pick_question` ranks on the `attempts` table, so a card that has been *sent*
and not yet answered is invisible to it. Two /quizme taps could therefore hand
back the same question, leaving two live cards for one question; the partial
unique index on `attempts` then refuses the second answer and the student is told
"You've already answered this one" about a card they are seeing for the first
time. The outstanding-card list is what closes that gap, and it only works if it
is also told, through `sender.forget_outstanding`, when a card stops being live.

The randomised tiebreak in `pick_question` means the duplicate was intermittent,
so these drive enough taps that chance is not the thing being measured.

    TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/studybot_test \
        python -m pytest tests/test_sender_live.py -v
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the live database tests",
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "schema.sql").read_text()
TABLES = ("note_deliveries", "tournament_answers", "tournament_points",
          "attempts", "tournaments", "notes", "questions", "users")


@pytest.fixture(scope="module")
async def pool():
    import asyncpg

    from bot.db import close, init

    dsn = os.environ["TEST_DATABASE_URL"]
    admin = await asyncpg.connect(dsn)
    for table in TABLES:
        await admin.execute(f"drop table if exists {table} cascade")
    await admin.execute(SCHEMA)
    await admin.close()

    await init(dsn)
    yield
    await close()


@pytest.fixture(autouse=True)
async def clean(pool):
    """Truncate the tables, and empty the outstanding-card cache.

    The cache is module-level state that outlives a test, so without this the
    order tests run in decides whether a question is served at all.
    """
    from bot import db, sender

    for table in TABLES:
        await db.pool.execute(f"truncate {table} cascade")
    sender.forget_outstanding()
    yield
    sender.forget_outstanding()


class CardBot:
    """Records which question id each card's answer buttons point at."""

    def __init__(self):
        self.cards: list[int] = []
        self.texts: list[str] = []

    async def send_message(self, uid, text, **kw):
        self.texts.append(text)
        markup = kw.get("reply_markup")
        if markup is not None:
            data = markup.inline_keyboard[0][0].callback_data
            self.cards.append(int(data.split(":")[1]))
        return SimpleNamespace(message_id=len(self.texts))


async def _student(uid=1, level="preclin", questions=6):
    from bot import db

    await db.upsert_user(uid, f"s{uid}")
    await db.set_level(uid, level)
    for i in range(questions):
        await db.pool.execute(
            """insert into questions (level, topic, tag, text, options,
                                      correct_idx, explanation)
               values ($1, $2, 't', $3, '["a","b","c","d"]'::jsonb, 0, 'why')""",
            level, f"T{i}", f"question body {i}")


async def test_quizme_taps_without_an_answer_never_repeat_a_card():
    from bot import sender

    await _student(questions=6)

    bot = CardBot()
    for _ in range(5):
        assert await sender.send_question_for_level(bot, 1, "preclin", "practice")

    assert len(bot.cards) == 5
    assert len(set(bot.cards)) == 5, "the same question was served twice"


async def test_the_card_already_waiting_is_explained_not_served_again():
    """With only one question at the level, a second tap has nothing new. Saying
    so beats re-serving a live card, and beats the "I could not find your next
    question" line, which is for a state that should not happen."""
    from bot import sender

    await _student(questions=1)

    bot = CardBot()
    assert await sender.send_question_for_level(bot, 1, "preclin", "practice")
    assert await sender.send_question_for_level(bot, 1, "preclin", "practice")

    assert len(bot.cards) == 1
    assert "already have a question waiting" in bot.texts[-1]
    assert "could not find" not in bot.texts[-1]


async def test_forgetting_a_dead_card_puts_its_question_back_in_the_pool():
    """A card stops being answerable for reasons this module cannot see: the
    student answered it, or a handler struck its buttons because the question row
    no longer matches what was sent. Until it is forgotten, the student is told
    to answer a card that may be dead."""
    from bot import sender

    await _student(questions=1)

    bot = CardBot()
    assert await sender.send_question_for_level(bot, 1, "preclin", "practice")
    dead = bot.cards[0]

    sender.forget_outstanding(1, dead)
    assert await sender.send_question_for_level(bot, 1, "preclin", "practice")

    assert bot.cards == [dead, dead], "the question should be servable again"


async def test_forgetting_one_card_leaves_the_others_outstanding():
    """Keyed by chat but holding several ids, because a student can have more
    than one live card, so forgetting one must not drop the rest. Three
    questions and three taps, so the only question that can come back next is
    the one that was forgotten."""
    from bot import sender

    await _student(questions=3)

    bot = CardBot()
    for _ in range(3):
        await sender.send_question_for_level(bot, 1, "preclin", "practice")
    assert len(set(bot.cards)) == 3

    sender.forget_outstanding(1, bot.cards[1])
    assert await sender.send_question_for_level(bot, 1, "preclin", "practice")
    assert bot.cards[3] == bot.cards[1], "the wrong card was dropped"

    sender.forget_outstanding(1)
    assert await sender.send_question_for_level(bot, 1, "preclin", "practice")
    assert bot.cards[4] in bot.cards[:3], "no qid means forget the whole chat"


def test_forgetting_an_unknown_card_is_not_an_error():
    """The caller should not have to know whether the TTL has already expired."""
    from bot import sender

    sender.forget_outstanding(999, 12345)
    sender.forget_outstanding(999)
    sender.forget_outstanding()
