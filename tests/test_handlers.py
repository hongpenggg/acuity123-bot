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
from bot import handlers, jobs

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
        self.streak = 0

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

    def _correct_ids(self, uid):
        """Question ids this user has already answered correctly."""
        return {rec["qid"] for (u, _), rec in self.attempts.items()
                if u == uid and rec["correct"]}

    def _attempted_ids(self, uid):
        return {rec["qid"] for (u, _), rec in self.attempts.items() if u == uid}

    def _level_questions(self, level):
        return sorted((q for q in self.questions.values() if q["level"] == level),
                      key=lambda q: q["id"])

    async def set_board(self, uid, level):
        """Mirrors bot.db: consecutive blocks of five, in id order."""
        questions = self._level_questions(level)
        attempted = self._attempted_ids(uid)
        correct = self._correct_ids(uid)
        board = []
        for index in range(0, len(questions), SET_SIZE):
            chunk = questions[index:index + SET_SIZE]
            board.append({
                "set_no": index // SET_SIZE,
                "size": len(chunk),
                "answered": sum(1 for q in chunk if q["id"] in attempted),
                "correct": sum(1 for q in chunk if q["id"] in correct),
            })
        return board

    async def quiz_set(self, uid, level):
        board = await self.set_board(uid, level)
        current = next((b for b in board if b["answered"] < b["size"]), None)
        if current is None:
            return None
        questions = self._level_questions(level)
        chunk = questions[current["set_no"] * SET_SIZE:
                          (current["set_no"] + 1) * SET_SIZE]
        attempted = self._attempted_ids(uid)
        return {**current, "number": current["set_no"] + 1,
                "total_sets": len(board),
                "questions": chunk,
                "remaining": [q for q in chunk if q["id"] not in attempted]}

    async def set_no_for(self, level, qid):
        ids = [q["id"] for q in self._level_questions(level)]
        return ids.index(qid) // SET_SIZE if qid in ids else 0

    async def set_score(self, uid, level, set_no):
        board = await self.set_board(uid, level)
        if not 0 <= set_no < len(board):
            return None
        return {**board[set_no], "total_sets": len(board),
                "total_questions": len(self._level_questions(level))}

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

    async def record_attempt(self, uid, question, idx, correct, mode, msg_id):
        if (uid, msg_id) in self.attempts:
            return False
        self.attempts[(uid, msg_id)] = {"idx": idx, "correct": correct,
                                        "qid": question["id"]}
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
        return []


@pytest.fixture
def fake(monkeypatch):
    fake_db = FakeDB()
    for name, value in vars(FakeDB).items():
        if callable(value) and not name.startswith("_"):
            monkeypatch.setattr(real_db, name, getattr(fake_db, name))
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
    assert text.startswith("👋 <b>Hi &lt;Zay&gt;!</b>")
    assert "/quizme" in text and "Acuity Team" in text


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
    assert "lkceye" in text
    assert "WHAT THIS BOT DOES" in text
    assert "3a." in text and "3b." in text
    assert "Pre-Clinical" in text
    assert "/changestreams" in text


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
    c = FakeCallback("res:get:b:B14", message=FakeMessage())

    await handlers.resources_cb(c, fake.bot)

    assert c.answers, "the client spinner must always be closed"
    assert any("document" in m for m in fake.bot.sent)


@pytest.mark.asyncio
async def test_unknown_sheet_code_is_handled(fake):
    msg = _chat_message()

    await handlers.resources_cmd(msg, SimpleNamespace(args="zzz"), fake.bot)

    assert "No sheet matches" in msg.answer.messages[-1]


@pytest.mark.asyncio
async def test_notes_falls_back_to_the_sheets_when_the_table_is_empty(fake):
    """The notes table is empty; /notes must not dead-end."""
    msg = _chat_message()

    await handlers.notes(msg, SimpleNamespace(args=None), fake.bot)

    assert "Revision sheets" in msg.answer.messages[-1]


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
    assert "Correct: 2 of 3 (67%)" in text
    assert "Optics: 1/2" in text
    assert "Retina: 1/1" in text
    assert "Weakest here: Optics" in text
    assert "Physiology | optics: 1/2" in text, "broken down by question type too"


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

    reserved = {note.path.name for note in resources.MONTHLY}
    assert seen, "a sheet should have been sent"
    assert not (seen & reserved), seen & reserved


@pytest.mark.asyncio
async def test_monthly_subscription_reports_what_it_sends(fake):
    msg = _chat_message()

    await handlers.monthlynotes(msg)

    text = msg.answer.messages[-1]
    assert "overview sheets" in text
    assert "focused ones" in text
    assert ("set_flag", 1, "notes_sub", True) in fake.calls
