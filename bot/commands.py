"""The bot's command menu, and how it changes once someone has started.

Telegram keeps a command list per *scope*. Two consequences shape this module:

* A brand-new chat shows the **default** scope. Ours is minimal - just /start and
  /help - because a stranger has nothing to practise with until the bot has
  recorded them.
* Sending /start sets a **chat-scoped** list for that user with everything on it.
  That is what makes the menu fill up with /practice and friends instead of
  sitting on /start. Telegram caches the menu per chat, so this also refreshes a
  client that is still showing a stale list.

Admin commands are chat-scoped too, so they only ever appear to admins.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

log = logging.getLogger(__name__)

#: Shown to someone who has not started the bot yet.
BEFORE_START = [
    BotCommand(command="start", description="What this bot does"),
    BotCommand(command="help", description="What this bot does"),
]

#: The student-facing menu, set for a chat once they send /start.
PUBLIC = [
    BotCommand(command="practice", description="Adaptive practice question"),
    BotCommand(command="resources", description="Revision sheets as PDFs"),
    BotCommand(command="changestreams", description="Pre-Clinical / Clinical / Post-MBBS"),
    BotCommand(command="tournament", description="Join or leave the tournament"),
    BotCommand(command="leaderboard", description="Top 3 and your rank"),
    BotCommand(command="subscribe", description="A question every Monday"),
    BotCommand(command="unsubscribe", description="Stop the weekly question"),
    BotCommand(command="notes", description="Notes by topic, on demand"),
    BotCommand(command="notes_sub", description="Tier A notes, fortnightly"),
    BotCommand(command="notes_unsub", description="Stop the fortnightly notes"),
    BotCommand(command="help", description="What this bot can do"),
]

#: Added on top of PUBLIC for chats in ADMIN_IDS.
ADMIN = [
    BotCommand(command="admin_tournament_start", description="Start a tournament"),
    BotCommand(command="admin_tournament_end", description="Close it and announce winners"),
    BotCommand(command="admin_weekly_now", description="Send this week's question now"),
    BotCommand(command="admin_notes_now", description="Send the Tier A notes now"),
]


async def sync_default(bot: Bot) -> None:
    """The pre-/start menu, for every chat that has not been given its own."""
    try:
        await bot.set_my_commands(BEFORE_START, scope=BotCommandScopeDefault())
    except Exception:
        log.warning("could not set the default command menu", exc_info=True)


async def sync_chat(bot: Bot, chat_id: int, *, is_admin: bool) -> None:
    """Give one chat its full menu. Called when a user sends /start."""
    commands = PUBLIC + ADMIN if is_admin else PUBLIC
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception:
        # Telegram rejects a chat scope for a chat it has never seen. Harmless:
        # the default menu still applies, and /start will retry next time.
        log.info("could not set the menu for chat %s", chat_id, exc_info=True)


async def sync_admins(bot: Bot, admin_ids) -> None:
    """Ensure admins get their extra commands even before they send /start."""
    for chat_id in admin_ids:
        await sync_chat(bot, chat_id, is_admin=True)
