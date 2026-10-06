"""Handler behaviour, exercised against an in-memory stand-in for the DB layer.

These cover the failure modes the old code had: a crash that leaves the user's
client spinning, an attempt recorded without the verdict ever being shown, and
the >4-option crash.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import db as real_db
from bot import handlers, jobs, sender

#: Kept in step with bot.db.SET_SIZE; the fake mirrors the real set boundaries.
SET_SIZE = real_db.SET_SIZE

# --------------------------------------------------------------------- fakes


class FakeBot:
    def __init__(self):
        self.sent = []
        self.commands = None
        self.command_scopes = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.sent.append({"chat_id": chat_id, "document": document,
                          "caption": caption, **kw})
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def set_my_commands(self, commands, scope=None):
        self.commands = commands
        self.command_scopes.append((commands, scope))


class FakeMessage:
    def __init__(self, text="Question body", message_id=10, fail_edit=False,
                 reply_markup=None):
        self.text = text
        self.message_id = message_id
        self.fail_edit = fail_edit
        self.reply_markup = reply_markup
        self.edits = []
        self.markup_edits = []
        self.replies = []

    @property
    def html_text(self):
        return self.text

    async def edit_text(self, text, **kw):
        if self.fail_edit:
            raise TelegramBadRequest(SimpleNamespace(), "message can't be edited")
        self.edits.append({"text": text, **kw})
        return True

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.markup_edits.append(reply_markup)
        return True

    async def answer(self, text, **kw):
        self.replies.append({"text": text, **kw})
        return SimpleNamespace(message_id=999)


class FakeCallback:
    def __init__(self, data, user_id=1, message=None, bot=None):
        self.data = data
        self.message = message
        # Handlers reach c.bot to send into the chat rather than only edit the
        # card, so a test that wants to see that message has to supply one.
        self.bot = bot
        self.from_user = SimpleNamespace(id=user_id, username="tester")
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "alert": show_alert})
        return True


class FakeDB:
    """Minimal in-memory equivalent of bot.db, enough for the handlers."""

    def __init__(self):
        self.users = {}
        self.questions = {}
        self.attempts = {}          # (user, msg_id) -> idx
        self.points = {}
        self.joined = set()
        self.tournament = None
        self.notes = []
        self.calls = []
        self.streak = 0
        self.delivered = {}        # (uid, level) -> {(tier, code), ...}
        self.flags = {}            # (uid, column) -> bool
        self.seq = 0

    # -- users
    async def upsert_user(self, uid, username):
        self.users.setdefault(uid, "preclin")

    async def get_level(self, uid):
        return self.users.get(uid)

    async def set_level(self, uid, level):
        self.users[uid] = level

    async def set_flag(self, uid, col, value):
        self.calls.append(("set_flag", uid, col, value))
        # Stored as well as recorded: /changestreams reads a flag back to tell a
        # push that ran out of content from one the student switched off.
        self.flags[(uid, col)] = value

    async def deactivate(self, uid):
        self.users.pop(uid, None)

    # -- questions
    async def get_question(self, qid):
        return self.questions.get(qid)

    def _correct_ids(self, uid):
        """Question ids this user has already answered correctly."""
        return {rec["qid"] for (u, _), rec in self.attempts.items()
                if u == uid and rec["correct"]}

    def _attempted_ids(self, uid):
        return {rec["qid"] for (u, _), rec in self.attempts.items() if u == uid}

    def _level_questions(self, level):
        return sorted((q for q in self.questions.values() if q["level"] == level),
                      key=lambda q: q["id"])

    def _ordered_attempts(self, uid, level):
        """Attempt records for this user at this level, oldest first."""
        by_id = {q["id"]: q for q in self.questions.values()}
        recs = [rec for (u, _), rec in self.attempts.items()
                if u == uid and by_id.get(rec["qid"], {}).get("level") == level]
        return sorted(recs, key=lambda r: r["seq"])

    async def answered_count(self, uid, level):
        seen = {rec["qid"] for rec in self._ordered_attempts(uid, level)}
        return len(seen)

    async def level_total(self, level):
        return len(self._level_questions(level))

    async def current_set(self, uid, level):
        total = len(self._level_questions(level))
        answered = await self.answered_count(uid, level)
        if total and answered >= total:
            return None
        return {"number": answered // SET_SIZE + 1,
                "answered_in_set": answered % SET_SIZE,
                "size": SET_SIZE,
                "total_sets": -(-total // SET_SIZE) if total else 0}

    async def pick_question(self, uid, level, exclude=()):
        """Stands in for the adaptive picker: anything unattempted, preferring a
        topic not already in the current partial set, as the real one does.

        `exclude` is cards already in the air - the Monday push builds five
        before any is answered, and sender keeps the last few per student - and
        the real statement filters on it, so the fake has to as well or a test
        sees a question production would never serve twice.
        """
        skip = self._attempted_ids(uid) | {int(qid) for qid in exclude}
        pool = [q for q in self._level_questions(level) if q["id"] not in skip]
        if not pool:
            return None
        answered = await self.answered_count(uid, level)
        in_set = self._ordered_attempts(uid, level)[answered - answered % SET_SIZE:]
        by_id = {q["id"]: q for q in self.questions.values()}
        seen_topics = {by_id[r["qid"]]["topic"] for r in in_set if r["qid"] in by_id}
        fresh = [q for q in pool if q.get("topic") not in seen_topics]
        return (fresh or pool)[0]

    async def last_set_score(self, uid, level):
        recs = self._ordered_attempts(uid, level)
        if len(recs) < SET_SIZE:
            return None
        window = recs[-SET_SIZE:]
        return {"number": len(recs) // SET_SIZE,
                "size": SET_SIZE,
                "correct": sum(1 for r in window if r["correct"])}

    async def wrong_questions(self, uid, level, limit=SET_SIZE):
        by_id = {q["id"]: q for q in self.questions.values()}
        latest = {}
        for rec in self._ordered_attempts(uid, level):
            latest[rec["qid"]] = rec["correct"]
        missed = [qid for qid, ok in latest.items() if not ok]
        return [by_id[qid] for qid in reversed(missed) if qid in by_id][:limit]

    async def wrong_count(self, uid, level):
        return len(await self.wrong_questions(uid, level, limit=10_000))

    # -- note deliveries
    async def record_notes_sent(self, uid, level, sheets):
        for tier, code in sheets:
            self.delivered.setdefault((uid, level), set()).add((tier, code))

    async def notes_delivered(self, uid, level):
        rows = self.delivered.get((uid, level), set())
        return {"a": {c for t, c in rows if t == "a"},
                "b": {c for t, c in rows if t == "b"}}

    async def sent_note_codes(self, uid, level, tier):
        return (await self.notes_delivered(uid, level))[tier]

    async def reset_notes(self, uid, level):
        return len(self.delivered.pop((uid, level), ()))

    async def stats(self, uid, level):
        by_id = {q["id"]: q for q in self.questions.values()}
        buckets = {}
        for (user, _), rec in self.attempts.items():
            question = by_id.get(rec["qid"])
            if user != uid or question is None or question["level"] != level:
                continue
            key = (question["topic"], question.get("tag") or "")
            answered, correct = buckets.setdefault(key, [0, 0])
            buckets[key] = [answered + 1, correct + (1 if rec["correct"] else 0)]
        return [{"topic": topic, "tag": tag,
                 "answered": counts[0], "correct": counts[1]}
                for (topic, tag), counts in buckets.items()]

    async def progress(self, uid, level):
        """Mirrors bot.db.progress: (still to get right, total) at this level."""
        done = self._correct_ids(uid)
        at_level = [q for q in self.questions.values() if q["level"] == level]
        return (len([q for q in at_level if q["id"] not in done]), len(at_level))

    async def topics(self, level):
        return sorted({q["topic"] for q in self.questions.values() if q["level"] == level})

    async def levels_with_questions(self):
        return sorted({q["level"] for q in self.questions.values()})

    async def practice_streak(self, uid):
        return self.streak

    async def reset_progress(self, uid, level=None):
        self.calls.append(("reset_progress", uid, level))
        doomed = [key for key, rec in self.attempts.items()
                  if key[0] == uid and (level is None
                                        or self.questions[rec["qid"]]["level"] == level)]
        for key in doomed:
            del self.attempts[key]
        return len(doomed)

    async def record_attempt(self, uid, question, idx, correct, mode, msg_id):
        """Both of the uniqueness rules bot.db.record_attempt relies on.

        `unique (user_id, msg_id)` is what makes a double tap on one card
        idempotent. `attempts_one_scoring_answer_idx` - unique
        (user_id, question_id) where mode <> 'review' - is what stops two
        *different* cards for the same question both being recorded, which is
        reachable whenever a /quizme card is left unanswered and the Monday push
        serves that question again. A review answer sits outside that index, so
        re-answering through /review keeps working however often they do it.
        """
        if (uid, msg_id) in self.attempts:
            return False
        if mode != "review" and any(
                user == uid and rec["qid"] == question["id"]
                and rec["mode"] != "review"
                for (user, _), rec in self.attempts.items()):
            return False
        # `seq` stands in for created_at: the new set model is "the Nth block of
        # five answers", so the order attempts arrived in is what matters.
        self.seq += 1
        self.attempts[(uid, msg_id)] = {"idx": idx, "correct": correct,
                                        "qid": question["id"], "mode": mode,
                                        "seq": self.seq}
        return True

    # -- tournaments
    async def award_point(self, uid, question_id):
        key = (uid, question_id)
        if key in self.points:
            return False
        self.points[key] = True
        return True

    async def active_tournament(self):
        return self.tournament

    async def is_joined(self, uid):
        return uid in self.joined

    async def join_tournament(self, uid):
        self.joined.add(uid)
        return True

    async def leave_tournament(self, uid):
        self.joined.discard(uid)
        return True

    async def leaderboard(self, limit=None):
        if self.tournament is None:
            return None
        rows = [{"user_id": u, "username": n, "points": 3 - i, "rk": i + 1}
                for i, (u, n) in enumerate([(7, "hongpenggg"), (8, None), (9, "zh")])
                if u in self.joined]
        return rows[:limit] if limit else rows

    async def my_rank(self, uid):
        return None

    async def start_tournament(self, days=14):
        self.tournament = {"id": 1, "ends_at": datetime.now(timezone.utc)}
        return 1

    async def all_users(self):
        return sorted(self.users)

    async def enrol_everyone(self, tid):
        added = [u for u in self.users if u not in self.joined]
        self.joined.update(added)
        return len(added)

    async def tournament_mark(self, uid):
        if self.tournament is None:
            return None
        return {"points": 0, "entrants": len(self.joined)}

    async def standings(self, tid, limit=None):
        return []

    async def end_tournament(self, tid):
        self.tournament = None

    async def expired_tournaments(self):
        return []

    # -- notes
    async def note_topics(self, level, tier):
        return sorted({n["topic"] for n in self.notes
                       if n["level"] == level and n["tier"] == tier})

    async def get_notes(self, topic, level, tier):
        return [n for n in self.notes
                if n["level"] == level and n["tier"] == tier
                and n["topic"].lower() == topic.lower()]

    async def all_notes(self, level, tier):
        return [n for n in self.notes if n["level"] == level and n["tier"] == tier]

    async def subscribers(self, col):
        return sorted(uid for (uid, column), on in self.flags.items()
                      if column == col and on)


@pytest.fixture
def fake(monkeypatch):
    fake_db = FakeDB()
    for name, value in vars(FakeDB).items():
        if callable(value) and not name.startswith("_"):
            monkeypatch.setattr(real_db, name, getattr(fake_db, name))
    # sender keeps the cards it has served but not seen answered, keyed by user
    # id and outliving any one test. Every test here is user 1, so a leftover
    # entry would make the next test's /quizme refuse to serve a question.
    sender._outstanding.clear()
    fake_db.bot = FakeBot()
    return fake_db


def add_question(fake, qid=1, n_options=4, correct_idx=0, level="preclin", tag=None):
    options = [f"option {i}" for i in range(n_options)]
    fake.questions[qid] = {
        "id": qid, "level": level, "topic": "Sample", "text": "Question?",
        "options": json.dumps(options), "correct_idx": correct_idx,
        "explanation": None, "tag": tag,
    }
    return fake.questions[qid]


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_correct_answer_is_scored_and_revealed(fake):
    add_question(fake, correct_idx=1)
    message = FakeMessage()
    c = FakeCallback("a:1:1:practice", message=message)

    await handlers.on_answer(c)

    assert c.answers, "callback must always be answered"
    assert not any(a["alert"] for a in c.answers)
    edit = message.edits[-1]
    assert edit["parse_mode"] == "HTML"
    assert "✅ <b>2.</b> option 1" in edit["text"], "the right option is ticked in place"
    assert edit["text"].rstrip().splitlines()[-1].startswith("✅ <b>")
    assert "❌" not in edit["text"]
    assert fake.points == {(1, 1): True}
    # Neighbours are offered back.
    labels = [b.text for b in message.edits[-1]["reply_markup"].inline_keyboard[0]]
    assert labels == ["💡 Explain", "Next ➡️"]


@pytest.mark.asyncio
async def test_five_option_question_no_longer_crashes(fake):
    """This raised IndexError on the old hardcoded LETTERS = "ABCD"."""
    add_question(fake, qid=5, n_options=5, correct_idx=4)
    message = FakeMessage()
    c = FakeCallback("a:5:4:practice", message=message)

    await handlers.on_answer(c)

    assert "✅ <b>5.</b> option 4" in message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_wrong_answer_shows_the_right_option(fake):
    add_question(fake, correct_idx=2, n_options=4)
    message = FakeMessage()
    c = FakeCallback("a:1:0:practice", message=message)

    await handlers.on_answer(c)

    text = message.edits[-1]["text"]
    assert "❌ <b>1.</b> option 0" in text, "the wrong pick is crossed"
    assert "✅ <b>3.</b> option 2" in text
    assert "The answer is <b>3</b>." in text
    # The verdict points at the letter; the option text appears once, not twice.
    assert text.count("option 2") == 1
    assert fake.points == {}


@pytest.mark.asyncio
async def test_double_tap_is_idempotent(fake):
    add_question(fake)
    message = FakeMessage()
    await handlers.on_answer(FakeCallback("a:1:0:practice", message=message))
    second = FakeCallback("a:1:0:practice", message=message)

    await handlers.on_answer(second)

    assert second.answers[-1]["text"] == "You've already answered this one 👍"
    assert len(message.edits) == 1


@pytest.mark.asyncio
async def test_a_second_scoring_card_for_one_question_is_refused(fake):
    """A student can hold two live cards for the same question: a /quizme card
    left unanswered, then the Monday push serving it again.

    Only the first is recorded (attempts_one_scoring_answer_idx), so the second
    card has to say so instead of scoring, re-reporting or redrawing itself.
    """
    add_question(fake, qid=1, correct_idx=0)
    await handlers.on_answer(FakeCallback("a:1:0:practice",
                                          message=FakeMessage(message_id=1)))

    second = FakeMessage(message_id=2)
    c = FakeCallback("a:1:0:weekly", message=second)
    await handlers.on_answer(c)

    assert c.answers[-1]["text"] == "You've already answered this one 👍"
    assert second.edits == []
    assert fake.points == {(1, 1): True}, "the question scored exactly once"


@pytest.mark.asyncio
async def test_unknown_question_id_does_not_hang(fake):
    c = FakeCallback("a:999:0:practice", message=FakeMessage())

    await handlers.on_answer(c)

    assert c.answers, "the spinner must be closed"
    assert c.answers[-1]["alert"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["a:1:2", "a:x:0:practice", "a:1:y:practice",
                                  "a:1:0:admin", "a:::", "a:1:99:practice"])
async def test_malformed_callback_data_is_ignored(fake, data):
    add_question(fake)
    c = FakeCallback(data, message=FakeMessage())

    await handlers.on_answer(c)

    assert c.answers
    assert fake.points == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["a:\u00b2:0:practice", "a:1:\u00b2:practice",
                                  "a:\u0662:0:practice", "a:1_0:0:practice",
                                  f"a:{2 ** 70}:0:practice",
                                  f"a:{2 ** 31}:0:practice"])
async def test_crafted_question_ids_never_reach_the_database(fake, monkeypatch, data):
    """`isdigit()` is true for '²' and other non-decimal digits, which `int()`
    then rejects, and questions.id is a `serial`, so an id wider than int4 is a
    DataError out of asyncpg. Both ended as a logged exception and a bare ack:
    the spinner stopped and the student was shown nothing at all."""
    add_question(fake)

    async def never(qid):
        raise AssertionError(f"the database was queried with {qid!r}")

    monkeypatch.setattr(real_db, "get_question", never)
    c = FakeCallback(data, message=FakeMessage())

    await handlers.on_answer(c)

    assert c.answers == [{"text": None, "alert": False}]
    assert fake.points == {}


@pytest.mark.asyncio
async def test_explain_ignores_a_crafted_question_id(fake, monkeypatch):
    """The Explain button parsed its id the same unsafe way on_answer did."""

    async def never(qid):
        raise AssertionError(f"the database was queried with {qid!r}")

    monkeypatch.setattr(real_db, "get_question", never)
    c = FakeCallback("e:\u00b2", message=FakeMessage())

    await handlers.on_explain(c, fake.bot)

    assert c.answers == [{"text": None, "alert": False}]
    assert fake.bot.sent == []


@pytest.mark.asyncio
async def test_a_shrunken_option_list_is_explained_not_ignored(fake):
    """A question row can lose options after its card was sent, which puts a tap
    that was in range for the card out of range for the row.

    That ended in a bare ack: nothing recorded, no text, and every button still
    live, so the student tapped and tapped and nothing ever happened.
    """
    from bot.sender import question_kb

    add_question(fake, qid=1, n_options=5, correct_idx=0)
    card = FakeMessage(reply_markup=question_kb(1, 5, "practice"))
    fake.questions[1]["options"] = json.dumps(["only", "two"])

    c = FakeCallback("a:1:4:practice", message=card)
    await handlers.on_answer(c)

    assert c.answers[-1]["alert"] is True
    assert "not on this question any more" in c.answers[-1]["text"]
    assert fake.attempts == {}, "an out-of-range pick must not be recorded"
    assert card.markup_edits == [None], "the dead answer buttons are taken away"


@pytest.mark.asyncio
async def test_a_question_row_too_small_to_render_says_so(fake):
    """Shrunk below the two-option floor, parse_options raises: that used to be a
    logged exception and a bare ack, which looks identical to a dead button."""
    from bot.sender import question_kb

    add_question(fake, qid=1, n_options=5, correct_idx=0)
    card = FakeMessage(reply_markup=question_kb(1, 5, "practice"))
    fake.questions[1]["options"] = json.dumps(["alone"])

    c = FakeCallback("a:1:0:practice", message=card)
    await handlers.on_answer(c)

    assert c.answers[-1]["alert"] is True
    assert "wrong with that question" in c.answers[-1]["text"]
    assert fake.attempts == {}
    assert card.markup_edits == [None]


@pytest.mark.asyncio
async def test_edit_failure_still_delivers_the_verdict(fake):
    """Telegram refuses edits after 48h (a Monday weekly question answered on
    Thursday). The old code recorded the attempt and then threw, so the user was
    locked out of ever seeing the answer."""
    add_question(fake)
    message = FakeMessage(fail_edit=True)
    c = FakeCallback("a:1:0:practice", message=message)

    await handlers.on_answer(c)

    assert message.replies, "must fall back to a fresh message"
    delivered = "\n".join(reply["text"] for reply in message.replies)
    assert "✅ <b>1.</b> option 0" in delivered
    assert any(reply.get("parse_mode") == "HTML" for reply in message.replies)


@pytest.mark.asyncio
async def test_weekly_answer_scores_nothing(fake):
    add_question(fake)
    fake.tournament = {"id": 1, "ends_at": None}
    message = FakeMessage()
    c = FakeCallback("a:1:0:weekly", message=message)

    await handlers.on_answer(c)

    assert fake.points == {}
    labels = [b.text for b in message.edits[-1]["reply_markup"].inline_keyboard[0]]
    assert labels == ["💡 Explain"]
    assert "Question of the week" in message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_streak_shows_from_three_in_a_row(fake):
    add_question(fake, correct_idx=0)
    fake.streak = 3
    message = FakeMessage()

    await handlers.on_answer(FakeCallback("a:1:0:practice", message=message))

    assert "🔥 3 in a row" in message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_no_streak_line_below_three(fake):
    add_question(fake, correct_idx=0)
    fake.streak = 2
    message = FakeMessage()

    await handlers.on_answer(FakeCallback("a:1:0:practice", message=message))

    assert "🔥" not in message.edits[-1]["text"]


def answered_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💡 Explain", callback_data="e:1"),
        InlineKeyboardButton(text="Next ➡️", callback_data="next"),
    ]])


@pytest.mark.asyncio
async def test_next_removes_its_button_from_the_old_card(fake):
    add_question(fake)
    old = FakeMessage(reply_markup=answered_keyboard())

    await handlers.next_question(FakeCallback("next", message=old), fake.bot)

    kept = [b.callback_data for row in old.markup_edits[-1].inline_keyboard for b in row]
    assert kept == ["e:1"], "Explain stays usable, the used Next goes"
    assert fake.bot.sent, "and the next question still arrives"

@pytest.mark.asyncio
async def test_explain_rate_limit(fake, monkeypatch):
    add_question(fake, correct_idx=0)

    async def fake_explain(question):
        return "because"

    monkeypatch.setattr(handlers.llm, "explain", fake_explain)
    handlers._recent_explain.clear()

    card = FakeMessage(text="card")
    await handlers.on_explain(FakeCallback("e:1", message=card), fake.bot)
    assert "because" in card.edits[-1]["text"]

    other = FakeMessage(text="card")
    second = FakeCallback("e:1", message=other)
    await handlers.on_explain(second, fake.bot)
    assert second.answers[-1]["alert"] is True
    assert other.edits == [] and fake.bot.sent == []


@pytest.mark.asyncio
async def test_explain_serves_the_written_explanation_without_an_llm(fake, monkeypatch):
    """The preclinical bank ships with explanations, so the button must work with
    no LLM configured — and must never touch the provider."""
    add_question(fake, correct_idx=0)
    fake.questions[1]["explanation"] = "Because the fissure failed to close."

    def no_http(*args, **kwargs):
        raise AssertionError("the LLM provider must not be contacted")

    monkeypatch.setattr(handlers.llm.httpx, "AsyncClient", no_http)
    handlers._recent_explain.clear()

    card = FakeMessage(text="card", reply_markup=answered_keyboard())
    await handlers.on_explain(FakeCallback("e:1", message=card), fake.bot)

    edit = card.edits[-1]
    assert edit["text"].startswith("card\n\n💡 <b>Why</b>\n")
    assert "fissure failed to close" in edit["text"]
    assert "clinical advice" not in edit["text"], "the disclaimer lives on /start only"
    kept = [b.callback_data for row in edit["reply_markup"].inline_keyboard for b in row]
    assert kept == ["next"], "Explain is used up, Next stays"

    # Written explanations cost nothing, so there is no cooldown on them.
    again = FakeCallback("e:1", message=FakeMessage(text="card"))
    await handlers.on_explain(again, fake.bot)
    assert not any(a["alert"] for a in again.answers)


@pytest.mark.asyncio
async def test_explanation_falls_back_to_a_new_message_when_the_card_is_locked(fake):
    add_question(fake, correct_idx=0)
    fake.questions[1]["explanation"] = "Because <reasons> & more."
    handlers._recent_explain.clear()

    card = FakeMessage(text="card", fail_edit=True)
    await handlers.on_explain(FakeCallback("e:1", message=card), fake.bot)

    sent = fake.bot.sent[-1]
    assert sent["parse_mode"] == "HTML"
    assert "Because &lt;reasons&gt; &amp; more." in sent["text"]


@pytest.mark.asyncio
async def test_explain_says_so_when_there_is_nothing_to_show(fake, monkeypatch):
    """No stored explanation and no provider: a plain message, not an error."""
    add_question(fake, correct_idx=0)

    async def no_explanation(question):
        return None

    monkeypatch.setattr(handlers.llm, "explain", no_explanation)
    handlers._recent_explain.clear()

    await handlers.on_explain(FakeCallback("e:1", message=FakeMessage()), fake.bot)

    assert fake.bot.sent[-1]["text"] == "No explanation written for this one yet."


@pytest.mark.asyncio
async def test_level_callback_updates_the_user(fake):
    picker = FakeMessage(text="the /start welcome")
    c = FakeCallback("lv:clin", message=picker)

    await handlers.set_level(c)

    assert fake.users[1] == "clin"
    assert c.answers[-1]["text"] == "🩺 You're on Clinical now"
    assert picker.edits == [], "the welcome text must survive a level change"
    labels = [row[0].text for row in picker.markup_edits[-1].inline_keyboard]
    assert labels == ["📖 Pre-Clinical", "✅ Clinical", "🎓 Post-MBBS"]


@pytest.mark.asyncio
async def test_level_callback_rejects_unknown_value(fake):
    c = FakeCallback("lv:consultant")

    await handlers.set_level(c)

    assert fake.users.get(1) != "consultant"
    assert c.answers[-1]["alert"] is True


async def _finished_preclin(fake):
    """Put the student in the state the scheduled pushes switch themselves off
    in: every preclinical sheet read and every preclinical question answered.
    Clinical keeps its content, so switching there has something to send."""
    from bot import resources

    fake.users[1] = "preclin"
    await real_db.record_notes_sent(
        1, "preclin", [(n.tier, n.code) for n in resources.all_for("preclin")])
    only = add_question(fake, qid=1, level="preclin", correct_idx=0)
    await real_db.record_attempt(1, only, 0, True, "practice", msg_id=1)
    add_question(fake, qid=2, level="clin", correct_idx=0)


@pytest.mark.asyncio
async def test_switching_stream_revives_a_push_that_ran_out_of_content(fake):
    """Finishing a level switches both pushes off, and nothing turned them back on.

    The fortnightly drop clears notes_sub once a student has had every sheet, and
    the Monday push clears weekly_sub once they have answered every question,
    both by design. But /changestreams to a level with 47 unread sheets left the
    drop dead, with nothing to show the student why.
    """
    await _finished_preclin(fake)
    fake.calls.clear()

    picker = FakeMessage(text="the picker")
    await handlers.set_level(FakeCallback("lv:clin", message=picker, bot=fake.bot))

    assert ("set_flag", 1, "weekly_sub", True) in fake.calls
    assert ("set_flag", 1, "notes_sub", True) in fake.calls
    said = fake.bot.sent[-1]
    assert "Monday question sets and fortnightly cheat sheets" in said["text"]
    assert "finished Pre-Clinical" in said["text"]
    assert "Clinical content waiting" in said["text"]
    assert said["parse_mode"] == "HTML", "the confirmation carries markup"
    # And they can turn them straight back off, which is how every other
    # subscription confirmation works.
    off = [b.callback_data for row in said["reply_markup"].inline_keyboard
           for b in row]
    assert off == ["sub:off:weekly", "sub:off:notes"]


@pytest.mark.asyncio
async def test_switching_stream_respects_a_deliberate_unsubscribe(fake):
    """/stopmonthly is a choice, not an accident.

    What tells the two apart is whether the level they are leaving still had
    content: a push only switches itself off when there is nothing left to send.
    """
    fake.users[1] = "preclin"
    add_question(fake, qid=1, level="preclin", correct_idx=0)
    add_question(fake, qid=2, level="clin", correct_idx=0)
    fake.calls.clear()

    await handlers.set_level(FakeCallback("lv:clin", message=FakeMessage(),
                                          bot=fake.bot))

    assert [call for call in fake.calls if call[0] == "set_flag"] == []
    assert "back on" not in fake.bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_switching_stream_leaves_a_live_subscription_alone(fake):
    """A subscription that is already on needs no repair, and writing it again
    would be a pointless round trip on every stream switch."""
    await _finished_preclin(fake)
    await real_db.set_flag(1, "notes_sub", True)
    fake.calls.clear()

    await handlers.set_level(FakeCallback("lv:clin", message=FakeMessage(),
                                          bot=fake.bot))

    assert ("set_flag", 1, "weekly_sub", True) in fake.calls
    assert ("set_flag", 1, "notes_sub", True) not in fake.calls
    changed = fake.bot.sent[-1]["text"].split("🔔")[-1]
    assert "Monday question sets stopped" in changed
    assert "cheat sheets" not in changed, "only what actually changed is reported"


@pytest.mark.asyncio
async def test_tournament_reports_status_rather_than_joining(fake):
    """Entry is automatic now, so /tournament only reports. It must never remove
    anyone: a student cannot opt out of a competition they are already in."""
    fake.tournament = {"id": 1, "ends_at": None}
    fake.joined = {1}
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1, username="t"),
                          answer=_recorder())

    await handlers.tournament(msg)

    text = msg.answer.messages[-1]
    assert "Tournament live" in text
    assert "/quizme" in text
    assert fake.joined == {1}, "it must not drop anyone"


@pytest.mark.asyncio
async def test_start_enters_a_running_tournament_automatically(fake):
    fake.tournament = {"id": 1, "ends_at": datetime.now(timezone.utc)}
    fake.joined = set()

    await handlers.start(_chat_message(), fake.bot)

    assert fake.joined == {1}, "sending /start enters a running tournament"


def _recorder():
    async def _answer(text, **kw):
        _answer.messages.append(text)
        # Keyboards matter now that the sheet pickers are built per level, so the
        # kwargs are kept alongside the text rather than dropped.
        _answer.kwargs.append(kw)
        return SimpleNamespace(message_id=1)

    _answer.messages = []
    _answer.kwargs = []
    return _answer


@pytest.mark.asyncio
async def test_leaderboard_masks_the_last_two_characters(fake):
    fake.tournament = {"id": 1, "ends_at": None}
    fake.joined = {7, 8, 9}
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=_recorder())

    await handlers.leaderboard(msg)

    text = msg.answer.messages[-1]
    assert "🥇 @hongpeng**, 3 pts" in text
    assert "🥉 @**, 1 pt" in text
    # No username on file falls back to a uid-derived label.
    assert "user8" in text


@pytest.mark.asyncio
async def test_admin_commands_are_ignored_for_non_admins(fake, monkeypatch):
    msg = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=_recorder())
    await handlers.admin_tournament_start(msg, fake.bot)
    assert msg.answer.messages == []


@pytest.mark.asyncio
async def test_finish_tournament_reads_standings_before_closing(monkeypatch):
    """The old job called leaderboard() *after* deactivating the tournament, so
    the announcement was always empty."""
    order = []
    rows = [{"rk": 1, "user_id": 7, "username": "hongpenggg", "points": 9}]

    async def standings(tid, limit=None):
        order.append("read")
        return rows

    async def end(tid):
        order.append("close")

    monkeypatch.setattr(real_db, "standings", standings)
    monkeypatch.setattr(real_db, "end_tournament", end)
    bot = FakeBot()

    result = await jobs.finish_tournament(bot, 1)

    assert order == ["read", "close"], "standings must be read before the close"
    assert result == rows
    assert any(m["chat_id"] == 7 and "finished #1" in m["text"] for m in bot.sent)
    assert any(m["chat_id"] == 42 for m in bot.sent), "admins get the standings"


@pytest.mark.asyncio
async def test_student_on_an_unwritten_level_is_told_what_is_available(fake):
    """The Clinical and Post-MBBS banks aren't loaded yet. A student who picks
    one must not be left staring at silence."""
    add_question(fake, level="preclin")
    fake.users[1] = "clin"
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1, username="t"), answer=_recorder())

    await handlers.practice(msg, fake.bot)

    text = fake.bot.sent[-1]["text"]
    assert "No Clinical questions yet" in text
    assert "Pre-Clinical" in text
    assert "/changestreams" in text


@pytest.mark.asyncio
async def test_practice_with_an_empty_database_says_so(fake):
    fake.users[1] = "preclin"
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1, username="t"), answer=_recorder())

    await handlers.practice(msg, fake.bot)

    assert fake.bot.sent[-1]["text"] == "No questions loaded yet. Check back soon!"


@pytest.mark.asyncio
async def test_start_greets_by_name_and_escapes_it(fake):
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="t", first_name="<Zay>"),
        chat=SimpleNamespace(id=1),
        answer=_recorder(),
    )

    await handlers.start(msg, fake.bot)

    text = msg.answer.messages[-1]
    assert text.startswith("👋 <b>Hi &lt;Zay&gt;!</b> This is the <b>LKC OphSoc Bot</b>.")
    assert "/quizme" in text and "Acuity Team" in text


def test_html_is_never_sent_without_parse_mode():
    """Copy rule: a message containing markup must declare parse_mode="HTML".

    Telegram does not guess. Without it a student sees the literal characters
    "<b>Revision sheets</b>", which is exactly what happened to RESOURCES_INTRO
    the moment it gained a bold heading. Static rather than per-handler, so a new
    message cannot slip through untested.
    """
    import ast
    import re
    from pathlib import Path as _Path

    tag = re.compile(r"</?(?:b|i|code|pre|u|s|a)\b")
    senders = ("answer", "edit_text", "send_message", "safe_send")
    offenders = []

    for path in sorted((_Path(__file__).resolve().parent.parent / "bot").glob("*.py")):
        tree = ast.parse(path.read_text())
        # Module-level string constants, so a message held in one is checked at
        # its send site rather than only where it is defined.
        consts = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                try:
                    value = ast.literal_eval(node.value)
                except Exception:
                    continue
                if isinstance(value, str):
                    consts[node.targets[0].id] = value

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # `m.answer(...)` is an Attribute; bare `safe_send(...)` is a Name.
            # Only checking the first let every safe_send call in bot/ through,
            # and the tournament announcement shipped literal <b> tags to every
            # user because of it.
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in senders or "parse_mode" in {k.arg for k in node.keywords}:
                continue
            # safe_send(bot, uid, text); everything else takes the text first.
            index = 2 if name == "safe_send" else 0
            if len(node.args) <= index:
                continue
            source = ast.unparse(node.args[index])
            if tag.search(consts.get(source, source)):
                offenders.append(f"{path.name}:{node.lineno} {name}({source[:40]})")

    assert offenders == [], offenders


def test_no_em_dashes_in_anything_students_see():
    """Copy rule: no em dashes in user-facing text."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot"
    offenders = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and "\u2014" in node.value):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


