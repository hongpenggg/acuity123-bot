"""Pure presentation helpers: option numbering, question rendering, name masking.

Kept free of aiogram and database imports so they can be unit-tested directly.
Question cards are Telegram HTML, so every piece of stored text goes through
`esc()` before it is placed inside markup.
"""
from __future__ import annotations

import html
import json
import random
from typing import Any

# Keep in step with the `jsonb_array_length(options) between 2 and ...` CHECK in
# schema.sql. The old code hardcoded "ABCD", so any 5-option question raised
# IndexError inside the handler and the bot appeared to do nothing.
MAX_OPTIONS = 6

TELEGRAM_LIMIT = 4096


def letter(index: int) -> str:
    """0 -> '1', 1 -> '2' ... up to MAX_OPTIONS.

    Options are numbered rather than lettered, matching how the society writes
    its questions. Everything downstream - the rendered list, the answer buttons
    and the "answer was X" verdict - goes through here, so the button label and
    the text can never disagree.
    """
    if not 0 <= index < MAX_OPTIONS:
        raise ValueError(f"option index {index} out of range (0..{MAX_OPTIONS - 1})")
    return str(index + 1)


def parse_options(raw: Any) -> list[str]:
    """`questions.options` is jsonb. asyncpg hands jsonb back as `str` unless a
    codec is registered, so accept both forms."""
    if isinstance(raw, (str, bytes, bytearray)):
        raw = json.loads(raw)
    opts = list(raw)
    if not 2 <= len(opts) <= MAX_OPTIONS:
        raise ValueError(f"question has {len(opts)} options; expected 2..{MAX_OPTIONS}")
    return opts


def esc(text: Any) -> str:
    """Escape stored text for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


# Options longer than this get a blank line between them, so a card full of
# two-line answers doesn't read as one wall of text.
_SPACED_OPTION_LEN = 40

WEEKLY_HEADER = "📅 <b>Question of the week</b>"

_RIGHT = ("Correct!", "Nice one!", "Spot on!")
_WRONG = ("Not quite.", "Not this one.")
STREAK_FROM = 3


def render(question, *, chosen: int | None = None,
           header: str | None = None) -> tuple[str, int]:
    """Return (HTML body, option count) for a question row.

    With `chosen` set the card is shown answered: ✅ on the right option and ❌
    on a wrong pick, so the verdict line never has to repeat a long option.
    """
    opts = parse_options(question["options"])
    correct = question["correct_idx"] if chosen is not None else None

    lines = []
    for i, option in enumerate(opts):
        mark = ""
        if chosen is not None:
            if i == correct:
                mark = "✅ "
            elif i == chosen:
                mark = "❌ "
        lines.append(f"{mark}<b>{letter(i)}.</b> {esc(option)}")
    gap = "\n\n" if max(len(o) for o in opts) > _SPACED_OPTION_LEN else "\n"

    top = f"👁 <b>{esc(question['topic'])}</b>"
    if header:
        top = f"{header}\n{top}"
    return f"{top}\n\n{esc(question['text'])}\n\n{gap.join(lines)}", len(opts)


def verdict(correct: bool, answer_idx: int, streak: int = 0,
            rng: random.Random | None = None) -> str:
    """The line under an answered card. Short on purpose: the options above
    already show which one was right."""
    pick = (rng or random).choice
    if correct:
        line = f"✅ <b>{pick(_RIGHT)}</b>"
        if streak >= STREAK_FROM:
            line += f"\n🔥 {streak} in a row"
        return line
    return f"❌ <b>{pick(_WRONG)}</b> The answer is <b>{letter(answer_idx)}</b>."


EXPLANATION_HEADING = "💡 <b>Why</b>"


def explanation_block(text: str) -> str:
    return f"{EXPLANATION_HEADING}\n{esc(text.strip())}"


SHEETS_DONE_HEADING = "🎉 <b>You've completed the notes.</b>"


def sheets_done(level_label: str) -> str:
    """What a student sees once they have been sent every sheet at their level.

    Reached from two places - /notes when the queue is empty, and the fortnightly
    drop when a subscriber has had everything - so the wording lives here rather
    than being written twice.
    """
    return (
        f"{SHEETS_DONE_HEADING}\n"
        f"Good job for completing the Eye {esc(level_label)} syllabus! 👁\n\n"
        "Keep the questions going with /quizme, or /review the ones you got "
        "wrong.\n"
        "Want to read the sheets again? /resources has all of them."
    )


def mask(username: str | None, uid: int) -> str:
    """Spec: show the Telegram username with its last two characters hidden."""
    if not username:
        return f"user{str(uid)[-4:]}"
    if len(username) <= 2:
        return "**"
    return username[:-2] + "**"


def chunks(text: str | None, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into pieces Telegram will accept.

    Telegram rejects anything over 4096 characters outright rather than truncating,
    so the old `msg[:4096]` slicing silently dropped the tail of long notes.
    Prefers paragraph, then line, then word boundaries.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n") + 1, window.rfind("\n") + 1)
        if cut <= limit // 2:
            cut = window.rfind(" ") + 1
        if cut <= limit // 2:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]
