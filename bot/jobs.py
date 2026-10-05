"""Scheduled work: the weekly quiz set, the monthly sheets, tournament closing."""
from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, resources
from .config import ADMIN_IDS, DEFAULT_LEVEL, TZ
from .sender import safe_send, send_note, send_question

log = logging.getLogger(__name__)

FANOUT_PAUSE = 0.05  # seconds between sends, comfortably under Telegram's ~30/s
TOURNAMENT_DAYS = 14


async def weekly_quiz(bot) -> int:
    """A set of five for every subscriber, sent question by question.

    The set is the student's current benchmark set, so everyone at the same point
    in the course gets the same five questions, and a student who stopped halfway
    through last week's set gets the rest of that one rather than a fresh set.

    Answers here do not score for the tournament: only /quizme does, which is
    what keeps the weekly push from quietly becoming a leaderboard farm.
    """
    subscribers = await db.subscribers("weekly_sub")
    sent = 0
    for uid in subscribers:
        level = await db.get_level(uid) or DEFAULT_LEVEL
        current = await db.quiz_set(uid, level)
        if current is None or not current["remaining"]:
            continue
        count = len(current["remaining"])
        await safe_send(
            bot, uid,
            f"📅 Your Monday quiz, set {current['number']} of "
            f"{current['total_sets']}: {count} question"
            f"{'s' if count != 1 else ''} to go.",
        )
        await asyncio.sleep(FANOUT_PAUSE)
        for question in current["remaining"]:
            if await send_question(bot, uid, question, "weekly"):
                sent += 1
            await asyncio.sleep(FANOUT_PAUSE)
    log.info("weekly quiz: %s question(s) to %s subscriber(s)", sent, len(subscribers))
    return sent


async def monthly_notes(bot) -> int:
    """The monthly drop: every overview sheet plus the reserved focused ones.

    Sent as the PDFs themselves. The six focused sheets here are exactly the ones
    `/randomnotes` withholds, so the bundle is not made up of sheets students have
    already been handed at random.
    """
    sheets = list(resources.TIER_A) + list(resources.MONTHLY)
    if not sheets:
        log.warning("no sheets on disk, skipping the monthly drop")
        return 0

    sent = 0
    for uid in await db.subscribers("notes_sub"):
        await safe_send(
            bot, uid,
            f"📚 Your monthly sheets are here: {len(resources.TIER_A)} overview "
            f"sheets plus {len(resources.MONTHLY)} focused ones. "
            "Send /stats any time to see how you are doing.",
        )
        await asyncio.sleep(FANOUT_PAUSE)
        for note in sheets:
            if await send_note(bot, uid, note):
                sent += 1
            await asyncio.sleep(FANOUT_PAUSE)
    log.info("monthly sheets: %s document(s)", sent)
    return sent


async def announce_tournament(bot) -> int:
    """Tell every active user the tournament has started.

    Needed because entry is automatic: students are in a competition the moment it
    opens, so the least the bot can do is say so.
    """
    t = await db.active_tournament()
    if not t:
        return 0
    ends = t["ends_at"].astimezone(ZoneInfo(TZ)).strftime("%d %b")
    sent = 0
    for uid in await db.all_users():
        if await safe_send(
            bot, uid,
            "🏆 <b>A tournament has started and you're in it.</b>\n\n"
            f"Runs until {ends}. Every question you get right in /quizme is a "
            "point, and each question counts once.\n\n"
            "/leaderboard to see where you stand, /stats for your weak topics.",
        ):
            sent += 1
        await asyncio.sleep(FANOUT_PAUSE)
    log.info("tournament announcement reached %s user(s)", sent)
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
            f"🏆 The tournament's over and you finished #{row['rk']} with "
            f"{row['points']} points! 🎉\n\n"
            "Message the LKC OphSoc EXCO (@lkceye) to claim your award.",
        )
        await asyncio.sleep(FANOUT_PAUSE)

    table = "\n".join(
        f"{r['rk']}. @{r['username'] or '(no username)'} (id {r['user_id']}): {r['points']} pts"
        for r in rows[:10]
    ) or "no participants"
    for admin in sorted(ADMIN_IDS):
        await safe_send(bot, admin, f"🏁 Tournament {tid} closed. Final standings:\n\n{table}")
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
    scheduler.add_job(weekly_quiz, "cron", day_of_week="mon", hour=9,
                      args=[bot], **common)
    # Monthly on the 1st. A cron month rollover is exact, unlike the ISO-week
    # parity trick the fortnightly schedule used to use.
    scheduler.add_job(monthly_notes, "cron", day=1, hour=10, args=[bot], **common)
    scheduler.add_job(close_tournaments, "cron", minute="*/30", args=[bot], **common)