# --------------------------------------------------------------------- menu


def _chat_message(uid=1, username="tester"):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid, username=username),
                           chat=SimpleNamespace(id=uid),
                           answer=_recorder())


@pytest.mark.asyncio
async def test_start_gives_that_chat_the_full_menu(fake):
    """The complaint was that the menu sat on /start. Telegram keeps commands per
    scope, so /start must set a chat-scoped list - that is what fills it in."""
    msg = _chat_message()

    await handlers.start(msg, fake.bot)

    assert fake.bot.command_scopes, "no command menu was set for the chat"
    commands_sent, scope = fake.bot.command_scopes[-1]
    names = [c.command for c in commands_sent]
    assert "quizme" in names
    assert "resources" in names
    assert "changestreams" in names
    assert scope is not None, "a default-scope call would not change this chat"
    assert "admin_tournament_start" not in names, "students must not see admin commands"


@pytest.mark.asyncio
async def test_start_states_the_functions_and_the_level(fake):
    msg = _chat_message()
    fake.users[1] = "preclin"

    await handlers.start(msg, fake.bot)

    text = msg.answer.messages[-1]
    assert "Acuity Team" in text
    assert "Zhong Han (Vice-Pres, LKC OphSoc 26/27)" in text
    assert "lkceye" in text
    assert "Revision only, not clinical advice." in text
    assert "<b>Functions</b>" in text
    for item in ("<b>1. 🧠", "<b>2. 🗒", "<b>3. 📬"):
        assert item in text, item
    assert "Annual Eye Trivia Tournament" in text
    assert "Pre-Clinical" in text
    assert "/changestreams" in text
    # The sets really are adaptive now (db.pick_question weights by per-topic
    # accuracy), so the copy is allowed to claim it. This assertion used to run
    # the other way, back when sets were a fixed block of five in id order.
    assert "Adaptive" in text
    assert "/review" in text


