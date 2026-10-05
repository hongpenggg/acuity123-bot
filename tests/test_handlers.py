"""Handler behaviour, exercised against an in-memory stand-in for the DB layer.

These cover the failure modes the old code had: a crash that leaves the user's
client spinning, an attempt recorded without the verdict ever being shown, and
the >4-option crash.
"""
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot import db as real_db
from bot import handlers, jobs

# --------------------------------------------------------------------- fakes


class FakeBot:
    def __init__(self):
        self.sent = []
        self.commands = None

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def set_my_commands(self, commands):
        self.commands = commands


class FakeMessage:
    def __init__(self, text="Question body", message_id=10, fail_edit=False):
        self.text = text
        self.message_id = message_id
        self.fail_edit = fail_edit
        self.edits = []
        self.replies = []

    async def edit_text(self, text, **kw):
        if self.fail_edit:
            raise TelegramBadRequest(SimpleNamespace(), "message can't be edited")
        self.edits.append({"text": text, **kw})
        return True

    async def answer(self, text, **kw):
        self.replies.append({"text": text, **kw})
        return SimpleNamespace(message_id=999)


class FakeCallback:
    def __init__(self, data, user_id=1, message=None):
        self.data = data
        self.message = message
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

    # -- users
    async def upsert_user(self, uid, username):
        self.users.setdefault(uid, "preclin")

    async def get_level(self, uid):
        return self.users.get(uid)

    async def set_level(self, uid, level):
        self.users[uid] = level

    async def set_flag(self, uid, col, value):
        self.calls.append(("set_flag", uid, col, value))

    async def deactivate(self, uid):
        self.users.pop(uid, None)

    # -- questions
    async def get_question(self, qid):
        return self.questions.get(qid)

    async def pick_question(self, uid, level, topic=None):
        for q in self.questions.values():
            if q["level"] == level:
                return q
        return None

    async def topics(self, level):
        return sorted({q["topic"] for q in self.questions.values() if q["level"] == level})

    async def levels_with_questions(self):
        return sorted({q["level"] for q in self.questions.values()})

    async def record_attempt(self, uid, question, idx, correct, mode, msg_id):
        if (uid, msg_id) in self.attempts:
            return False
        self.attempts[(uid, msg_id)] = idx
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
        return []


@pytest.fixture
def fake(monkeypatch):
    fake_db = FakeDB()
    for name, value in vars(FakeDB).items():
        if callable(value) and not name.startswith("_"):
            monkeypatch.setattr(real_db, name, getattr(fake_db, name))
    fake_db.bot = FakeBot()
    return fake_db


