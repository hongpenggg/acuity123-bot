import asyncio, logging
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from . import db, handlers, jobs
from .config import BOT_TOKEN, DATABASE_URL, TZ

async def main():
    logging.basicConfig(level=logging.INFO)
    await db.init(DATABASE_URL)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(handlers.router)
    sched = AsyncIOScheduler(timezone=TZ)
    jobs.register(sched, bot)
    sched.start()
    await bot.delete_webhook()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())