@pytest.mark.asyncio
async def test_welcome_only_names_commands_that_exist(fake):
    """Every /command printed in the welcome has to be registered.

    A draft of this copy advertised /subscribeqn, /subscribenotes, /unsub_qns
    and /unsub_notes, none of which existed - a student tapping one would have
    got silence. The welcome now uses the canonical names from commands.PUBLIC,
    and this walks the router rather than a hand-kept list, so a renamed handler
    fails here too.
    """
    import re

    from aiogram.filters import Command

    registered = set()
    for handler in handlers.router.message.handlers:
        for flt in handler.filters or ():
            callback = getattr(flt, "callback", None)
            if isinstance(callback, Command):
                registered |= {str(c) for c in callback.commands}

    msg = _chat_message()
    await handlers.start(msg, fake.bot)
    # (?<!<) so the closing "</b>" of an HTML tag is not read as a command.
    mentioned = set(re.findall(r"(?<!<)/([a-z_]+)", msg.answer.messages[-1]))

    assert mentioned, "the welcome should name some commands"
    assert mentioned <= registered, f"not registered: {sorted(mentioned - registered)}"

    # And they must be the canonical names, so the welcome and Telegram's own
    # command menu read as one vocabulary rather than two sets of synonyms.
    from bot import commands as cmds
    menu = {c.command for c in cmds.PUBLIC}
    assert mentioned <= menu, f"not in the menu: {sorted(mentioned - menu)}"


