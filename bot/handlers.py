"""Command and callback handlers."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from cachetools import TTLCache
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import db, jobs, llm
from .config import ADMIN_IDS, CREDIT, DEFAULT_LEVEL, DISCLAIMER, LEVEL_EMOJI, LEVELS, TZ
from .sender import (ack, card_header, deliver_verdict, drop_buttons, edit_in_place,
                     remaining_buttons, safe_send, send_question_for_level)
from .text import (EXPLANATION_HEADING, TELEGRAM_LIMIT, esc, explanation_block, mask,
                   parse_options, render, verdict)

log = logging.getLogger(__name__)
router = Router()

_EXPLAIN_COOLDOWN = 10.0
# Bounded and self-expiring: the old plain dict grew forever and survived restarts
# with stale timestamps.
_recent_explain: TTLCache = TTLCache(maxsize=10_000, ttl=900)

MODES = ("practice", "weekly")

HELP = (
    "📝 /practice for a question\n"
    "📚 /level to switch level\n"
    "📅 /subscribe for a question every Monday\n"
    "🗒 /notes for cheat sheets\n"
    "🏆 /tournament to join the tournament\n"
    "🥇 /leaderboard to see who's leading\n\n"
    "/unsubscribe stops the Monday question. /notes_sub sends you the high-yield "
    "cheat sheets every two weeks (/notes_unsub to stop)."
)

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _level(level: str | None) -> str:
    return level if level in LEVELS else DEFAULT_LEVEL


def _level_kb(current: str | None) -> InlineKeyboardMarkup:
    current = _level(current)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if key == current else LEVEL_EMOJI[key]} {label}",
            callback_data=f"lv:{key}",
        )]
        for key, label in LEVELS.items()
    ])


def _pts(n: int) -> str:
    return f"{n} pt" if n == 1 else f"{n} pts"


def _local(when: datetime | None) -> str:
    """Render a timestamptz in the society's timezone rather than the DB server's."""
    if when is None:
        return "?"
    return when.astimezone(ZoneInfo(TZ)).strftime("%d %b %Y")


# ------------------------------------------------------------------ onboarding


@router.message(Command("start", "help"))
async def start(m: Message):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)
    name = getattr(m.from_user, "first_name", None)
    hello = f"👋 <b>Hi {esc(name)}!</b>" if name else "👋 <b>Hi!</b>"
    await m.answer(
        f"{hello} This is the LKC OphSoc revision bot.\n\n"
        "Practise ophthalmology MCQs at your level. The more you answer, the more "
        "it focuses on the topics you find tricky.\n\n"
        f"{HELP}\n\n"
        "<b>Pick your level below</b> 👇 You can change it any time.\n\n"
        f"<i>{esc(CREDIT)}\n{esc(DISCLAIMER)}</i>",
        parse_mode="HTML",
        reply_markup=_level_kb(level),
    )


@router.message(Command("level"))
async def level_cmd(m: Message):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    current = await db.get_level(uid)
    await m.answer(
        "📚 Pick your level. Your questions and cheat sheets follow it.",
        reply_markup=_level_kb(current),
    )


@router.callback_query(F.data.startswith("lv:"))
async def set_level(c: CallbackQuery):
    answered = False
    try:
        key = c.data.split(":", 1)[1]
        if key not in LEVELS:
            answered = True
            return await ack(c, "That level doesn't exist.", True)
        uid = c.from_user.id
        await db.upsert_user(uid, c.from_user.username)
        await db.set_level(uid, key)
        answered = True
        await ack(c, f"{LEVEL_EMOJI[key]} You're on {LEVELS[key]} now")
        if c.message is not None:
            # Only move the tick. Rewriting the text would wipe the /start welcome
            # when the picker is tapped from there.
            try:
                await c.message.edit_reply_markup(reply_markup=_level_kb(key))
            except Exception:
                log.debug("level picker update failed", exc_info=True)
    except Exception:
        log.exception("set_level failed")
    finally:
        if not answered:
            await ack(c)


# -------------------------------------------------------------------- practice


@router.message(Command("practice"))
async def practice(m: Message, bot: Bot):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))
    await send_question_for_level(bot, uid, level, "practice")


@router.callback_query(F.data == "next")
async def next_question(c: CallbackQuery, bot: Bot):
    answered = False
    try:
        await ack(c)
        answered = True
        await drop_buttons(c.message, lambda data: data == "next")
        uid = c.from_user.id
        level = _level(await db.get_level(uid))
        await send_question_for_level(bot, uid, level, "practice")
    except Exception:
        log.exception("next failed")
    finally:
        if not answered:
            await ack(c)


@router.callback_query(F.data.startswith("a:"))
async def on_answer(c: CallbackQuery):
    answered = False
    try:
        parts = c.data.split(":")
        if len(parts) != 4:
            answered = True
            return await ack(c)
        _, qid_raw, idx_raw, mode = parts
        if not (qid_raw.isdigit() and idx_raw.isdigit()) or mode not in MODES:
            # callback_data is attacker-controllable; anything unexpected is ignored.
            answered = True
            return await ack(c)

        question = await db.get_question(int(qid_raw))
        if question is None:
            answered = True
            return await ack(c, "That question is no longer available.", True)

        options = parse_options(question["options"])
        idx = int(idx_raw)
        if not 0 <= idx < len(options):
            answered = True
            return await ack(c)

        message = getattr(c, "message", None)
        if message is None:
            answered = True
            return await ack(c)

        correct = idx == question["correct_idx"]
        scored = await db.record_attempt(
            c.from_user.id, question, idx, correct, mode, message.message_id)

        if not scored:
            answered = True
            return await ack(c, "You've already answered this one 👍")

        streak = 0
        if correct and mode == "practice":
            await db.award_point(c.from_user.id, question["id"])
            try:
                streak = await db.practice_streak(c.from_user.id)
            except Exception:
                log.debug("streak lookup failed", exc_info=True)

        # Re-rendered from the database rather than appended to message.text, so
        # the answered card can mark the options in place.
        body, _ = render(question, chosen=idx, header=card_header(mode))
        buttons = [InlineKeyboardButton(text="💡 Explain", callback_data=f"e:{question['id']}")]
        if mode == "practice":
            buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data="next"))

        answered = True
        await ack(c)
        await deliver_verdict(
            c,
            f"{body}\n\n{verdict(correct, question['correct_idx'], streak)}",
            InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )
    except Exception:
        log.exception("on_answer failed")
    finally:
        # Guarantees the spinner always stops, on every path.
        if not answered:
            await ack(c)


@router.callback_query(F.data.startswith("e:"))
async def on_explain(c: CallbackQuery, bot: Bot):
    answered = False
    try:
        parts = c.data.split(":")
        if len(parts) != 2 or not parts[1].isdigit():
            answered = True
            return await ack(c)

        uid = c.from_user.id
        question = await db.get_question(int(parts[1]))
        if question is None:
            answered = True
            return await ack(c, "That question is no longer available.", True)

        # The cooldown only guards LLM spend. Written explanations are free, so
        # a student moving quickly through the bank is never told to wait.
        if not question["explanation"]:
            last = _recent_explain.get(uid)
            if last is not None and time.monotonic() - last < _EXPLAIN_COOLDOWN:
                answered = True
                return await ack(c, "Give it a few seconds and try again 🙏", True)
            _recent_explain[uid] = time.monotonic()
            answered = True
            await ack(c, "Thinking 🤔")
        else:
            answered = True
            await ack(c)

        failed = False
        try:
            text = await llm.explain(question)
        except Exception:
            log.exception("explain failed for question %s", question["id"])
            text, failed = None, True
        if not text:
            await safe_send(bot, uid, "Couldn't load the explanation right now. Try again in a bit."
                            if failed else "No explanation written for this one yet.")
            return

        # Show it inside the answered card, so question, answer and reasoning sit
        # together. A card past Telegram's 48h edit window, or one that would grow
        # past the length limit, gets the explanation as its own message instead.
        block = explanation_block(text)
        message = c.message
        base = getattr(message, "html_text", None) if message is not None else None
        if base and EXPLANATION_HEADING in base:
            return  # already showing (a second tap raced the first)
        if base and len(base) + len(block) + 2 <= TELEGRAM_LIMIT:
            keyboard = remaining_buttons(message, lambda data: data.startswith("e:"))
            if await edit_in_place(message, f"{base}\n\n{block}", keyboard):
                return
        await safe_send(bot, uid, block, parse_mode="HTML")
    except Exception:
        log.exception("on_explain failed")
    finally:
        if not answered:
            await ack(c)


# ---------------------------------------------------------------- subscriptions


@router.message(Command("subscribe"))
async def subscribe(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", True)
    await m.answer("📅 You're subscribed! A new question lands every Monday at 9am.")


@router.message(Command("unsubscribe"))
async def unsubscribe(m: Message):
    # upsert first, otherwise the flag write hits no rows and the confirmation lies.
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", False)
    await m.answer("Done, no more Monday questions. /subscribe if you change your mind.")


@router.message(Command("notes"))
async def notes(m: Message, command: CommandObject, bot: Bot):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))

    if not command.args:
        topics = await db.note_topics(level, "B")
        if not topics:
            return await m.answer(
                f"🗒 No {LEVELS[level]} cheat sheets up yet, they're on the way.\n\n"
                "Want the high-yield ones sent to you every two weeks? /notes_sub"
            )
        listing = "\n".join(f"• {t}" for t in topics)
        return await m.answer(
            f"🗒 Cheat sheets for {LEVELS[level]}:\n{listing}\n\n"
            f"Send /notes with a topic name, like:\n/notes {topics[0]}"
        )

    rows = await db.get_notes(command.args.strip(), level, "B")
    if not rows:
        return await m.answer("Couldn't find that topic. Send /notes to see the list.")
    for row in rows:
        # safe_send splits anything over Telegram's 4096-char limit instead of
        # slicing the tail off.
        await safe_send(bot, uid, f"📝 {row['title']}\n\n{row['body']}")


@router.message(Command("notes_sub"))
async def notes_sub(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    level = _level(await db.get_level(m.from_user.id))
    await db.set_flag(m.from_user.id, "notes_sub", True)
    await m.answer(
        f"📘 Done! You'll get the high-yield {LEVELS[level]} cheat sheets every two "
        "weeks. /notes_unsub to stop."
    )


@router.message(Command("notes_unsub"))
async def notes_unsub(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "notes_sub", False)
    await m.answer("Done, no more fortnightly cheat sheets.")


# ------------------------------------------------------------------- tournament


@router.message(Command("tournament"))
async def tournament(m: Message):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    t = await db.active_tournament()
    if not t:
        return await m.answer("No tournament on right now. We'll announce the next one 👀")
    if await db.is_joined(uid):
        await db.leave_tournament(uid)
        return await m.answer("You've left the tournament. Changed your mind? /tournament")
    await db.join_tournament(uid)
    await m.answer(
        f"🏆 You're in! The tournament ends {_local(t['ends_at'])}.\n\n"
        "Every question you get right in /practice is worth 1 point (each question "
        "only counts once). Top 3 win a prize from LKC OphSoc 🎁"
    )


@router.message(Command("leaderboard"))
async def leaderboard(m: Message):
    rows = await db.leaderboard(limit=3)
    if rows is None:
        return await m.answer("No tournament on right now.")
    if not rows:
        return await m.answer("🏆 Nobody's joined yet. Be the first: /tournament")

    top = "\n".join(
        f"{MEDALS.get(r['rk'], str(r['rk']) + '.')} @{mask(r['username'], r['user_id'])}, "
        f"{_pts(r['points'])}"
        for r in rows
    )
    mine = await db.my_rank(m.from_user.id)
    if mine is None:
        tail = "\n\nYou're not in yet. Join with /tournament"
    else:
        tail = f"\n\nYou're #{mine['rk']} with {_pts(mine['points'])}."
    await m.answer(f"🏆 Leaderboard\n\n{top}{tail}")


# ------------------------------------------------------------------------ admin


def _is_admin(m: Message) -> bool:
    if m.from_user.id in ADMIN_IDS:
        return True
    log.warning("ignoring admin command from %s", m.from_user.id)
    return False


@router.message(Command("admin_tournament_start"))
async def admin_tournament_start(m: Message):
    if not _is_admin(m):
        return
    if await db.active_tournament():
        return await m.answer("There's already a tournament running.")
    tid = await db.start_tournament(14)
    await m.answer(f"🏆 Tournament {tid} is live for 2 weeks. Students join with /tournament.")


@router.message(Command("admin_tournament_end"))
async def admin_tournament_end(m: Message, bot: Bot):
    if not _is_admin(m):
        return
    t = await db.active_tournament()
    if not t:
        return await m.answer("No tournament running.")
    await m.answer(f"Closing tournament {t['id']} and messaging the top 3...")
    rows = await jobs.finish_tournament(bot, t["id"])
    await m.answer(f"Done. {len(rows)} participant(s) scored.")


@router.message(Command("admin_weekly_now"))
async def admin_weekly_now(m: Message, bot: Bot):
    """Manual trigger, so you never have to wait for Monday to demo the push."""
    if not _is_admin(m):
        return
    await m.answer("Sending this week's question...")
    sent = await jobs.weekly_question(bot)
    await m.answer(f"Sent to {sent} subscriber(s).")


@router.message(Command("admin_notes_now"))
async def admin_notes_now(m: Message, bot: Bot):
    if not _is_admin(m):
        return
    await m.answer("Sending the cheat sheets...")
    sent = await jobs.fortnightly_notes(bot)
    await m.answer(f"Sent {sent} message(s).")
