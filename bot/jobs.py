"""Scheduled work: the weekly quiz set, the monthly sheets, tournament closing."""
from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, resources
from .config import ADMIN_IDS, DEFAULT_LEVEL, LEVELS, TZ
from .sender import safe_send, send_note, send_question
from .text import sheets_done

log = logging.getLogger(__name__)

TOURNAMENT_DAYS = 14

# How many students a fan-out serves at once. Sends are paced per chat
# (sender.PACER), and a chat waiting out its gap must not hold up everybody else:
# one subscriber at a time meant a drop of N students spent N seconds purely
# waiting. Bounded because each student in flight holds a pool connection while
# it reads their level and history. Telegram's stream-wide limit is the pacer's
# job, not this number's.
FANOUT_CONCURRENCY = 8

# How many sheets one fortnightly drop hands over. One: a drop is a nudge to read
# something, not a dump of PDFs to scroll past. The old monthly drop sent twelve
# at once and that is exactly what it looked like. A student who wants more does
# not have to wait - /notes hands over the next one on demand, from the same
# queue and the same delivery history.
SHEETS_PER_DROP = 1

# How many questions the Monday push sends. One set's worth.
SET_PER_PUSH = 5

T = TypeVar("T")

# Every job here has two live call sites: the cron trigger and the matching
# /admin_*_now command. aiogram polls with handle_as_tasks=True, so two taps of
# /admin_notes_now, or one tap landing on top of the 10:00 fire, ran the whole
# fan-out twice at once. Both runs read db.notes_delivered before either reached
# db.record_notes_sent, so the subscriber got the same PDF twice; the same race
# between close_tournaments and /admin_tournament_end sent the winners two award
# DMs each. APScheduler's max_instances=1 only covers cron against cron, because
# the admin command is a different call site entirely.
#
# There is exactly one bot process (two would make Telegram answer Conflict on
# every poll, which the README is emphatic about), so an in-process guard is
# enough.
_in_flight: dict[str, asyncio.Task] = {}