@pytest.mark.asyncio
async def test_help_adds_the_full_command_list(fake):
    """The welcome names the headline commands; /help still lists everything, so
    /stats and the sheet browsers stay reachable."""
    from aiogram.filters import CommandObject

    msg = _chat_message()
    await handlers.start(msg, fake.bot,
                         CommandObject(prefix="/", command="help", args=None))

    text = msg.answer.messages[-1]
    assert "All commands" in text
    for cmd in ("/stats", "/topicalnotes", "/randomnotes", "/resources", "/reset"):
        assert cmd in text, cmd


# ----------------------------------------------------------------- resources


@pytest.mark.asyncio
async def test_resources_intro_never_says_tier_names(fake):
    msg = _chat_message()

    await handlers.resources_cmd(msg, SimpleNamespace(args=None), fake.bot)

    text = msg.answer.messages[-1]
    assert "Revision sheets" in text
    assert "Tier A" not in text and "Tier B" not in text


@pytest.mark.asyncio
async def test_resources_by_code_sends_the_pdf_itself(fake):
    """Students should get the document, not a description of it."""
    msg = _chat_message()

    await handlers.resources_cmd(msg, SimpleNamespace(args="B14"), fake.bot)

    sent = fake.bot.sent[-1]
    assert "document" in sent, "expected a document upload"
    assert "Saccades" in sent["caption"]
    assert "Tier" not in sent["caption"]


