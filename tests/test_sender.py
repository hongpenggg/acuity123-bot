"""Unit tests for the outbound plumbing: retries, flood control, pacing.

`sender` is the only module that talks to the Bot API, so what is worth testing
here is the failure modes rather than the happy path. An audit found three of
them live: a 429 on a document cost the student the PDF for good, the pacing said
nothing about how fast the bot pushed into a single chat, and a split message
dropped the keyboard from every part but the last without saying so.

No database and no network: the Bot API edge is faked and `db.deactivate` is the
only database call any of these paths make.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot import resources, sender


class Recorder:
    """The Bot API edge, and nothing above it."""

    def __init__(self, document_error: BaseException | None = None,
                 fail_times: int = 0):
        self.document_error = document_error
        self.fail_times = fail_times
        self.documents = 0
        self.messages: list[tuple[str, dict]] = []

    async def send_message(self, uid, text, **kw):
        self.messages.append((text, kw))
        return SimpleNamespace(message_id=len(self.messages))

    async def send_document(self, uid, document, **kw):
        self.documents += 1
        if self.document_error is not None and self.documents <= self.fail_times:
            raise self.document_error
        return SimpleNamespace(message_id=self.documents)


def flood(seconds: int = 1) -> TelegramRetryAfter:
    return TelegramRetryAfter(SimpleNamespace(), "flood control exceeded",
                              retry_after=seconds)


@pytest.fixture
def instant(monkeypatch):
    """Take the real waiting out of the retry path, which is 2s an attempt."""
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(sender.asyncio, "sleep", no_wait)


@pytest.fixture
def deactivated(monkeypatch):
    """Record `db.deactivate` instead of reaching for a connection pool."""
    seen: list[int] = []

    async def deactivate(uid):
        seen.append(uid)

    monkeypatch.setattr(sender.db, "deactivate", deactivate)
    return seen


@pytest.fixture
def sheet():
    note = resources.overview("preclin")[0]
    assert note.path.exists(), "the test needs a real PDF to upload"
    return note


# ------------------------------------------------- the document upload path


async def test_a_flood_controlled_upload_is_retried_not_turned_into_a_link(
        instant, sheet):
    """`send_document` had only a Forbidden branch and a bare `except`, so a
    transient 429 fell through to the GitHub link and the caller recorded the
    sheet as delivered: one 429 permanently cost the student the actual PDF."""
    bot = Recorder(document_error=flood(), fail_times=1)

    assert await sender.send_note(bot, 1, sheet, pace=False) is True
    assert bot.documents == 2, "the upload was not retried"
    assert bot.messages == [], "the PDF landed, so there is nothing to link to"


async def test_flood_control_that_outlasts_the_retries_leaves_the_sheet_unsent(
        instant, sheet):
    """Not a link: a link is a message too, so it is throttled the same way, and
    spending the delivery record on it would mean this sheet never comes round
    again. Unsent keeps it in the student's queue for the next drop."""
    bot = Recorder(document_error=flood(), fail_times=99)

    assert await sender.send_note(bot, 1, sheet, pace=False) is False
    assert bot.documents == sender._SEND_ATTEMPTS
    assert bot.messages == []


async def test_a_file_telegram_refuses_still_degrades_to_a_link(sheet):
    """The link fallback is for what cannot be retried away."""
    bot = Recorder(document_error=RuntimeError("unsupported file"), fail_times=99)

    assert await sender.send_note(bot, 1, sheet, pace=False) is True
    assert bot.documents == 1, "a rejected file is not worth a second attempt"
    assert sheet.url in bot.messages[0][0]


async def test_a_missing_file_degrades_to_a_link_without_an_upload():
    """Committed after the last `git pull` is the other thing the link is for."""
    # Under resources/notes, because the GitHub fallback link is built from the
    # path relative to the repo root.
    missing = resources.Note(
        level="preclin", tier="a", code="99", topic="Not on disk",
        path=resources.NOTES_DIR / "preclin" / "tier_a" / "99_Not_on_disk.pdf")
    assert not missing.path.exists()
    bot = Recorder()

    assert await sender.send_note(bot, 1, missing, pace=False) is True
    assert bot.documents == 0
    assert missing.url in bot.messages[0][0]


