"""Validate the real preclinical question bank.

This is the test that would have caught the original bot's hardcoded "ABCD"
crash: every question in the bank is rendered through the same code path the
handler uses. Skipped unless TEST_DATABASE_URL is set (CI provides one).

    docker compose up -d db
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/studybot_test \
        python -m pytest tests/test_seed_data.py -v
"""
from __future__ import annotations

import json
import os
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

from bot.text import MAX_OPTIONS, esc, explanation_block, render


def _balanced_telegram_html(body: str) -> bool:
    """True if `body` only uses the tags the bot emits, properly nested."""
    stack: list[str] = []
    ok = True

    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            nonlocal ok
            ok &= tag in ("b", "i") and not attrs
            stack.append(tag)

        def handle_endtag(self, tag):
            nonlocal ok
            ok &= bool(stack) and stack.pop() == tag

    Check(convert_charrefs=True).feed(body)
    return ok and not stack

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the seed-data tests",
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "schema.sql").read_text()
# Loaded in file order, exactly as the setup instructions do.
SEEDS = [(p.name, p.read_text()) for p in sorted((ROOT / "seeds").glob("*.sql"))]
SEED = dict(SEEDS)["01_preclin_mcqs.sql"]

TABLES = ("tournament_answers", "tournament_points", "attempts", "tournaments",
          "notes", "questions", "users")

EXPECTED_TOPICS = {
    "Development and ocular histology": 21,
    "Orbit and eye movements": 21,
    "Optics and visual transduction": 20,
    "Visual pathways and pupil reflexes": 20,
    "Aqueous humour and glaucoma mechanisms": 16,
    "Retinal and anterior segment pathology": 22,
}


@pytest.fixture(scope="module")
async def seeded():
    import asyncpg

    from bot.db import close, init

    dsn = os.environ["TEST_DATABASE_URL"]
    admin = await asyncpg.connect(dsn)
    for table in TABLES:
        await admin.execute(f"drop table if exists {table} cascade")
    await admin.execute(SCHEMA)
    for _, sql in SEEDS:
        await admin.execute(sql)
    await admin.close()

    await init(dsn)
    yield
    await close()


@pytest.fixture(scope="module")
def bank(seeded):
    from bot import db
    return db


async def test_bank_is_complete(bank):
    rows = await bank.pool.fetch("select * from questions order by id")
    assert len(rows) == 120
    assert {r["level"] for r in rows} == {"preclin"}


async def test_every_seed_file_loads_and_only_preclinical_has_content(bank):
    """The Clinical and Post-MBBS seeds are placeholders that load cleanly and
    add nothing — so loading every seed in order is always safe, and adding
    those banks later needs no change to the loader."""
    levels = await bank.levels_with_questions()
    assert levels == ["preclin"]

    counts = await bank.pool.fetch(
        "select level, count(*) as n from questions group by level")
    assert {r["level"]: r["n"] for r in counts} == {"preclin": 120}


async def test_topic_distribution_matches_the_source_document(bank):
    rows = await bank.pool.fetch(
        "select topic, count(*) as n from questions group by topic")
    counts = {r["topic"]: r["n"] for r in rows}
    assert counts == EXPECTED_TOPICS


async def test_overview_sheets_cover_the_same_topics_as_the_bank(bank):
    """The six overview sheets and the six question topics are the same six
    things. Renaming one without the other fails here, not in front of students."""
    from bot import resources

    rows = await bank.pool.fetch(
        "select distinct topic from questions where level = 'preclin'")
    assert {r["topic"] for r in rows} == set(resources.TIER_A_TOPICS)


async def test_every_question_is_renderable(bank):
    """The whole bank goes through the handler's render path. 118 questions have
    five options, which the original `LETTERS = "ABCD"` implementation could not
    handle at all."""
    rows = await bank.pool.fetch("select * from questions order by id")
    sizes = Counter()
    for row in rows:
        body, count = render(row)
        sizes[count] += 1
        assert body.startswith(f"👁 <b>{esc(row['topic'])}</b>")
        assert esc(row["text"]) in body
        assert count <= MAX_OPTIONS
        # Telegram rejects the whole message if the HTML is malformed, so check
        # both the fresh card and the answered one with its explanation.
        answered, _ = render(row, chosen=0)
        for html_body in (body, f"{answered}\n\n{explanation_block(row['explanation'])}"):
            assert _balanced_telegram_html(html_body), row["id"]

    assert sizes[5] == 118
    assert sizes[4] == 2


async def test_correct_index_matches_a_real_option_and_the_explanation_exists(bank):
    rows = await bank.pool.fetch("select * from questions order by id")
    for row in rows:
        options = json.loads(row["options"])
        assert options[row["correct_idx"]]
        assert row["explanation"] and len(row["explanation"]) > 80, f"Q{row['id']}"
        # The written explanation is what the 💡 button serves, so the LLM is
        # never called for this bank.
        assert row["tag"], f"Q{row['id']} lost its question-type tag"


async def test_known_question_is_intact(bank):
    """A content-level check: if the bank is ever regenerated and the answer
    letters shift, this fails loudly instead of shipping wrong answers."""
    row = await bank.pool.fetchrow(
        "select * from questions where level = 'preclin' order by id limit 1")
    assert row["topic"] == "Development and ocular histology"
    assert row["text"].startswith("During examination of a newborn, Dara")
    options = json.loads(row["options"])
    assert len(options) == 5
    assert row["correct_idx"] == 1  # option b)
    assert options[1].startswith("Failure of the embryonic optic fissure")


async def test_a_preclinical_set_is_served_from_the_real_bank(bank):
    """The sets are built from the loaded bank, at the student's level only."""
    await bank.upsert_user(1, None)
    current = await bank.quiz_set(1, "preclin")

    assert current is not None
    assert current["number"] == 1
    assert current["size"] == bank.SET_SIZE
    assert {q["level"] for q in current["questions"]} == {"preclin"}

    # No clinical or post-MBBS content exists yet, and the bot must cope.
    assert await bank.quiz_set(1, "clin") is None
    assert await bank.quiz_set(1, "postmbbs") is None


async def test_the_bank_covers_all_six_topics(bank):
    assert await bank.topics("preclin") == sorted(EXPECTED_TOPICS)


async def test_seed_refuses_to_load_twice(bank):
    """The guard in the seed file, so a fumbled re-run cannot duplicate the bank."""
    import asyncpg

    with pytest.raises(asyncpg.exceptions.RaiseError) as excinfo:
        await bank.pool.execute(SEED)
    assert "already present" in str(excinfo.value)

    remaining = await bank.pool.fetchval("select count(*) from questions")
    assert remaining == 120
