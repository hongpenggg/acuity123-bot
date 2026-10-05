"""Command and callback handlers."""
from __future__ import annotations

import contextlib
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from cachetools import TTLCache
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import commands, db, jobs, llm, resources
from .config import ADMIN_IDS, CREDIT, DEFAULT_LEVEL, DISCLAIMER, LEVEL_EMOJI, LEVELS, TZ
from .sender import (ack, card_header, deliver_verdict, drop_buttons, edit_in_place,
                     remaining_buttons, safe_send, send_note, send_question,
                     send_question_for_level)
from .text import (EXPLANATION_HEADING, TELEGRAM_LIMIT, esc, explanation_block, mask,
                   parse_options, render, sheets_done, verdict)

log = logging.getLogger(__name__)
router = Router()

_EXPLAIN_COOLDOWN = 10.0
# Bounded and self-expiring: the old plain dict grew forever and survived restarts
# with stale timestamps.
_recent_explain: TTLCache = TTLCache(maxsize=10_000, ttl=900)

MODES = ("practice", "weekly")

TAGLINE = (
    "Your high-yield, one-stop ophthalmology hub for medical students and "
    "residents, anytime, anywhere. 🤩"
)

# The welcome's numbered list.
#
# Every claim here has to be something the code actually does, and every command
# named has to be one of the canonical names in commands.PUBLIC, so the welcome
# and Telegram's own command menu read as one vocabulary. Two tests enforce that
# (test_welcome_only_names_commands_that_exist) and the no-em-dash copy rule.
#
# The older aliases (/practice, /subscribe, /notes_sub, /topicalnotes ...) still
# work for anyone who has them in their chat history; they are just not
# advertised here or in the menu.
FUNCTIONS = (
    "<b>Functions</b>\n"
    "<b>1. 🧠 Adaptive MCQs</b>\n"
    "Sets of five, spread across topics and weighted towards the ones you keep "
    "getting wrong. /quizme\n"
    "Redo your wrong answers: /review\n"
    "Your weakest topics, ranked: /stats\n"
    "<b>2. 🗒 High-Yield Cheat Sheets</b>\n"
    "Quick summaries, flowcharts, clinical approaches, one at a time. /notes\n"
    "Jump to any sheet by code or topic: /resources\n"
    "<b>3. 📬 Be Consistent Automatically</b>\n"
    "A question set every Monday -> /weeklyquiz\n"
    "Cheat sheets every fortnight -> /subscribenotes\n\n"
    "<b>🏆 Annual Eye Trivia Tournament</b>\n"
    "Compete for prizes! /tournament /leaderboard\n\n"
    "<b>Start now!</b> /quizme"
)

HELP = (
    "📝 /quizme a set of five, picked for you\n"
    "🔁 /review the questions you got wrong\n"
    "📊 /stats your scores, weakest topics first\n"
    "🗒 /notes your next cheat sheet\n"
    "📚 /resources browse, or fetch one by code or topic\n"
    "🎓 /changestreams switch Pre-Clinical / Clinical / Post-MBBS\n"
    "📅 /weeklyquiz a set every Monday\n"
    "🗓 /subscribenotes cheat sheets every fortnight\n"
    "🏆 /tournament the competition status\n"
    "🥇 /leaderboard the top 3 and your rank\n\n"
    "Turn the two subscriptions off with /stopweekly and /stopmonthly, or with "
    "the button on their confirmation.\n"
    "/topicalnotes and /randomnotes still work if you prefer them."
)

RESOURCES_INTRO = (
    "📚 <b>Revision sheets</b>\n"
    "Overview sheets cover a whole topic; focused sheets go deep on one point. "
    "Pick one and it arrives here as a PDF.\n\n"
    "Know what you want? <code>/resources b14</code> or "
    "<code>/resources glaucoma</code> fetches it straight away.\n"
    "Or send /notes and I'll just hand you the next one you have not read."
)

