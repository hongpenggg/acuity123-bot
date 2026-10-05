"""Scheduled work: the weekly quiz set, the monthly sheets, tournament closing."""
from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, resources
from .config import ADMIN_IDS, DEFAULT_LEVEL, LEVELS, TZ
from .sender import safe_send, send_note, send_question
from .text import sheets_done

log = logging.getLogger(__name__)

FANOUT_PAUSE = 0.05  # seconds between sends, comfortably under Telegram's ~30/s
TOURNAMENT_DAYS = 14

# How many sheets one fortnightly drop hands over. One: a drop is a nudge to read
# something, not a dump of PDFs to scroll past. The old monthly drop sent twelve
# at once and that is exactly what it looked like. A student who wants more does
# not have to wait - /notes hands over the next one on demand, from the same
# queue and the same delivery history.
SHEETS_PER_DROP = 1

# How many questions the Monday push sends. One set's worth.
SET_PER_PUSH = 5


async def weekly_quiz(bot) -> dict:
    """A set of five for every subscriber, sent question by question.

    Each student gets five questions picked for *them* - spread across topics and
    weighted toward the ones they keep getting wrong - and picked one at a time,
    so answering the first influences the second. A student who stopped halfway
    through last week continues from where they were, because a set is simply
    their next five answers.

    Answers here do not score for the tournament: only /quizme does, which is
    what keeps the weekly push from quietly becoming a leaderboard farm.

    Returns a summary rather than a bare total, because "15" on its own told one
    admin the 5-question push was broken when it had simply reached three people.
    """
    subscribers = await db.subscribers("weekly_sub")
    sent = 0
    reached = 0
    for uid in subscribers:
        level = await db.get_level(uid) or DEFAULT_LEVEL
        current = await db.current_set(uid, level)
        if current is None:
            continue          # finished this level, nothing left to push
        await safe_send(
            bot, uid,
            f"📅 Your Monday quiz: set {current['number']} of "
            f"{current['total_sets']}, {SET_PER_PUSH} questions.",
        )
        await asyncio.sleep(FANOUT_PAUSE)
        # Picked one at a time rather than as a batch: pick_question reads this
        # student's history, so answering question 1 should influence what
        # question 2 is, exactly as it does in /quizme.
        got = 0
        for _ in range(SET_PER_PUSH):
            question = await db.pick_question(uid, level)
            if question is None:
                break
            if await send_question(bot, uid, question, "weekly"):
                got += 1
            await asyncio.sleep(FANOUT_PAUSE)
        sent += got
        reached += bool(got)
    log.info("weekly quiz: %s question(s) to %s of %s subscriber(s)",
             sent, reached, len(subscribers))
    return {"sent": sent, "reached": reached, "subscribers": len(subscribers)}


async def fortnightly_notes(bot) -> int:
    """The fortnightly sheet drop: the next few sheets this student has not had.

    Walks the student's own level, overview sheets first and then focused ones -
    the same order `/notes` uses, and sharing the same `note_deliveries` history,
    so a sheet a student already pulled by hand is never pushed at them again.

    A subscriber who has had everything is congratulated **and unsubscribed**,
    because the alternative is pinging them every fortnight with nothing to send.
    They can start again with /subscribenotes once there is more content, and
    /resources still has every sheet.
    """
    sent = 0
    reached = 0
    finished = 0
    subscribers = await db.subscribers("notes_sub")
    for uid in subscribers:
        level = await db.get_level(uid) or DEFAULT_LEVEL
        delivered = await db.notes_delivered(uid, level)
        queue = (resources.unsent(level, "a", delivered["a"])
                 + resources.unsent(level, "b", delivered["b"]))

        if not queue:
            await safe_send(bot, uid, sheets_done(LEVELS.get(level, level)),
                            parse_mode="HTML")
            await db.set_flag(uid, "notes_sub", False)
            finished += 1
            await asyncio.sleep(FANOUT_PAUSE)
            continue

        batch = queue[:SHEETS_PER_DROP]
        left = len(queue) - len(batch)
        head = ("📬 Your fortnightly cheat sheet." if len(batch) == 1
                else f"📬 Your fortnightly cheat sheets: {len(batch)} of them.")
        tail = (f" {left} to go after this." if left
                else " That is the last one.")
        await safe_send(
            bot, uid,
            f"{head}{tail}\n"
            "Want the next one sooner? /notes hands it over any time.",
        )
        await asyncio.sleep(FANOUT_PAUSE)

        # Recorded only for the sheets that actually reached the student. Note
        # that `send_note` degrades a failed upload to a GitHub link and still
        # returns True - they got the sheet, so it counts. False means they got
        # nothing at all (they have blocked the bot), and then it is left in the
        # queue to come round again rather than silently skipped forever.
        landed = []
        for note in batch:
            if await send_note(bot, uid, note):
                landed.append((note.tier, note.code))
                sent += 1
            await asyncio.sleep(FANOUT_PAUSE)
        if landed:
            await db.record_notes_sent(uid, level, landed)
        reached += bool(landed)

    log.info("fortnightly sheets: %s document(s) to %s of %s subscriber(s), "
             "%s finished their level", sent, reached, len(subscribers), finished)
    return {"sent": sent, "reached": reached,
            "subscribers": len(subscribers), "finished": finished}


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
    # Fortnightly, as the 1st and the 15th. Two fixed days a month is exact,
    # unlike the ISO-week parity trick an earlier fortnightly schedule used,
    # which drifted at the turn of a year.
    scheduler.add_job(fortnightly_notes, "cron", day="1,15", hour=10,
                      args=[bot], **common)
    scheduler.add_job(close_tournaments, "cron", minute="*/30", args=[bot], **common)