@pytest.mark.asyncio
async def test_tapping_a_sheet_sends_it(fake):
    fake.users[1] = "preclin"
    c = FakeCallback("res:get:preclin:b:B14", message=FakeMessage())

    await handlers.resources_cb(c, fake.bot)

    assert c.answers, "the client spinner must always be closed"
    assert any("document" in m for m in fake.bot.sent)


@pytest.mark.asyncio
async def test_a_sheet_button_from_another_stream_is_refused(fake):
    """Codes repeat across levels, so a button drawn before the student switched
    stream would otherwise hand over whatever carries that code at the new level,
    silently, and mark it delivered so /notes skips it."""
    fake.users[1] = "clin"
    c = FakeCallback("res:get:preclin:b:B14", message=FakeMessage())

    await handlers.resources_cb(c, fake.bot)

    assert not any("document" in m for m in fake.bot.sent), "wrong level served"
    assert any("switched stream" in (a.get("text") or "") for a in c.answers)


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["abc", "", "²", "-5", "1_0",
                                  "../../etc", "99999999999999999999"])
async def test_res_page_never_crashes_on_its_callback_data(fake, tail):
    """`int(parts[3])` raised on a non-numeric or empty page number, which logged
    an exception, answered the callback a second time and left the tap doing
    nothing visible: the student presses the arrow and the list never moves."""
    fake.users[1] = "postmbbs"
    card = FakeMessage()
    c = FakeCallback(f"res:page:b:{tail}", message=card)

    await handlers.resources_cb(c, fake.bot)

    assert len(c.answers) == 1, "every callback path answers exactly once"
    assert card.edits, "and the student gets a usable list back"
    labels = [b.text for row in card.edits[-1]["reply_markup"].inline_keyboard
              for b in row]
    assert "1/4" in labels, labels


