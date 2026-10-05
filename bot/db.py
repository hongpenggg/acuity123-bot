"""All Postgres access.

Conventions:
  * every statement is parameterised ($1, $2 ...) — no caller data is ever
    interpolated into SQL;
  * functions take plain ints/strings and return asyncpg Records, so the handler
    layer never sees a connection.
"""
from __future__ import annotations

import random

import asyncpg

from .config import MIN_ATTEMPTS_FOR_ADAPTIVE, WEIGHT_FLOOR

pool: asyncpg.Pool | None = None

# Columns a user is allowed to toggle. Chosen from this tuple, never built from
# caller input — and unlike `assert`, a lookup still works under `python -O`.
_FLAGS = ("weekly_sub", "notes_sub")

_ACTIVE_TOURNAMENT = """
    select * from tournaments
     where active and now() between starts_at and ends_at
     order by id
     limit 1
"""

_RANKED = """
    select p.user_id,
           u.username,
           p.points,
           rank() over (order by p.points desc, p.joined_at, p.user_id) as rk
      from tournament_points p
      join users u on u.telegram_id = p.user_id
     where p.tournament_id = $1
     order by rk
     limit $2::int
"""


def _flag(col: str) -> str:
    if col not in _FLAGS:
        raise ValueError(f"unknown flag column: {col!r}")
    return col


def _require_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("db.init() has not been called")
    return pool


async def init(dsn: str) -> None:
    global pool
    # statement_cache_size=0 is required when talking to a pgbouncer-style
    # transaction pooler (Supabase's shared pooler), which cannot keep prepared
    # statements pinned across transactions.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, statement_cache_size=0)


async def close() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


# --------------------------------------------------------------------------- users


async def upsert_user(uid: int, username: str | None) -> None:
    conn = _require_pool()
    await conn.execute(
        """insert into users (telegram_id, username) values ($1, $2)
           on conflict (telegram_id) do update
              set username = excluded.username,
                  active = true""",
        uid, username,
    )


async def deactivate(uid: int) -> None:
    conn = _require_pool()
    await conn.execute("update users set active = false where telegram_id = $1", uid)


async def set_flag(uid: int, col: str, value: bool) -> None:
    conn = _require_pool()
    col = _flag(col)
    await conn.execute(f"update users set {col} = $2 where telegram_id = $1", uid, value)


async def subscribers(col: str) -> list[int]:
    conn = _require_pool()
    col = _flag(col)
    rows = await conn.fetch(
        f"select telegram_id from users where {col} and active order by telegram_id"
    )
    return [r["telegram_id"] for r in rows]


async def get_level(uid: int) -> str | None:
    conn = _require_pool()
    return await conn.fetchval("select level from users where telegram_id = $1", uid)


async def set_level(uid: int, level: str) -> None:
    conn = _require_pool()
    await conn.execute("update users set level = $2 where telegram_id = $1", uid, level)


# ----------------------------------------------------------------------- questions


async def topics(level: str) -> list[str]:
    conn = _require_pool()
    rows = await conn.fetch(
        "select distinct topic from questions where level = $1 order by topic", level
    )
    return [r["topic"] for r in rows]


async def levels_with_questions() -> list[str]:
    """Which audience levels actually have content — so the bot can tell a
    student whose level is still being written what is available instead of
    looking broken."""
    conn = _require_pool()
    rows = await conn.fetch(
        "select distinct level from questions order by level")
    return [r["level"] for r in rows]


async def get_question(qid: int):
    conn = _require_pool()
    return await conn.fetchrow("select * from questions where id = $1", qid)


async def _weighted_topic(uid: int, level: str, candidates: list[str]) -> str:
    """Pick a topic, biased towards the user's weakest ones once they have
    answered enough to make a per-topic accuracy meaningful."""
    conn = _require_pool()
    rows = await conn.fetch(
        """select topic, count(*) as n, sum(correct::int) as c
             from attempts
            where user_id = $1 and level = $2
            group by topic""",
        uid, level,
    )
    stats = {r["topic"]: (r["n"], r["c"] or 0) for r in rows}
    answered = sum(n for n, _ in stats.values())
    if answered < MIN_ATTEMPTS_FOR_ADAPTIVE:
        return random.choice(candidates)

    def weight(topic: str) -> float:
        n, correct = stats.get(topic, (0, 0))
        # Laplace-smoothed error rate: unseen topics sit mid-range rather than
        # being starved or dominating.
        return (1.0 - (correct + 1) / (n + 2)) + WEIGHT_FLOOR

    return random.choices(candidates, weights=[weight(t) for t in candidates], k=1)[0]


