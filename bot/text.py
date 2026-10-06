"""Pure presentation helpers: option numbering, question rendering, name masking.

Kept free of aiogram and database imports so they can be unit-tested directly.
Question cards are Telegram HTML, so every piece of stored text goes through
`esc()` before it is placed inside markup.
"""
from __future__ import annotations

import html
import json
import random
import re
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
           header: str | None = None,
           footer: str | None = None) -> tuple[str, int]:
    """Return (HTML body, option count) for a question row.

    With `chosen` set the card is shown answered: ✅ on the right option and ❌
    on a wrong pick, so the verdict line never has to repeat a long option.
    `footer` sits under the options, which is where the Notes pointer goes.
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
    body = f"{top}\n\n{esc(question['text'])}\n\n{gap.join(lines)}"
    if footer:
        body = f"{body}\n\n{footer}"
    return body, len(opts)


def notes_line(code: str) -> str:
    """The pointer under a question to the sheets covering its topic.

    Telegram turns a `/command` in message text into a tappable entity, so this is
    a real affordance rather than a caption: tapping it sends `/notes 03`, which
    `handlers.notes` already serves. The code rather than the topic name, because
    it is short enough to sit on one line and unambiguous within a level.
    """
    return f"📘 Notes for this topic: /notes {code}"


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


# Telegram's HTML subset has no void elements, so every tag is either an opener
# or the closer that matches it and a flat stack says exactly what is open at a
# given point. Attributes are kept whole because reopening <a href="..."> in the
# next part means reproducing the opener verbatim.
_TAG_RE = re.compile(r"<(?P<slash>/?)(?P<name>[A-Za-z][A-Za-z0-9-]*)[^<>]*>")
# Named, decimal and hex character references. `esc()` emits "&amp;" and "&lt;",
# and a cut through one leaves "&a" in one part and "mp;" in the next.
_ENTITY_RE = re.compile(
    r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{0,31});")

# A unit of text a cut may fall either side of but never inside.
# (start, end, tag name or "" for plain text, True if it is a closing tag)
_Piece = tuple[int, int, str, bool]


def _pieces(text: str) -> list[_Piece]:
    """`text` as the smallest units a cut may fall between.

    A tag and a character reference are each one unit. Telegram rejects a message
    that ends mid-tag or mid-entity outright, and `sender._send_once` logs the
    rejection and returns, so the student is left with silence rather than a
    mangled card.
    """
    pieces: list[_Piece] = []
    index = 0
    while index < len(text):
        match = None
        if text[index] == "<":
            match = _TAG_RE.match(text, index)
            if match:
                pieces.append((index, match.end(), match["name"].lower(),
                               bool(match["slash"])))
        elif text[index] == "&":
            match = _ENTITY_RE.match(text, index)
            if match:
                pieces.append((index, match.end(), "", False))
        if match:
            index = match.end()
        else:
            pieces.append((index, index + 1, "", False))
            index += 1
    return pieces


def _apply(stack: list[tuple[str, str]], piece: _Piece,
           text: str) -> list[tuple[str, str]]:
    """The open-tag stack after `piece`, as (name, the opener verbatim) pairs."""
    start, end, name, closing = piece
    if not name:
        return stack
    if closing:
        # A closer with no opener is the caller's problem, not something to
        # invent an opener for: leave the stack as it is.
        return stack[:-1] if stack and stack[-1][0] == name else stack
    return [*stack, (name, text[start:end])]


def _closers(stack: list[tuple[str, str]]) -> str:
    """Close what is open, innermost first."""
    return "".join(f"</{name}>" for name, _opener in reversed(stack))


def _boundary(text: str, pieces: list[_Piece], start: int, cut: int,
              floor: int) -> int:
    """The nicest piece index to cut before: after a blank line, else after a
    line break, else after a space, else the hard cut at `cut`.

    `floor` is the shortest body worth taking. It both keeps a single giant token
    splitting at the limit and bounds what preferring a paragraph break can cost:
    a part is never shortened past half the limit to find one.
    """
    paragraph = line = word = None
    origin = pieces[start][0]
    for index in range(cut, start, -1):
        previous = pieces[index - 1]
        if previous[1] - origin <= floor:
            break
        if text[previous[0]:previous[1]] == "\n":
            if line is None:
                line = index
            blank = (index - 2 >= start
                     and text[pieces[index - 2][0]:pieces[index - 2][1]] == "\n")
            if blank and paragraph is None:
                paragraph = index
        elif text[previous[0]:previous[1]] == " " and word is None:
            word = index
    for candidate in (paragraph, line, word):
        if candidate is not None:
            return candidate
    return cut


def chunks(text: str | None, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into pieces Telegram will accept, each valid HTML on its own.

    Telegram rejects anything over 4096 characters outright rather than
    truncating, so the old `msg[:4096]` slicing silently dropped the tail of long
    notes. It also rejects *both* halves of a naive split, because one is left
    holding "<b>" and the other "</b>": a tag still open at the cut is therefore
    closed at the end of the part and reopened at the start of the next, and the
    room that costs is taken out of the limit rather than discovered afterwards.
    Prefers paragraph, then line, then word boundaries.

    Input that arrives unbalanced is handed back unbalanced. This splits text, it
    does not repair it, and the single-part fast path could not repair anything
    anyway, so repairing only long messages would hide the caller's bug on
    exactly the inputs that are hardest to reproduce.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces = _pieces(text)
    parts: list[str] = []
    start = 0                              # first piece of the part being built
    carried: list[tuple[str, str]] = []    # tags the previous cut left open
    while start < len(pieces):
        prefix = "".join(opener for _name, opener in carried)
        stack = list(carried)
        used = len(prefix)
        cut = start                        # first piece *after* this part
        while cut < len(pieces):
            after = _apply(stack, pieces[cut], text)
            size = pieces[cut][1] - pieces[cut][0]
            if used + size + len(_closers(after)) > limit:
                break
            used += size
            stack = after
            cut += 1
        if cut == start:
            # The reopened prefix plus one indivisible piece is already over the
            # limit, which needs a limit far smaller than Telegram's. Take the
            # piece anyway: a part that can never grow would loop forever.
            stack = _apply(stack, pieces[start], text)
            cut = start + 1

        end = cut
        if cut < len(pieces):
            end = _boundary(text, pieces, start, cut, limit // 2)
            if end != cut:
                stack = list(carried)
                for index in range(start, end):
                    stack = _apply(stack, pieces[index], text)

        body = text[pieces[start][0]:pieces[end - 1][1]].strip()
        if body:
            tail = _closers(stack) if end < len(pieces) else ""
            parts.append(f"{prefix}{body}{tail}")
        carried = stack if end < len(pieces) else []
        start = end
    return parts
