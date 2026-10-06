"""Unit tests for the pure presentation helpers."""
import json
import random
import re

import pytest

from bot.text import (MAX_OPTIONS, WEEKLY_HEADER, chunks, explanation_block, letter,
                      mask, parse_options, render, verdict)


def q(options, correct_idx=0, topic="Sample", text="Question?"):
    return {"id": 1, "topic": topic, "text": text,
            "options": json.dumps(options), "correct_idx": correct_idx}


def test_options_are_numbered_from_one():
    """Numbered, not lettered: the society writes its questions as 1..5, and the
    answer buttons and the rendered card share this one function."""
    assert letter(0) == "1"
    assert letter(4) == "5"
    assert letter(MAX_OPTIONS - 1) == str(MAX_OPTIONS)


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
    assert "❌ <b>1.</b> w" in body
    assert "<b>2.</b> x" in body and "❌ <b>2.</b>" not in body and "✅ <b>2.</b>" not in body
    assert "✅ <b>3.</b> y" in body

    right, _ = render(q(["w", "x"], correct_idx=1), chosen=1)
    assert "✅ <b>2.</b> x" in right and "❌" not in right


def test_long_options_get_breathing_room():
    short, _ = render(q(["III", "IV", "VI"]))
    assert "\n\n<b>2.</b>" not in short
    long_opts = ["a fairly long option that wraps onto two lines on a phone"] * 3
    spaced, _ = render(q(long_opts))
    assert "\n\n<b>2.</b>" in spaced


def test_weekly_header_sits_above_the_topic():
    body, _ = render(q(["a", "b"]), header=WEEKLY_HEADER)
    assert body.startswith(WEEKLY_HEADER + "\n👁 <b>Sample</b>")


def test_verdict_lines():
    rng = random.Random(0)
    assert verdict(True, 0, rng=rng).startswith("✅ <b>")
    assert "🔥 4 in a row" in verdict(True, 0, streak=4, rng=rng)
    assert "🔥" not in verdict(True, 0, streak=2, rng=rng)
    wrong = verdict(False, 4, rng=rng)
    assert wrong.startswith("❌ <b>") and wrong.endswith("The answer is <b>5</b>.")


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


# ---------------------------------------------------- splitting HTML safely


def _visible(text):
    """What the student actually reads: tags and whitespace stripped out."""
    return re.sub(r"<[^>]*>", "", text).replace(" ", "").replace("\n", "")


def test_chunks_keeps_a_tag_balanced_across_the_cut():
    """Telegram rejects *both* halves of a split that leaves "<b>" in one part
    and "</b>" in the other, and `sender._send_once` logs the rejection and
    returns, so the student is left with silence rather than a mangled card."""
    body = "<b>" + "word " * 300 + "</b>"
    parts = chunks(body, limit=200)

    assert len(parts) > 1
    for part in parts:
        assert part.count("<b>") == part.count("</b>") == 1, part
    assert parts[0].endswith("</b>") and parts[1].startswith("<b>")
    assert _visible("".join(parts)) == _visible(body), "text went missing"


def test_chunks_reopens_nested_tags_in_the_right_order():
    parts = chunks("<b><i>" + "z" * 500 + "</i></b>", limit=120)

    assert len(parts) > 1
    assert parts[0].startswith("<b><i>") and parts[0].endswith("</i></b>")
    assert parts[1].startswith("<b><i>")


def test_chunks_reopens_a_tag_with_its_attributes():
    """Reopening "<a>" without the href would strip the link from everything
    after the cut."""
    opener = '<a href="https://example.test/sheet">'
    parts = chunks("q" * 80 + opener + "link text " * 20 + "</a>", limit=120)

    assert len(parts) > 1
    assert parts[1].startswith(opener)


@pytest.mark.parametrize("limit", range(60, 140, 7))
def test_chunks_never_cuts_through_a_tag(limit):
    for part in chunks("a" * 99 + "<b>bold</b>" + "c" * 200, limit=limit):
        assert re.search(r"<[^>]*$", part) is None, part[-12:]
        assert not re.match(r"^[a-z]*>", part), part[:12]


@pytest.mark.parametrize("limit", range(40, 130, 7))
def test_chunks_never_cuts_through_a_character_reference(limit):
    """`esc()` emits "&amp;" and "&lt;". A cut inside one leaves "&a" in one part
    and "mp;" in the next, and Telegram refuses both."""
    for part in chunks("x" * 98 + "&amp;" + "y" * 200, limit=limit):
        assert re.search(r"&[^;\s]*$", part) is None, part[-8:]
        assert not re.match(r"^[a-z]+;", part), part[:8]


@pytest.mark.parametrize("limit", [80, 200, 1000])
def test_chunks_pays_for_its_own_tags_out_of_the_limit(limit):
    """The reopened prefix and the closers come out of the budget, rather than
    being discovered by Telegram after the fact."""
    body = "<b>" + " ".join("w" * 9 for _ in range(400)) + "</b>"
    assert all(len(p) <= limit for p in chunks(body, limit=limit))


def test_chunks_leaves_unbalanced_input_unbalanced():
    """This splits text, it does not repair it. The single-part fast path cannot
    repair anything either, so repairing only long messages would hide a
    caller's bug on exactly the inputs hardest to reproduce."""
    parts = chunks("<b>" + "k" * 300, limit=100)

    assert len(parts) > 1
    assert "</b>" not in parts[-1], "the input's own tag was closed for it"


def test_chunks_prefers_a_paragraph_break_to_a_mid_sentence_cut():
    """Two of these paragraphs do not fit in 200 characters and one does, so
    every part should be exactly one paragraph."""
    paragraph = "sentence " * 12
    parts = chunks("\n\n".join(paragraph for _ in range(6)), limit=200)

    assert parts == [paragraph.strip()] * 6
