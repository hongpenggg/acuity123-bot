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
                     remaining_buttons, safe_send, send_note,
                     send_question_for_level)
from .text import (EXPLANATION_HEADING, TELEGRAM_LIMIT, esc, explanation_block, mask,
                   parse_options, render, verdict)

log = logging.getLogger(__name__)
router = Router()

_EXPLAIN_COOLDOWN = 10.0
# Bounded and self-expiring: the old plain dict grew forever and survived restarts
# with stale timestamps.
_recent_explain: TTLCache = TTLCache(maxsize=10_000, ttl=900)

MODES = ("practice", "weekly")

FEATURES = (
    "WHAT THIS BOT DOES\n"
    "1. Practice: sets of five questions. The same five for everyone, so your\n"
    "   scores are comparable, and you get a score at the end of each set.\n"
    "2. Compete: a two-week tournament, a set every Monday, and a top-3 board.\n"
    "3. What is inside:\n"
    "   3a. Quiz: a 120-question Pre-Clinical bank across 6 topics, with Clinical\n"
    "       and Post-MBBS banks on the way.\n"
    "   3b. Notes: overview sheets, one per topic, plus focused sheets on single\n"
    "       points. /stats shows how you are doing."
)

HELP = (
    "📝 /quizme for a set of five questions\n"
    "📊 /stats for your scores and weak topics\n"
    "📘 /topicalnotes for an overview sheet by topic\n"
    "📄 /randomnotes for a focused sheet, at random\n"
    "📚 /resources to browse every sheet\n"
    "🎓 /changestreams to switch level\n"
    "📅 /weeklyquiz for a set every Monday\n"
    "🗓 /monthlynotes for the monthly sheet drop\n"
    "🏆 /tournament for the competition status\n"
    "🥇 /leaderboard for the top 3\n\n"
    "/stopweekly and /stopmonthly stop those two."
)

