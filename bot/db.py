"""All Postgres access.

Conventions:
  * every statement is parameterised ($1, $2 ...) — no caller data is ever
    interpolated into SQL;
  * functions take plain ints/strings and return asyncpg Records, so the handler
    layer never sees a connection.
"""
from __future__ import annotations

import asyncpg

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
    """How many /quizme answers in a row the user has got right, counting back
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


#: Quiz sets are consecutive blocks of this many questions, in id order. Because
#: the blocks are fixed, every student's set 1 is the same five questions: the
#: scores are comparable, which is the point of a benchmark set.
SET_SIZE = 5

_SET_BOARD_SQL = """
    with numbered as (
      select id, (row_number() over (order by id) - 1) / $3::int as set_no
        from questions
       where level = $2
    )
    select numbered.set_no,
           count(*) as size,
           count(*) filter (where exists (select 1 from attempts a
                            where a.user_id = $1 and a.question_id = numbered.id))
             as answered,
           count(*) filter (where exists (select 1 from attempts a
                            where a.user_id = $1 and a.question_id = numbered.id
                              and a.correct))
             as correct
      from numbered
     group by numbered.set_no
     order by numbered.set_no
"""

_SET_QUESTIONS_SQL = """
    with numbered as (
      select *, (row_number() over (order by id) - 1) / $3::int as set_no
        from questions
       where level = $2
    )
    select numbered.*,
           exists (select 1 from attempts a
                    where a.user_id = $1 and a.question_id = numbered.id) as seen
      from numbered
     where numbered.set_no = $4
     order by numbered.id
"""


async def set_board(uid: int, level: str):
    """Per-set progress at this level, one row per set, in order."""
    conn = _require_pool()
    return await conn.fetch(_SET_BOARD_SQL, uid, level, SET_SIZE)


async def quiz_set(uid: int, level: str) -> dict | None:
    """The student's current set of five, or None once the level is finished.

    A set is finished when all five of its questions have been attempted. The
    caller serves `remaining[0]`, which is the first question of the set this
    student has not answered yet, so a student who stops mid-set resumes it.
    """
    board = await set_board(uid, level)
    if not board:
        return None
    current = next((row for row in board if row["answered"] < row["size"]), None)
    if current is None:
        return None

    conn = _require_pool()
    rows = await conn.fetch(_SET_QUESTIONS_SQL, uid, level, SET_SIZE,
                            current["set_no"])
    return {
        "set_no": current["set_no"],
        "number": current["set_no"] + 1,
        "size": current["size"],
        "answered": current["answered"],
        "correct": current["correct"],
        "total_sets": len(board),
        "questions": rows,
        "remaining": [row for row in rows if not row["seen"]],
    }


async def set_no_for(level: str, qid: int) -> int:
    """Which set a question belongs to. Derived from id order, so it holds even if
    the id sequence has gaps."""
    conn = _require_pool()
    return await conn.fetchval(
        """select ((select count(*) from questions
                     where level = $1 and id <= $2) - 1) / $3::int""",
        level, qid, SET_SIZE,
    )


async def set_score(uid: int, level: str, set_no: int):
    """How one set went: ``(size, answered, correct, total_sets, total_questions)``."""
    conn = _require_pool()
    return await conn.fetchrow(
        """with numbered as (
             select id, (row_number() over (order by id) - 1) / $3::int as set_no
               from questions
              where level = $2)
           select
             (select count(*) from numbered) as total_questions,
             (select count(distinct set_no) from numbered) as total_sets,
             count(*) as size,
             count(*) filter (where exists (select 1 from attempts a
                              where a.user_id = $1 and a.question_id = numbered.id))
               as answered,
             count(*) filter (where exists (select 1 from attempts a
                              where a.user_id = $1 and a.question_id = numbered.id
                                and a.correct))
               as correct
             from numbered
            where numbered.set_no = $4""",
        uid, level, SET_SIZE, set_no,
    )


async def stats(uid: int, level: str):
    """Marker A: every answer at this level, split by topic and by tag.

    Returns rows of ``(topic, tag, answered, correct)``. The tag comes from the
    question because `attempts` does not carry it.
    """
    conn = _require_pool()
    return await conn.fetch(
        """select a.topic,
                  coalesce(q.tag, '') as tag,
                  count(*) as answered,
                  count(*) filter (where a.correct) as correct
             from attempts a
             join questions q on q.id = a.question_id
            where a.user_id = $1 and a.level = $2
            group by a.topic, coalesce(q.tag, '')""",
        uid, level,
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


async def all_users() -> list[int]:
    """Everyone who has ever sent /start and is still reachable."""
    conn = _require_pool()
    rows = await conn.fetch(
        "select telegram_id from users where active order by telegram_id")
    return [r["telegram_id"] for r in rows]


async def enrol_everyone(tid: int) -> int:
    """Put every active user into a tournament in one statement.

    Entry is automatic by design: a student who has used /start should not have to
    remember to opt in to a competition they are already answering questions for.
    Returns how many were actually added.
    """
    conn = _require_pool()
    rows = await conn.fetch(
        """insert into tournament_points (tournament_id, user_id)
           select $1, telegram_id from users where active
           on conflict (tournament_id, user_id) do nothing
           returning user_id""",
        tid,
    )
    return len(rows)


async def tournament_mark(uid: int):
    """Marker B: this student's tournament-window score, and the board size.

    Deliberately narrow: it reports points and rank, never how many questions
    anyone answered, so a student's activity is not visible to other students.
    """
    conn = _require_pool()
    t = await active_tournament()
    if not t:
        return None
    return await conn.fetchrow(
        """select
             coalesce((select points from tournament_points
                        where tournament_id = $2 and user_id = $1), 0) as points,
             (select count(*) from tournament_points
               where tournament_id = $2) as entrants""",
        uid, t["id"],
    )


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

    Belts and braces: the fixed sets mean a question comes round once anyway, but
    without the tournament_answers uniqueness a student could still farm the board
    by re-answering one they already know.
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
