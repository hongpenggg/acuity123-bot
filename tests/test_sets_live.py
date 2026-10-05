"""The set mechanic and the tournament roll-up, against real PostgreSQL.

The sets are the load-bearing part of the redesign: fixed blocks of five in id
order, so every student's set 1 is the same five questions and the scores mean the
same thing for everyone. That property is worth asserting against the real SQL,
because it depends on window-function ordering that an in-memory stand-in cannot
prove.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import db  # noqa: E402

SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_DATABASE_URL to run the live-database tests")

TABLES = ("tournament_answers", "tournament_points", "tournaments", "attempts",
          "notes", "questions", "users")

#: 7 questions at preclin => set 1 of five, then set 2 of two. The uneven tail is
#: deliberate: the last set is short, and the code must cope with that.
QUESTIONS = [
    (1, "Alpha", "alpha one"), (2, "Alpha", "alpha two"),
    (3, "Beta", "beta one"), (4, "Beta", "beta two"),
    (5, "Gamma", "gamma one"), (6, "Gamma", "gamma two"),
    (7, "Delta", "delta one"),
]


@pytest.fixture(scope="module")
async def bank():
    import asyncpg

    admin = await asyncpg.connect(DSN)
    for table in TABLES:
        await admin.execute(f"drop table if exists {table} cascade")
    await admin.execute(SCHEMA)
    await admin.execute(
        "insert into users (telegram_id, level) values (1, 'preclin') "
        "on conflict do nothing")
    for qid, topic, text in QUESTIONS:
        await admin.execute(
            """insert into questions (id, level, topic, tag, text, options,
                                      correct_idx, explanation)
               values ($1, 'preclin', $2, $3, $4, $5::jsonb, 0, 'because')""",
            qid, topic, f"Physiology | {topic.lower()}", text,
            json.dumps(["a", "b", "c", "d", "e"]),
        )
    await admin.execute(
        "select setval(pg_get_serial_sequence('questions', 'id'), 100)")
    await admin.close()

    await db.init(DSN)
    yield db
    await db.close()


@pytest.fixture(autouse=True)
async def _clean(bank):
    """Each test starts with no answers and no tournament on record.

    The tournaments have to go too: `active_tournament()` returns the *first*
    active row, so a tournament left open by an earlier test would shadow the one
    this test creates and mask a real failure.
    """
    await bank.pool.execute(
        "truncate attempts, tournament_answers, tournament_points, tournaments "
        "restart identity cascade")
    yield


async def answer(uid: int, text: str, *, correct: bool, msg_id: int) -> None:
    row = await db.pool.fetchrow("select * from questions where text = $1", text)
    await db.record_attempt(uid, row, 0 if correct else 1, correct, "practice",
                            msg_id)


# ------------------------------------------------------------------ set shape


async def test_sets_are_consecutive_blocks_of_five(bank):
    board = await db.set_board(1, "preclin")
    assert [(r["set_no"], r["size"]) for r in board] == [(0, 5), (1, 2)]


async def test_every_student_gets_the_same_first_set(bank):
    """The benchmark property: set 1 is the same five questions for everyone."""
    mine = await db.quiz_set(1, "preclin")
    assert [q["text"] for q in mine["questions"]] == [
        "alpha one", "alpha two", "beta one", "beta two", "gamma one"]
    assert mine["number"] == 1 and mine["total_sets"] == 2


async def test_a_stopped_set_is_resumed(bank):
    """Soft: leaving halfway keeps your place rather than restarting the set."""
    await answer(1, "alpha one", correct=True, msg_id=1)
    await answer(1, "alpha two", correct=False, msg_id=2)

    current = await db.quiz_set(1, "preclin")
    assert current["number"] == 1
    assert current["answered"] == 2
    assert [q["text"] for q in current["remaining"]] == [
        "beta one", "beta two", "gamma one"]


async def test_the_next_set_starts_once_five_are_in(bank):
    for offset, text in enumerate(
            ["alpha one", "alpha two", "beta one", "beta two", "gamma one"]):
        await answer(1, text, correct=offset % 2 == 0, msg_id=10 + offset)

    current = await db.quiz_set(1, "preclin")
    assert current["number"] == 2
    assert current["size"] == 2, "the short tail set reports its real size"
    assert [q["text"] for q in current["remaining"]] == ["gamma two", "delta one"]


async def test_finishing_every_set_returns_nothing(bank):
    for offset, (_, _, text) in enumerate(QUESTIONS):
        await answer(1, text, correct=True, msg_id=20 + offset)

    assert await db.quiz_set(1, "preclin") is None
    fresh, total = await db.progress(1, "preclin")
    assert (fresh, total) == (0, len(QUESTIONS))


async def test_set_score_counts_only_the_finished_set(bank):
    await answer(1, "alpha one", correct=True, msg_id=1)
    await answer(1, "alpha two", correct=True, msg_id=2)
    await answer(1, "beta one", correct=False, msg_id=3)
    await answer(1, "beta two", correct=False, msg_id=4)
    await answer(1, "gamma one", correct=True, msg_id=5)

    score = await db.set_score(1, "preclin", 0)
    assert (score["size"], score["answered"], score["correct"]) == (5, 5, 3)
    assert score["total_sets"] == 2

    # The second set is untouched, so its score is not reported yet.
    second = await db.set_score(1, "preclin", 1)
    assert (second["answered"], second["correct"]) == (0, 0)


async def test_set_number_follows_id_order_not_id_value(bank):
    """Ids have gaps after a reload; set membership must follow position, not
    arithmetic on the id."""
    assert await db.set_no_for("preclin", 1) == 0
    assert await db.set_no_for("preclin", 5) == 0
    assert await db.set_no_for("preclin", 6) == 1
    assert await db.set_no_for("preclin", 7) == 1


# ---------------------------------------------------------------- marker A


async def test_stats_split_by_topic_and_tag(bank):
    await answer(1, "alpha one", correct=True, msg_id=1)
    await answer(1, "alpha two", correct=False, msg_id=2)
    await answer(1, "beta one", correct=True, msg_id=3)

    rows = await db.stats(1, "preclin")
    by_topic = {r["topic"]: (r["answered"], r["correct"]) for r in rows}
    assert by_topic == {"Alpha": (2, 1), "Beta": (1, 1)}
    assert all(r["tag"] for r in rows), "the tag travels with the answer"


async def test_stats_are_empty_before_any_answer(bank):
    assert await db.stats(1, "preclin") == []


# ---------------------------------------------------------------- marker B


async def test_tournament_entry_is_everyone_and_automatic(bank):
    await bank.pool.execute(
        "insert into users (telegram_id, level) values (2, 'preclin'), (3, 'clin') "
        "on conflict do nothing")

    tid = await db.start_tournament(days=14)
    added = await db.enrol_everyone(tid)

    assert added == 3, "every active user is entered, not just the opted-in"
    assert len(await db.all_users()) == 3

    mark = await db.tournament_mark(1)
    assert mark["entrants"] == 3
    assert mark["points"] == 0

    # Idempotent: a second start of the same tournament adds nobody twice.
    assert await db.enrol_everyone(tid) == 0


async def test_a_correct_practice_answer_scores_once(bank):
    tid = await db.start_tournament(days=14)
    await db.enrol_everyone(tid)

    assert await db.award_point(1, 1) is True
    assert await db.award_point(1, 1) is False, "no farming the same question"
    assert (await db.tournament_mark(1))["points"] == 1

    await db.end_tournament(tid)
    assert await db.active_tournament() is None
    assert await db.tournament_mark(1) is None, "marker B stops with the tournament"