async def test_a_blocked_student_stops_the_upload_and_is_deactivated(
        deactivated, sheet):
    bot = Recorder(document_error=TelegramForbiddenError(
        SimpleNamespace(), "bot was blocked by the user"), fail_times=99)

    assert await sender.send_note(bot, 1, sheet, pace=False) is False
    assert bot.documents == 1, "there is no point trying a blocked chat again"
    assert bot.messages == [], "nor sending them a link"
    assert deactivated == [1]


# ------------------------------------------------------------- the pacer


async def test_a_chat_takes_a_burst_and_is_then_held_to_one_message_an_interval():
    """Telegram tolerates a few messages back to back and then wants about one a
    second. The fortnightly drop sends a header then documents, and the Monday
    push a header then five cards, both to one chat."""
    pacer = sender.Pacer(burst=3, interval=0.1,
                         stream_burst=1000, stream_interval=0.0)
    loop = asyncio.get_running_loop()
    start = loop.time()

    stamps = []
    for _ in range(6):
        await pacer.reserve(7)
        stamps.append(loop.time() - start)

    assert stamps[2] < 0.05, "the burst should go straight through"
    assert stamps[5] >= 3 * 0.1 * 0.9, stamps
    assert stamps[5] < 3 * 0.1 + 0.2, stamps


async def test_one_chat_waiting_does_not_hold_up_another():
    """The whole point of pacing per chat. The old 0.05s sleep was stream-wide,
    and `_send_once` then slept `retry_after + 1` inline, so one throttled chat
    stalled the fan-out to every other subscriber."""
    pacer = sender.Pacer(burst=1, interval=0.2,
                         stream_burst=1000, stream_interval=0.0)
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def four_messages(uid):
        for _ in range(4):
            await pacer.reserve(uid)
        return loop.time() - start

    elapsed = await asyncio.gather(*(four_messages(uid) for uid in range(10)))

    # Four messages to one chat is three waits however many chats there are, and
    # ten chats must not make it ten times that.
    assert max(elapsed) >= 3 * 0.2 * 0.9, elapsed
    assert max(elapsed) < 3 * 0.2 * 2, f"{max(elapsed)}s looks serialised"


async def test_the_stream_is_held_under_its_own_limit_too():
    """One message each to many chats never touches the per-chat limit, which is
    how `announce_tournament` could outrun Telegram's overall rate."""
    pacer = sender.Pacer(burst=3, interval=0.0,
                         stream_burst=5, stream_interval=0.02)
    loop = asyncio.get_running_loop()
    start = loop.time()

    for uid in range(25):
        await pacer.reserve(uid)

    assert loop.time() - start >= (25 - 5) * 0.02 * 0.9


async def test_an_idle_chat_gets_its_burst_back():
    pacer = sender.Pacer(burst=2, interval=0.05,
                         stream_burst=1000, stream_interval=0.0)
    for _ in range(2):
        await pacer.reserve(7)

    await asyncio.sleep(0.2)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(2):
        await pacer.reserve(7)

    assert loop.time() - start < 0.03


async def test_only_callers_that_ask_for_it_are_paced(monkeypatch):
    """A reply to a command or a button is one message answering one tap, already
    spaced by the student. Holding one back for a second to respect a limit only
    bulk delivery reaches would make the bot feel broken, so the handlers' sends
    never touch the pacer."""
    monkeypatch.setattr(sender, "PACER",
                        sender.Pacer(burst=1, interval=5.0,
                                     stream_burst=1, stream_interval=5.0))
    bot = Recorder()
    loop = asyncio.get_running_loop()
    start = loop.time()

    for _ in range(4):
        assert await sender.safe_send(bot, 1, "hello")

    assert loop.time() - start < 0.1, "an interactive reply was held back"
    assert len(bot.messages) == 4


async def test_a_bulk_caller_is_paced(monkeypatch):
    """The other half of the same contract: pace=True does go through the pacer,
    which is what the scheduled fan-outs pass."""
    monkeypatch.setattr(sender, "PACER",
                        sender.Pacer(burst=1, interval=0.1,
                                     stream_burst=1000, stream_interval=0.0))
    bot = Recorder()
    loop = asyncio.get_running_loop()
    start = loop.time()

    for _ in range(4):
        assert await sender.safe_send(bot, 1, "hello", pace=True)

    assert loop.time() - start >= 3 * 0.1 * 0.9


# ------------------------------------------------------- splitting a message


