"""All Postgres access.

Conventions:
  * every statement is parameterised ($1, $2 ...) — no caller data is ever
    interpolated into SQL;
  * functions take plain ints/strings and return asyncpg Records, so the handler
    layer never sees a connection.
"""
from __future__ import annotations

from collections.abc import Collection

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


#: A set is a rolling block of this many answers. There is no stored membership:
#: a set is simply the student's Nth group of five answers at a level. That is
#: what lets `pick_question` choose freely across topics — the old fixed blocks
#: of five in id order gave every student the same set 1, which made the scores
#: comparable but meant nothing ever revisited a topic a student was failing.
SET_SIZE = 5

#: How far through a level a student is, and how big the level is, in one round
#: trip. Counts distinct *questions*, not attempt rows: the same question can be
#: served twice before either card is answered (a weekly push and a /quizme),
#: and a set must not shrink because a student answered both.
_SET_COUNTS_SQL = """
    select (select count(*) from questions where level = $2) as total,
           (select count(distinct question_id) from attempts
             where user_id = $1 and level = $2) as answered
"""

#: The next question to serve. One statement, because the ranking needs the
#: student's whole answer history at this level and a round trip per topic would
#: be 18 of them on the post-MBBS bank.
#:
#: `history` collapses to one row per question (latest answer wins) so the
#: numbering here agrees with `answered_count`; `recent` turns it into a
#: recency rank; `partial_set` is the tail of that — the answers already in the
#: set being built, which is the only sense in which a set has members.
_PICK_SQL = """
    -- Ordered by each question's FIRST attempt, not its latest. A /review
    -- answer is always a later attempt, so collapsing on the latest would
    -- re-date an old question into the set being built and push a genuine
    -- member out of it.
    with history as (
      select distinct on (question_id) question_id, id
        from attempts
       where user_id = $1 and level = $2
       order by question_id, id asc
    ),
    recent as (
      select question_id,
             row_number() over (order by id desc) as recency,
             count(*)     over ()                 as answered
        from history
    ),
    partial_set as (
      select q.topic, q.tag
        from recent r
        join questions q on q.id = r.question_id
       where r.recency <= r.answered % $3::int
    ),
    -- Laplace-smoothed accuracy, (correct + 1) / (answered + 2). The smoothing
    -- is what keeps one unlucky answer from branding a topic the student's
    -- weakest, and it puts an untouched topic on exactly 0.5, which is why the
    -- coalesce below is mid-range and not 0.0: a topic nobody has tried should
    -- rank ahead of one the student has mastered but behind one they are
    -- failing, so a student still meets new material.
    -- One vote per question, and it is the student's LATEST answer that says
    -- where they stand: aggregating raw attempt rows let a /review answer count
    -- twice and kept a topic they have since fixed looking weak.
    topic_accuracy as (
      select topic,
             (count(*) filter (where correct) + 1.0) / (count(*) + 2.0) as accuracy
        from (select distinct on (question_id) question_id, topic, correct
                from attempts
               where user_id = $1 and level = $2
               order by question_id, id desc) latest
       group by topic
    )
    select q.*
      from questions q
      left join topic_accuracy ta on ta.topic = q.topic
     where q.level = $2
       -- Never repeat a question. Unlike the old selector this counts any
       -- attempt, not just a correct one: a set is five *new* questions, so a
       -- miss is revisited through its topic's weight, not by re-serving it.
       and not exists (select 1 from attempts a
                        where a.user_id = $1 and a.question_id = q.id)
       -- Questions already handed out this round but not yet answered. The
       -- Monday push draws five in a row with nothing recorded in between, so
       -- without this every call sees identical history and the same top-ranked
       -- topic, and the student is sent the same question up to five times.
       and not (q.id = any($4::int[]))
     -- Lowest score wins. The weights are spaced so each term outvotes
     -- everything under it (4 > 1 + 1, 1 > any accuracy, which is always < 1),
     -- so this reads as one number but ranks strictly by priority: spread the
     -- set across topics first, then across tags, then lean on weak topics.
     order by case when exists (select 1 from partial_set p
                                 where p.topic = q.topic)
                   then 4.0 else 0.0 end
            + case when q.tag is not null
                    and exists (select 1 from partial_set p where p.tag = q.tag)
                   then 1.0 else 0.0 end
            + coalesce(ta.accuracy, 0.5),
     -- Last, so two students with the same history do not walk an identical
     -- path through the bank.
              random()
     limit 1
"""