def _single_flight(key: Callable[..., str]):
    """One run of this job at a time; a second caller joins the first.

    Joining rather than refusing is the point. An admin who taps during the cron
    fire gets back the real summary of the run that covered their tap, so the
    reply they already get spells out what went out. Returning a zeroed result
    instead would read as "nothing happened" and invite them to tap again.

    `key` turns the call's arguments into the identity of the run, so
    finish_tournament is guarded per tournament rather than across all of them.
    """
    def decorate(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def guarded(*args: Any, **kw: Any) -> T:
            name = key(*args, **kw)
            joined = _in_flight.get(name)
            if joined is not None:
                log.warning("%s is already running; joining that run rather "
                            "than sending everything twice", name)
                # Shielded: a caller who gives up must not cancel the run the
                # other callers are waiting on.
                return await asyncio.shield(joined)
            task = asyncio.ensure_future(fn(*args, **kw))
            _in_flight[name] = task
            try:
                return await task
            finally:
                _in_flight.pop(name, None)
        return guarded
    return decorate


async def drain(timeout: float = 5.0) -> None:
    """Let a fan-out already in flight finish, within reason.

    Called by main.py before the scheduler goes down. APScheduler's
    AsyncIOExecutor cancels every running job on shutdown whatever `wait` is set
    to (it cancels `_pending_futures` unconditionally), so a deploy during the
    10:00 drop otherwise lands between the upload and db.record_notes_sent and
    that student is sent the same sheet again next fortnight.

    Bounded rather than unlimited: a full fan-out is paced per chat and can run
    for minutes, and systemd would SIGKILL us at TimeoutStopSec anyway. The
    subscribers not reached keep their place in the queue, so a truncated drop
    costs a fortnight's nudge rather than a sheet.
    """
    running = [task for task in _in_flight.values() if not task.done()]
    if not running:
        return
    log.info("waiting up to %ss for %s job(s) still in flight", timeout, len(running))
    _, pending = await asyncio.wait(running, timeout=timeout)
    if pending:
        log.warning("%s job(s) did not finish inside %ss and will be cancelled",
                    len(pending), timeout)


async def _fan_out(subscribers: list[int],
                   serve: Callable[[int], Awaitable[dict[str, int]]],
                   ) -> list[dict[str, int]]:
    """Serve every subscriber, a few at a time, and collect their tallies.

    Concurrent because the per-chat pacing is only free if another chat can be
    served while this one waits out its gap. Bounded by FANOUT_CONCURRENCY, which
    is also what holds the stream under Telegram's overall limit.
    """
    gate = asyncio.Semaphore(FANOUT_CONCURRENCY)

    async def one(uid: int) -> dict[str, int]:
        async with gate:
            try:
                return await serve(uid)
            except Exception:
                # One student's row, level or send should not take down the drop
                # for everybody behind them in the list.
                log.exception("fan-out failed for %s", uid)
                return {"sent": 0, "reached": 0, "finished": 0}

    return list(await asyncio.gather(*(one(uid) for uid in subscribers)))


async def _weekly_for(bot, uid: int) -> dict[str, int]:
    """One student's Monday set, and what it contributes to the summary."""
    level = await db.get_level(uid) or DEFAULT_LEVEL
    current = await db.current_set(uid, level)
    if current is None:
        # current_set says None both for a finished level and for one with no
        # bank loaded. Silence either way left students subscribed and waiting
        # every Monday for something that was never coming.
        if not await db.level_total(level):
            log.warning("no %s questions loaded; skipping user %s", level, uid)
            return {"sent": 0, "reached": 0, "finished": 0}
        await safe_send(
            bot, uid,
            "🏁 That is every "
            f"{LEVELS.get(level, level)} question answered, so there is "
            "no Monday set to send.\n\n"
            "/review the ones you missed, or /changestreams to move on. "
            "I'll stop sending these until then.", pace=True)
        await db.set_flag(uid, "weekly_sub", False)
        return {"sent": 0, "reached": 0, "finished": 1}

    # A full five every Monday, not "whatever is left of the set you are
    # part-way through". A set is just a rolling block of five answers, so
    # five new questions always advances the student by one set; only a level
    # that is nearly exhausted sends fewer.
    remaining, _ = await db.progress(uid, level)
    count = min(SET_PER_PUSH, remaining)
    if not await safe_send(
        bot, uid,
        f"📅 Your Monday quiz: {count} question"
        f"{'s' if count != 1 else ''}, picked for you. "
        f"You're on set {current['number']} of {current['total_sets']}.",
        pace=True,
    ):
        # The lead not landing means this chat is gone (blocked, and _send_once
        # has already deactivated them) or throttled past what the retries cover.
        # Five more cards would be five more Forbidden round trips and five more
        # redundant UPDATEs, every Monday.
        log.info("skipping %s's Monday set: the lead message did not land", uid)
        return {"sent": 0, "reached": 0, "finished": 0}

    # `exclude` is load-bearing. Nothing is answered during a push, so
    # ranking sees identical history on every call: without it the same
    # question came back up to five times under a "5 questions" heading.
    got = 0
    taken: list[int] = []
    for position in range(count):
        question = await db.pick_question(uid, level, taken)
        if question is None:
            break
        taken.append(question["id"])
        lead = (f"📅 <b>Monday quiz</b> · question {position + 1} of {count}")
        if not await send_question(bot, uid, question, "weekly", lead=lead,
                                   pace=True):
            # They were promised `count` questions. Pushing the rest into a chat
            # that just refused one only burns round trips.
            log.warning("stopped %s's Monday set after %s of %s: a card did "
                        "not land", uid, got, count)
            break
        got += 1
    return {"sent": got, "reached": int(bool(got)), "finished": 0}


@_single_flight(lambda bot: "weekly_quiz")
async def weekly_quiz(bot) -> dict[str, int]:
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
    tallies = await _fan_out(subscribers, lambda uid: _weekly_for(bot, uid))
    sent = sum(t["sent"] for t in tallies)
    reached = sum(t["reached"] for t in tallies)
    finished = sum(t["finished"] for t in tallies)
    log.info("weekly quiz: %s question(s) to %s of %s subscriber(s), "
             "%s finished their level", sent, reached, len(subscribers), finished)
    return {"sent": sent, "reached": reached,
            "subscribers": len(subscribers), "finished": finished}


async def _notes_for(bot, uid: int) -> dict[str, int]:
    """One student's sheets for this drop, and what they add to the summary."""
    level = await db.get_level(uid) or DEFAULT_LEVEL
    delivered = await db.notes_delivered(uid, level)
    queue = (resources.unsent(level, "a", delivered["a"])
             + resources.unsent(level, "b", delivered["b"]))

    if not queue:
        await safe_send(bot, uid, sheets_done(LEVELS.get(level, level)),
                        parse_mode="HTML", pace=True)
        await db.set_flag(uid, "notes_sub", False)
        return {"sent": 0, "reached": 0, "finished": 1}

    batch = queue[:SHEETS_PER_DROP]
    left = len(queue) - len(batch)
    head = ("📬 Your fortnightly cheat sheet." if len(batch) == 1
            else f"📬 Your fortnightly cheat sheets: {len(batch)} of them.")
    tail = (f" {left} to go after this." if left
            else " That is the last one.")
    if not await safe_send(
        bot, uid,
        f"{head}{tail}\n"
        "Want the next one sooner? /notes hands it over any time.",
        pace=True,
    ):
        # A student who has blocked the bot is deactivated by this first send.
        # Carrying on meant another Forbidden round trip and another redundant
        # UPDATE per sheet, every fortnight.
        log.info("skipping %s's drop: the lead message did not land", uid)
        return {"sent": 0, "reached": 0, "finished": 0}

    # Recorded only for the sheets that actually reached the student. Note
    # that `send_note` degrades a failed upload to a GitHub link and still
    # returns True - they got the sheet, so it counts. False means they got
    # nothing at all, and then it is left in the queue to come round again
    # rather than silently skipped forever.
    landed = []
    for note in batch:
        if not await send_note(bot, uid, note, pace=True):
            log.warning("stopped %s's drop after %s of %s sheet(s): one did "
                        "not land", uid, len(landed), len(batch))
            break
        landed.append((note.tier, note.code))
    if landed:
        await db.record_notes_sent(uid, level, landed)
    return {"sent": len(landed), "reached": int(bool(landed)), "finished": 0}


@_single_flight(lambda bot: "fortnightly_notes")
async def fortnightly_notes(bot) -> dict[str, int]:
    """The fortnightly sheet drop: the next few sheets this student has not had.

    Walks the student's own level, overview sheets first and then focused ones -
    the same order `/notes` uses, and sharing the same `note_deliveries` history,
    so a sheet a student already pulled by hand is never pushed at them again.

    A subscriber who has had everything is congratulated **and unsubscribed**,
    because the alternative is pinging them every fortnight with nothing to send.
    They can start again with /subscribenotes once there is more content, and
    /resources still has every sheet.
    """
    subscribers = await db.subscribers("notes_sub")
    tallies = await _fan_out(subscribers, lambda uid: _notes_for(bot, uid))
    sent = sum(t["sent"] for t in tallies)
    reached = sum(t["reached"] for t in tallies)
    finished = sum(t["finished"] for t in tallies)
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
            parse_mode="HTML", pace=True,
        ):
            sent += 1
    log.info("tournament announcement reached %s user(s)", sent)
    return sent


