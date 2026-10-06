"""Outbound Telegram plumbing shared by the handlers and the scheduled jobs.

Lives in its own module so `handlers` and `jobs` can both use it without
importing each other.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup)
from cachetools import TTLCache

from . import db
from .config import LEVELS
from .resources import MAX_UPLOAD_BYTES, for_topic
from .text import (MAX_OPTIONS, TELEGRAM_LIMIT, WEEKLY_HEADER, chunks, letter,
                   notes_line, render)

log = logging.getLogger(__name__)

_SEND_ATTEMPTS = 3
# One row of letters: five A-E buttons on a 4-wide grid left E stranded alone.
_BUTTONS_PER_ROW = MAX_OPTIONS

# Telegram has two limits and they are different animals. Roughly one message a
# second to any single chat, which is the one a fan-out to a handful of
# subscribers trips over; and roughly thirty a second across all chats, which
# only a fan-out to hundreds reaches. Both allow a short burst first.
CHAT_BURST = 3
CHAT_INTERVAL = 1.0
STREAM_BURST = 30
STREAM_INTERVAL = 1 / 25


class Pacer:
    """Spaces bulk sends: per chat, and across the whole outbound stream.

    The old pacing was a single 0.05s sleep between sends (jobs.FANOUT_PAUSE).
    That held the stream under ~30/s correctly and said nothing at all about one
    chat, which is the limit that actually bites: the fortnightly drop sends a
    header and then the documents to *one* chat back to back, and the Monday push
    sends a header and five cards. So the push invited the 429s that `_send_once`
    then answered by sleeping inline, holding up the sequential fan-out to every
    other subscriber as well.

    A burst allowance rather than a flat gap, because Telegram tolerates a few
    messages back to back. The chat waits are per chat, so a chat serving out its
    gap does not hold up anybody else, provided the caller runs several chats at
    once (`jobs._fan_out` does); the stream wait is what stops that concurrency
    turning into a 429 of its own.

    Only callers that pass `pace=True` come through here, which in practice means
    the scheduled fan-outs. A reply to a command or a button is one message
    answering one tap, already spaced by the student, and holding one back for a
    second to respect a limit only bulk delivery reaches would make the bot feel
    broken.
    """

    def __init__(self, burst: int = CHAT_BURST, interval: float = CHAT_INTERVAL,
                 stream_burst: int = STREAM_BURST,
                 stream_interval: float = STREAM_INTERVAL) -> None:
        self.burst = burst
        self.interval = interval
        self.stream_burst = stream_burst
        self.stream_interval = stream_interval
        # The time an unthrottled stream of sends would have reached, per chat
        # and overall. `burst * interval` ahead of now means the burst is spent.
        self._chat_ready: dict[int, float] = {}
        self._stream_ready = 0.0

    async def reserve(self, uid: int) -> None:
        """Wait until this chat may take another message, then claim the slot."""
        while True:
            now = asyncio.get_running_loop().time()
            chat = max(self._chat_ready.get(uid, now), now)
            stream = max(self._stream_ready, now)
            delay = max(self._owed(chat, now, self.burst, self.interval),
                        self._owed(stream, now, self.stream_burst,
                                   self.stream_interval))
            if delay <= 0:
                # Both slots claimed with no await in between, so two tasks
                # cannot come away having booked the same one.
                self._chat_ready[uid] = chat + self.interval
                self._stream_ready = stream + self.stream_interval
                self._forget_idle(now)
                return
            await asyncio.sleep(delay)

    @staticmethod
    def _owed(ready: float, now: float, burst: int, interval: float) -> float:
        """How long a sender must wait, once the burst allowance is counted."""
        return (ready - now) - (burst - 1) * interval

    def _forget_idle(self, now: float) -> None:
        # One entry per chat the bot has ever sent to would grow without bound in
        # a process that stays up for months.
        if len(self._chat_ready) > 1024:
            self._chat_ready = {uid: ready
                                for uid, ready in self._chat_ready.items()
                                if ready > now}


PACER = Pacer()

# A card the student has been served but not yet answered is invisible to
# db.pick_question, which ranks on the `attempts` table: two /quizme taps without
# an answer in between could hand back the same question, leaving two live cards
# for one question. The partial unique index on `attempts` then refuses the
# second answer, so the student is told "You've already answered this one" about
# a card they are genuinely seeing for the first time.
#
# Kept here rather than in the database because "outstanding" is a property of
# what is on screen, not of the student's history: there is no row worth writing
# and nothing to reconcile after a restart, where serving the card again is the
# right answer anyway. `handlers._recent_explain` holds its state the same way.
#
# Keyed by chat, holding the ids of the last few cards sent there, because a
# student can legitimately have several live cards: one per tap they have not got
# round to answering. Only the last few, because somebody with five cards in the
# air is past the point an exclude list helps.
#
# The TTL is the backstop, not the mechanism. A card stops being live for reasons
# this module cannot see - the student answered it, or a handler struck its
# buttons because the question row no longer matches what was sent - so callers
# tell us with `forget_outstanding`. Without that the id sits here for the full
# TTL and, when it is the last unanswered question at that level, the student is
# told to answer a card that may no longer be answerable.
#
# Module-level state that outlives one test, so a test driving /quizme has to
# reset it with `forget_outstanding()` or test order decides whether a question
# is served at all.
_MAX_OUTSTANDING = 5
_outstanding: TTLCache = TTLCache(maxsize=10_000, ttl=900)


def forget_outstanding(uid: int | None = None, qid: int | None = None) -> None:
    """A card is no longer live, so stop excluding its question from the pool.

    `uid` and `qid` for one card, `uid` alone for every card in that chat, and
    neither for all of them, which is what a test wants between cases. Ids that
    are not there are fine: a caller should not have to know whether the TTL has
    already expired.
    """
    if uid is None:
        _outstanding.clear()
        return
    if qid is None:
        _outstanding.pop(uid, None)
        return
    kept = tuple(seen for seen in _outstanding.get(uid, ()) if seen != qid)
    if kept:
        _outstanding[uid] = kept
    else:
        _outstanding.pop(uid, None)


async def _send_once(bot: Bot, uid: int, text: str, *,
                     pace: bool = False, **kw) -> bool:
    for attempt in range(_SEND_ATTEMPTS):
        try:
            if pace:
                await PACER.reserve(uid)
            await bot.send_message(uid, text, **kw)
            return True
        except TelegramForbiddenError:
            # Blocked the bot or deleted the chat: stop trying, forever.
            await db.deactivate(uid)
            return False
        except TelegramRetryAfter as exc:
            if attempt == _SEND_ATTEMPTS - 1:
                # Loud, because the message is now lost: a student promised five
                # questions gets however many landed and no explanation.
                log.error("flood control: %s never got a message, gave up after "
                          "%s attempt(s) (retry_after %ss)",
                          uid, _SEND_ATTEMPTS, exc.retry_after)
                return False
            # Actually resend after waiting; the old code slept and then dropped
            # the message on the floor.
            await asyncio.sleep(exc.retry_after + 1)
        except Exception:
            log.exception("send to %s failed", uid)
            return False
    return False


async def safe_send(bot: Bot, uid: int, text: str, *,
                    pace: bool = False, **kw) -> bool:
    """Send text, splitting anything over Telegram's 4096-character cap.

    Never raises: a blocked user is deactivated, a failed send is logged. Long
    bodies are split rather than silently truncated, and `chunks` keeps each part
    valid HTML so a tag spanning the cut cannot get both parts rejected.

    `pace` spaces this chat's sends against Telegram's per-chat limit; see
    `Pacer` for why only bulk callers want it.
    """
    parts = chunks(text, TELEGRAM_LIMIT)
    if not parts:
        log.warning("refusing to send empty message to %s", uid)
        return False
    if len(parts) > 1 and kw.get("reply_markup") is not None:
        # Telegram attaches a keyboard to one message, so it rides the last part:
        # a question card renders its options last, which is what the buttons
        # answer. A card long enough to split would still leave some options on
        # the part without the buttons, so say so rather than let it look fine.
        log.warning("splitting a %s-character message for %s into %s parts; its "
                    "keyboard can only go on the last one", len(text), uid, len(parts))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        opts = kw if last else {k: v for k, v in kw.items() if k != "reply_markup"}
        if not await _send_once(bot, uid, part, pace=pace, **opts):
            return False
    return True


def question_kb(qid: int, count: int, mode: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=letter(i), callback_data=f"a:{qid}:{i}:{mode}")
        for i in range(count)
    ]
    rows = [buttons[i:i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_header(mode: str) -> str | None:
    """The banner above a card, when the caller has not supplied its own lead.

    `jobs.weekly_quiz` passes a lead carrying the position in the set, so it
    would be a second header on the same card; the banner is for a weekly card
    sent without one.
    """
    return WEEKLY_HEADER if mode == "weekly" else None


def notes_footer(question) -> str | None:
    """The Notes pointer for a question's topic, or None if no sheet covers it.

    Read off the question's own level and topic, so the same card points at the
    same sheet no matter which command sent it, and a question in a topic with no
    sheet yet simply goes without the line rather than showing a dead reference.
    """
    note = for_topic(question.get("level"), question.get("topic") or "")
    return notes_line(note.code) if note else None


async def send_question(bot: Bot, uid: int, question, mode: str,
                        lead: str | None = None, *, pace: bool = False) -> bool:
    """Send one question card. `lead` replaces the mode's default banner."""
    if question is None:
        return await safe_send(bot, uid, "No questions loaded yet. Check back soon!",
                               pace=pace)
    try:
        body, count = render(question,
                             header=None if lead else card_header(mode),
                             footer=notes_footer(question))
    except ValueError:
        # Malformed row (wrong number of options). Report it instead of crashing.
        log.exception("question %s is malformed", question.get("id"))
        return await safe_send(
            bot, uid, "That question has a problem on our side, so we skipped it. "
                      "Tap /quizme for another one.", pace=pace)
    if lead:
        body = f"{lead}\n{body}"
    return await safe_send(bot, uid, body, parse_mode="HTML", pace=pace,
                           reply_markup=question_kb(question["id"], count, mode))


