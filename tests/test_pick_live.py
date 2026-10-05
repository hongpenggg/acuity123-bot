"""The no-repeat rule, against a real PostgreSQL.

`pick_question` holds back anything the student has already answered correctly, so
they work through the bank instead of looping on the same sheet. Wrong answers
deliberately stay in the pool.

Skipped unless TEST_DATABASE_URL is set (CI provides one).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bot import db

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the live-database tests",
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")

TABLES = ("tournament_answers", "tournament_points", "attempts", "tournaments",
          "notes", "questions", "users")

# Two topics, two questions each: enough to prove the topic selection never
# strands a student on a topic they have cleared.
QUESTIONS = [
    ("preclin", "Alpha topic", "alpha one"),
    ("preclin", "Alpha topic", "alpha two"),
    ("preclin", "Beta topic", "beta one"),
    ("preclin", "Beta topic", "beta two"),
]


@pytest.fixture(scope="module")
async def bank():
    import asyncpg

    from bot.db import close, init

    dsn = os.environ["TEST_DATABASE_URL"]
    admin = await asyncpg.connect(dsn)
    for table in TABLES:
        await admin.execute(f"drop table if exists {table} cascade")
    await admin.execute(SCHEMA)
    # attempts.user_id has a foreign key to users, so the student has to exist
    # before any attempt can be recorded for them.
    await admin.execute(
        "insert into users (telegram_id, level) values (1, 'preclin') "
        "on conflict do nothing")
    for level, topic, text in QUESTIONS:
        await admin.execute(
            """insert into questions (level, topic, tag, text, options, correct_idx,
                                      explanation)
               values ($1, $2, 'Test', $3, $4::jsonb, 0, 'because')""",
            level, topic, text, json.dumps(["right", "wrong", "other"]),
        )
    await admin.close()

    await init(dsn)
    yield db
    await close()


@pytest.fixture(autouse=True)
async def _clean(bank):
    """Each test starts with nobody having answered anything."""
    await bank.pool.execute("truncate attempts restart identity cascade")
    yield


async def answer(uid: int, text: str, *, correct: bool, msg_id: int) -> None:
    """Answer one question by its text, recording the outcome."""
    row = await db.pool.fetchrow("select * from questions where text = $1", text)
    assert row is not None, text
    await db.record_attempt(uid, row, 0 if correct else 1, correct, "practice", msg_id)


async def test_a_correct_answer_retires_the_question(bank):
    await answer(1, "alpha one", correct=True, msg_id=1)

    seen = {await served_text(1) for _ in range(12)}

    assert "alpha one" not in seen
    assert "alpha two" in seen


async def test_a_wrong_answer_keeps_the_question_in_play(bank):
    await answer(1, "alpha one", correct=False, msg_id=1)

    seen = {await served_text(1) for _ in range(20)}

    assert "alpha one" in seen, "a wrong answer must be offered again"


async def test_never_serves_a_finished_question_while_others_remain(bank):
    """The heart of it: with 3 of 4 still to get right, the finished one is not
    served again."""
    await answer(1, "alpha one", correct=True, msg_id=1)

    # Note the naming: a local called `served` would shadow the helper below.
    served_texts = [await served_text(1) for _ in range(15)]

    assert "alpha one" not in served_texts


async def test_picks_a_topic_that_still_has_something_fresh(bank):
    """Regression: topic selection must not land on a topic the student has
    cleared, or they get a repeat while other topics still have new questions."""
    await answer(1, "alpha one", correct=True, msg_id=1)
    await answer(1, "alpha two", correct=True, msg_id=2)

    topics = {await served_topic(1) for _ in range(12)}

    assert topics == {"Beta topic"}, topics


async def test_progress_counts_what_is_left(bank):
    assert await bank.progress(1, "preclin") == (4, 4)

    await answer(1, "alpha one", correct=True, msg_id=1)
    assert await bank.progress(1, "preclin") == (3, 4)

    await answer(1, "alpha two", correct=False, msg_id=2)
    assert await bank.progress(1, "preclin") == (3, 4), "a wrong answer is not progress"


async def test_everything_cleared_still_serves_a_question(bank):
    """The bot must not dead-end; it repeats, least-repeated first."""
    for i, (_, _, text) in enumerate(QUESTIONS, start=1):
        await answer(1, text, correct=True, msg_id=i)

    assert await bank.progress(1, "preclin") == (0, 4)
    assert await bank.pick_question(1, "preclin") is not None


async def test_an_empty_level_returns_nothing(bank):
    assert await bank.pick_question(1, "postmbbs") is None
    assert await bank.progress(1, "postmbbs") == (0, 0)


async def served_text(uid: int) -> str:
    return (await db.pick_question(uid, "preclin"))["text"]


async def served_topic(uid: int) -> str:
    return (await db.pick_question(uid, "preclin"))["topic"]
