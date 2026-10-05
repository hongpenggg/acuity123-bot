"""Entry point: wire up the DB, the dispatcher, the scheduler, then poll."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import commands, db, handlers, jobs
from .config import ADMIN_IDS, BOT_TOKEN, DATABASE_URL, TZ

log = logging.getLogger(__name__)

async def on_error(event: ErrorEvent) -> None:
    """Last line of defence: log, and never leave a user's client spinning."""
    log.error("unhandled %s", type(event.update.event).__name__,
              exc_info=event.exception)
    if isinstance(event.update.event, CallbackQuery):
        with contextlib.suppress(Exception):
            await event.update.event.answer("Something went wrong. Please try again.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    await db.init(DATABASE_URL)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp.errors.register(on_error)

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(TZ))
    jobs.register(scheduler, bot)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        # Minimal until someone starts the bot; /start then gives their chat the
        # full menu (see bot/commands.py).
        await commands.sync_default(bot)
        await commands.sync_admins(bot, ADMIN_IDS)
        scheduler.start()
        log.info("polling started (tz=%s)", TZ)
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types() or None)
    finally:
        # Without this the pool and the HTTP session leak on every restart.
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