async def send_question_for_level(bot: Bot, uid: int, level: str, mode: str) -> bool:
    """The next question for this student, chosen for them.

    A set is a rolling block of five answers; which question fills the next slot
    is `db.pick_question`'s decision. It spreads topics across the set and leans
    toward the topics this student keeps getting wrong, so a set is varied, two
    students do not walk an identical path, and weak material comes back round.
    Questions already attempted are never re-served here, which is what `/review`
    is for.

    Three states deserve a sentence rather than silence: their level has no bank
    yet, they have answered everything, or there is a question to serve.
    """
    _, total = await db.progress(uid, level)
    if total == 0:
        available = await db.levels_with_questions()
        if not available:
            return await send_question(bot, uid, None, mode)
        names = " and ".join(LEVELS.get(name, name) for name in available)
        return await safe_send(
            bot, uid,
            f"🚧 No {LEVELS.get(level, level)} questions yet, we're still writing them.\n\n"
            f"For now you can practise {names}. Switch with /changestreams.",
        )

    current = await db.current_set(uid, level)
    if current is None:
        return await safe_send(
            bot, uid,
            f"🏁 You have answered every one of the {total} "
            f"{LEVELS.get(level, level)} questions. /review the ones you missed, "
            "/stats for your breakdown, or /changestreams to switch level.",
        )

    outstanding = _outstanding.get(uid, ())
    question = await db.pick_question(uid, level, outstanding)
    if question is None and outstanding:
        # Everything left is already sitting unanswered in this chat. Saying so
        # beats re-serving a live card, and beats the "something went wrong" line
        # below, which is for a state that should not happen.
        return await safe_send(
            bot, uid,
            "📋 You already have a question waiting just above. Answer that one "
            "and I'll send the next.")
    if question is None:
        # current_set said there is room in the set, so there should be a question
        # to fill it. Guarded rather than trusted: the two read the same tables but
        # not in the same transaction.
        log.error("no question available for %s at %s despite an open set", uid, level)
        return await safe_send(
            bot, uid, "I could not find your next question. Try /quizme again.")

    position = current["answered_in_set"] + 1
    lead = (f"📋 <b>Set {current['number']} of {current['total_sets']}</b> · "
            f"question {position} of {current['size']}")
    if not await send_question(bot, uid, question, mode, lead=lead):
        return False
    _outstanding[uid] = (*outstanding, question["id"])[-_MAX_OUTSTANDING:]
    return True


