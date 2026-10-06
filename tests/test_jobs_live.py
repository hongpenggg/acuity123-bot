"""The two scheduled fan-outs, against a real database.

These had **no coverage at all**, which is how two bugs reached a live user: the
fortnightly drop sent six PDFs at once instead of one, and the Monday push told
an admin "Sent 15 question(s)." for a five-question set, because the total was
summed across every subscriber while the sentence read as one student's set.

Both jobs loop over subscribers and resolve each one's level, so the thing worth
testing is that every subscriber gets the right thing for *their own* level, that
the reported numbers mean what they say, and that nothing is ever sent twice.

    TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/studybot_test \
        python -m pytest tests/test_jobs_live.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError

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
    from bot import db

    for table in TABLES:
        await db.pool.execute(f"truncate {table} cascade")
    yield


@pytest.fixture(autouse=True)
def instant_pacing(monkeypatch):
    """Pace nothing by default.

    The fan-outs space their sends per chat, and `sender.Pacer` is tested for
    that directly in tests/test_sender.py. Left at its real setting, a header
    plus five cards is three seconds per student here, which would add a minute
    to this module and tell us nothing these tests are about. The two tests that
    care about pacing install their own pacer over this one.
    """
    from bot import sender

    monkeypatch.setattr(sender, "PACER",
                        sender.Pacer(burst=1, interval=0.0,
                                     stream_burst=1, stream_interval=0.0))


class FakeBot:
    """Stands in for the Bot API edge only; everything above it is the real code."""

    def __init__(self, fail_documents=False):
        self.fail_documents = fail_documents
        self.per_user: dict[int, list[tuple[str, str]]] = {}

    async def send_message(self, uid, text, **kw):
        self.per_user.setdefault(uid, []).append(("msg", text))
        return SimpleNamespace(message_id=len(self.per_user[uid]))

    async def send_document(self, uid, document, **kw):
        if self.fail_documents:
            raise RuntimeError("upload failed")
        name = os.path.basename(str(getattr(document, "path", document)))
        self.per_user.setdefault(uid, []).append(("doc", name))
        return SimpleNamespace(message_id=len(self.per_user[uid]))

    def docs(self, uid):
        return [v for kind, v in self.per_user.get(uid, []) if kind == "doc"]

    def msgs(self, uid):
        return [v for kind, v in self.per_user.get(uid, []) if kind == "msg"]


async def _subscriber(uid, level, *, weekly=False, notes=False):
    from bot import db

    await db.upsert_user(uid, f"s{uid}")
    await db.set_level(uid, level)
    if weekly:
        await db.set_flag(uid, "weekly_sub", True)
    if notes:
        await db.set_flag(uid, "notes_sub", True)


async def _add_questions(level, topics, per_topic=5):
    from bot import db

    for topic in topics:
        for i in range(per_topic):
            await db.pool.execute(
                """insert into questions (level, topic, tag, text, options,
                                          correct_idx, explanation)
                   values ($1, $2, $3, $4, $5::jsonb, 0, 'because')""",
                level, topic, f"{topic} | t{i}", f"{topic} q{i}",
                json.dumps(["a", "b", "c", "d"]))


# ------------------------------------------------------------- weekly quiz


async def test_weekly_push_gives_each_subscriber_five_at_their_own_level():
    """Three subscribers on three levels. Each gets five questions from their own
    bank, and the summary explains the total rather than just stating it."""
    from bot import jobs

    await _add_questions("preclin", ["P1", "P2"])
    await _add_questions("clin", ["C1", "C2"])
    await _subscriber(1, "preclin", weekly=True)
    await _subscriber(2, "clin", weekly=True)
    await _subscriber(3, "preclin", weekly=False)      # not subscribed

    bot = FakeBot()
    result = await jobs.weekly_quiz(bot)

    # One lead message plus one card per question, for each subscriber.
    assert len(bot.msgs(1)) == jobs.SET_PER_PUSH + 1
    assert len(bot.msgs(2)) == jobs.SET_PER_PUSH + 1
    assert 3 not in bot.per_user, "a non-subscriber must not be pushed to"

    assert result == {"sent": 2 * jobs.SET_PER_PUSH, "reached": 2,
                      "subscribers": 2, "finished": 0}

    # The level each student is on is the bank they are served from.
    assert all("P" in body for body in bot.msgs(1)[1:])
    assert all("C" in body for body in bot.msgs(2)[1:])


async def test_the_weekly_push_never_repeats_a_question_within_one_set():
    """Nothing is answered during a push, so every pick_question call sees the
    same history. Without an exclude list the top-ranked topic's question came
    back up to five times under a "5 questions" heading."""
    from bot import db, jobs

    # Two topics, four questions each: a narrow pool, which is where the bug bit.
    await _add_questions("preclin", ["P1", "P2"], per_topic=4)
    await _subscriber(1, "preclin", weekly=True)
    # Give them history so the ranking has a clear favourite.
    first = await db.pool.fetch(
        "select * from questions where topic = 'P1' order by id limit 2")
    for row in first:
        await db.record_attempt(1, row, 1, False, "practice", row["id"])

    bot = FakeBot()
    await jobs.weekly_quiz(bot)

    served = await db.pool.fetch(
        "select question_id, count(*) as n from attempts where user_id = 1 "
        "and mode = 'weekly' group by question_id having count(*) > 1")
    cards = [b for b in bot.msgs(1)[1:]]
    assert len(cards) == jobs.SET_PER_PUSH
    assert len(set(cards)) == jobs.SET_PER_PUSH, "the same question was sent twice"
    assert served == []


async def test_a_subscriber_who_has_finished_is_told_and_unsubscribed():
    """Silence left them subscribed and waiting every Monday for something that
    was never coming."""
    from bot import db, jobs

    await _add_questions("preclin", ["P1"], per_topic=3)
    await _subscriber(1, "preclin", weekly=True)
    for row in await db.pool.fetch("select * from questions"):
        await db.record_attempt(1, row, 0, True, "practice", row["id"])

    bot = FakeBot()
    result = await jobs.weekly_quiz(bot)

    assert result == {"sent": 0, "reached": 0, "subscribers": 1, "finished": 1}
    assert "every Pre-Clinical question answered" in bot.msgs(1)[0]
    still_on = await db.pool.fetchval(
        "select weekly_sub from users where telegram_id = 1")
    assert still_on is False


async def test_weekly_push_does_not_repeat_a_question_the_student_has_seen():
    from bot import db, jobs

    await _add_questions("preclin", ["P1", "P2"])
    await _subscriber(1, "preclin", weekly=True)

    first = await jobs.weekly_quiz(FakeBot())
    second = await jobs.weekly_quiz(FakeBot())

    assert first["sent"] == second["sent"] == jobs.SET_PER_PUSH
    seen = await db.pool.fetch(
        "select question_id, count(*) as n from attempts where user_id = 1 "
        "group by question_id having count(*) > 1")
    assert seen == [], "the push served a question twice"


# -------------------------------------------------------- fortnightly notes


async def test_the_drop_sends_exactly_one_sheet_per_subscriber():
    """The bug a student actually hit: twelve PDFs arrived at once."""
    from bot import jobs

    await _subscriber(1, "preclin", notes=True)
    await _subscriber(2, "clin", notes=True)

    bot = FakeBot()
    result = await jobs.fortnightly_notes(bot)

    assert jobs.SHEETS_PER_DROP == 1
    assert len(bot.docs(1)) == 1, bot.docs(1)
    assert len(bot.docs(2)) == 1, bot.docs(2)
    assert result == {"sent": 2, "reached": 2, "subscribers": 2, "finished": 0}


async def test_the_drop_serves_each_subscribers_own_level():
    from bot import jobs

    await _subscriber(1, "preclin", notes=True)
    await _subscriber(2, "clin", notes=True)
    await _subscriber(3, "postmbbs", notes=True)

    bot = FakeBot()
    await jobs.fortnightly_notes(bot)

    assert bot.docs(1)[0].startswith("01_")
    assert bot.docs(2)[0].startswith("A01_")
    assert bot.docs(3)[0].startswith("A01_")


async def test_successive_drops_walk_the_catalogue_without_repeating():
    from bot import jobs

    await _subscriber(1, "preclin", notes=True)

    handed = []
    for _ in range(4):
        bot = FakeBot()
        await jobs.fortnightly_notes(bot)
        handed += bot.docs(1)

    assert len(handed) == 4
    assert len(set(handed)) == 4, "a sheet went out twice"
    assert handed == sorted(handed), "overview sheets should come in code order"


async def test_a_sheet_already_read_through_notes_is_not_pushed_again():
    """The drop and /notes share one delivery history."""
    from bot import db, jobs, resources

    await _subscriber(1, "preclin", notes=True)
    first = resources.overview("preclin")[0]
    await db.record_notes_sent(1, "preclin", [(first.tier, first.code)])

    bot = FakeBot()
    await jobs.fortnightly_notes(bot)

    assert bot.docs(1)[0] != first.path.name


async def test_a_failed_upload_degrades_to_a_link_and_still_counts():
    """`send_note` falls back to a GitHub link when the file cannot be uploaded,
    so the student does end up with the sheet and it is marked delivered. Getting
    this wrong the other way would re-send the same sheet every fortnight."""
    from bot import db, jobs

    await _subscriber(1, "preclin", notes=True)

    result = await jobs.fortnightly_notes(FakeBot(fail_documents=True))

    assert result["sent"] == 1
    assert await db.notes_delivered(1, "preclin") == {"a": {"01"}, "b": set()}


async def test_a_blocked_student_is_not_recorded_as_delivered():
    """They got nothing, so the sheet stays in their queue."""
    from aiogram.exceptions import TelegramForbiddenError

    from bot import db, jobs

    class Blocked(FakeBot):
        async def send_document(self, uid, document, **kw):
            raise TelegramForbiddenError(SimpleNamespace(), "bot was blocked")

        async def send_message(self, uid, text, **kw):
            raise TelegramForbiddenError(SimpleNamespace(), "bot was blocked")

    await _subscriber(1, "preclin", notes=True)

    result = await jobs.fortnightly_notes(Blocked())

    assert result["sent"] == 0 and result["reached"] == 0
    assert await db.notes_delivered(1, "preclin") == {"a": set(), "b": set()}
    active = await db.pool.fetchval(
        "select active from users where telegram_id = 1")
    assert active is False, "a blocked student should be deactivated"


async def test_a_subscriber_who_has_had_everything_is_congratulated_and_unsubscribed():
    from bot import db, jobs, resources

    await _subscriber(1, "preclin", notes=True)
    await db.record_notes_sent(
        1, "preclin", [(n.tier, n.code) for n in resources.all_for("preclin")])

    bot = FakeBot()
    result = await jobs.fortnightly_notes(bot)

    assert bot.docs(1) == []
    assert "completed the notes" in bot.msgs(1)[0]
    assert result["finished"] == 1
    still_on = await db.pool.fetchval(
        "select notes_sub from users where telegram_id = 1")
    assert still_on is False, "they would be pinged forever otherwise"


# ------------------------------------------------------- one run at a time


async def test_two_drops_at_once_send_one_sheet():
    """aiogram polls with handle_as_tasks=True, so /admin_notes_now landing on
    top of the 10:00 cron fire ran the whole fan-out twice. Both runs read
    db.notes_delivered before either reached db.record_notes_sent, so the
    subscriber got the same PDF twice."""
    from bot import db, jobs

    await _subscriber(1, "preclin", notes=True)

    bot = FakeBot()
    first, second = await asyncio.gather(jobs.fortnightly_notes(bot),
                                         jobs.fortnightly_notes(bot))

    assert len(bot.docs(1)) == 1, bot.docs(1)
    assert len(bot.msgs(1)) == 1, "one header, not two"
    assert await db.notes_delivered(1, "preclin") == {"a": {"01"}, "b": set()}
    # The second caller joins the run in flight and is handed its summary, so the
    # admin who tapped is told what actually went out. A zeroed result would read
    # as "nothing happened" and invite them to tap again.
    assert first == second == {"sent": 1, "reached": 1, "subscribers": 1,
                              "finished": 0}


async def test_two_monday_pushes_at_once_send_one_set():
    from bot import db, jobs

    await _add_questions("preclin", ["P1", "P2"])
    await _subscriber(1, "preclin", weekly=True)

    bot = FakeBot()
    first, second = await asyncio.gather(jobs.weekly_quiz(bot),
                                         jobs.weekly_quiz(bot))

    assert len(bot.msgs(1)) == jobs.SET_PER_PUSH + 1, bot.msgs(1)
    assert first == second
    assert first["sent"] == jobs.SET_PER_PUSH
    served = await db.pool.fetch(
        "select question_id from attempts where user_id = 1 and mode = 'weekly' "
        "group by question_id having count(*) > 1")
    assert served == []


async def test_two_closes_of_one_tournament_send_one_award_dm():
    """The 30-minute close_tournaments sweep and /admin_tournament_end are two
    call sites for the same close, and running both at once DM'd every winner
    twice and told the admins the table twice."""
    from bot import db, jobs

    await db.upsert_user(7, "winner")
    tid = await db.start_tournament(14)
    await db.enrol_everyone(tid)
    await db.pool.execute(
        "update tournament_points set points = 9 "
        "where tournament_id = $1 and user_id = 7", tid)

    bot = FakeBot()
    first, second = await asyncio.gather(jobs.finish_tournament(bot, tid),
                                         jobs.finish_tournament(bot, tid))

    assert len(bot.msgs(7)) == 1, bot.msgs(7)
    assert "finished #1" in bot.msgs(7)[0]
    assert len(bot.msgs(42)) == 1, "the admin table went out twice"
    assert first == second


async def test_a_later_run_is_a_real_run_not_a_refusal():
    """The guard is for overlap, not a lock: once the first run is finished the
    next tap must do its own work."""
    from bot import jobs

    await _subscriber(1, "preclin", notes=True)

    first = await jobs.fortnightly_notes(FakeBot())
    bot = FakeBot()
    second = await jobs.fortnightly_notes(bot)

    assert first["sent"] == 1
    assert second["sent"] == 1
    assert len(bot.docs(1)) == 1
    assert bot.docs(1)[0].startswith("02_"), "it moved on to the next sheet"


async def test_drain_lets_a_drop_in_flight_finish():
    """APScheduler's AsyncIOExecutor cancels running jobs on shutdown whatever
    `wait` says, so main.py drains first. Without it a deploy during the drop
    lands between the upload and db.record_notes_sent, and the student is sent
    the same sheet again next fortnight."""
    from bot import db, jobs

    class Slow(FakeBot):
        async def send_document(self, uid, document, **kw):
            await asyncio.sleep(0.05)
            return await super().send_document(uid, document, **kw)

    await _subscriber(1, "preclin", notes=True)

    bot = Slow()
    running = asyncio.ensure_future(jobs.fortnightly_notes(bot))
    await asyncio.sleep(0.01)          # part-way through the upload
    assert not running.done()

    await jobs.drain(timeout=5)

    assert running.done()
    assert len(bot.docs(1)) == 1
    assert await db.notes_delivered(1, "preclin") == {"a": {"01"}, "b": set()}
    assert await jobs.drain(timeout=0.01) is None, "nothing left to wait for"


# ------------------------------------------------- a subscriber who is gone


class Counting(FakeBot):
    """Blocked the bot, and counting how many times we find that out."""

    def __init__(self):
        super().__init__()
        self.trips = 0

    async def send_message(self, uid, text, **kw):
        self.trips += 1
        raise TelegramForbiddenError(SimpleNamespace(), "bot was blocked")

    async def send_document(self, uid, document, **kw):
        self.trips += 1
        raise TelegramForbiddenError(SimpleNamespace(), "bot was blocked")


async def test_a_blocked_student_costs_the_monday_push_one_round_trip():
    """_send_once deactivates them on the first Forbidden. Pushing the rest of
    the set at them was five more Forbidden round trips and five more redundant
    UPDATEs, every Monday."""
    from bot import db, jobs

    await _add_questions("preclin", ["P1", "P2"])
    await _subscriber(1, "preclin", weekly=True)

    bot = Counting()
    result = await jobs.weekly_quiz(bot)

    assert bot.trips == 1, "the lead message already told us the chat is gone"
    assert result == {"sent": 0, "reached": 0, "subscribers": 1, "finished": 0}
    assert await db.pool.fetchval(
        "select active from users where telegram_id = 1") is False


async def test_a_blocked_student_costs_the_drop_one_round_trip():
    from bot import db, jobs

    await _subscriber(1, "preclin", notes=True)

    bot = Counting()
    result = await jobs.fortnightly_notes(bot)

    assert bot.trips == 1, "the header already told us the chat is gone"
    assert result == {"sent": 0, "reached": 0, "subscribers": 1, "finished": 0}
    assert await db.notes_delivered(1, "preclin") == {"a": set(), "b": set()}


# ------------------------------------------------------------- the fan-out


async def test_the_monday_push_overlaps_students_rather_than_queueing_them(
        monkeypatch):
    """Per-chat pacing is only free if another student can be served while this
    one waits out their gap. One student at a time, N students cost N times one
    student's pacing, which is the fan-out this replaced."""
    from bot import jobs, sender

    monkeypatch.setattr(sender, "PACER",
                        sender.Pacer(burst=1, interval=0.1,
                                     stream_burst=1000, stream_interval=0.0))
    await _add_questions("preclin", ["P1", "P2"])
    students = 6
    assert students <= jobs.FANOUT_CONCURRENCY
    for uid in range(1, students + 1):
        await _subscriber(uid, "preclin", weekly=True)

    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await jobs.weekly_quiz(FakeBot())
    elapsed = loop.time() - start

    assert result["reached"] == students
    one_student = jobs.SET_PER_PUSH * 0.1       # 6 messages, the first free
    assert elapsed >= one_student * 0.9, elapsed
    assert elapsed < one_student * 2, f"{elapsed:.2f}s looks like one at a time"