@pytest.mark.asyncio
async def test_res_page_still_turns_the_page(fake):
    """The guard above must not flatten every page to the first one."""
    fake.users[1] = "postmbbs"
    card = FakeMessage()

    await handlers.resources_cb(FakeCallback("res:page:b:2", message=card), fake.bot)

    labels = [b.text for row in card.edits[-1]["reply_markup"].inline_keyboard
              for b in row]
    assert "3/4" in labels, labels


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["notes", "resources"])
async def test_a_sheet_that_cannot_be_sent_is_never_silent(fake, monkeypatch, door):
    """send_note returning False means neither the PDF nor the GitHub link got
    through. /notes then returned without replying at all: zero sends, zero
    replies, nothing recorded, and a student staring at absolute silence."""

    async def nothing_gets_through(bot, uid, note):
        return False

    monkeypatch.setattr(handlers, "send_note", nothing_gets_through)
    fake.users[1] = "preclin"
    msg = _chat_message()

    if door == "notes":
        await handlers.notes(msg, SimpleNamespace(args=None), fake.bot)
    else:
        await handlers.resources_cmd(msg, SimpleNamespace(args="B14"), fake.bot)

    said = [m["text"] for m in fake.bot.sent if "text" in m] + msg.answer.messages
    assert any("Could not send" in text for text in said), said
    assert any("/resources" in text for text in said), said
    assert fake.delivered == {}, "a sheet that never arrived stays in the queue"


@pytest.mark.asyncio
async def test_notes_does_not_congratulate_on_an_empty_catalogue(fake, monkeypatch):
    """An empty queue means "nothing on disk" as well as "you have read them
    all". Telling a student they finished a syllabus they were never sent a page
    of is the one reading that cannot be true. /topicalnotes and /randomnotes
    both guard this already."""
    from bot import resources

    monkeypatch.setitem(resources.CATALOGUE, "preclin", {"a": [], "b": []})
    fake.users[1] = "preclin"
    msg = _chat_message()

    await handlers.notes(msg, SimpleNamespace(args=None), fake.bot)

    assert msg.answer.messages == ["No cheat sheets on disk for your level yet."]
    assert not [m for m in fake.bot.sent if "document" in m]


@pytest.mark.asyncio
async def test_unknown_sheet_code_is_handled(fake):
    msg = _chat_message()

    await handlers.resources_cmd(msg, SimpleNamespace(args="zzz"), fake.bot)

    assert "No sheet matches" in msg.answer.messages[-1]


@pytest.mark.asyncio
async def test_notes_walks_overview_then_focused_then_congratulates(fake):
    """/notes is a progression: every overview sheet, then every focused one,
    then the syllabus-complete message, and never the same sheet twice."""
    from bot import resources

    fake.users[1] = "preclin"
    overview = [n.path.name for n in resources.overview("preclin")]
    focused = [n.path.name for n in resources.focused("preclin")]

    handed = []
    for _ in range(len(overview) + len(focused)):
        msg = _chat_message()
        await handlers.notes(msg, SimpleNamespace(args=None), fake.bot)
        handed.append([m for m in fake.bot.sent if "document" in m][-1]
                      ["document"].path.name)

    assert handed == overview + focused, "wrong order, or a sheet repeated"
    assert len(set(handed)) == len(handed)

    # One more and there is nothing left to send.
    msg = _chat_message()
    before = len([m for m in fake.bot.sent if "document" in m])
    await handlers.notes(msg, SimpleNamespace(args=None), fake.bot)

    assert len([m for m in fake.bot.sent if "document" in m]) == before
    assert "completed the notes" in msg.answer.messages[-1]
    assert "Pre-Clinical syllabus" in msg.answer.messages[-1]


@pytest.mark.asyncio
async def test_a_sheet_pulled_by_hand_is_not_pushed_again(fake):
    """/resources, /notes and the fortnightly drop share one delivery history, so
    browsing to a sheet takes it out of the queue."""
    from bot import resources

    fake.users[1] = "preclin"
    first = resources.overview("preclin")[0]

    msg = _chat_message()
    await handlers.resources_cmd(msg, SimpleNamespace(args=first.code), fake.bot)
    msg = _chat_message()
    await handlers.notes(msg, SimpleNamespace(args=None), fake.bot)

    served = [m for m in fake.bot.sent if "document" in m][-1]
    assert served["document"].path.name != first.path.name


@pytest.mark.asyncio
async def test_review_does_not_re_announce_a_finished_set(fake):
    """A /review answer re-answers a question that already counted, so it must not
    close a set again.

    answered_count counts *distinct* questions, so it does not move on a
    re-answer. Before this was guarded, a student sitting exactly on a set
    boundary was congratulated with "Set N done" after every review answer.
    """
    for qid in range(1, SET_SIZE + 1):
        add_question(fake, qid=qid, correct_idx=0)

    # Answer a full set, the last one wrongly so it lands in the review pile.
    messages = []
    for qid in range(1, SET_SIZE + 1):
        message = FakeMessage(message_id=qid)
        messages.append(message)
        wrong = qid == SET_SIZE
        await handlers.on_answer(
            FakeCallback(f"a:{qid}:{1 if wrong else 0}:practice", message=message))

    reports = [r for msg in messages for r in msg.replies if "Set" in r["text"]]
    assert len(reports) == 1, f"the set should close exactly once, got {len(reports)}"

    # Now re-answer the missed one through the route /review actually serves:
    # a fresh card, under `review` mode, which is the only mode exempt from
    # attempts_one_scoring_answer_idx.
    again = FakeMessage(message_id=99)
    await handlers.on_answer(FakeCallback("a:5:0:review", message=again))

    assert fake.attempts[(1, 99)]["mode"] == "review", "the re-answer was recorded"

    assert not [r for r in again.replies if "Set" in r["text"]], (
        "a review answer must not announce the set again")


# ------------------------------------------------------------- sets of five


def _set_of_five(fake, level="preclin", first_id=1):
    """Five questions forming one set, in id order, each with distinct text."""
    made = []
    for offset in range(5):
        qid = first_id + offset
        question = add_question(fake, qid=qid, level=level, correct_idx=0)
        question["text"] = f"Question number {qid}."
        made.append(question)
    return made


