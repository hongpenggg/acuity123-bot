"""Integration tests against a real PostgreSQL database.

Skipped unless TEST_DATABASE_URL points at a throwaway database, e.g.:

    docker compose up -d db
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/studybot_test \
        python -m pytest tests/test_db_live.py -v

These are the tests that prove the SQL actually means what it says — the
uniqueness that makes double taps idempotent, the CTE that stops leaderboard
farming, and the standings read that has to happen before the tournament closes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the live database tests",
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "schema.sql").read_text()


@pytest.fixture(scope="module")
def dsn():
    return os.environ["TEST_DATABASE_URL"]


@pytest.fixture(scope="module")
async def pool(dsn):
    import asyncpg
    from bot.db import init, close

    admin = await asyncpg.connect(dsn)
    for table in ("tournament_answers", "tournament_points", "attempts",
                  "tournaments", "notes", "questions", "users"):
        await admin.execute(f"drop table if exists {table} cascade")
    await admin.execute(SCHEMA)
    await admin.close()

    await init(dsn)
    yield
    await close()


@pytest.fixture(autouse=True)
async def clean(pool):
    from bot import db

    for table in ("tournament_answers", "tournament_points", "attempts",
                  "tournaments", "notes", "questions", "users"):
        await db.pool.execute(f"truncate {table} cascade")
    yield


async def add_question(db, level="preclin", topic="Sample", n=4, correct_idx=0):
    return await db.pool.fetchval(
        """insert into questions (level, topic, text, options, correct_idx)
           values ($1, $2, 'Question?', $3::jsonb, $4) returning id""",
        level, topic, json.dumps([f"option {i}" for i in range(n)]), correct_idx,
    )


# ------------------------------------------------------------------ users


async def test_level_round_trip(pool):
    from bot import db

    await db.upsert_user(1, "hongpenggg")
    assert await db.get_level(1) == "preclin"      # the column default
    await db.set_level(1, "clin")
    assert await db.get_level(1) == "clin"


async def test_upsert_reactivates_and_renames(pool):
    from bot import db

    await db.upsert_user(1, "old")
    await db.set_flag(1, "weekly_sub", True)
    assert await db.subscribers("weekly_sub") == [1]
    await db.deactivate(1)
    assert await db.subscribers("weekly_sub") == []
    await db.upsert_user(1, "new")
    assert await db.subscribers("weekly_sub") == [1], "coming back must reactivate"
    assert (await db.pool.fetchrow(
        "select username from users where telegram_id = 1"))["username"] == "new"


async def test_set_flag_rejects_unknown_column(pool):
    from bot import db

    await db.upsert_user(1, None)
    with pytest.raises(ValueError):
        await db.set_flag(1, "is_admin", True)


# -------------------------------------------------------------- questions


async def test_pick_question_respects_level(pool):
    from bot import db

    await db.upsert_user(1, None)
    await add_question(db, level="preclin", topic="A")
    await add_question(db, level="clin", topic="B")

    seen = {await db.pick_question(1, "preclin") for _ in range(5)}
    assert {q["level"] for q in seen} == {"preclin"}
    assert {q["topic"] for q in seen} == {"A"}


async def test_pick_question_returns_none_for_an_empty_level(pool):
    from bot import db

    await db.upsert_user(1, None)
    assert await db.pick_question(1, "postmbbs") is None


async def test_pick_question_prefers_unseen_questions(pool):
    from bot import db

    await db.upsert_user(1, None)
    first = await add_question(db, topic="A")
    await add_question(db, topic="A")
    await db.record_attempt(1, await db.get_question(first), 0, True, "practice", 111)

    picks = {(await db.pick_question(1, "preclin", topic="A"))["id"] for _ in range(10)}
    assert first not in picks


async def test_schema_rejects_a_correct_idx_outside_the_options(pool):
    import asyncpg

    from bot import db

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.pool.execute(
            """insert into questions (level, topic, text, options, correct_idx)
               values ('preclin', 'X', 'Q', '["a","b"]'::jsonb, 5)"""
        )


# --------------------------------------------------------------- attempts


async def test_record_attempt_is_idempotent_per_message(pool):
    from bot import db

    await db.upsert_user(1, None)
    q = await db.get_question(await add_question(db))

    assert await db.record_attempt(1, q, 0, True, "practice", 500) is True
    assert await db.record_attempt(1, q, 1, False, "practice", 500) is False
    rows = await db.pool.fetch("select chosen_idx from attempts where msg_id = 500")
    assert [r["chosen_idx"] for r in rows] == [0], "the first answer must stand"


async def test_same_message_id_from_two_users_both_count(pool):
    from bot import db

    await db.upsert_user(1, None)
    await db.upsert_user(2, None)
    q = await db.get_question(await add_question(db))

    assert await db.record_attempt(1, q, 0, True, "practice", 500) is True
    assert await db.record_attempt(2, q, 0, True, "practice", 500) is True


async def test_practice_streak_counts_back_to_the_last_miss(pool):
    from bot import db

    await db.upsert_user(1, None)
    q = await db.get_question(await add_question(db))
    assert await db.practice_streak(1) == 0

    results = [True, False, True, True, True]
    for msg_id, correct in enumerate(results, start=600):
        await db.record_attempt(1, q, 0, correct, "practice", msg_id)
    # Weekly answers don't break or extend a practice streak.
    await db.record_attempt(1, q, 1, False, "weekly", 700)

    assert await db.practice_streak(1) == 3


# ------------------------------------------------------------- tournament


async def test_award_point_scores_each_question_once(pool):
    from bot import db

    await db.upsert_user(1, None)
    qid = await add_question(db)
    await db.start_tournament()
    await db.join_tournament(1)

    assert await db.award_point(1, qid) is True
    assert await db.award_point(1, qid) is False, "re-answering must not score again"
    assert await db.award_point(1, qid) is False

    rows = await db.leaderboard()
    assert rows[0]["points"] == 1


async def test_award_point_without_a_joined_tournament_does_nothing(pool):
    from bot import db

    await db.upsert_user(1, None)
    qid = await add_question(db)
    assert await db.award_point(1, qid) is False


async def test_leaderboard_ranks_and_breaks_ties_by_join_order(pool):
    from bot import db

    for uid in (1, 2, 3):
        await db.upsert_user(uid, f"user{uid}")
    q1 = await add_question(db, topic="A")
    q2 = await add_question(db, topic="B")
    await db.start_tournament()
    for uid in (1, 2, 3):
        await db.join_tournament(uid)
    await db.award_point(3, q1)
    await db.award_point(3, q2)
    await db.award_point(1, q1)   # 1 joined first, so 1 leads on a tie

    rows = await db.leaderboard()
    assert [(r["user_id"], r["points"], r["rk"]) for r in rows] == [
        (3, 2, 1), (1, 1, 2), (2, 0, 3)]


async def test_my_rank(pool):
    from bot import db

    await db.upsert_user(1, None)
    await db.start_tournament()
    await db.join_tournament(1)
    me = await db.my_rank(1)
    assert (me["rk"], me["points"]) == (1, 0)


async def test_standings_survive_the_tournament_closing(pool):
    """The original close job read the leaderboard after deactivating, and so
    always announced an empty table."""
    from bot import db

    await db.upsert_user(1, "winner")
    await db.start_tournament()
    await db.join_tournament(1)
    await db.award_point(1, await add_question(db))

    tid = (await db.active_tournament())["id"]
    await db.end_tournament(tid)

    assert await db.leaderboard() is None
    rows = await db.standings(tid)
    assert [(r["username"], r["points"]) for r in rows] == [("winner", 1)]


async def test_expired_tournaments(pool):
    from bot import db

    # Built directly: the schema forbids ends_at <= starts_at, and
    # start_tournament() always spans forward from now().
    tid = await db.pool.fetchval(
        """insert into tournaments (starts_at, ends_at)
           values (now() - interval '15 days', now() - interval '1 day')
           returning id"""
    )
    assert await db.expired_tournaments() == [tid]
    await db.end_tournament(tid)
    assert await db.expired_tournaments() == []


async def test_join_and_leave_are_idempotent(pool):
    from bot import db

    await db.upsert_user(1, None)
    await db.start_tournament()
    assert await db.join_tournament(1) is True
    assert await db.join_tournament(1) is True
    assert await db.is_joined(1) is True
    await db.leave_tournament(1)
    await db.leave_tournament(1)
    assert await db.is_joined(1) is False


# ------------------------------------------------------------------ notes


async def test_notes_are_scoped_by_level_and_tier(pool):
    from bot import db

    await db.pool.execute(
        """insert into notes (level, topic, tier, title, body) values
           ('preclin', 'Anatomy', 'B', 't1', 'b1'),
           ('clin',    'Anatomy', 'B', 't2', 'b2'),
           ('preclin', 'Anatomy', 'A', 't3', 'b3')"""
    )
    assert await db.note_topics("preclin", "B") == ["Anatomy"]
    assert [r["title"] for r in await db.get_notes("anatomy", "preclin", "B")] == ["t1"]
    assert [r["title"] for r in await db.get_notes("Anatomy", "preclin", "A")] == ["t3"]
    assert [r["title"] for r in await db.all_notes("clin", "B")] == ["t2"]


async def test_save_explanation_is_single_writer(pool):
    from bot import db

    qid = await add_question(db)
    await db.save_explanation(qid, "first")
    await db.save_explanation(qid, "second")
    assert (await db.get_question(qid))["explanation"] == "first"
