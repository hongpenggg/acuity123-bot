import asyncio, logging
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from . import db
from .handlers import send_question, safe_send
from .config import ADMIN_IDS

log = logging.getLogger(__name__)

async def weekly_question(bot):
    ts = await db.topics()
    if not ts:
        return
    topic = ts[date.today().isocalendar().week % len(ts)]
    for uid in await db.subscribers("weekly_sub"):
        await send_question(bot, uid, await db.pick_question(uid, topic=topic), "weekly")
        await asyncio.sleep(0.05)          # stay well under ~30 msg/s

async def fortnightly_notes(bot):
    notes = await db.all_notes("A")
    if not notes:
        return
    for uid in await db.subscribers("notes_sub"):
        for n in notes:
            await safe_send(bot, uid, f"📘 {n['topic']}: {n['title']}\n\n{n['body']}"[:4096])
            await asyncio.sleep(0.05)

async def close_tournaments(bot):
    for t in await db.close_expired_tournaments():
        rows = await db.leaderboard()   # inactive now, so announce via admin only
        for a in ADMIN_IDS:
            await safe_send(bot, a, f"Tournament {t['id']} closed. Pull final standings from the DB for rewards.")

def register(s: AsyncIOScheduler, bot):
    s.add_job(weekly_question, "cron", day_of_week="mon", hour=9, args=[bot])
    s.add_job(fortnightly_notes, "cron", day_of_week="mon", hour=10, week="*/2", args=[bot])
    s.add_job(close_tournaments, "cron", minute="*/30", args=[bot])