async def test_one_students_failure_does_not_take_down_the_drop():
    """A row, a level or a send going wrong for one subscriber must not stop
    everybody behind them in the list."""
    from bot import db, jobs

    await _subscriber(1, "preclin", notes=True)
    await _subscriber(2, "clin", notes=True)

    real = db.notes_delivered

    async def explode(uid, level):
        if uid == 1:
            raise RuntimeError("bad row")
        return await real(uid, level)

    db.notes_delivered = explode
    try:
        result = await jobs.fortnightly_notes(FakeBot())
    finally:
        db.notes_delivered = real

    assert result == {"sent": 1, "reached": 1, "subscribers": 2, "finished": 0}


async def test_the_jobs_return_the_summary_their_annotation_promises():
    """fortnightly_notes was annotated `-> int` and returned a dict. The dict is
    right, because handlers.py indexes four keys out of it; the annotation was
    the thing that had drifted."""
    from bot import jobs

    await _add_questions("preclin", ["P1"])
    await _subscriber(1, "preclin", weekly=True, notes=True)

    for result in (await jobs.fortnightly_notes(FakeBot()),
                   await jobs.weekly_quiz(FakeBot())):
        assert set(result) == {"sent", "reached", "subscribers", "finished"}
        assert all(isinstance(value, int) for value in result.values())

    assert (inspect.signature(jobs.fortnightly_notes).return_annotation
            == inspect.signature(jobs.weekly_quiz).return_annotation
            == "dict[str, int]")