#: A question is "fresh" for a user until they answer it correctly. Wrong answers
#: deliberately do not remove it — getting it again is the point.
_FRESH_SQL = """select q.* from questions q
                 where q.level = $2 and q.topic = $3
                   and not exists (select 1 from attempts a
                                    where a.user_id = $1
                                      and a.question_id = q.id
                                      and a.correct)
                 order by random()
                 limit 1"""

#: Everything in the topic has been answered correctly. Hand back the one they
#: have attempted fewest times, so repeats at least spread out.
_LEAST_REPEATED_SQL = """select q.* from questions q
                          where q.level = $2 and q.topic = $3
                          order by (select count(*) from attempts a
                                     where a.user_id = $1 and a.question_id = q.id),
                                   random()
                          limit 1"""


async def _topics_with_fresh(uid: int, level: str) -> list[str]:
    """Topics at this level where the user still has something left to get right."""
    conn = _require_pool()
    rows = await conn.fetch(
        """select distinct q.topic from questions q
            where q.level = $2
              and not exists (select 1 from attempts a
                               where a.user_id = $1
                                 and a.question_id = q.id
                                 and a.correct)
            order by q.topic""",
        uid, level,
    )
    return [r["topic"] for r in rows]


async def pick_question(uid: int, level: str, topic: str | None = None):
    """One question at the user's level, avoiding repeats.

    A question is *finished* once the user answers it correctly; finished questions
    are held back so a student works through the bank instead of seeing the same
    one repeatedly. Wrong answers stay in the pool on purpose.

    The topic for unfixed-topic practice is chosen from the topics that still have
    something fresh, not from every topic — otherwise a cleared topic could be
    selected and the student would be handed a repeat while other topics still had
    unseen questions. Once *everything* at the level has been answered correctly
    the filter is dropped and the least-repeated question is served, so the bot
    never dead-ends. Callers check `progress` first to say so plainly.
    """
    conn = _require_pool()

    if topic is not None:
        row = await conn.fetchrow(_FRESH_SQL, uid, level, topic)
        return row if row is not None else await conn.fetchrow(
            _LEAST_REPEATED_SQL, uid, level, topic)

    fresh_topics = await _topics_with_fresh(uid, level)
    if fresh_topics:
        chosen = await _weighted_topic(uid, level, fresh_topics)
        row = await conn.fetchrow(_FRESH_SQL, uid, level, chosen)
        if row is not None:
            return row

    all_topics = await topics(level)
    if not all_topics:
        return None
    chosen = await _weighted_topic(uid, level, all_topics)
    return await conn.fetchrow(_LEAST_REPEATED_SQL, uid, level, chosen)


async def progress(uid: int, level: str) -> tuple[int, int]:
    """``(still to get right, total)`` at this level, in one round trip.

    ``total == 0`` means no bank is loaded for the level; ``fresh == 0`` with
    ``total > 0`` means the student has answered everything correctly.
    """
    conn = _require_pool()
    row = await conn.fetchrow(
        """select
             (select count(*) from questions where level = $2) as total,
             (select count(*) from questions q
               where q.level = $2
                 and not exists (select 1 from attempts a
                                  where a.user_id = $1
                                    and a.question_id = q.id
                                    and a.correct)) as fresh""",
        uid, level,
    )
    return (row["fresh"], row["total"])


async def record_attempt(uid: int, question, idx: int, correct: bool,
                         mode: str, msg_id: int) -> bool:
    """Returns False if this message was already scored (double tap / retry)."""
    conn = _require_pool()
    row = await conn.fetchval(
        """insert into attempts (user_id, question_id, level, topic, chosen_idx,
                                 correct, mode, msg_id)
           values ($1, $2, $3, $4, $5, $6, $7, $8)
           on conflict (user_id, msg_id) do nothing
           returning id""",
        uid, question["id"], question["level"], question["topic"],
        idx, correct, mode, msg_id,
    )
    return row is not None


async def practice_streak(uid: int) -> int:
    """How many /practice answers in a row the user has got right, counting back
    from the latest. Capped by the limit, which is far beyond what we display."""
    conn = _require_pool()
    rows = await conn.fetch(
        """select correct from attempts
            where user_id = $1 and mode = 'practice'
            order by id desc
            limit 50""",
        uid,
    )
    streak = 0
    for row in rows:
        if not row["correct"]:
            break
        streak += 1
    return streak