@pytest.mark.asyncio
async def test_the_set_heading_and_the_next_question(fake):
    """Five questions make one set. Answering one moves to the next, because the
    block is fixed: the same five for every student."""
    _set_of_five(fake)
    fake.users[1] = "preclin"

    await handlers.practice(_chat_message(), fake.bot)
    assert "Set 1 of 1" in fake.bot.sent[-1]["text"]
    assert "question 1 of 5" in fake.bot.sent[-1]["text"]

    await real_db.record_attempt(1, fake.questions[1], 0, True, "practice", msg_id=99)
    await handlers.practice(_chat_message(), fake.bot)

    text = fake.bot.sent[-1]["text"]
    assert "Question number 2." in text
    assert "Question number 1." not in text, "a question is not repeated inside a set"
    assert "question 2 of 5" in text


@pytest.mark.asyncio
async def test_a_wrong_answer_still_advances_the_set(fake):
    """A set is five questions, right or wrong: the benchmark moves on."""
    _set_of_five(fake)
    fake.users[1] = "preclin"
    await real_db.record_attempt(1, fake.questions[1], 3, False, "practice", msg_id=99)

    await handlers.practice(_chat_message(), fake.bot)

    assert "Question number 2." in fake.bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_finishing_a_set_reports_the_score(fake):
    """The score lands when the fifth question is answered, not before."""
    questions = _set_of_five(fake)
    fake.users[1] = "preclin"
    # Four in, three of them right, so the fifth is what completes the set.
    for offset, question in enumerate(questions[:4]):
        await real_db.record_attempt(1, question, 0, offset != 3, "practice",
                                     msg_id=100 + offset)

    message = FakeMessage()
    await handlers.on_answer(FakeCallback(f"a:{questions[4]['id']}:0:practice",
                                          message=message))

    reported = "\n".join(reply["text"] for reply in message.replies)
    assert "Set 1 of 1" in reported, message.replies
    assert "score <b>4/5</b>" in reported, message.replies


@pytest.mark.asyncio
async def test_a_set_closed_by_the_monday_push_is_reported(fake):
    """A weekly answer fills a set slot, so it can be the fifth of a set.

    db.answered_count counts every first answer, whatever door it came through,
    and the report fires on that count crossing a multiple of five - which
    happens exactly once. Gating the report on /quizme therefore did not delay
    the score, it destroyed it: the student answered five questions and was
    never told how the set went.
    """
    questions = _set_of_five(fake)
    fake.users[1] = "preclin"
    for offset, question in enumerate(questions[:4]):
        await real_db.record_attempt(1, question, 0, True, "practice",
                                     msg_id=100 + offset)

    message = FakeMessage()
    await handlers.on_answer(FakeCallback(f"a:{questions[4]['id']}:1:weekly",
                                          message=message))

    reported = "\n".join(reply["text"] for reply in message.replies)
    assert "Set 1 of 1" in reported, message.replies
    assert "score <b>4/5</b>" in reported, message.replies
    assert fake.points == {}, "and the Monday set still scores no tournament points"


@pytest.mark.asyncio
async def test_a_review_answer_never_closes_a_set(fake):
    """/review is excluded by mode as well as by the count.

    A re-answer cannot move answered_count, so this is belt and braces, but it is
    the pin that keeps "report the weekly push too" from turning into "report
    every answer".
    """
    questions = _set_of_five(fake)
    fake.users[1] = "preclin"
    for offset, question in enumerate(questions[:4]):
        await real_db.record_attempt(1, question, 0, True, "practice",
                                     msg_id=100 + offset)

    message = FakeMessage()
    await handlers.on_answer(FakeCallback(f"a:{questions[4]['id']}:0:review",
                                          message=message))

    assert not [r for r in message.replies if "Set" in r["text"]], message.replies


@pytest.mark.asyncio
async def test_a_partly_answered_set_does_not_report_a_score(fake):
    questions = _set_of_five(fake)
    fake.users[1] = "preclin"
    await real_db.record_attempt(1, questions[0], 0, True, "practice", msg_id=1)

    message = FakeMessage()
    await handlers.on_answer(FakeCallback(f"a:{questions[1]['id']}:0:practice",
                                          message=message))

    reported = "\n".join(reply["text"] for reply in message.replies)
    assert "score <b>" not in reported, "two of five is not a finished set"


@pytest.mark.asyncio
async def test_every_set_finished_says_so(fake):
    only = add_question(fake, qid=1, correct_idx=0)
    fake.users[1] = "preclin"
    await real_db.record_attempt(1, only, 0, True, "practice", msg_id=99)

    await handlers.practice(_chat_message(), fake.bot)

    assert "answered every one" in fake.bot.sent[-1]["text"].lower()


# --------------------------------------------------------------------- stats


@pytest.mark.asyncio
async def test_stats_breaks_the_running_total_down(fake):
    """Marker A: the running total, and which topics and question types to work on."""
    for qid, topic, tag, correct in ((1, "Optics", "Physiology | optics", True),
                                     (2, "Optics", "Physiology | optics", False),
                                     (3, "Retina", "Pathology | retina", True)):
        question = add_question(fake, qid=qid, correct_idx=0)
        question["topic"] = topic
        question["tag"] = tag
        await real_db.record_attempt(1, question, 0, correct, "practice", msg_id=qid)
    fake.users[1] = "preclin"

    msg = _chat_message()
    await handlers.stats_cmd(msg)

    text = msg.answer.messages[-1]
    assert "Correct <b>2</b> of 3 (67%)" in text
    # Topics are ranked by accuracy, weakest first, with the percentage spelled
    # out: there are only 6 to 18 of them per level and each carries enough
    # answers for a percentage to mean something.
    assert "Optics \u00b7 1/2 (50%)" in text
    assert "Retina \u00b7 1/1 (100%)" in text
    assert text.index("Optics \u00b7") < text.index("Retina \u00b7"), "weakest first"

    # Question types are listed, NOT ranked. `questions.tag` is close to a
    # per-question label (71 to 92 distinct types over 103 to 170 questions), so
    # a percentage on one of them would be fake precision. What the student gets
    # is which kinds of question they actually missed.
    assert "Question types you have missed" in text
    assert "Physiology | optics" in text
    assert "Pathology | retina" not in text, "they got that one right"
    assert "%)" not in text.split("Question types you have missed")[1], (
        "a type with one or two answers must not be given a percentage")


@pytest.mark.asyncio
async def test_stats_says_so_before_anything_is_answered(fake):
    add_question(fake)
    fake.users[1] = "preclin"

    msg = _chat_message()
    await handlers.stats_cmd(msg)

    assert "Nothing answered yet" in msg.answer.messages[-1]


# ---------------------------------------------------------- monthly / random


@pytest.mark.asyncio
async def test_randomnotes_never_sends_a_reserved_sheet(fake):
    """The six reserved focused sheets belong to the monthly drop, so a student
    must never be handed one at random before it goes out."""
    from bot import resources

    seen = set()
    for _ in range(60):
        msg = _chat_message()
        await handlers.randomnotes(msg, SimpleNamespace(args=None), fake.bot)
        sent = [m for m in fake.bot.sent if "document" in m]
        seen.add(sent[-1]["document"].path.name)

    # The fake user's level is preclin, which is the only level with a curated
    # monthly reserve today.
    assert seen, "a sheet should have been sent"
    # Nothing is reserved any more: deliveries are tracked, so the guarantee is
    # simply that /randomnotes never hands over the same sheet twice.
    assert len(seen) == len(resources.focused("preclin")), (
        "every focused sheet should come round exactly once")


@pytest.mark.asyncio
@pytest.mark.parametrize("level,prefix", [("preclin", "B"), ("clin", "B"),
                                          ("postmbbs", "B")])