async def test_a_split_message_keeps_its_keyboard_on_the_last_part():
    """Telegram attaches a keyboard to one message. A question card renders its
    options last, so the buttons belong with the final part; the earlier parts
    must not carry a copy of them."""
    bot = Recorder()
    markup = sender.question_kb(1, 3, "practice")
    body = "<b>stem</b>\n\n" + "\n\n".join(
        f"paragraph {i} " + "y" * 200 for i in range(40))
    assert len(body) > sender.TELEGRAM_LIMIT

    assert await sender.safe_send(bot, 1, body, parse_mode="HTML",
                                  reply_markup=markup)

    assert len(bot.messages) > 1
    markups = [kw.get("reply_markup") for _text, kw in bot.messages]
    assert markups == [None] * (len(markups) - 1) + [markup]
    assert all(kw["parse_mode"] == "HTML" for _text, kw in bot.messages)


async def test_a_split_message_stops_at_the_first_part_that_fails(deactivated):
    """Carrying on into a chat that just refused a message only burns round
    trips, and the student cannot read half a card anyway."""
    class Blocked(Recorder):
        async def send_message(self, uid, text, **kw):
            await super().send_message(uid, text, **kw)
            raise TelegramForbiddenError(SimpleNamespace(), "bot was blocked")

    bot = Blocked()
    body = "\n\n".join(f"paragraph {i} " + "y" * 200 for i in range(40))

    assert await sender.safe_send(bot, 1, body) is False
    assert len(bot.messages) == 1
    assert deactivated == [1]


async def test_safe_send_refuses_an_empty_message():
    bot = Recorder()

    assert await sender.safe_send(bot, 1, "   ") is False
    assert bot.messages == []


def a_question(level, topic):
    """A real level and topic, so the Notes button is resolved for real rather
    than through a stub."""
    return {"id": 1, "level": level, "topic": topic, "text": "Question?",
            "options": '["a","b","c","d","e"]', "correct_idx": 0}


def notes_row(kwargs):
    """The trailing Notes button from a sent card, if there is one."""
    rows = kwargs["reply_markup"].inline_keyboard
    if len(rows) < 2:
        return None
    return rows[-1][0]


@pytest.mark.asyncio
async def test_a_question_carries_a_notes_button_for_its_topic():
    bot = Recorder()

    await sender.send_question(bot, 7, a_question(
        "preclin", "Optics and visual transduction"), "practice")

    _, kwargs = bot.messages[-1]
    button = notes_row(kwargs)
    assert button is not None, "the card should offer the topic's sheet"
    assert button.text == "📘 Notes for this topic"
    # The sheet itself travels in the callback. A `/notes 03` line in the text
    # did not survive being tapped: Telegram only makes the command word
    # tappable, so the tap arrived as a bare `/notes` and sent the wrong sheet.
    assert button.callback_data == "res:get:preclin:a:03"
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_the_option_buttons_are_untouched_by_the_notes_row():
    bot = Recorder()

    await sender.send_question(bot, 7, a_question(
        "preclin", "Optics and visual transduction"), "practice")

    _, kwargs = bot.messages[-1]
    rows = kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 2, "options on one row, Notes on its own below them"
    assert [b.text for b in rows[0]] == ["1", "2", "3", "4", "5"]
    assert [b.callback_data for b in rows[0]] == [
        "a:1:0:practice", "a:1:1:practice", "a:1:2:practice",
        "a:1:3:practice", "a:1:4:practice"]


@pytest.mark.asyncio
async def test_a_postmbbs_question_finds_its_sheet_through_the_title():
    bot = Recorder()

    await sender.send_question(bot, 7, a_question(
        "postmbbs", "Uveitis and inflammatory medicine"), "practice")

    _, kwargs = bot.messages[-1]
    assert notes_row(kwargs).callback_data == "res:get:postmbbs:a:A07"


@pytest.mark.asyncio
async def test_a_topic_with_no_sheet_carries_no_notes_button():
    """No sheet for the topic means no button, rather than one that would send
    the wrong sheet or an error."""
    bot = Recorder()

    await sender.send_question(bot, 7, a_question("postmbbs", "Pathology"),
                               "practice")

    _, kwargs = bot.messages[-1]
    rows = kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1, "options only"
    assert all(b.callback_data.startswith("a:") for row in rows for b in row)
