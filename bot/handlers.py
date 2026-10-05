import json, time, logging, asyncio
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from . import db, llm
from .config import ADMIN_IDS

router = Router()
log = logging.getLogger(__name__)
LETTERS = "ABCD"
_last_explain: dict[int, float] = {}

def render(q):
    opts = json.loads(q["options"])
    body = "\n".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(opts))
    return f"[{q['topic']}]\n{q['text']}\n\n{body}", len(opts)

def question_kb(qid, n, mode):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=LETTERS[i], callback_data=f"a:{qid}:{i}:{mode}")
        for i in range(n)]])

async def safe_send(bot: Bot, uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except TelegramForbiddenError:
        await db.deactivate(uid)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except Exception:
        log.exception("send failed to %s", uid)
    return False

async def send_question(bot, uid, q, mode):
    if not q:
        return await safe_send(bot, uid, "No questions available yet.")
    text, n = render(q)
    return await safe_send(bot, uid, text, reply_markup=question_kb(q["id"], n, mode))

def mask(username, uid):
    if not username:
        return f"user{str(uid)[-4:]}"
    return "**" if len(username) <= 2 else username[:-2] + "**"

@router.message(Command("start"))
async def start(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await m.answer(
        "Welcome!\n/practice - adaptive questions\n/subscribe - weekly question\n"
        "/unsubscribe\n/tournament - join/leave the tournament\n/leaderboard\n"
        "/notes - cheat sheets\n/notes_sub - fortnightly Tier A notes")

@router.message(Command("practice"))
async def practice(m: Message, bot: Bot):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await send_question(bot, m.from_user.id, await db.pick_question(m.from_user.id), "practice")

@router.callback_query(F.data == "next")
async def nxt(c: CallbackQuery, bot: Bot):
    await c.answer()
    await send_question(bot, c.from_user.id, await db.pick_question(c.from_user.id), "practice")

@router.callback_query(F.data.startswith("a:"))
async def on_answer(c: CallbackQuery):
    _, qid, idx, mode = c.data.split(":")
    qid, idx = int(qid), int(idx)
    q = await db.get_question(qid)
    correct = idx == q["correct_idx"]
    if not await db.record_attempt(c.from_user.id, q, idx, correct, mode, c.message.message_id):
        return await c.answer("Already answered")
    if correct and mode == "practice":
        await db.award_point(c.from_user.id)
    opts = json.loads(q["options"])
    verdict = "✅ Correct!" if correct else \
        f"❌ Wrong. Answer: {LETTERS[q['correct_idx']]}. {opts[q['correct_idx']]}"
    row = [InlineKeyboardButton(text="💡 Explain", callback_data=f"e:{qid}")]
    if mode == "practice":
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data="next"))
    await c.message.edit_text(f"{c.message.text}\n\n{verdict}",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[row]))
    await c.answer()

@router.callback_query(F.data.startswith("e:"))
async def on_explain(c: CallbackQuery):
    now = time.time()
    if now - _last_explain.get(c.from_user.id, 0) < 10:
        return await c.answer("Slow down a little", show_alert=True)
    _last_explain[c.from_user.id] = now
    await c.answer("Thinking...")
    q = await db.get_question(int(c.data.split(":")[1]))
    try:
        text = await llm.explain(q)
    except Exception:
        log.exception("llm failed")
        text = "Sorry, explanations are unavailable right now."
    await c.message.answer(f"💡 {text}")

@router.message(Command("subscribe"))
async def sub(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "weekly_sub", True)
    await m.answer("Subscribed to the weekly question.")

@router.message(Command("unsubscribe"))
async def unsub(m: Message):
    await db.set_flag(m.from_user.id, "weekly_sub", False)
    await m.answer("Unsubscribed.")

@router.message(Command("tournament"))
async def tournament(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    t = await db.active_tournament()
    if not t:
        return await m.answer("No tournament is running right now.")
    rows = await db.leaderboard() or []
    if any(r["user_id"] == m.from_user.id for r in rows):
        await db.leave_tournament(m.from_user.id)
        return await m.answer("You left the tournament.")
    await db.join_tournament(m.from_user.id)
    await m.answer(f"Joined! Ends {t['ends_at']:%d %b %Y}. Correct /practice answers earn points.")

@router.message(Command("leaderboard"))
async def leaderboard(m: Message):
    rows = await db.leaderboard()
    if rows is None:
        return await m.answer("No tournament is running.")
    top = "\n".join(f"{r['rk']}. @{mask(r['username'], r['user_id'])} - {r['points']} pts"
                    for r in rows[:3]) or "No participants yet."
    me = next((r for r in rows if r["user_id"] == m.from_user.id), None)
    mine = f"\n\nYou: rank {me['rk']}, {me['points']} pts" if me else "\n\nYou haven't joined (/tournament)."
    await m.answer("🏆 Top 3\n" + top + mine)

@router.message(Command("admin_tournament_start"))
async def admin_start(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    if await db.active_tournament():
        return await m.answer("One is already running.")
    tid = await db.start_tournament(14)
    await m.answer(f"Tournament {tid} started for 14 days.")

@router.message(Command("notes"))
async def notes(m: Message, command: CommandObject):
    if not command.args:
        ts = await db.note_topics("B")
        return await m.answer("Topics:\n" + "\n".join(f"- {t}" for t in ts) +
                              "\n\nUse /notes <topic>" if ts else "No notes yet.")
    rows = await db.get_notes(command.args.strip(), "B")
    if not rows:
        return await m.answer("Topic not found. Try /notes")
    for r in rows:
        await m.answer(f"📝 {r['title']}\n\n{r['body']}"[:4096])

@router.message(Command("notes_sub"))
async def notes_sub(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await db.set_flag(m.from_user.id, "notes_sub", True)
    await m.answer("You'll get Tier A cheat sheets fortnightly. /notes_unsub to stop.")

@router.message(Command("notes_unsub"))
async def notes_unsub(m: Message):
    await db.set_flag(m.from_user.id, "notes_sub", False)
    await m.answer("Unsubscribed from notes.")