OVERVIEW_INTRO = (
    "📘 Overview sheets: one broad sheet per topic. Pick one and it arrives as "
    "a PDF."
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


def _resources_home_kb(level: str | None) -> InlineKeyboardMarkup:
    rows = []
    if (overview := resources.overview(level)):
        rows.append([InlineKeyboardButton(
            text=f"📘 Overview sheets · {len(overview)}",
            callback_data="res:tier:a")])
    if (focused := resources.focused(level)):
        rows.append([InlineKeyboardButton(
            text=f"📄 Focused sheets · {len(focused)}",
            callback_data="res:tier:b")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Post-MBBS has 65 focused sheets. One button each is a wall a student has to
# scroll past, and Telegram starts rejecting very large keyboards, so the list is
# paged. The page number rides in the callback data; the level never does, because
# it is read from the database each time (see resources_cb).
_PAGE = 20


def _resources_tier_kb(level: str | None, tier: str,
                       page: int = 0) -> InlineKeyboardMarkup:
    notes = resources.tier(level, tier)
    pages = max(1, -(-len(notes) // _PAGE))
    page = max(0, min(page, pages - 1))
    window = notes[page * _PAGE:(page + 1) * _PAGE]
    buttons = [
        InlineKeyboardButton(text=note.label,
                             callback_data=f"res:get:{note.tier}:{note.code}")
        for note in window
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if pages > 1:
        nav = []
        if page:
            nav.append(InlineKeyboardButton(
                text="◀", callback_data=f"res:page:{tier}:{page - 1}"))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{pages}", callback_data="res:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶", callback_data=f"res:page:{tier}:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="res:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _local(when: datetime | None) -> str:
    """Render a timestamptz in the society's timezone rather than the DB server's."""
    if when is None:
        return "?"
    return when.astimezone(ZoneInfo(TZ)).strftime("%d %b %Y")


# ------------------------------------------------------------------ onboarding


def welcome(user, level: str | None) -> str:
    """The /start screen. Greets by the sender's own first name, which Telegram
    supplies on every message, and falls back to a bare hello when it does not."""
    name = getattr(user, "first_name", None)
    hello = (f"👋 <b>Hi {esc(name)}!</b> This is the <b>LKC OphSoc Bot</b>."
             if name else "👋 <b>Hi! This is the LKC OphSoc Bot.</b>")
    lv = _level(level)
    return (
        f"{hello}\n"
        f"{TAGLINE}\n\n"
        f"{FUNCTIONS}\n\n"
        f"Level: {LEVEL_EMOJI[lv]} <b>{LEVELS[lv]}</b>\n"
        "Choose below 👇 or use /changestreams. Your questions and sheets both "
        "follow it.\n\n"
        f"<i>{esc(CREDIT)} {esc(DISCLAIMER)}</i>"
    )


@router.message(Command("start", "help"))
async def start(m: Message, bot: Bot, command: CommandObject | None = None):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)
    # Automatic entry to a running tournament: a student who has started the bot
    # should not have to remember to opt in to a competition they are already
    # answering questions for. Costs one cheap query, and does nothing when no
    # tournament is running.
    await db.join_tournament(uid)
    # Give this chat its full menu. Telegram keeps the command list per chat, so
    # this is also what refreshes a client still showing only /start.
    await commands.sync_chat(bot, m.chat.id, is_admin=uid in ADMIN_IDS)
    body = welcome(m.from_user, level)
    # The welcome names the headline commands only. /help adds the full list, so
    # /stats and the sheet browsers stay one message away.
    if command is not None and command.command == "help":
        body += f"\n\n<b>All commands</b>\n{HELP}"
    await m.answer(body, parse_mode="HTML", reply_markup=_level_kb(level))


@router.message(Command("changestreams", "level"))
async def level_cmd(m: Message):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    current = await db.get_level(uid)
    await m.answer(
        "📚 Pick your level. Your questions and sheets both follow it.",
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
        # The toast disappears, so say what to do next in the chat itself -
        # picking a level used to dead-end here.
        with contextlib.suppress(Exception):
            await c.bot.send_message(
                uid,
                f"{LEVEL_EMOJI[key]} You're on <b>{LEVELS[key]}</b>. Your "
                "questions and cheat sheets both follow it.\n\n"
                "Send /quizme for a set of five, or /notes for a cheat sheet.",
                parse_mode="HTML")
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


@router.message(Command("quizme", "practice"))
async def practice(m: Message, bot: Bot):
    """Serve the next question of the student's current set of five.

    `/practice` stays registered as an alias: it is what the copy, the docs and any
    message already in a student's chat refer to.
    """
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


async def _report_set_if_finished(message, uid: int, level: str,
                                  before: int) -> None:
    """Say how the set went, once a set of five is complete.

    Sets are rolling blocks of five answers rather than fixed blocks of five
    question ids, so "complete" is simply "the distinct answer count is a
    multiple of five". Two consequences worth knowing:

    * The last set at a level can be short (103 clinical questions is twenty
      sets of five and then one of three), which never hits the multiple, so
      finishing the level is reported on its own branch instead.
    * `before` is the count taken *before* this answer was recorded. A /review
      answer does not change the count, so passing it through is what stops a
      student on a set boundary being congratulated after every review answer.
    """
    try:
        answered = await db.answered_count(uid, level)
        if answered == before:
            return          # a re-answer, not a new question: no set advanced
        total = await db.level_total(level)
        score = await db.last_set_score(uid, level)
    except Exception:
        log.debug("set score lookup failed", exc_info=True)
        return

    exhausted = bool(total) and answered >= total
    finished_a_set = score is not None and answered % db.SET_SIZE == 0
    if not (finished_a_set or exhausted):
        return

    total_sets = -(-total // db.SET_SIZE) if total else 0
    if finished_a_set:
        head = (f"📊 <b>Set {score['number']} of {total_sets}</b> done · "
                f"score <b>{score['correct']}/{score['size']}</b>")
    else:
        head = f"📊 <b>{answered} of {total}</b> answered"

    if exhausted:
        tail = ("That is every question at this level. 🎉 /review the ones you "
                "missed, /stats for the full breakdown, or /changestreams to "
                "switch level.")
    else:
        left = total - answered
        tail = (f"{left} question{'s' if left != 1 else ''} left at this level. "
                "Send /quizme for the next five.")

    await message.answer(f"{head}\n{tail}", parse_mode="HTML")


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
        # Taken before the write: answered_count counts *distinct* questions, so a
        # /review answer leaves it unchanged. Without this, a student sitting on a
        # set boundary was told "Set N done" again after every review answer.
        before = await db.answered_count(c.from_user.id, question["level"])
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
        if mode == "practice":
            # Only a question answered for the first time advances a set, so only
            # that can close one. Re-answering through /review never does.
            await _report_set_if_finished(message, c.from_user.id,
                                          question["level"], before)
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


@router.message(Command("review"))
async def review(m: Message, bot: Bot):
    """Re-serve the questions this student got wrong, with their explanations.

    Deliberately separate from /quizme: `pick_question` only ever serves
    unattempted questions, so without this a missed question never comes back.
    Re-answering cannot inflate anything - set progress counts *distinct*
    questions, and a tournament point is deduplicated per question in SQL.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))

    waiting = await db.wrong_count(uid, level)
    if not waiting:
        return await m.answer(
            f"🎯 Nothing to review yet: you have not got a "
            f"{LEVELS[level]} question wrong.\n"
            "Send /quizme for a set of five.")

    pile = await db.wrong_questions(uid, level)
    shown = len(pile)
    more = waiting - shown
    head = ("🔁 <b>Review</b> · the one you most recently missed."
            if shown == 1
            else f"🔁 <b>Review</b> · the {shown} you most recently missed.")
    if more > 0:
        head += f" {more} more after these."
    await m.answer(head, parse_mode="HTML")
    for question in pile:
        await send_question(bot, uid, question, "practice")


# ---------------------------------------------------------------- subscriptions


def _sub_off_kb(which: str) -> InlineKeyboardMarkup:
    """A turn-off button on the confirmation itself.

    The stop commands are not in the menu: a student turns a subscription off
    once, if ever, and the moment they want to is the moment they are reading the
    confirmation. The commands stay registered for anyone who knows them.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔕 Turn this off",
                             callback_data=f"sub:off:{which}")]])


_SUB_FLAGS = {"weekly": ("weekly_sub", "Monday question sets"),
              "notes": ("notes_sub", "fortnightly cheat sheets")}


@router.callback_query(F.data.startswith("sub:off:"))
async def sub_off(c: CallbackQuery):
    answered = False
    try:
        which = (c.data or "").split(":")[-1]
        if which not in _SUB_FLAGS:
            answered = True
            return await ack(c, "Unknown option.", True)
        column, label = _SUB_FLAGS[which]
        uid = c.from_user.id
        await db.upsert_user(uid, c.from_user.username)
        await db.set_flag(uid, column, False)
        answered = True
        await ack(c, "Turned off")
        await drop_buttons(c.message, lambda data: data.startswith("sub:off:"))
        if c.message is not None:
            await c.message.answer(f"🔕 No more {label}.")
    except Exception:
        log.exception("sub_off failed")
    finally:
        if not answered:
            await ack(c)


@router.message(Command("weeklyquiz", "subscribe"))
async def weeklyquiz(m: Message):
    """The Monday set of five.

    `/subscribe` stays as an alias so an older message in a student's chat still
    does what it says.
    """
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", True)
    await m.answer(
        "📅 You're in. Every Monday at 9am you'll get a set of five.\n\n"
        "Only questions you answer through /quizme score for the tournament, so "
        "the Monday set is pure practice.",
        reply_markup=_sub_off_kb("weekly"),
    )


@router.message(Command("stopweekly", "unsubscribe"))
async def stopweekly(m: Message):
    # upsert first, otherwise the flag write hits no rows and the confirmation lies.
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", False)
    await m.answer("Done, no more Monday sets. /weeklyquiz if you change your mind.")


async def _deliver(bot: Bot, uid: int, level: str | None, note) -> bool:
    """Send one sheet and record it as delivered.

    Every path that hands a student a sheet goes through here - /notes,
    /resources, /topicalnotes, /randomnotes and the fortnightly drop - so a sheet
    read once is never pushed at them again, whichever door they came through.
    Recording only on success means a failed upload is retried rather than lost.
    """
    if not await send_note(bot, uid, note):
        return False
    await db.record_notes_sent(uid, level, [(note.tier, note.code)])
    return True


def _find_note(level: str | None, text: str) -> resources.Note | None:
    """A sheet by code ('b14'), or by any distinctive part of its topic name.

    Scoped to one level: the codes repeat across levels, so a level-free search
    would hand a clinical student a preclinical sheet.
    """
    note = resources.find(level, text)
    if note is not None:
        return note
    needle = text.strip().lower()
    if not needle:
        return None
    for candidate in resources.all_for(level):
        if needle in candidate.topic.lower():
            return candidate
    return None


@router.message(Command("resources"))
async def resources_cmd(m: Message, command: CommandObject, bot: Bot):
    """The revision sheets. Sends the actual PDF, not a description of it."""
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)
    if command.args:
        note = _find_note(level, command.args)
        if note is None:
            return await m.answer("No sheet matches that. Send /resources to browse.")
        await _deliver(bot, uid, level, note)
        return None
    await m.answer(RESOURCES_INTRO, parse_mode="HTML",
                          reply_markup=_resources_home_kb(level))


@router.callback_query(F.data.startswith("res:"))
async def resources_cb(c: CallbackQuery, bot: Bot):
    """Browse the sheets: home -> one kind -> one PDF."""
    answered = False
    try:
        parts = (c.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        message = getattr(c, "message", None)
        # The level is read here rather than carried in the callback data, so the
        # buttons always serve the student's *current* stream even if they
        # switched it after the message was sent.
        level = await db.get_level(c.from_user.id)

        if action == "home":
            await ack(c)
            if message is not None:
                with contextlib.suppress(Exception):
                    await message.edit_text(
                        RESOURCES_INTRO, parse_mode="HTML",
                        reply_markup=_resources_home_kb(level))
        elif action in ("tier", "page"):
            await ack(c)
            tier_code = parts[2] if len(parts) > 2 else "a"
            page = int(parts[3]) if action == "page" and len(parts) > 3 else 0
            if message is not None:
                with contextlib.suppress(Exception):
                    await message.edit_text(
                        f"{resources.TIERS.get(tier_code, 'Notes')} sheets, pick one:",
                        reply_markup=_resources_tier_kb(level, tier_code, page))
        elif action == "get" and len(parts) > 3:
            note = resources.get(level, parts[2], parts[3])
            if note is None:
                await ack(c, "That sheet has moved. Try /resources again.", True)
            else:
                await ack(c)
                await _deliver(bot, c.from_user.id, level, note)
        elif action == "noop":
            await ack(c)          # the page counter is a label, not a button
        else:
            await ack(c, "Unknown option.", True)
        answered = True
    except Exception:
        log.exception("resources callback failed")
    finally:
        if not answered:
            await ack(c)


@router.message(Command("notes"))
async def notes(m: Message, command: CommandObject, bot: Bot):
    """The next cheat sheet this student has not been sent.

    A progression, not a picker: overview sheets first, then focused ones, then
    the syllabus-complete message. `note_deliveries` is what makes it resumable
    and stops a sheet going out twice, and it is shared with the fortnightly drop
    so a sheet pulled by hand is never pushed at them later.

    An argument still short-circuits the queue (`/notes glaucoma`), because
    someone revising one topic should not have to walk to it.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)

    if command.args:
        note = _find_note(level, command.args)
        if note is None:
            return await m.answer(
                "Couldn't find that sheet. Send /resources to browse, or /notes "
                "on its own for your next one.")
        await _deliver(bot, uid, level, note)
        return None

    delivered = await db.notes_delivered(uid, level)
    queue = (resources.unsent(level, "a", delivered["a"])
             + resources.unsent(level, "b", delivered["b"]))
    if not queue:
        return await m.answer(sheets_done(LEVELS[_level(level)]), parse_mode="HTML")

    note = queue[0]
    if not await _deliver(bot, uid, level, note):
        return None

    left = len(queue) - 1
    kind = resources.TIERS[note.tier].lower()
    if left:
        tail = (f"{left} sheet{'s' if left != 1 else ''} to go. "
                "/notes for the next one.")
    else:
        tail = "That was the last one. Send /notes again for the good news."
    await m.answer(f"🗒 Your next {kind} sheet. {tail}")
    return None


@router.message(Command("subscribenotes", "monthlynotes", "notes_sub"))
async def subscribenotes(m: Message):
    """The fortnightly sheet drop.

    `/monthlynotes` and `/notes_sub` stay as aliases: the cadence changed, and a
    message already sitting in a student's chat should still do something sane.
    The reply counts what is left for *this* student, because the drop walks their
    own delivery history rather than resending a fixed bundle.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    await db.set_flag(uid, "notes_sub", True)
    level = await db.get_level(uid)
    delivered = await db.notes_delivered(uid, level)
    left = (len(resources.unsent(level, "a", delivered["a"]))
            + len(resources.unsent(level, "b", delivered["b"])))
    if left:
        body = (f"📬 You're in. On the 1st and the 15th you'll get up to "
                f"{jobs.SHEETS_PER_DROP} cheat sheets for "
                f"{LEVELS[_level(level)]}, picking up where you left off. "
                f"{left} to go.\n\nWant one right now? /notes")
    else:
        body = (f"📬 You're in, but you have already had every "
                f"{LEVELS[_level(level)]} sheet. Nothing will be sent until there "
                "is new content, and /resources still has them all.")
    await m.answer(body, reply_markup=_sub_off_kb("notes"))


@router.message(Command("stopmonthly", "notes_unsub", "unsubscribenotes"))
async def subscribenotes_off(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "notes_sub", False)
    await m.answer("Done, no more cheat sheets. /subscribenotes to start again.")


@router.message(Command("topicalnotes"))
async def topicalnotes(m: Message, command: CommandObject, bot: Bot):
    """Pick an overview sheet: one broad sheet per topic."""
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)
    if command.args:
        note = _find_note(level, command.args)
        if note is None or note.tier != "a":
            return await m.answer(
                "No overview sheet matches that. Send /topicalnotes to see them.")
        await _deliver(bot, uid, level, note)
        return None
    if not resources.overview(level):
        return await m.answer("No overview sheets on disk for your level yet.")
    await m.answer(OVERVIEW_INTRO, reply_markup=_resources_tier_kb(level, "a"))


@router.message(Command("randomnotes"))
async def randomnotes(m: Message, command: CommandObject, bot: Bot):
    """One focused sheet at random that this student has not had.

    Kept as an unlisted alternative to /notes for anyone who wants the shuffle
    rather than the queue. It draws only from sheets they have not been sent, so
    it cannot repeat itself and it runs out at the same point /notes does.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)
    if command.args:
        note = _find_note(level, command.args)
        if note is None or note.tier != "b":
            return await m.answer(
                "No focused sheet matches that. Send /randomnotes for a random one.")
        await _deliver(bot, uid, level, note)
        return None

    delivered = await db.notes_delivered(uid, level)
    note = resources.random_focused(level, delivered["b"])
    if note is None:
        if not resources.focused(level):
            return await m.answer("No focused sheets on disk for your level yet.")
        return await m.answer(sheets_done(LEVELS[_level(level)]), parse_mode="HTML")
    await _deliver(bot, uid, level, note)
    return None


@router.message(Command("stats"))
def _ranked(rows, key) -> list[tuple[str, int, int]]:
    """(name, correct, answered) grouped by `key`, weakest first."""
    out: dict[str, list[int]] = {}
    for row in rows:
        name = key(row) or "(not labelled)"
        bucket = out.setdefault(name, [0, 0])
        bucket[0] += row["answered"]
        bucket[1] += row["correct"]
    ranked = sorted(out.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[0]))
    return [(name, c, n) for name, (n, c) in ranked]


def _score_line(name: str, correct: int, answered: int) -> str:
    return f"{esc(name)} · {correct}/{answered} ({round(100 * correct / answered)}%)"


#: How many question types /stats names. Each level has 71 to 92 of them and some
#: run to 62 characters, so the full list runs to thousands of characters and
#: Telegram would split it into a two-message wall.
_TYPES_SHOWN = 5


@router.message(Command("stats"))
async def stats_cmd(m: Message):
    """Marker A: how this student is doing, overall and broken down.

    Split by topic and by question type (`questions.tag`, e.g. "Physiology |
    optical compensation"), because "you are weak on optics" is far less useful
    than knowing which kind of optics question keeps catching them out. Weakest
    first throughout, since that is the list a student should act on, and it is
    the same ranking `db.pick_question` uses to pick what to serve next.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))

    total = await db.level_total(level)
    if not total:
        return await m.answer(
            f"🚧 No {LEVELS[level]} questions yet, so there is nothing "
            "to report on.")

    answered = await db.answered_count(uid, level)
    if answered == 0:
        return await m.answer(
            f"📊 Nothing answered yet at {LEVELS[level]}.\n"
            "Send /quizme for your first set of five.")

    rows = await db.stats(uid, level)
    correct = sum(row["correct"] for row in rows)
    attempts = sum(row["answered"] for row in rows)
    total_sets = -(-total // db.SET_SIZE)
    streak = await db.practice_streak(uid)
    waiting = await db.wrong_count(uid, level)

    lines = [
        f"📊 <b>Your {LEVELS[level]} progress</b>",
        f"Answered <b>{answered}</b> of {total} "
        f"({round(100 * answered / total)}%)",
        f"Correct <b>{correct}</b> of {attempts} "
        f"({round(100 * correct / attempts)}%)",
        f"Sets finished <b>{answered // db.SET_SIZE}</b> of {total_sets}",
    ]
    if streak >= 2:
        lines.append(f"🔥 {streak} in a row right now")
    if waiting:
        plural = "" if waiting == 1 else "s"
        lines.append(f"🔁 {waiting} question{plural} waiting in /review")

    topics = _ranked(rows, lambda r: r["topic"])
    if topics:
        lines.append("\n<b>By topic</b>  <i>weakest first</i>")
        lines += [_score_line(*t) for t in topics]

    # Question types are NOT ranked by accuracy, deliberately. `questions.tag` is
    # close to a per-question label - 71 to 92 distinct types over 103 to 170
    # questions, averaging 1.1 to 2.4 questions each - so "your weakest type is
    # 0/1" is noise dressed up as a statistic. What is honest, and what a student
    # can act on, is which kinds of question they have actually missed.
    missed = sorted({(row["tag"] or "(not labelled)")
                     for row in rows if row["answered"] > row["correct"]})
    if missed:
        shown = missed[:_TYPES_SHOWN]
        lines.append("\n<b>Question types you have missed</b>")
        lines += [esc(name) for name in shown]
        rest = len(missed) - len(shown)
        if rest:
            lines.append(f"<i>and {rest} more. /review serves them back.</i>")

    await m.answer("\n".join(lines), parse_mode="HTML")


# ------------------------------------------------------------------- tournament


@router.message(Command("tournament"))
async def tournament(m: Message):
    """Status only. Entry is automatic, so there is nothing to join.

    Kept as a command because "when does the tournament end and where am I" is a
    question students will ask regardless.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    t = await db.active_tournament()
    if not t:
        return await m.answer(
            "No tournament running. When OphSoc starts one you are entered "
            "automatically and we'll announce it here 👀")
    mark = await db.tournament_mark(uid)
    await m.answer(
        f"🏆 <b>Tournament live</b>, ends {_local(t['ends_at'])}.\n"
        f"You're in it with {_pts(mark['points'])} out of {mark['entrants']} "
        "entered.\n\n"
        "Only questions you answer through /quizme score, and each one counts once. "
        "/leaderboard for the top 3.",
        parse_mode="HTML",
    )


@router.message(Command("leaderboard"))
async def leaderboard(m: Message):
    rows = await db.leaderboard(limit=3)
    if rows is None:
        return await m.answer(
            "🏆 No tournament running. When OphSoc starts one you are "
            "entered automatically.\n\n"
            "Meanwhile /quizme keeps your /stats moving.")
    if not rows:
        return await m.answer("🏆 Nobody has scored yet. Set the pace with /quizme!")

    top = "\n".join(
        f"{MEDALS.get(r['rk'], str(r['rk']) + '.')} @{mask(r['username'], r['user_id'])}, "
        f"{_pts(r['points'])}"
        for r in rows
    )
    mine = await db.my_rank(m.from_user.id)
    if mine is None:
        tail = "\n\nYou have no points yet. Answer some /quizme sets!"
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
async def admin_tournament_start(m: Message, bot: Bot):
    """Start a tournament and put everybody in it.

    Entry is automatic rather than opt-in, so this also announces the competition:
    otherwise students would be scoring in a tournament nobody told them about.
    """
    if not _is_admin(m):
        return
    if await db.active_tournament():
        return await m.answer("There's already a tournament running.")
    tid = await db.start_tournament(jobs.TOURNAMENT_DAYS)
    entrants = await db.enrol_everyone(tid)
    await m.answer(
        f"🏆 Tournament {tid} is live for {jobs.TOURNAMENT_DAYS} days. "
        f"{entrants} student(s) entered automatically. Announcing it now...")
    sent = await jobs.announce_tournament(bot)
    await m.answer(f"Announced to {sent} student(s).")


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
    await m.answer("Sending this week's sets...")
    sent = await jobs.weekly_quiz(bot)
    await m.answer(f"Sent {sent} question(s).")


@router.message(Command("admin_notes_now"))
async def admin_notes_now(m: Message, bot: Bot):
    if not _is_admin(m):
        return
    await m.answer("Sending the fortnightly sheets...")
    sent = await jobs.fortnightly_notes(bot)
    await m.answer(f"Sent {sent} document(s).")
