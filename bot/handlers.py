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
from .config import ADMIN_IDS, CREDIT, DEFAULT_LEVEL, DISCLAIMER, LEVELS, TZ
from .sender import ack, deliver_verdict, safe_send, send_question
from .text import letter, mask, parse_options

log = logging.getLogger(__name__)
router = Router()

_EXPLAIN_COOLDOWN = 10.0
# Bounded and self-expiring: the old plain dict grew forever and survived restarts
# with stale timestamps.
_recent_explain: TTLCache = TTLCache(maxsize=10_000, ttl=900)

MODES = ("practice", "weekly")

HELP = (
    "/practice — adaptive question, weighted to your weak topics\n"
    "/level — Pre-Clinical / Clinical / Post-MBBS\n"
    "/subscribe — a question every Monday\n"
    "/unsubscribe — stop the weekly question\n"
    "/notes — Tier B cheat sheets, on demand\n"
    "/notes_sub — Tier A cheat sheets, fortnightly\n"
    "/notes_unsub — stop the fortnightly notes\n"
    "/tournament — join or leave the running tournament\n"
    "/leaderboard — top 3 and your rank"
)


def _level(level: str | None) -> str:
    return level if level in LEVELS else DEFAULT_LEVEL


def _level_kb(current: str | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ " if key == current else "") + label,
            callback_data=f"lv:{key}",
        )]
        for key, label in LEVELS.items()
    ])


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
    await m.answer(
        f"{CREDIT}\n\n"
        f"Your level: {LEVELS[_level(level)]}\n"
        "Tap below to set it (you can change it any time):\n\n"
        f"{HELP}\n\n"
        f"{DISCLAIMER}",
        reply_markup=_level_kb(level),
    )


@router.message(Command("level"))
async def level_cmd(m: Message):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    current = await db.get_level(uid)
    await m.answer(
        f"Your level: {LEVELS[_level(current)]}\n"
        "Questions and notes follow your level. Change it any time.",
        reply_markup=_level_kb(current),
    )


@router.callback_query(F.data.startswith("lv:"))
async def set_level(c: CallbackQuery):
    answered = False
    try:
        key = c.data.split(":", 1)[1]
        if key not in LEVELS:
            answered = True
            return await ack(c, "Unknown level.", True)
        uid = c.from_user.id
        await db.upsert_user(uid, c.from_user.username)
        await db.set_level(uid, key)
        answered = True
        await ack(c, f"Level set to {LEVELS[key]}")
        if c.message is not None:
            try:
                await c.message.edit_text(
                    f"✅ Level set to {LEVELS[key]}. Your questions and notes "
                    f"now follow this level. Change it any time with /level.",
                )
            except Exception:
                log.debug("level confirmation edit failed", exc_info=True)
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
    await send_question(bot, uid, await db.pick_question(uid, level), "practice")


@router.callback_query(F.data == "next")
async def next_question(c: CallbackQuery, bot: Bot):
    answered = False
    try:
        await ack(c)
        answered = True
        uid = c.from_user.id
        level = _level(await db.get_level(uid))
        await send_question(bot, uid, await db.pick_question(uid, level), "practice")
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
            return await ack(c, "Already answered")

        if correct and mode == "practice":
            await db.award_point(c.from_user.id, question["id"])

        verdict = "✅ Correct!" if correct else (
            f"❌ Wrong. Answer: {letter(question['correct_idx'])}. "
            f"{options[question['correct_idx']]}"
        )
        buttons = [InlineKeyboardButton(text="💡 Explain", callback_data=f"e:{question['id']}")]
        if mode == "practice":
            buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data="next"))

        answered = True
        await ack(c)
        await deliver_verdict(
            c,
            f"{message.text}\n\n{verdict}",
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

        last = _recent_explain.get(uid)
        if last is not None and time.monotonic() - last < _EXPLAIN_COOLDOWN:
            answered = True
            return await ack(c, "Slow down a little — try again in a few seconds.", True)
        _recent_explain[uid] = time.monotonic()

        answered = True
        await ack(c, "Thinking…")

        try:
            text = await llm.explain(question)
        except Exception:
            log.exception("explain failed for question %s", question["id"])
            text = "Sorry, explanations are unavailable right now."
        await safe_send(bot, uid, f"💡 {text}\n\n({DISCLAIMER})")
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
    await m.answer("Subscribed — you'll get a question every Monday morning (SGT).")


@router.message(Command("unsubscribe"))
async def unsubscribe(m: Message):
    # upsert first, otherwise the flag write hits no rows and the confirmation lies.
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", False)
    await m.answer("Unsubscribed from the weekly question.")


@router.message(Command("notes"))
async def notes(m: Message, command: CommandObject, bot: Bot):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    level = _level(await db.get_level(uid))

    if not command.args:
        topics = await db.note_topics(level, "B")
        if not topics:
            return await m.answer(
                f"No Tier B notes for {LEVELS[level]} yet. /level to switch level, "
                "or /notes_sub for the fortnightly Tier A set."
            )
        listing = "\n".join(f"— {t}" for t in topics)
        return await m.answer(
            f"Tier B notes for {LEVELS[level]}:\n{listing}\n\n"
            "Use /notes <topic>. Tier A sets arrive by subscription: /notes_sub."
        )

    rows = await db.get_notes(command.args.strip(), level, "B")
    if not rows:
        return await m.answer("No notes for that topic. Send /notes to see the list.")
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
        f"Subscribed — Tier A ({LEVELS[level]}) cheat sheets arrive fortnightly. "
        "Stop with /notes_unsub."
    )