async def _upload_note(bot: Bot, uid: int, note, *, pace: bool) -> bool | None:
    """Upload the PDF itself, with the retries the text path has.

    True the student has it, False do not try anything else for them (blocked, or
    flood control that outlasted the retries), None the upload is not going to
    work and the link is the right answer.

    The retries are the point. Flood control is transient and the document is
    what the student actually wants, so a 429 used to fall straight through the
    bare `except` into a GitHub link, and the caller then recorded the sheet as
    delivered: one 429 permanently cost them the PDF.
    """
    for attempt in range(_SEND_ATTEMPTS):
        try:
            if pace:
                await PACER.reserve(uid)
            await bot.send_document(
                uid, FSInputFile(note.path, filename=note.path.name),
                caption=note.caption)
            return True
        except TelegramForbiddenError:
            await db.deactivate(uid)
            return False
        except TelegramRetryAfter as exc:
            if attempt == _SEND_ATTEMPTS - 1:
                # Not a link: the link is a message too, so it would be
                # throttled the same way, and spending the delivery record on it
                # would mean this sheet never comes round again. Leaving it
                # unsent keeps it in the student's queue for the next drop.
                log.error("flood control: %s never got %s, gave up after %s "
                          "attempt(s) (retry_after %ss)",
                          uid, note.relpath, _SEND_ATTEMPTS, exc.retry_after)
                return False
            await asyncio.sleep(exc.retry_after + 1)
        except Exception:
            # Telegram refusing this particular file, or anything else we cannot
            # retry our way out of. The student should still end up with
            # something readable.
            log.warning("could not upload %s, falling back to a link",
                        note.relpath, exc_info=True)
            return None
    return False


