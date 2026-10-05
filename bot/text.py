"""Pure presentation helpers: option letters, question rendering, name masking.

Kept free of aiogram and database imports so they can be unit-tested directly.
"""
from __future__ import annotations

import json
from typing import Any

# Keep in step with the `jsonb_array_length(options) between 2 and ...` CHECK in
# schema.sql. The old code hardcoded "ABCD", so any 5-option question raised
# IndexError inside the handler and the bot appeared to do nothing.
MAX_OPTIONS = 6

TELEGRAM_LIMIT = 4096


def letter(index: int) -> str:
    """0 -> 'A', 1 -> 'B' ... up to MAX_OPTIONS."""
    if not 0 <= index < MAX_OPTIONS:
        raise ValueError(f"option index {index} out of range (0..{MAX_OPTIONS - 1})")
    return chr(ord("A") + index)


def letters(count: int) -> str:
    return "".join(letter(i) for i in range(count))


def parse_options(raw: Any) -> list[str]:
    """`questions.options` is jsonb. asyncpg hands jsonb back as `str` unless a
    codec is registered, so accept both forms."""
    if isinstance(raw, (str, bytes, bytearray)):
        raw = json.loads(raw)
    opts = list(raw)
    if not 2 <= len(opts) <= MAX_OPTIONS:
        raise ValueError(f"question has {len(opts)} options; expected 2..{MAX_OPTIONS}")
    return opts


def render(question) -> tuple[str, int]:
    """Return (message body, option count) for a question row."""
    opts = parse_options(question["options"])
    body = "\n".join(f"{letter(i)}. {o}" for i, o in enumerate(opts))
    return f"[{question['topic']}]\n{question['text']}\n\n{body}", len(opts)


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