async def test_randomnotes_serves_the_students_own_level(fake, level, prefix):
    """Codes repeat across levels, so the only proof a sheet came from the right
    place is its path. A clinical student must never be handed a preclinical
    sheet."""
    from bot import resources

    fake.users[1] = level
    msg = _chat_message()

    await handlers.randomnotes(msg, SimpleNamespace(args=None), fake.bot)

    sent = [m for m in fake.bot.sent if "document" in m][-1]
    path = sent["document"].path.as_posix()
    assert f"/notes/{level}/tier_b/" in path, path
    assert path.rsplit("/", 1)[-1].startswith(prefix)
    assert sent["document"].path.name in {
        n.path.name for n in resources.focused(level)}


@pytest.mark.asyncio
async def test_topicalnotes_lists_the_students_own_level(fake):
    """The overview picker is built from the student's level, so the clinical
    student sees C01-C07 and the post-MBBS student A01-A15."""
    from bot import resources

    for level, expected in (("preclin", "01"), ("clin", "C01"),
                            ("postmbbs", "A01")):
        fake.users[1] = level
        msg = _chat_message()

        await handlers.topicalnotes(msg, SimpleNamespace(args=None), fake.bot)

        keyboard = msg.answer.kwargs[-1]["reply_markup"].inline_keyboard
        labels = [b.text for row in keyboard for b in row]
        assert any(label.startswith(expected) for label in labels), (level, labels)
        # and nothing from another level leaked in
        own = {n.label for n in resources.overview(level)}
        assert {lab for lab in labels if lab != "⬅ Back"} <= own


@pytest.mark.asyncio
async def test_a_code_resolves_to_the_students_own_level(fake):
    """B01 exists at all three levels. Whichever one the student is on is the one
    they must get."""
    paths = {}
    for level in ("preclin", "clin", "postmbbs"):
        fake.users[1] = level
        msg = _chat_message()
        await handlers.resources_cmd(msg, SimpleNamespace(args="B01"), fake.bot)
        sent = [m for m in fake.bot.sent if "document" in m][-1]
        paths[level] = sent["document"].path.as_posix()

    for level, path in paths.items():
        assert f"/notes/{level}/tier_b/" in path, path
    assert len(set(paths.values())) == 3, "the same file was served three times"


@pytest.mark.asyncio
async def test_subscribenotes_does_not_claim_you_read_an_empty_catalogue(fake, monkeypatch):
    """An empty catalogue and a finished one both leave nothing unsent, so the
    "you have already had every sheet" branch was telling a student who had
    received none that they had read them all."""
    from bot import resources

    monkeypatch.setattr(resources, "unsent", lambda *a, **k: [])
    monkeypatch.setattr(resources, "all_for", lambda *a, **k: [])
    fake.users[1] = "preclin"
    msg = _chat_message()

    await handlers.subscribenotes(msg)

    text = msg.answer.messages[-1]
    assert "no Pre-Clinical cheat sheets on disk yet" in text
    assert "already had every" not in text
    assert ("set_flag", 1, "notes_sub", True) in fake.calls, "still subscribed"


@pytest.mark.asyncio
async def test_notes_subscription_reports_what_is_left_for_this_student(fake):
    from bot import resources

    fake.users[1] = "preclin"
    msg = _chat_message()

    await handlers.subscribenotes(msg)

    text = msg.answer.messages[-1]
    assert "1st and the 15th" in text
    assert str(len(resources.all_for("preclin"))) in text, "should count what is left"
    assert ("set_flag", 1, "notes_sub", True) in fake.calls
    # The confirmation carries its own off switch, since the stop command is not
    # in the menu.
    keyboard = msg.answer.kwargs[-1]["reply_markup"].inline_keyboard
    assert any(b.callback_data == "sub:off:notes"
               for row in keyboard for b in row)


@pytest.mark.asyncio
async def test_the_off_button_turns_a_subscription_off(fake):
    c = SimpleNamespace(data="sub:off:notes",
                        from_user=SimpleNamespace(id=1, username="t"),
                        message=SimpleNamespace(answer=_recorder(),
                                                reply_markup=None),
                        answer=_recorder(), bot=fake.bot)

    await handlers.sub_off(c)

    assert ("set_flag", 1, "notes_sub", False) in fake.calls


# ------------------------------------------------------------------- /reset


def _reset_buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_reset_asks_before_clearing_anything(fake):
    add_question(fake, level="clin")
    await handlers.on_answer(FakeCallback("a:1:0:practice", message=FakeMessage()))
    fake.users[1] = "clin"
    msg = _chat_message()
    sent = []

    async def answer(text, **kw):
        sent.append((text, kw))
    msg.answer = answer

    await handlers.reset_cmd(msg)

    text, kw = sent[-1]
    assert "Start again?" in text and "can't be undone" in text
    assert kw["parse_mode"] == "HTML"
    assert _reset_buttons(kw["reply_markup"]) == ["rs:lvl:clin", "rs:all", "rs:no"]
    assert kw["reply_markup"].inline_keyboard[0][0].text == "🔄 Reset Clinical"
    assert fake.attempts, "asking must not clear anything"
    assert not any(c[0] == "reset_progress" for c in fake.calls)


@pytest.mark.asyncio
async def test_reset_level_clears_only_that_level(fake):
    add_question(fake, qid=1, level="preclin")
    add_question(fake, qid=2, level="clin")
    await handlers.on_answer(FakeCallback("a:1:0:practice", message=FakeMessage(message_id=11)))
    await handlers.on_answer(FakeCallback("a:2:0:practice", message=FakeMessage(message_id=12)))
    card = FakeMessage(text="confirm")
    c = FakeCallback("rs:lvl:clin", message=card)

    await handlers.reset_cb(c)

    assert ("reset_progress", 1, "clin") in fake.calls
    assert [rec["qid"] for rec in fake.attempts.values()] == [1], "Pre-Clinical untouched"
    assert c.answers[-1]["text"] == "Done"
    edit = card.edits[-1]
    assert "Cleared your Clinical answers" in edit["text"]
    assert "set 1 of Clinical" in edit["text"]
    assert edit["reply_markup"] is None, "the confirmation cannot be tapped twice"


@pytest.mark.asyncio
async def test_reset_all_clears_every_level(fake):
    add_question(fake, qid=1, level="preclin")
    add_question(fake, qid=2, level="clin")
    await handlers.on_answer(FakeCallback("a:1:0:practice", message=FakeMessage(message_id=11)))
    await handlers.on_answer(FakeCallback("a:2:0:practice", message=FakeMessage(message_id=12)))
    card = FakeMessage(text="confirm")

    await handlers.reset_cb(FakeCallback("rs:all", message=card))

    assert ("reset_progress", 1, None) in fake.calls
    assert fake.attempts == {}
    assert "at every level" in card.edits[-1]["text"]


@pytest.mark.asyncio
async def test_reset_cancel_changes_nothing(fake):
    add_question(fake)
    await handlers.on_answer(FakeCallback("a:1:0:practice", message=FakeMessage()))
    card = FakeMessage(text="confirm")

    await handlers.reset_cb(FakeCallback("rs:no", message=card))

    assert fake.attempts
    assert not any(c[0] == "reset_progress" for c in fake.calls)
    assert card.edits[-1]["text"] == "Cancelled. Nothing was changed."


@pytest.mark.asyncio
async def test_reset_with_nothing_answered_says_so(fake):
    card = FakeMessage(text="confirm")

    await handlers.reset_cb(FakeCallback("rs:lvl:postmbbs", message=card))

    assert "hadn't answered any Post-MBBS questions" in card.edits[-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["rs:lvl:consultant", "rs:lvl", "rs:", "rs:lvl:clin:x",
                                  "rs:everything"])
async def test_reset_refuses_unexpected_callback_data(fake, data):
    c = FakeCallback(data, message=FakeMessage(text="confirm"))

    await handlers.reset_cb(c)

    assert c.answers and c.answers[-1]["alert"] is True
    assert not any(call[0] == "reset_progress" for call in fake.calls)