async def send_note(bot: Bot, uid: int, note, *, pace: bool = False) -> bool:
    """Send a revision sheet as a PDF, falling back to a GitHub link.

    Telegram caps bot uploads at 50 MB, and the file may simply be missing from
    the deployed working copy (committed after the last `git pull`). That, and a
    file Telegram rejects outright, is what the link is for: the student should
    end up with something usable rather than nothing. A 429 is not one of those
    cases and is retried instead, by `_upload_note`.
    """
    if note.path.exists() and note.size <= MAX_UPLOAD_BYTES:
        uploaded = await _upload_note(bot, uid, note, pace=pace)
        if uploaded is not None:
            return uploaded
    else:
        log.warning("sheet %s not available locally (%s bytes)",
                    note.relpath, note.size)

    return await safe_send(
        bot, uid,
        f"{note.caption}\n\nCould not attach the file, so it is here on GitHub:\n"
        f"{note.url}", pace=pace)


async def ack(c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    """Close the client-side spinner on a callback query.

    Every callback path must call this exactly once or the user's Telegram client
    spins forever. Answering twice, or after the query expired, raises, so this
    must never propagate.
    """
    try:
        await c.answer(text, show_alert=alert)
    except Exception:
        log.debug("callback answer failed", exc_info=True)


async def edit_in_place(message, text: str,
                        keyboard: InlineKeyboardMarkup | None) -> bool:
    """Edit a card's HTML text and buttons. False if Telegram refused the edit."""
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        return True
    except Exception as exc:
        log.info("edit of message %s failed (%s)",
                 getattr(message, "message_id", "?"), exc)
        return False


async def deliver_verdict(c: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Replace the question message with the scored version.

    Telegram refuses edits more than 48 hours after the message was sent, which a
    Monday weekly question answered on Thursday hits, and a double tap whose text
    is unchanged raises too. Falling back to a new message means the user always
    sees their result, instead of the attempt being recorded invisibly.
    """
    message = getattr(c, "message", None)
    if message is None:
        return
    if await edit_in_place(message, text, keyboard):
        return
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        log.exception("could not deliver verdict for message %s",
                      getattr(message, "message_id", "?"))


def remaining_buttons(message, drop) -> InlineKeyboardMarkup | None:
    """The message's current keyboard minus every button `drop(data)` matches.

    Read from the message itself, so the bot needs no record of which buttons a
    card still shows. None when nothing is left.
    """
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        return None
    rows = [[b for b in row if not drop(b.callback_data or "")]
            for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def drop_buttons(message, drop) -> None:
    """Remove used buttons from an old card so the chat doesn't fill up with
    stale Next buttons. Purely cosmetic: failures are ignored."""
    if message is None or getattr(message, "reply_markup", None) is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=remaining_buttons(message, drop))
    except Exception:
        log.debug("could not trim buttons", exc_info=True)


__all__ = [
    "PACER",
    "Pacer",
    "ack",
    "forget_outstanding",
    "card_header",
    "deliver_verdict",
    "drop_buttons",
    "edit_in_place",
    "remaining_buttons",
    "question_kb",
    "safe_send",
    "send_note",
    "send_question",
    "send_question_for_level",
]
