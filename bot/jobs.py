"""Scheduled work: the weekly question, the fortnightly notes, tournament closing."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db
from .config import ADMIN_IDS, DEFAULT_LEVEL, LEVELS
from .sender import safe_send, send_question

log = logging.getLogger(__name__)

FANOUT_PAUSE = 0.05  # seconds between sends, comfortably under Telegram's ~30/s
TOURNAMENT_DAYS = 14


async def weekly_question(bot) -> int:
    """One question per subscriber, same topic for everyone at a given level.

    Returns the number of subscribers reached.
    """
    week = date.today().isocalendar().week
    topics_by_level: dict[str, list[str]] = {}
    subscribers = await db.subscribers("weekly_sub")
    sent = 0
    for uid in subscribers:
        level = await db.get_level(uid) or DEFAULT_LEVEL
        if level not in topics_by_level:
            topics_by_level[level] = await db.topics(level)
        topics = topics_by_level[level]
        if not topics:
            continue
        topic = topics[week % len(topics)]
        question = await db.pick_question(uid, level, topic=topic)
        if await send_question(bot, uid, question, "weekly"):
            sent += 1
        await asyncio.sleep(FANOUT_PAUSE)
    log.info("weekly question delivered to %s/%s subscriber(s)", sent, len(subscribers))
    return sent


async def fortnightly_notes(bot) -> int:
    notes_by_level: dict[str, list] = {}
    sent = 0
    for uid in await db.subscribers("notes_sub"):
        level = await db.get_level(uid) or DEFAULT_LEVEL
        if level not in notes_by_level:
            notes_by_level[level] = await db.all_notes(level, "A")
        notes = notes_by_level[level]
        if not notes:
            continue
        await safe_send(
            bot, uid,
            f"📘 Tier A cheat sheets — {LEVELS[level]}\n"
            f"{len(notes)} topic(s) this fortnight:",
        )
        await asyncio.sleep(FANOUT_PAUSE)
        for note in notes:
            if await safe_send(bot, uid, f"📘 {note['topic']}: {note['title']}\n\n{note['body']}"):
                sent += 1
            await asyncio.sleep(FANOUT_PAUSE)
    log.info("fortnightly notes: %s message(s)", sent)
    return sent


async def finish_tournament(bot, tid: int) -> list:
    """Announce and close one tournament.

    Standings are read *before* the tournament is deactivated — `leaderboard()`
    only looks at the active row, which is why the original announcement always
    came back empty. Winners get their own DM (only the top 3), and admins get the
    full table including the Telegram IDs OphSoc needs to hand out rewards.
    """
    rows = await db.standings(tid)
    await db.end_tournament(tid)

    for row in rows[:3]:
        await safe_send(
            bot, row["user_id"],
            f"🏆 The tournament has closed — you finished #{row['rk']} with "
            f"{row['points']} points!\n\nMessage LKC OphSoc to claim your reward. 🎉",
        )
        await asyncio.sleep(FANOUT_PAUSE)

    table = "\n".join(
        f"{r['rk']}. @{r['username'] or '—'} (id {r['user_id']}) — {r['points']} pts"
        for r in rows[:10]
    ) or "no participants"
    for admin in sorted(ADMIN_IDS):
        await safe_send(bot, admin, f"Tournament {tid} closed.\nFinal standings:\n{table}")
        await asyncio.sleep(FANOUT_PAUSE)

    log.info("tournament %s closed with %s participant(s)", tid, len(rows))
    return rows


async def close_tournaments(bot) -> int:
    closed = 0
    for tid in await db.expired_tournaments():
        await finish_tournament(bot, tid)
        closed += 1
    return closed


def register(scheduler: AsyncIOScheduler, bot) -> None:
    # misfire_grace_time matters: APScheduler's 1-second default silently skips a
    # job whose scheduled minute passed while the process was restarting or busy,
    # which is exactly how a weekly push goes missing without any error.
    common = dict(coalesce=True, max_instances=1, misfire_grace_time=3600)
    scheduler.add_job(weekly_question, "cron", day_of_week="mon", hour=9,
                      args=[bot], **common)
    # `week="*/2"` is ISO week parity (odd weeks). A 53-week year therefore leaves
    # one 1-week gap instead of 2 — acceptable, but it is not "every 14 days".
    scheduler.add_job(fortnightly_notes, "cron", day_of_week="mon", hour=10,
                      week="*/2", args=[bot], **common)
    scheduler.add_job(close_tournaments, "cron", minute="*/30", args=[bot], **common)
