"""The quiz mechanics against real PostgreSQL: sets, selection, review.

Sets used to be fixed blocks of five in id order, so every student's set 1 was
the same five questions. They are now a **rolling block of five answers** with
nothing stored: the Nth group of five a student answers at a level is their set
N, and `db.pick_question` fills it by spreading across topics and tags and
leaning on whatever they are getting wrong. That trade is deliberate — the
scores are no longer a shared benchmark — and the tests below encode the new
behaviour where the old ones encoded the blocks.

The ranking lives in one `order by` of weighted terms plus `random()`, so it
cannot be proved by an in-memory stand-in: the bank here is shaped to make a
selector that ignored any one of those terms fail.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import db  # noqa: E402

SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TEST_DATABASE_URL to run the live-database tests")

TABLES = ("note_deliveries", "tournament_answers", "tournament_points",
          "tournaments", "attempts", "notes", "questions", "users")

#: Pre-clinical: five topics of eight, inserted topic by topic so the ids are
#: blocked. The blocking is the point — a selector that walked ids, as the fixed
#: sets did, would spend a whole set inside "Alpha" — so the diversity tests
#: have something that can actually fail.
PRECLIN_TOPICS = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")
PRECLIN_PER_TOPIC = 8
PRECLIN_TOTAL = len(PRECLIN_TOPICS) * PRECLIN_PER_TOPIC

#: Clinical: a short tail. Seven questions is one full set and a set of two,
#: which is the rounding `total_sets` has to get right.
CLIN = (("Iota", 4), ("Kappa", 3))
CLIN_TOTAL = sum(count for _, count in CLIN)

#: Post-MBBS: two topics, each with two tags used twice. Fewer topics than
#: SET_SIZE forces the selector to repeat a topic inside a set, which is the
#: only situation in which the tag term can decide anything.
POSTMBBS = (("Tee", "T1"), ("Tee", "T1"), ("Tee", "T2"), ("Tee", "T2"),
            ("Vee", "V1"), ("Vee", "V1"), ("Vee", "V2"), ("Vee", "V2"))

#: Draws per statistical assertion. The ranking is strict — the weights are
#: spaced so a higher-priority term cannot be outvoted — so the preferred topic
#: should win every draw; the bound is slack enough that it is not a coin flip
#: dressed up as a test, and tight enough to fail if the weighting inverts.
DRAWS = 40
MOST = DRAWS * 0.8


async def _insert(conn, level: str, topic: str, tag: str, text: str) -> None:
    await conn.execute(
        """insert into questions (level, topic, tag, text, options, correct_idx,
                                  explanation)
           values ($1, $2, $3, $4, $5::jsonb, 0, 'because')""",
        level, topic, tag, text, json.dumps(["a", "b", "c", "d", "e"]),
    )


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
    for topic in PRECLIN_TOPICS:
        for n in range(1, PRECLIN_PER_TOPIC + 1):
            await _insert(admin, "preclin", topic,
                          f"Physiology | {topic.lower()}", f"{topic} {n}")
    for topic, count in CLIN:
        for n in range(1, count + 1):
            await _insert(admin, "clin", topic, f"Clinical | {topic.lower()}",
                          f"{topic} {n}")
    for n, (topic, tag) in enumerate(POSTMBBS, start=1):
        await _insert(admin, "postmbbs", topic, tag, f"{topic} {n}")
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
    """Answer one named question, for the tests that care which one it is."""
    row = await db.pool.fetchrow("select * from questions where text = $1", text)
    await db.record_attempt(uid, row, 0 if correct else 1, correct, "practice",
                            msg_id)


async def answer_next(uid: int, level: str, *, correct: bool, msg_id: int):
    """Serve and answer whatever the selector offers, the way a student does."""
    question = await db.pick_question(uid, level)
    assert question is not None, f"{level} ran dry"
    await db.record_attempt(uid, question, 0 if correct else 1, correct,
                            "practice", msg_id)
    return question


async def seed_topic(uid: int, level: str, topic: str, answered: int, *,
                     correct: bool) -> None:
    """Answer the first `answered` questions of one topic, all right or all wrong.

    Written straight into `attempts` instead of through the selector so a trial
    can be rebuilt dozens of times cheaply. msg_id borrows the question id,
    which is unique per user and all the dedupe index needs.
    """
    await db.pool.execute(
        """insert into attempts (user_id, question_id, level, topic, chosen_idx,
                                 correct, mode, msg_id)
           select $1, q.id, q.level, q.topic, 0, $4, 'practice', q.id
             from (select id, level, topic from questions
                    where level = $2 and topic = $3
                    order by id
                    limit $5) q""",
        uid, level, topic, correct, answered,
    )


async def draw_topics(uid: int, level: str,
                      history: dict[str, tuple[int, bool]]) -> Counter:
    """Which topic the selector reaches for, over many independent draws.

    The history is rebuilt before every draw because `pick_question` never
    repeats a question: without the reset the pool would drain and the later
    draws would measure what was left rather than what is preferred.
    """
    counts: Counter = Counter()
    for _ in range(DRAWS):
        await db.pool.execute("truncate attempts")
        for topic, (answered, correct) in history.items():
            await seed_topic(uid, level, topic, answered, correct=correct)
        question = await db.pick_question(uid, level)
        assert question is not None
        counts[question["topic"]] += 1
    return counts


# ------------------------------------------------------------- where am I


async def test_a_fresh_student_starts_on_set_one(bank):
    assert await db.answered_count(1, "preclin") == 0
    assert await db.level_total("preclin") == PRECLIN_TOTAL
    assert await db.current_set(1, "preclin") == {
        "number": 1,
        "answered_in_set": 0,
        "size": db.SET_SIZE,
        "total_sets": PRECLIN_TOTAL // db.SET_SIZE,
    }


async def test_the_set_counter_rolls_over_every_five_answers(bank):
    """A set is five answers, and the sixth starts the next one. Nothing is
    stored, so stopping after two and coming back lands on the same count."""
    seen = []
    for msg_id in range(1, db.SET_SIZE + 2):
        await answer_next(1, "preclin", correct=True, msg_id=msg_id)
        current = await db.current_set(1, "preclin")
        seen.append((current["number"], current["answered_in_set"]))

    assert seen == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 0), (2, 1)]
    assert await db.answered_count(1, "preclin") == db.SET_SIZE + 1


async def test_total_sets_rounds_a_short_tail_up(bank):
    """Seven questions is a set of five and a set of two, not one set."""
    assert await db.level_total("clin") == CLIN_TOTAL
    assert (await db.current_set(1, "clin"))["total_sets"] == 2


async def test_counts_are_scoped_to_one_level(bank):
    await answer_next(1, "clin", correct=True, msg_id=1)
    assert await db.answered_count(1, "clin") == 1
    assert await db.answered_count(1, "preclin") == 0
    assert (await db.current_set(1, "preclin"))["answered_in_set"] == 0


async def test_current_set_is_none_once_the_level_is_answered(bank):
    for msg_id in range(1, CLIN_TOTAL + 1):
        await answer_next(1, "clin", correct=True, msg_id=msg_id)

    assert await db.answered_count(1, "clin") == CLIN_TOTAL
    assert await db.current_set(1, "clin") is None
    assert await db.pick_question(1, "clin") is None
    fresh, total = await db.progress(1, "clin")
    assert (fresh, total) == (0, CLIN_TOTAL)
    # Finishing one level does not finish another.
    assert (await db.current_set(1, "preclin"))["number"] == 1


async def test_an_empty_bank_reports_no_set_rather_than_set_one(bank):
    """The clinical bank was empty at release and the handler has to say so
    plainly rather than offering a set it cannot serve. Any level with nothing
    loaded behaves the same way, so this uses one the fixture leaves out."""
    assert await db.level_total("unloaded") == 0
    assert await db.current_set(1, "unloaded") is None
    assert await db.pick_question(1, "unloaded") is None
    assert await db.last_set_score(1, "unloaded") is None


# ---------------------------------------------------------- the set score


async def test_last_set_score_needs_a_full_set(bank):
    assert await db.last_set_score(1, "preclin") is None
    for msg_id in range(1, db.SET_SIZE):
        await answer_next(1, "preclin", correct=True, msg_id=msg_id)
    assert await db.last_set_score(1, "preclin") is None, "four is not a set"


async def test_last_set_score_counts_the_five_that_just_closed(bank):
    for msg_id, correct in enumerate([True, False, True, False, True], start=1):
        await answer_next(1, "preclin", correct=correct, msg_id=msg_id)
    assert await db.last_set_score(1, "preclin") == {
        "number": 1, "size": db.SET_SIZE, "correct": 3}

    # The second set scores the new five, not all ten.
    for msg_id, correct in enumerate([False, False, True, False, False], start=6):
        await answer_next(1, "preclin", correct=correct, msg_id=msg_id)
    assert await db.last_set_score(1, "preclin") == {
        "number": 2, "size": db.SET_SIZE, "correct": 1}


# --------------------------------------------------------- what gets served


async def test_pick_question_never_repeats_and_then_runs_out(bank):
    served = []
    for msg_id in range(1, PRECLIN_TOTAL + 1):
        question = await answer_next(1, "preclin", correct=True, msg_id=msg_id)
        assert question["level"] == "preclin", "never another level's bank"
        served.append(question["id"])

    assert len(set(served)) == PRECLIN_TOTAL, "a question came round twice"
    assert await db.pick_question(1, "preclin") is None


async def test_a_miss_is_not_re_served_inside_the_set(bank):
    """Wrong answers used to stay in the pool. They no longer do: a set is five
    questions the student has not seen, and a miss comes back as weight on its
    topic instead."""
    missed = await answer_next(1, "preclin", correct=False, msg_id=1)
    later = [await answer_next(1, "preclin", correct=False, msg_id=m)
             for m in range(2, db.SET_SIZE + 1)]
    assert missed["id"] not in [q["id"] for q in later]


async def test_pick_question_spreads_topics_across_a_set(bank):
    picked = [await answer_next(1, "preclin", correct=True, msg_id=msg_id)
              for msg_id in range(1, db.SET_SIZE + 1)]
    topics = [question["topic"] for question in picked]
    assert len(set(topics)) == db.SET_SIZE, topics

    # What the fixed blocks did: the first five in id order, which in this bank
    # is five questions out of one topic.
    naive = await db.pool.fetch(
        "select topic from questions where level = 'preclin' order by id limit $1",
        db.SET_SIZE)
    assert len({row["topic"] for row in naive}) < len(set(topics))


async def test_pick_question_spreads_tags_once_the_topics_repeat(bank):
    """Post-MBBS here has two topics and four tags, so after two picks every
    further pick has to repeat a topic. That is when the tag term decides."""
    tags = len({tag for _, tag in POSTMBBS})
    picked = [await answer_next(1, "postmbbs", correct=True, msg_id=msg_id)
              for msg_id in range(1, tags + 1)]

    assert len({question["topic"] for question in picked}) == 2
    assert len({question["tag"] for question in picked}) == tags, \
        [question["tag"] for question in picked]

    naive = await db.pool.fetch(
        "select tag from questions where level = 'postmbbs' order by id limit $1",
        tags)
    assert len({row["tag"] for row in naive}) < tags


async def test_pick_question_leans_on_the_topic_being_missed(bank):
    """Five answered in each topic, so ten in all: the partial set is empty
    (10 % 5 == 0) and the only thing left to rank by is how it is going."""
    counts = await draw_topics(1, "preclin",
                               {"Alpha": (5, False), "Beta": (5, True)})

    assert counts["Alpha"] >= MOST, counts
    assert counts["Beta"] == 0, "a topic the student has mastered comes last"


async def test_an_untouched_topic_ranks_between_weak_and_mastered(bank):
    """The neutral-middle rule. A new topic must not beat one the student is
    failing (the test above shows Alpha winning over three untouched topics) and
    must not lose to one they have mastered, or they never meet new material.

    Alpha is cleared out here — all eight answered, so it has nothing left to
    serve — which leaves the choice between Beta, which the student is good at,
    and the three topics they have never touched. 8 + 7 answers keeps the
    partial set empty again.
    """
    counts = await draw_topics(1, "preclin",
                               {"Alpha": (8, False), "Beta": (7, True)})

    assert counts["Beta"] == 0, counts
    assert set(counts) <= {"Gamma", "Delta", "Epsilon"}, counts
    assert len(counts) > 1, "the random tiebreak should still spread the draws"


# --------------------------------------------------------------- the review pile


async def test_review_pile_drops_a_question_once_it_is_got_right(bank):
    """The re-answer is recorded under `review`, which is the only mode that can
    re-answer a question: a second scoring answer is refused outright now (see
    test_a_second_card_for_one_question_is_not_recorded). This used to pass
    'practice' here, which no /review card ever sends."""
    missed = await answer_next(1, "preclin", correct=False, msg_id=1)
    assert await db.wrong_count(1, "preclin") == 1
    assert [row["id"] for row in await db.wrong_questions(1, "preclin")] == \
        [missed["id"]]

    assert await db.record_attempt(1, missed, 0, True, "review", 2) is True
    assert await db.wrong_count(1, "preclin") == 0
    assert await db.wrong_questions(1, "preclin") == []


async def test_review_pile_keeps_a_question_only_ever_missed(bank):
    missed = [await answer_next(1, "preclin", correct=False, msg_id=msg_id)
              for msg_id in (1, 2)]
    got_right = await answer_next(1, "preclin", correct=True, msg_id=3)

    pile = {row["id"] for row in await db.wrong_questions(1, "preclin")}
    assert pile == {question["id"] for question in missed}
    assert got_right["id"] not in pile


async def test_a_question_got_right_and_then_missed_comes_back(bank):
    """The semantics worth pinning: the *latest* answer decides, not whether the
    student was ever right. `progress` reads it the other way and still counts
    the question as done, so the two numbers disagree here on purpose.

    The second answer is a `review` one, the only kind that can re-answer a
    question - so the pile is still driven by the latest answer whatever mode it
    arrived in."""
    question = await answer_next(1, "preclin", correct=True, msg_id=1)
    assert await db.wrong_count(1, "preclin") == 0

    await db.record_attempt(1, question, 1, False, "review", 2)
    assert [row["id"] for row in await db.wrong_questions(1, "preclin")] == \
        [question["id"]]
    fresh, _ = await db.progress(1, "preclin")
    assert fresh == PRECLIN_TOTAL - 1, "progress still calls it finished"


async def test_review_pile_is_newest_miss_first_and_stable(bank):
    missed = [await answer_next(1, "preclin", correct=False, msg_id=msg_id)
              for msg_id in (1, 2, 3)]
    order = [row["id"] for row in await db.wrong_questions(1, "preclin")]

    assert order == [question["id"] for question in reversed(missed)]
    # Unlike pick_question, nothing here is randomised.
    assert order == [row["id"] for row in await db.wrong_questions(1, "preclin")]


async def test_review_pile_is_level_scoped(bank):
    await answer_next(1, "preclin", correct=False, msg_id=1)
    await answer_next(1, "clin", correct=False, msg_id=2)

    assert await db.wrong_count(1, "preclin") == 1
    assert await db.wrong_count(1, "clin") == 1
    assert {row["level"] for row in await db.wrong_questions(1, "clin")} == {"clin"}


async def test_a_second_card_for_one_question_corrupts_nothing(bank):
    """Two live cards for one question, which is reachable: a /quizme card left
    unanswered is invisible to `pick_question`, so the Monday push can serve the
    same question before the first card is answered.

    Answering both used to write two attempt rows, and all three reads below
    went wrong at once - the question re-entered /review on the second (wrong)
    answer, the practice streak reset, and db.stats counted two answers for one
    question. Only the first card is recorded now, so none of them move.
    """
    first = await answer_next(1, "preclin", correct=True, msg_id=1)
    second = await answer_next(1, "preclin", correct=True, msg_id=2)
    assert await db.practice_streak(1) == 2

    # The Monday card for `first`, answered wrong, after the /quizme card.
    assert await db.record_attempt(1, first, 1, False, "weekly", 3) is False

    assert await db.wrong_count(1, "preclin") == 0, \
        "a question answered right must not re-enter /review"
    assert await db.practice_streak(1) == 2, "the streak must not reset"
    assert sum(row["answered"] for row in await db.stats(1, "preclin")) == 2, \
        "two questions answered, not three attempts"
    assert await db.answered_count(1, "preclin") == 2
    assert (await db.current_set(1, "preclin"))["answered_in_set"] == 2
    assert {row["id"] for row in await db.pool.fetch(
        "select question_id as id from attempts where user_id = 1")} == \
        {first["id"], second["id"]}


async def test_a_review_answer_still_counts_in_stats_and_moves_the_pile(bank):
    """What the uniqueness deliberately does *not* change. /review re-answers a
    question on purpose, those rows are exempt, and both reads treat them as
    real answers: the pile tracks what the student knows now, and db.stats
    counts every answer at the level."""
    missed = await answer_next(1, "preclin", correct=False, msg_id=1)
    assert await db.wrong_count(1, "preclin") == 1
    assert sum(row["answered"] for row in await db.stats(1, "preclin")) == 1

    assert await db.record_attempt(1, missed, 0, True, "review", 2) is True
    assert await db.wrong_count(1, "preclin") == 0, "the pile follows the latest"
    assert sum(row["answered"] for row in await db.stats(1, "preclin")) == 2

    # And a set is not advanced by it: answered_count counts distinct questions.
    assert await db.answered_count(1, "preclin") == 1


async def test_review_pile_respects_its_limit_and_agrees_with_the_count(bank):
    misses = db.SET_SIZE + 2
    for msg_id in range(1, misses + 1):
        await answer_next(1, "preclin", correct=False, msg_id=msg_id)

    assert await db.wrong_count(1, "preclin") == misses
    assert len(await db.wrong_questions(1, "preclin")) == db.SET_SIZE, "the default"
    assert len(await db.wrong_questions(1, "preclin", limit=2)) == 2
    assert len(await db.wrong_questions(1, "preclin", limit=1000)) == misses


async def test_an_empty_review_pile_is_empty_not_an_error(bank):
    assert await db.wrong_questions(1, "preclin") == []
    assert await db.wrong_count(1, "preclin") == 0

    await answer_next(1, "preclin", correct=True, msg_id=1)
    assert await db.wrong_questions(1, "preclin") == []
    assert await db.wrong_count(1, "preclin") == 0


# ---------------------------------------------------------------- marker A


async def test_stats_split_by_topic_and_tag(bank):
    await answer(1, "Alpha 1", correct=True, msg_id=1)
    await answer(1, "Alpha 2", correct=False, msg_id=2)
    await answer(1, "Beta 1", correct=True, msg_id=3)

    rows = await db.stats(1, "preclin")
    by_topic = {row["topic"]: (row["answered"], row["correct"]) for row in rows}
    assert by_topic == {"Alpha": (2, 1), "Beta": (1, 1)}
    assert all(row["tag"] for row in rows), "the tag travels with the answer"


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
    qid = await bank.pool.fetchval(
        "select id from questions where text = 'Alpha 1'")
    tid = await db.start_tournament(days=14)
    await db.enrol_everyone(tid)

    assert await db.award_point(1, qid) is True
    assert await db.award_point(1, qid) is False, "no farming the same question"
    assert (await db.tournament_mark(1))["points"] == 1

    await db.end_tournament(tid)
    assert await db.active_tournament() is None
    assert await db.tournament_mark(1) is None, "marker B stops with the tournament"
