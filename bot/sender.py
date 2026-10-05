"""Outbound Telegram plumbing shared by the handlers and the scheduled jobs.

Lives in its own module so `handlers` and `jobs` can both use it without
importing each other.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import LEVELS
from .text import TELEGRAM_LIMIT, chunks, letter, render

log = logging.getLogger(__name__)

_SEND_ATTEMPTS = 3
_BUTTONS_PER_ROW = 4


async def _send_once(bot: Bot, uid: int, text: str, **kw) -> bool:
    for attempt in range(_SEND_ATTEMPTS):
        try:
            await bot.send_message(uid, text, **kw)
            return True
        except TelegramForbiddenError:
            # Blocked the bot or deleted the chat — stop trying, forever.
            await db.deactivate(uid)
            return False
        except TelegramRetryAfter as exc:
            if attempt == _SEND_ATTEMPTS - 1:
                log.warning("flood control: giving up on %s after %ss",
                            uid, exc.retry_after)
                return False
            # Actually resend after waiting; the old code slept and then dropped
            # the message on the floor.
            await asyncio.sleep(exc.retry_after + 1)
        except Exception:
            log.exception("send to %s failed", uid)
            return False
    return False


async def safe_send(bot: Bot, uid: int, text: str, **kw) -> bool:
    """Send text, splitting anything over Telegram's 4096-character cap.

    Never raises: a blocked user is deactivated, a failed send is logged. Long
    bodies are split rather than silently truncated.
    """
    parts = chunks(text, TELEGRAM_LIMIT)
    if not parts:
        log.warning("refusing to send empty message to %s", uid)
        return False
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        opts = kw if last else {k: v for k, v in kw.items() if k != "reply_markup"}
        if not await _send_once(bot, uid, part, **opts):
            return False
    return True


def question_kb(qid: int, count: int, mode: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=letter(i), callback_data=f"a:{qid}:{i}:{mode}")
        for i in range(count)
    ]
    rows = [buttons[i:i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_question(bot: Bot, uid: int, question, mode: str) -> bool:
    if question is None:
        return await safe_send(bot, uid, "No questions available yet — check back soon.")
    try:
        body, count = render(question)
    except ValueError:
        # Malformed row (wrong number of options). Report it instead of crashing.
        log.exception("question %s is malformed", question.get("id"))
        return await safe_send(bot, uid, "That question is mis-configured — skipping it.")
    return await safe_send(bot, uid, body, reply_markup=question_kb(question["id"], count, mode))


async def send_question_for_level(bot: Bot, uid: int, level: str, mode: str) -> bool:
    """A question at this level, or a clear explanation of why there isn't one.

    The Clinical and Post-MBBS banks are still being written, so a student who
    picks one of those levels must be told what is available rather than being
    met with silence or a crash.
    """
    question = await db.pick_question(uid, level)
    if question is not None:
        return await send_question(bot, uid, question, mode)

    available = await db.levels_with_questions()
    if not available:
        return await send_question(bot, uid, None, mode)

    names = ", ".join(LEVELS.get(name, name) for name in available)
    return await safe_send(
        bot, uid,
        f"No {LEVELS.get(level, level)} questions yet — that bank is still being "
        f"written.\nAvailable now: {names}.\nSwitch with /level.",
    )


async def ack(c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    """Close the client-side spinner on a callback query.

    Every callback path must call this exactly once or the user's Telegram client
    spins forever. Answering twice, or after the query expired, raises — so this
    must never propagate.
    """
    try:
        await c.answer(text, show_alert=alert)
    except Exception:
        log.debug("callback answer failed", exc_info=True)


async def deliver_verdict(c: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Replace the question message with the scored version.

    Telegram refuses edits more than 48 hours after the message was sent, which a
    Monday weekly question answered on Thursday hits — and a double tap whose text
    is unchanged raises too. Falling back to a new message means the user always
    sees their result, instead of the attempt being recorded invisibly.
    """
    message = getattr(c, "message", None)
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=keyboard)
        return
    except Exception as exc:
        log.info("edit of message %s failed (%s); sending a new message",
                 getattr(message, "message_id", "?"), exc)
    try:
        await message.answer(text, reply_markup=keyboard)
    except Exception:
        log.exception("could not deliver verdict for message %s",
                      getattr(message, "message_id", "?"))


__all__ = [
    "ack",
    "deliver_verdict",
    "question_kb",
    "safe_send",
    "send_question",
    "send_question_for_level",
]