#: How the set that just closed went. Collapsed on each question's FIRST attempt
#: for the same reason as _PICK_SQL: a /review answer must not re-date an old
#: question into the window and evict one of the five that genuinely belong to
#: the set. The score is therefore how the student did on those five the first
#: time they met them, which is what "your set score" means.
_LAST_SET_SQL = """
    with history as (
      select distinct on (question_id) question_id, id, correct
        from attempts
       where user_id = $1 and level = $2
       order by question_id, id asc
    ),
    recent as (
      select correct,
             row_number() over (order by id desc) as recency,
             count(*)     over ()                 as answered
        from history
    )
    select coalesce(max(answered), 0)      as answered,
           count(*) filter (where correct) as correct
      from recent
     where recency <= $3::int
"""


async def answered_count(uid: int, level: str) -> int:
    """How many distinct questions this student has attempted at this level."""
    conn = _require_pool()
    return await conn.fetchval(
        """select count(distinct question_id) from attempts
            where user_id = $1 and level = $2""",
        uid, level,
    )


async def level_total(level: str) -> int:
    """How many questions exist at this level."""
    conn = _require_pool()
    return await conn.fetchval(
        "select count(*) from questions where level = $1", level)


async def current_set(uid: int, level: str) -> dict | None:
    """Where this student is in their rolling set of five, or None when the level
    is finished.

    There is nothing stored to resume: the set is derived from how many questions
    have been answered, so stopping mid-set and coming back next week lands on
    the same set with the same count, and a bank that grows under a student only
    moves `total_sets`.
    """
    conn = _require_pool()
    row = await conn.fetchrow(_SET_COUNTS_SQL, uid, level)
    answered, total = row["answered"], row["total"]
    # An empty bank reports None too: there is nothing to answer, which the
    # caller already has to handle separately to say so plainly.
    if total == 0 or answered >= total:
        return None
    # The last set at a level is short whenever the bank is not a multiple of
    # five - 103 clinical questions is twenty fives and then a three - so `size`
    # is the real size of *this* set, not the constant. Reporting five made the
    # card read "question 3 of 5" for a set holding three questions.
    done_sets = answered // SET_SIZE
    return {
        "number": done_sets + 1,
        "answered_in_set": answered % SET_SIZE,
        "size": min(SET_SIZE, total - done_sets * SET_SIZE),
        "total_sets": (total + SET_SIZE - 1) // SET_SIZE,   # ceil, no float
    }


async def pick_question(uid: int, level: str, exclude: Collection[int] = ()):
    """The next question for this student, or None once the level is exhausted.

    Always a question they have never attempted, ranked for variety first and
    weakness second — see _PICK_SQL for the weights. The randomised tiebreak
    means two students at the same point do not get the same question, so this
    is deliberately *not* a shared benchmark: compare students with `stats` or
    the tournament, not by set number.

    `exclude` is for a caller that builds several questions before any of them
    is answered - the Monday push does exactly that. Ranking reads `attempts`,
    so five calls in a row would otherwise see identical history and hand back
    the same question five times.
    """
    conn = _require_pool()
    return await conn.fetchrow(_PICK_SQL, uid, level, SET_SIZE,
                               [int(qid) for qid in exclude])


async def last_set_score(uid: int, level: str) -> dict | None:
    """How the set that just closed went, or None before the first five answers.

    Call it when `current_set` has wrapped to ``answered_in_set == 0``, which is
    the only moment the most recent five answers are exactly one set. Mid-set it
    still answers, but with a window that straddles two sets, so the caller
    decides when to report rather than this deciding for it.
    """
    conn = _require_pool()
    row = await conn.fetchrow(_LAST_SET_SQL, uid, level, SET_SIZE)
    answered = row["answered"]
    if answered < SET_SIZE:
        return None
    return {"number": answered // SET_SIZE, "size": SET_SIZE,
            "correct": row["correct"]}


