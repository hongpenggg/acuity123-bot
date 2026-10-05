"""Unit tests for the pure presentation helpers."""
import json

import pytest

import random

from bot.text import (MAX_OPTIONS, WEEKLY_HEADER, chunks, explanation_block, letter,
                      letters, mask, parse_options, render, verdict)


def q(options, correct_idx=0, topic="Sample", text="Question?"):
    return {"id": 1, "topic": topic, "text": text,
            "options": json.dumps(options), "correct_idx": correct_idx}


def test_letters_are_positional():
    assert letter(0) == "A"
    assert letter(5) == "F"
    assert letters(4) == "ABCD"


@pytest.mark.parametrize("index", [-1, MAX_OPTIONS, 99])
def test_letter_rejects_out_of_range(index):
    with pytest.raises(ValueError):
        letter(index)


@pytest.mark.parametrize("count", [2, 4, 5, MAX_OPTIONS])
def test_render_handles_every_allowed_option_count(count):
    """Regression: LETTERS was hardcoded "ABCD", so a 5-option question raised
    IndexError inside the handler and the bot silently did nothing."""
    options = [f"option {i}" for i in range(count)]
    body, n = render(q(options, correct_idx=count - 1))
    assert n == count
    assert f"<b>{letter(count - 1)}.</b> option {count - 1}" in body
    assert body.startswith("👁 <b>Sample</b>\n\nQuestion?")


def test_render_escapes_stored_text_for_html():
    body, _ = render(q(["IOP < 21", "a & b"], text="Which is <normal>?", topic="R&D"))
    assert "<b>R&amp;D</b>" in body
    assert "Which is &lt;normal&gt;?" in body
    assert "IOP &lt; 21" in body and "a &amp; b" in body


def test_answered_card_marks_the_options_in_place():
    body, _ = render(q(["w", "x", "y"], correct_idx=2), chosen=0)
    assert "❌ <b>A.</b> w" in body
    assert "<b>B.</b> x" in body and "❌ <b>B.</b>" not in body and "✅ <b>B.</b>" not in body
    assert "✅ <b>C.</b> y" in body

    right, _ = render(q(["w", "x"], correct_idx=1), chosen=1)
    assert "✅ <b>B.</b> x" in right and "❌" not in right


def test_long_options_get_breathing_room():
    short, _ = render(q(["III", "IV", "VI"]))
    assert "\n\n<b>B.</b>" not in short
    long_opts = ["a fairly long option that wraps onto two lines on a phone"] * 3
    spaced, _ = render(q(long_opts))
    assert "\n\n<b>B.</b>" in spaced


def test_weekly_header_sits_above_the_topic():
    body, _ = render(q(["a", "b"]), header=WEEKLY_HEADER)
    assert body.startswith(WEEKLY_HEADER + "\n👁 <b>Sample</b>")


def test_verdict_lines():
    rng = random.Random(0)
    assert verdict(True, 0, rng=rng).startswith("✅ <b>")
    assert "🔥 4 in a row" in verdict(True, 0, streak=4, rng=rng)
    assert "🔥" not in verdict(True, 0, streak=2, rng=rng)
    wrong = verdict(False, 4, rng=rng)
    assert wrong.startswith("❌ <b>") and wrong.endswith("The answer is <b>E</b>.")


def test_explanation_block_is_escaped():
    assert explanation_block(" A < B \n") == "💡 <b>Why</b>\nA &lt; B"


def test_render_accepts_jsonb_returned_as_text():
    """asyncpg hands jsonb back as a str unless a codec is registered."""
    assert parse_options('["a", "b"]') == ["a", "b"]
    assert parse_options(["a", "b"]) == ["a", "b"]


@pytest.mark.parametrize("bad", [[], ["only-one"], [str(i) for i in range(MAX_OPTIONS + 1)]])
def test_render_rejects_malformed_options(bad):
    with pytest.raises(ValueError):
        render(q(bad))


def test_mask_hides_the_last_two_characters():
    """Spec: 'Telegram Username, with last 2 letters hidden for anonymity'."""
    assert mask("hongpenggg", 1) == "hongpeng**"
    assert mask("abc", 1) == "a**"
    assert mask("ab", 1) == "**"
    assert mask(None, 987654321) == "user4321"


def test_chunks_splits_without_losing_text():
    body = "\n\n".join(f"paragraph {i} " + "x" * 100 for i in range(200))
    parts = chunks(body, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    # Nothing is dropped, only whitespace at the joins.
    strip_ws = lambda s: s.replace(" ", "").replace("\n", "")  # noqa: E731
    assert strip_ws("".join(parts)) == strip_ws(body)
    assert len(parts) == len([p for p in parts if p])


def test_chunks_hard_splits_a_single_giant_token():
    parts = chunks("y" * 2500, limit=1000)
    assert [len(p) for p in parts] == [1000, 1000, 500]


def test_chunks_ignores_empty_input():
    assert chunks("") == []
    assert chunks(None) == []
    assert chunks("   ") == []
