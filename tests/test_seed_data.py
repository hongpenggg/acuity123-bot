"""Validate the real question banks: preclinical, clinical and post-MBBS.

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

TABLES = ("note_deliveries", "tournament_answers", "tournament_points",
          "attempts", "tournaments", "notes", "questions", "users")

# Each bank's coverage table, written out by hand here rather than imported from
# tools/build_question_seed.py: the generator asserts the same numbers against
# the .docx sources, so a regeneration that quietly changes the split has to get
# past two independent copies of it.
#
# The clinical bank is 152, not the 170 questions in its document: eighteen are
# built around an embedded fundus or slit-lamp photograph and the bot sends
# text-only cards, so SKIP_FIGURE_QUESTIONS holds them back.
TOPICS: dict[str, dict[str, int]] = {
    "preclin": {
        "Development and ocular histology": 21,
        "Orbit and eye movements": 21,
        "Optics and visual transduction": 20,
        "Visual pathways and pupil reflexes": 20,
        "Aqueous humour and glaucoma mechanisms": 16,
        "Retinal and anterior segment pathology": 22,
    },
    "clin": {
        "Assessment refraction and vision loss": 28,
        "Red eye cornea and uveitis": 28,
        "Retina macula and vitreous": 26,
        "Neuro ophthalmology and orbit": 26,
        "Glaucoma": 24,
        "Lens lids lacrimal and paediatric eye": 20,
    },
    "postmbbs": {
        "Physiology and biochemistry": 17,
        "Cornea and ocular surface": 15,
        "Optics and refraction": 14,
        "Medical retina and macular decisions": 13,
        "Optics and refractive surgery": 12,
        "Cataract and lens surgery": 12,
        "Vitreoretinal surgery and trauma": 12,
        "Orbit lids and lacrimal selection": 12,
        "Advanced neuro ophthalmology": 10,
        "Uveitis and inflammatory medicine": 9,
        "Glaucoma": 9,
        "Paediatric ophthalmology and strabismus": 9,
        "Anatomy and embryology": 7,
        "Biostatistics and evidence": 5,
        "Genetics": 4,
        "Microbiology and immunology": 4,
        "Pharmacology": 4,
        "Pathology": 2,
    },
}
SIZES = {level: sum(topics.values()) for level, topics in TOPICS.items()}
TOTAL = sum(SIZES.values())


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
    assert len(rows) == TOTAL
    assert {r["level"] for r in rows} == set(TOPICS)


async def test_every_seed_file_loads_and_each_level_has_its_bank(bank):
    """All three levels carry content now. Each seed guards its own level, so
    loading every file in order stays a single pass and the loader is unchanged
    from when two of the three were empty placeholders."""
    assert await bank.levels_with_questions() == sorted(TOPICS)

    counts = await bank.pool.fetch(
        "select level, count(*) as n from questions group by level")
    assert {r["level"]: r["n"] for r in counts} == SIZES


async def test_topic_distribution_matches_the_source_document(bank):
    rows = await bank.pool.fetch(
        "select level, topic, count(*) as n from questions group by level, topic")
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["level"], {})[row["topic"]] = row["n"]
    assert counts == TOPICS


@pytest.mark.parametrize("level", ["preclin", "clin"])
async def test_overview_sheets_cover_the_same_topics_as_the_bank(bank, level):
    """At these two levels the overview sheets and the question topics are the
    same list. Renaming one without the other fails here, not in front of
    students.

    The clinical sheets arrived prefixed and were renamed to the bank's own topic
    names, and the M3 bank's "Topic: T1 ..." group labels are stripped when the
    seed is generated, which is what keeps this holding for `clin` as well as
    `preclin`.
    """
    from bot import resources

    rows = await bank.pool.fetch(
        "select distinct topic from questions where level = $1", level)
    assert {r["topic"] for r in rows} == set(resources.topics(level))


async def test_post_mbbs_sheets_do_not_line_up_with_its_topics(bank):
    """Deliberately a weaker claim than the test above, because it is the truth:
    the post-MBBS bank has eighteen question topics and fifteen overview sheets,
    organised differently (A05 "Therapeutics investigations and quantitative
    evidence" spans the Pharmacology and Biostatistics topics, for instance).

    Every level still has to have overview sheets, and some of them do match, so
    this pins what is actually there rather than asserting a mapping that does
    not exist. If the content team ever aligns them, fold this into the test
    above and delete it.
    """
    from bot import resources

    rows = await bank.pool.fetch(
        "select distinct topic from questions where level = 'postmbbs'")
    topics = {r["topic"] for r in rows}
    sheets = set(resources.topics("postmbbs"))

    assert len(topics) == 18
    assert len(sheets) == 15
    assert topics != sheets
    # The five that do line up exactly, so a rename on either side is noticed.
    assert topics & sheets == {
        "Medical retina and macular decisions",
        "Vitreoretinal surgery and trauma",
        "Advanced neuro ophthalmology",
        "Orbit lids and lacrimal selection",
        "Paediatric ophthalmology and strabismus",
    }


async def test_every_question_is_renderable(bank):
    """Every bank goes through the handler's render path. 128 questions have five
    options, which the original `LETTERS = "ABCD"` implementation could not handle
    at all, and the FRCOphth bank letters its four A)-D) rather than a)-d)."""
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

    # 118 preclinical and 10 clinical have five options; the rest have four.
    assert sizes[5] == 128
    assert sizes[4] == 265
    assert sum(sizes.values()) == TOTAL


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
    """One content anchor per bank: if a bank is ever regenerated and the answer
    letters shift, this fails loudly instead of shipping wrong answers."""
    first = {}
    for level in TOPICS:
        first[level] = await bank.pool.fetchrow(
            "select * from questions where level = $1 order by id limit 1", level)

    row = first["preclin"]
    assert row["topic"] == "Development and ocular histology"
    assert row["text"].startswith("During examination of a newborn, Dara")
    options = json.loads(row["options"])
    assert len(options) == 5
    assert row["correct_idx"] == 1  # option b)
    assert options[1].startswith("Failure of the embryonic optic fissure")

    row = first["clin"]
    assert row["topic"] == "Assessment refraction and vision loss"
    assert row["text"].startswith("At a six-metre Snellen chart, Zay reads")
    options = json.loads(row["options"])
    assert len(options) == 4
    assert row["correct_idx"] == 2  # option C)
    assert options[2].startswith("6/15+2")

    # The FRCOphth documents letter their options A)-D). The generator lowercases
    # the answer letter before indexing, so C) has to land on index 2 - getting
    # that wrong would mis-key all 170 questions at once.
    row = first["postmbbs"]
    assert row["topic"] == "Optics and refraction"
    assert row["text"].startswith("Two monochromatic sources each emit 1 mW")
    options = json.loads(row["options"])
    assert len(options) == 4
    assert row["correct_idx"] == 2  # option C)
    assert options[2].startswith("The first gives 0.683 lumen")


async def test_a_set_is_served_from_every_real_bank(bank):
    """Sets are no longer fixed blocks of five in id order, so this no longer
    asserts *which* five: it asserts that every real bank can fill a set, that
    the selector stays inside the student's own level, and that it spreads the
    five across topics rather than walking one. Pre-clinical is the tight case
    with six topics to five picks."""
    await bank.upsert_user(1, None)
    for level in TOPICS:
        current = await bank.current_set(1, level)
        assert current is not None, level
        assert current["number"] == 1
        assert current["size"] == bank.SET_SIZE
        assert current["answered_in_set"] == 0

        served = []
        for _ in range(bank.SET_SIZE):
            question = await bank.pick_question(1, level)
            assert question is not None, level
            assert question["level"] == level
            served.append(question)
            # The question id doubles as the message id: unique per user, which
            # is all the double-tap index needs.
            await bank.record_attempt(1, question, 0, True, "practice",
                                      question["id"])

        assert len({q["id"] for q in served}) == bank.SET_SIZE, level
        assert len({q["topic"] for q in served}) == bank.SET_SIZE, (
            level, [q["topic"] for q in served])
        assert (await bank.current_set(1, level))["number"] == 2, level


async def test_every_bank_covers_its_own_topics(bank):
    for level, topics in TOPICS.items():
        assert await bank.topics(level) == sorted(topics), level


async def test_seed_refuses_to_load_twice(bank):
    """The guard in each seed file, so a fumbled re-run cannot duplicate a bank."""
    import asyncpg

    for name, sql in SEEDS:
        with pytest.raises(asyncpg.exceptions.RaiseError) as excinfo:
            await bank.pool.execute(sql)
        assert "already present" in str(excinfo.value), name

    remaining = await bank.pool.fetchval("select count(*) from questions")
    assert remaining == TOTAL