RESOURCES_INTRO = (
    "📚 Revision sheets\n"
    "Overview sheets cover a whole topic; focused sheets go deep on one point.\n"
    "Pick one and it arrives here as a PDF."
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


def _resources_home_kb() -> InlineKeyboardMarkup:
    rows = []
    if resources.TIER_A:
        rows.append([InlineKeyboardButton(
            text=f"📘 Overview sheets: all {len(resources.TIER_A)} topics",
            callback_data="res:tier:a")])
    if resources.TIER_B:
        rows.append([InlineKeyboardButton(
            text=f"📄 Focused sheets: {len(resources.TIER_B)} deep-dives",
            callback_data="res:tier:b")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _resources_tier_kb(tier: str) -> InlineKeyboardMarkup:
    notes = resources.tier(tier)
    buttons = [
        InlineKeyboardButton(text=note.label,
                             callback_data=f"res:get:{note.tier}:{note.code}")
        for note in notes
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="res:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _local(when: datetime | None) -> str:
    """Render a timestamptz in the society's timezone rather than the DB server's."""
    if when is None:
        return "?"
    return when.astimezone(ZoneInfo(TZ)).strftime("%d %b %Y")


# ------------------------------------------------------------------ onboarding


@router.message(Command("start", "help"))
async def start(m: Message, bot: Bot):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = await db.get_level(uid)
    name = getattr(m.from_user, "first_name", None)
    hello = f"👋 <b>Hi {esc(name)}!</b>" if name else "👋 <b>Hi!</b>"
    # Automatic entry to a running tournament: a student who has started the bot
    # should not have to remember to opt in to a competition they are already
    # answering questions for. Costs one cheap query, and does nothing when no
    # tournament is running.
    await db.join_tournament(uid)
    # Give this chat its full menu. Telegram keeps the command list per chat, so
    # this is also what refreshes a client still showing only /start.
    await commands.sync_chat(bot, m.chat.id, is_admin=uid in ADMIN_IDS)
    await m.answer(
        f"{hello}\n\n"
        f"{FEATURES}\n\n"
        f"{HELP}\n\n"
        f"Your level: {LEVEL_EMOJI[_level(level)]} <b>{LEVELS[_level(level)]}</b>\n"
        "<b>Pick your level below</b> 👇 or send /changestreams. Your questions "
        "and sheets both follow it.\n\n"
        f"<i>{esc(CREDIT)}\n{esc(DISCLAIMER)}</i>",
        parse_mode="HTML",
        reply_markup=_level_kb(level),
    )


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


async def _report_set_if_finished(message, uid: int, level: str, qid: int) -> None:
    """If that answer completed a set of five, say how the set went.

    A set is complete once all five of its questions have been attempted, so this
    fires exactly once per set and never for a partial one. Only practice sets are
    scored this way: the Monday set is a separate flow that does not count toward
    the tournament.
    """
    try:
        set_no = await db.set_no_for(level, qid)
        score = await db.set_score(uid, level, set_no)
    except Exception:
        log.debug("set score lookup failed", exc_info=True)
        return
    if score is None or score["answered"] < score["size"]:
        return

    done = set_no + 1
    left = score["total_sets"] - done
    if left <= 0:
        tail = ("That is every set in the bank. 🎉 /stats has the full breakdown, "
                "and /resources has the sheets.")
    else:
        tail = (f"{left} set{'s' if left != 1 else ''} to go. "
                "Send /quizme for the next five.")

    await message.answer(
        f"📊 <b>Set {done} of {score['total_sets']}</b> done · "
        f"score <b>{score['correct']}/{score['size']}</b>\n{tail}",
        parse_mode="HTML")


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
        if mode == "practice":
            # Scored straight off the attempts table, so the set boundaries are the
            # same fixed blocks of five for everyone.
            await _report_set_if_finished(message, c.from_user.id,
                                          question["level"], question["id"])
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


@router.message(Command("weeklyquiz", "subscribe"))
async def weeklyquiz(m: Message):
    """The Monday set of five.

    `/subscribe` stays as an alias so an older message in a student's chat still
    does what it says.
    """
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", True)
    await m.answer(
        "📅 You're in. Every Monday at 9am you'll get your current set of five.\n\n"
        "Only questions you answer through /quizme score for the tournament, so the "
        "Monday set is pure practice. /stopweekly to stop."
    )


@router.message(Command("stopweekly", "unsubscribe"))
async def stopweekly(m: Message):
    # upsert first, otherwise the flag write hits no rows and the confirmation lies.
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", False)
    await m.answer("Done, no more Monday sets. /weeklyquiz if you change your mind.")


def _find_note(text: str) -> resources.Note | None:
    """A sheet by code ('b14'), or by any distinctive part of its topic name."""
    note = resources.find(text)
    if note is not None:
        return note
    needle = text.strip().lower()
    if not needle:
        return None
    for candidate in resources.ALL:
        if needle in candidate.topic.lower():
            return candidate
    return None


@router.message(Command("resources"))
async def resources_cmd(m: Message, command: CommandObject, bot: Bot):
    """The revision sheets. Sends the actual PDF, not a description of it."""
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    if command.args:
        note = _find_note(command.args)
        if note is None:
            return await m.answer("No sheet matches that. Send /resources to browse.")
        return await send_note(bot, uid, note)
    await m.answer(RESOURCES_INTRO, reply_markup=_resources_home_kb())


@router.callback_query(F.data.startswith("res:"))
async def resources_cb(c: CallbackQuery, bot: Bot):
    """Browse the sheets: home -> one kind -> one PDF."""
    answered = False
    try:
        parts = (c.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        message = getattr(c, "message", None)

        if action == "home":
            await ack(c)
            if message is not None:
                with contextlib.suppress(Exception):
                    await message.edit_text(RESOURCES_INTRO,
                                            reply_markup=_resources_home_kb())
        elif action == "tier":
            await ack(c)
            tier_code = parts[2] if len(parts) > 2 else "a"
            if message is not None:
                with contextlib.suppress(Exception):
                    await message.edit_text(
                        f"{resources.TIERS.get(tier_code, 'Notes')} sheets, pick one:",
                        reply_markup=_resources_tier_kb(tier_code))
        elif action == "get" and len(parts) > 3:
            note = resources.get(parts[2], parts[3])
            if note is None:
                await ack(c, "That sheet has moved. Try /resources again.", True)
            else:
                await ack(c)
                await send_note(bot, c.from_user.id, note)
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
    """Sheets by topic: the database if it has any, otherwise the PDFs.

    The notes table is empty today — the society's sheets live in resources/notes
    as PDFs — so this must not dead-end on "nothing up yet".
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))

    if command.args:
        wanted = command.args.strip()
        rows = await db.get_notes(wanted, level, "B")
        if rows:
            for row in rows:
                # safe_send splits anything over Telegram's 4096-char limit instead
                # of slicing the tail off.
                await safe_send(bot, uid, f"📝 {row['title']}\n\n{row['body']}")
            return
        note = _find_note(wanted)
        if note is not None:
            return await send_note(bot, uid, note)
        return await m.answer("Couldn't find that topic. Send /resources to browse.")

    topics = await db.note_topics(level, "B")
    if topics:
        listing = "\n".join(f"• {t}" for t in topics)
        return await m.answer(
            f"🗒 Sheets for {LEVELS[level]}:\n{listing}\n\n"
            f"Send /notes with a topic name, like:\n/notes {topics[0]}"
        )

    return await m.answer(RESOURCES_INTRO, reply_markup=_resources_home_kb())


@router.message(Command("monthlynotes", "notes_sub"))
async def monthlynotes(m: Message):
    """The monthly drop: six overview sheets plus the six reserved focused ones."""
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "notes_sub", True)
    await m.answer(
        f"📚 You're in. On the 1st of each month you'll get all "
        f"{len(resources.TIER_A)} overview sheets plus {len(resources.MONTHLY)} "
        "focused ones.\n\nWant one now? /topicalnotes or /randomnotes."
        "\n/stopmonthly to stop."
    )


@router.message(Command("stopmonthly", "notes_unsub"))
async def monthlynotes_off(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "notes_sub", False)
    await m.answer("Done, no more monthly sheets. /monthlynotes to start again.")


@router.message(Command("topicalnotes"))
async def topicalnotes(m: Message, command: CommandObject, bot: Bot):
    """Pick an overview sheet: one broad sheet per topic."""
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    if command.args:
        note = _find_note(command.args)
        if note is None or note.tier != "a":
            return await m.answer(
                "No overview sheet matches that. Send /topicalnotes to see them.")
        return await send_note(bot, uid, note)
    if not resources.TIER_A:
        return await m.answer("No overview sheets on disk yet.")
    await m.answer(OVERVIEW_INTRO, reply_markup=_resources_tier_kb("a"))


@router.message(Command("randomnotes"))
async def randomnotes(m: Message, command: CommandObject, bot: Bot):
    """One focused sheet at random, from the ones the monthly drop holds back.

    The reserved six are excluded so the monthly bundle is not made up of sheets
    students have already been handed at random.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    if command.args:
        note = _find_note(command.args)
        if note is None or note.tier != "b":
            return await m.answer(
                "No focused sheet matches that. Send /randomnotes for a random one.")
        return await send_note(bot, uid, note)
    note = resources.random_focused()
    if note is None:
        return await m.answer("No focused sheets on disk yet.")
    await send_note(bot, uid, note)


@router.message(Command("stats"))
async def stats_cmd(m: Message):
    """Marker A: how this student is doing, overall and broken down.

    Split by topic and by question type (`questions.tag`, e.g. "Physiology |
    optical compensation"), because "you are weak on optics" is far less useful
    than knowing which kind of optics question keeps catching them out.
    """
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))

    board = await db.set_board(uid, level)
    if not board:
        return await m.answer(
            f"🚧 No {LEVELS[level]} questions yet, so there is nothing to report on.")

    total = sum(row["size"] for row in board)
    answered = sum(row["answered"] for row in board)
    correct = sum(row["correct"] for row in board)
    finished = sum(1 for row in board if row["answered"] >= row["size"])

    if answered == 0:
        return await m.answer(
            "📊 Nothing answered yet. Send /quizme to start your first set of five.")

    rows = await db.stats(uid, level)
    lines = [
        f"📊 <b>Your {LEVELS[level]} progress</b>",
        f"Answered: {answered} of {total} ({round(100 * answered / total)}%)",
        f"Correct: {correct} of {answered} ({round(100 * correct / answered)}%)",
        f"Sets finished: {finished} of {len(board)}",
    ]

    def _group(key) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for row in rows:
            name = key(row) or "(not labelled)"
            bucket = out.setdefault(name, [0, 0])
            bucket[0] += row["answered"]
            bucket[1] += row["correct"]
        return out

    for heading, key in (("By topic", lambda r: r["topic"]),
                         ("By question type", lambda r: r["tag"])):
        grouped = _group(key)
        if not grouped:
            continue
        # Weakest first: that is the list a student should actually act on.
        ranked = sorted(grouped.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[0]))
        lines.append(f"\n<b>{heading}</b>")
        for name, (n, c) in ranked:
            lines.append(f"{name}: {c}/{n}")
        if len(ranked) > 1:
            lines.append(f"Weakest here: {ranked[0][0]}")

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
        return await m.answer("No tournament on right now.")
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
    await m.answer("Sending the monthly sheets...")
    sent = await jobs.monthly_notes(bot)
    await m.answer(f"Sent {sent} document(s).")