#: The review pile: questions whose *latest* answer at this level was wrong.
#:
#: "Latest answer wrong" rather than "never answered correctly" (which is how
#: `progress` defines unfinished) because a revision tool should track what the
#: student knows now. A question they once got right and have since forgotten
#: belongs back in the pile, and a question they missed and later nailed does
#: not. The two definitions therefore disagree on a right-then-wrong question,
#: deliberately: `progress` calls it done, /review calls it due.
#:
#: `distinct on` picks each question's newest attempt, and the ordering is that
#: attempt's id, so the pile is stable across calls — unlike `pick_question`,
#: which randomises on purpose.
_WRONG_SQL = """
    with latest as (
      select distinct on (question_id) question_id, id, correct
        from attempts
       where user_id = $1 and level = $2
       order by question_id, id desc
    )
    select q.*
      from latest
      join questions q on q.id = latest.question_id
     where not latest.correct
     order by latest.id desc
     limit $3::int
"""

_WRONG_COUNT_SQL = """
    with latest as (
      select distinct on (question_id) question_id, correct
        from attempts
       where user_id = $1 and level = $2
       order by question_id, id desc
    )
    select count(*) from latest where not correct
"""


async def wrong_questions(uid: int, level: str, limit: int = SET_SIZE):
    """Questions this student got wrong and has not since got right, newest miss
    first, capped at `limit`. Full question rows, so the caller renders them with
    the ordinary question card. Empty list when there is nothing to review."""
    conn = _require_pool()
    return await conn.fetch(_WRONG_SQL, uid, level, limit)


async def wrong_count(uid: int, level: str) -> int:
    """How many questions are waiting in the review pile, without fetching them,
    so a handler can offer or decline /review in one round trip."""
    conn = _require_pool()
    return await conn.fetchval(_WRONG_COUNT_SQL, uid, level)


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


# --------------------------------------------------------------- note delivery
# What a student has already been handed. The sheets themselves are files on
# disk (bot/resources.py scans resources/notes), so only the code is stored,
# and a code is unique only *within* a level, which is why every row carries the
# level and the kind. `resources.unsent` takes these codes and does the rest.


async def record_notes_sent(uid: int, level: str,
                            sheets: list[tuple[str, str]]) -> None:
    """Remember that these sheets went out. `sheets` is [(tier, code), ...].

    One insert, not a loop: the monthly drop sends up to 21 sheets to every
    subscriber, and a round trip each would multiply the fan-out. `on conflict
    do nothing` covers both ways a pair can repeat, a row already in the table
    and the same pair twice inside `sheets` (speculative insertion sees the
    earlier row from its own command) — a test pins the second case, because it
    is the one that reads as if it should fail.

    The casts are load-bearing: a bare $1 in the select list gives Postgres
    nothing to infer the type from, so it settles on text and the insert then
    fails against the bigint key.
    """
    if not sheets:
        return
    conn = _require_pool()
    await conn.execute(
        """insert into note_deliveries (user_id, level, tier, code)
           select $1::bigint, $2::text, tier, code
             from unnest($3::text[], $4::text[]) as sent (tier, code)
           on conflict do nothing""",
        uid, level, [tier for tier, _ in sheets], [code for _, code in sheets],
    )


async def sent_note_codes(uid: int, level: str, tier: str) -> set[str]:
    """Codes of one kind already sent to this student at this level."""
    conn = _require_pool()
    rows = await conn.fetch(
        """select code from note_deliveries
            where user_id = $1 and level = $2 and tier = $3""",
        uid, level, tier,
    )
    return {row["code"] for row in rows}


async def notes_delivered(uid: int, level: str) -> dict[str, set[str]]:
    """Both kinds at once: ``{"a": {...}, "b": {...}}``.

    One query, because deciding what to send next means knowing whether tier A
    is finished *and* what of tier B has gone — two round trips for one
    decision. Both keys are always present, so a caller can index straight in.
    """
    conn = _require_pool()
    rows = await conn.fetch(
        """select tier, code from note_deliveries
            where user_id = $1 and level = $2""",
        uid, level,
    )
    delivered: dict[str, set[str]] = {"a": set(), "b": set()}
    for row in rows:
        delivered.setdefault(row["tier"], set()).add(row["code"])
    return delivered


async def reset_notes(uid: int, level: str) -> int:
    """Forget one level's delivery history, returning how many rows went.

    Scoped to a level on purpose: a student who has finished the preclinical
    sheets and wants another pass should not lose the clinical history they
    built up while their level was switched.
    """
    conn = _require_pool()
    rows = await conn.fetch(
        """delete from note_deliveries
            where user_id = $1 and level = $2
            returning code""",
        uid, level,
    )
    return len(rows)