async def save_explanation(qid: int, text: str) -> None:
    """Single-writer cache: if two people tap Explain at the same moment only the
    first answer is kept, so a stale explanation can never overwrite a fresh one."""
    conn = _require_pool()
    await conn.execute(
        "update questions set explanation = $2 where id = $1 and explanation is null",
        qid, text,
    )


# --------------------------------------------------------------------- tournament


async def active_tournament():
    conn = _require_pool()
    return await conn.fetchrow(_ACTIVE_TOURNAMENT)


async def start_tournament(days: int = 14) -> int:
    conn = _require_pool()
    return await conn.fetchval(
        """insert into tournaments (starts_at, ends_at)
           values (now(), now() + make_interval(days => $1))
           returning id""",
        days,
    )


async def end_tournament(tid: int) -> None:
    conn = _require_pool()
    await conn.execute("update tournaments set active = false where id = $1", tid)


async def expired_tournaments() -> list[int]:
    conn = _require_pool()
    rows = await conn.fetch(
        "select id from tournaments where active and ends_at < now() order by id"
    )
    return [r["id"] for r in rows]


async def is_joined(uid: int) -> bool:
    conn = _require_pool()
    return bool(await conn.fetchval(
        """select exists (
             select 1 from tournament_points p
               join tournaments t on t.id = p.tournament_id
              where p.user_id = $1 and t.active
                and now() between t.starts_at and t.ends_at)""",
        uid,
    ))


async def join_tournament(uid: int) -> bool:
    conn = _require_pool()
    t = await active_tournament()
    if not t:
        return False
    await conn.execute(
        """insert into tournament_points (tournament_id, user_id) values ($1, $2)
           on conflict (tournament_id, user_id) do nothing""",
        t["id"], uid,
    )
    return True


async def leave_tournament(uid: int) -> bool:
    conn = _require_pool()
    t = await active_tournament()
    if not t:
        return False
    await conn.execute(
        "delete from tournament_points where tournament_id = $1 and user_id = $2",
        t["id"], uid,
    )
    return True


async def award_point(uid: int, question_id: int) -> bool:
    """+1 point, at most once per question per tournament.

    `pick_question` prefers unseen questions but will eventually re-serve a topic
    the user has cleared; without the tournament_answers uniqueness a user could
    farm the leaderboard by re-answering questions they already know.
    Returns True only when this answer actually scored.
    """
    conn = _require_pool()
    row = await conn.fetchval(
        """with active as (
               select id from tournaments
                where active and now() between starts_at and ends_at
                order by id
                limit 1),
             scored as (
               insert into tournament_answers (tournament_id, user_id, question_id)
               select active.id, $1, $2 from active
               on conflict (tournament_id, user_id, question_id) do nothing
               returning tournament_id)
           update tournament_points p
              set points = p.points + 1
             from scored
            where p.tournament_id = scored.tournament_id
              and p.user_id = $1
           returning p.points""",
        uid, question_id,
    )
    return row is not None


async def standings(tid: int, limit: int | None = None):
    """Ranked table for one tournament. Works after it has been deactivated,
    which is what the winner announcement needs."""
    conn = _require_pool()
    return await conn.fetch(_RANKED, tid, limit)


async def leaderboard(limit: int | None = None):
    t = await active_tournament()
    if not t:
        return None
    return await standings(t["id"], limit)


async def my_rank(uid: int):
    conn = _require_pool()
    t = await active_tournament()
    if not t:
        return None
    return await conn.fetchrow(
        """select p.points, r.rk
             from tournament_points p
             join (select user_id,
                          rank() over (order by points desc, joined_at, user_id) as rk
                     from tournament_points
                    where tournament_id = $2) r on r.user_id = p.user_id
            where p.tournament_id = $2 and p.user_id = $1""",
        uid, t["id"],
    )


# -------------------------------------------------------------------------- notes


async def note_topics(level: str, tier: str) -> list[str]:
    conn = _require_pool()
    rows = await conn.fetch(
        """select distinct topic from notes
            where level = $1 and tier = $2
            order by 1""",
        level, tier,
    )
    return [r["topic"] for r in rows]


async def get_notes(topic: str, level: str, tier: str):
    conn = _require_pool()
    return await conn.fetch(
        """select title, body from notes
            where level = $1 and tier = $2 and lower(topic) = lower($3)
            order by id""",
        level, tier, topic,
    )


async def all_notes(level: str, tier: str):
    conn = _require_pool()
    return await conn.fetch(
        """select topic, title, body from notes
            where level = $1 and tier = $2
            order by topic, id""",
        level, tier,
    )
