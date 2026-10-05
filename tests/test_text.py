"""Unit tests for the pure presentation helpers."""
import json

import pytest

from bot.text import (MAX_OPTIONS, chunks, letter, letters, mask,
                      parse_options, render)


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
    assert f"{letter(count - 1)}. option {count - 1}" in body
    assert body.startswith("[Sample]\nQuestion?")


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