@_single_flight(lambda bot, tid: f"finish_tournament:{tid}")
async def finish_tournament(bot, tid: int) -> list:
    """Announce and close one tournament.

    Standings are read *before* the tournament is deactivated — `leaderboard()`
    only looks at the active row, which is why the original announcement always
    came back empty. Winners get their own DM (only the top 3), and admins get the
    full table including the Telegram IDs OphSoc needs to hand out rewards.

    Guarded per tournament: the 30-minute `close_tournaments` sweep and
    /admin_tournament_end are two call sites for the same close, and running both
    at once sent every winner two award DMs.
    """
    rows = await db.standings(tid)
    await db.end_tournament(tid)

    # Entry is automatic, so `rows` is everyone who ever pressed /start, most of
    # them on zero. Telling someone they placed third with 0 points and should
    # claim an award is worse than telling them nothing.
    winners = [row for row in rows if row["points"] > 0][:3]
    for row in winners:
        points = f"{row['points']} point" + ("" if row["points"] == 1 else "s")
        await safe_send(
            bot, row["user_id"],
            f"🏆 The tournament's over and you finished #{row['rk']} with "
            f"{points}! 🎉\n\n"
            "Message the LKC OphSoc EXCO (@lkceye) to claim your award.",
            pace=True,
        )

    scorers = [r for r in rows if r["points"] > 0]
    table = "\n".join(
        f"{r['rk']}. @{r['username'] or '(no username)'} (id {r['user_id']}): {r['points']} pts"
        for r in scorers[:10]
    ) or "nobody scored"
    for admin in sorted(ADMIN_IDS):
        await safe_send(
            bot, admin,
            f"🏁 Tournament {tid} closed.\n"
            f"{len(scorers)} of {len(rows)} entrant(s) scored; "
            f"{len(winners)} award message(s) sent.\n\n{table}", pace=True)

    log.info("tournament %s closed: %s entrant(s), %s scored",
             tid, len(rows), len(scorers))
    return rows


async def close_tournaments(bot) -> int:
    closed = 0
    for tid in await db.expired_tournaments():
        await finish_tournament(bot, tid)
        closed += 1
    return closed


def register(scheduler: AsyncIOScheduler, bot) -> None:
    # misfire_grace_time is not restart insurance, which is what it looks like.
    # The default MemoryJobStore starts empty and these jobs are re-added here on
    # every boot, so next_run_time is always computed from the moment the process
    # started: a restart at 09:05 on a Monday does not leave the 09:00 push
    # "missed", it schedules it for the following Monday, and that week is
    # skipped with nothing in the log either way. What the hour of grace does
    # cover is the process being alive but not getting to the job on time, which
    # APScheduler's 1-second default would drop.
    #
    # Firing a missed weekly on the next boot costs more than a keyword. Either a
    # persistent job store (SQLAlchemyJobStore against the same Postgres: a new
    # SQLAlchemy dependency, a table APScheduler owns and migrates itself, stable
    # job ids with replace_existing=True, and a long outage replaying a push that
    # is no longer wanted), or a "last fired" row this module stamps and checks
    # at startup (no new dependency, but a schema change, a migration, and a
    # policy for how late is too late to bother). Neither is worth it while a
    # missed Monday costs a nudge: /quizme and /notes hand over the same content
    # on demand, from the same queue.
    common = dict(coalesce=True, max_instances=1, misfire_grace_time=3600)
    scheduler.add_job(weekly_quiz, "cron", day_of_week="mon", hour=9,
                      args=[bot], **common)
    # Fortnightly, as the 1st and the 15th. Two fixed days a month is exact,
    # unlike the ISO-week parity trick an earlier fortnightly schedule used,
    # which drifted at the turn of a year.
    scheduler.add_job(fortnightly_notes, "cron", day="1,15", hour=10,
                      args=[bot], **common)
    scheduler.add_job(close_tournaments, "cron", minute="*/30", args=[bot], **common)