def add_question(fake, qid=1, n_options=4, correct_idx=0, level="preclin"):
    options = [f"option {i}" for i in range(n_options)]
    fake.questions[qid] = {
        "id": qid, "level": level, "topic": "Sample", "text": "Question?",
        "options": json.dumps(options), "correct_idx": correct_idx,
        "explanation": None,
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
    assert "✅ Correct!" in message.edits[-1]["text"]
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

    assert "✅ Correct!" in message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_wrong_answer_shows_the_right_option(fake):
    add_question(fake, correct_idx=2, n_options=4)
    message = FakeMessage()
    c = FakeCallback("a:1:0:practice", message=message)

    await handlers.on_answer(c)

    assert "❌ Wrong" in message.edits[-1]["text"]
    assert "C. option 2" in message.edits[-1]["text"]
    assert fake.points == {}


@pytest.mark.asyncio
async def test_double_tap_is_idempotent(fake):
    add_question(fake)
    message = FakeMessage()
    await handlers.on_answer(FakeCallback("a:1:0:practice", message=message))
    second = FakeCallback("a:1:0:practice", message=message)

    await handlers.on_answer(second)

    assert second.answers[-1]["text"] == "Already answered"
    assert len(message.edits) == 1


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
async def test_edit_failure_still_delivers_the_verdict(fake):
    """Telegram refuses edits after 48h (a Monday weekly question answered on
    Thursday). The old code recorded the attempt and then threw, so the user was
    locked out of ever seeing the answer."""
    add_question(fake)
    message = FakeMessage(fail_edit=True)
    c = FakeCallback("a:1:0:practice", message=message)

    await handlers.on_answer(c)

    assert message.replies, "must fall back to a fresh message"
    assert "✅ Correct!" in message.replies[-1]["text"]


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


@pytest.mark.asyncio
async def test_explain_rate_limit(fake, monkeypatch):
    add_question(fake, correct_idx=0)

    async def fake_explain(question):
        return "because"

    monkeypatch.setattr(handlers.llm, "explain", fake_explain)
    handlers._recent_explain.clear()

    first = FakeCallback("e:1", message=FakeMessage())
    await handlers.on_explain(first, fake.bot)
    assert any("because" in m["text"] for m in fake.bot.sent)

    second = FakeCallback("e:1", message=FakeMessage())
    await handlers.on_explain(second, fake.bot)
    assert second.answers[-1]["alert"] is True
    assert len(fake.bot.sent) == 1


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

    await handlers.on_explain(FakeCallback("e:1", message=FakeMessage()), fake.bot)

    assert any("fissure failed to close" in m["text"] for m in fake.bot.sent)
    assert any("not clinical advice" in m["text"] for m in fake.bot.sent)


@pytest.mark.asyncio
async def test_explain_says_so_when_there_is_nothing_to_show(fake, monkeypatch):
    """No stored explanation and no provider: a plain message, not an error."""
    add_question(fake, correct_idx=0)

    async def no_explanation(question):
        return None

    monkeypatch.setattr(handlers.llm, "explain", no_explanation)
    handlers._recent_explain.clear()

    await handlers.on_explain(FakeCallback("e:1", message=FakeMessage()), fake.bot)

    assert fake.bot.sent[-1]["text"] == "No written explanation for this question yet."


@pytest.mark.asyncio
async def test_level_callback_updates_the_user(fake):
    c = FakeCallback("lv:clin")

    await handlers.set_level(c)

    assert fake.users[1] == "clin"
    assert c.answers[-1]["text"] == "Level set to Clinical"


@pytest.mark.asyncio
async def test_level_callback_rejects_unknown_value(fake):
    c = FakeCallback("lv:consultant")

    await handlers.set_level(c)

    assert fake.users.get(1) != "consultant"
    assert c.answers[-1]["alert"] is True


@pytest.mark.asyncio
async def test_tournament_join_then_leave(fake):
    fake.tournament = {"id": 1, "ends_at": None}
    joined = SimpleNamespace(from_user=SimpleNamespace(id=1, username="t"),
                             answer=_recorder())
    await handlers.tournament(joined)
    assert fake.joined == {1}
    assert "You're in!" in joined.answer.messages[-1]

    await handlers.tournament(joined)
    assert fake.joined == set()
    assert "You left the tournament" in joined.answer.messages[-1]


def _recorder():
    async def _answer(text, **kw):
        _answer.messages.append(text)
        return SimpleNamespace(message_id=1)

    _answer.messages = []
    return _answer


@pytest.mark.asyncio
async def test_leaderboard_masks_the_last_two_characters(fake):
    fake.tournament = {"id": 1, "ends_at": None}
    fake.joined = {7, 8, 9}
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=_recorder())

    await handlers.leaderboard(msg)

    text = msg.answer.messages[-1]
    assert "@hongpeng**" in text
    # No username on file falls back to a uid-derived label.
    assert "user8" in text


@pytest.mark.asyncio
async def test_admin_commands_are_ignored_for_non_admins(fake, monkeypatch):
    msg = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=_recorder())
    await handlers.admin_tournament_start(msg)
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
    assert "/level" in text


@pytest.mark.asyncio
async def test_practice_with_an_empty_database_says_so(fake):
    fake.users[1] = "preclin"
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1, username="t"), answer=_recorder())

    await handlers.practice(msg, fake.bot)

    assert fake.bot.sent[-1]["text"] == "No questions available yet — check back soon."