@router.message(Command("notes_unsub"))
async def notes_unsub(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "notes_sub", False)
    await m.answer("Unsubscribed from the fortnightly notes.")


# ------------------------------------------------------------------- tournament


@router.message(Command("tournament"))
async def tournament(m: Message):
    uid = m.from_user.id
    await db.upsert_user(uid, m.from_user.username)
    t = await db.active_tournament()
    if not t:
        return await m.answer("No tournament is running right now — watch this space.")
    if await db.is_joined(uid):
        await db.leave_tournament(uid)
        return await m.answer("You left the tournament. Send /tournament to rejoin.")
    await db.join_tournament(uid)
    await m.answer(
        f"🏆 You're in! This tournament closes {_local(t['ends_at'])}.\n"
        "Every correct /practice answer scores a point — once per question. "
        "Top 3 win a reward: contact LKC OphSoc when it closes."
    )


@router.message(Command("leaderboard"))
async def leaderboard(m: Message):
    rows = await db.leaderboard(limit=3)
    if rows is None:
        return await m.answer("No tournament is running right now.")
    if not rows:
        return await m.answer("🏆 No participants yet — be first: /tournament")

    top = "\n".join(
        f"{r['rk']}. @{mask(r['username'], r['user_id'])} — {r['points']} pts"
        for r in rows
    )
    mine = await db.my_rank(m.from_user.id)
    if mine is None:
        tail = "\n\nYou haven't joined — send /tournament."
    else:
        tail = f"\n\nYou: rank {mine['rk']}, {mine['points']} pts"
    await m.answer(f"🏆 Top 3\n{top}{tail}")


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
        return await m.answer("A tournament is already running.")
    tid = await db.start_tournament(14)
    await m.answer(f"Tournament {tid} started and runs for 2 weeks. Users join with /tournament.")


@router.message(Command("admin_tournament_end"))
async def admin_tournament_end(m: Message, bot: Bot):
    if not _is_admin(m):
        return
    t = await db.active_tournament()
    if not t:
        return await m.answer("No active tournament.")
    await m.answer(f"Closing tournament {t['id']} and notifying the top 3…")
    rows = await jobs.finish_tournament(bot, t["id"])
    await m.answer(f"Done — {len(rows)} participant(s) scored.")


@router.message(Command("admin_weekly_now"))
async def admin_weekly_now(m: Message, bot: Bot):
    """Manual trigger, so you never have to wait for Monday to demo the push."""
    if not _is_admin(m):
        return
    await m.answer("Sending the weekly question now…")
    sent = await jobs.weekly_question(bot)
    await m.answer(f"Sent to {sent} subscriber(s).")


@router.message(Command("admin_notes_now"))
async def admin_notes_now(m: Message, bot: Bot):
    if not _is_admin(m):
        return
    await m.answer("Sending the fortnightly notes now…")
    sent = await jobs.fortnightly_notes(bot)
    await m.answer(f"Sent {sent} message(s).")
