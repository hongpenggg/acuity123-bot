"""The note catalogue must match what is actually on disk.

The catalogue is built by scanning `resources/notes`, so these tests are what stop
a commit that renames or drops a file from silently hiding a sheet from students.

Sheets are per level now, and a bare code is only unique *within* a level - `B01`
exists in all three - so the level-collision test below is the one that would
catch a lookup that forgot to scope itself.
"""
import random

import pytest

from bot import resources

LEVELS = ("preclin", "clin", "postmbbs")

#: (overview, focused) counts per level, written out by hand so a dropped or
#: added PDF has to be acknowledged here.
COUNTS = {
    "preclin": (6, 20),
    "clin": (7, 40),
    "postmbbs": (15, 65),
}

#: The first and last code in each level's tier, in scan order.
CODE_RANGE = {
    ("preclin", "a"): ("01", "06"),
    ("preclin", "b"): ("B01", "B20"),
    ("clin", "a"): ("C01", "C07"),
    ("clin", "b"): ("B01", "B40"),
    ("postmbbs", "a"): ("A01", "A15"),
    ("postmbbs", "b"): ("B01", "B65"),
}

#: Every overview topic, per level. For preclin and clin these are exactly the
#: question-bank topics (see test_seed_data.py); the post-MBBS bank has eighteen
#: topics against fifteen overview sheets, so there they are pinned rather than
#: matched.
OVERVIEW_TOPICS = {
    "preclin": {
        "Development and ocular histology",
        "Orbit and eye movements",
        "Optics and visual transduction",
        "Visual pathways and pupil reflexes",
        "Aqueous humour and glaucoma mechanisms",
        "Retinal and anterior segment pathology",
    },
    "clin": {
        "Clinical assessment and vision loss",
        "Red eye cornea and uveitis",
        "Glaucoma",
        "Retinal vascular disease",
        "Macular and vitreoretinal disease",
        "Neuro ophthalmology and orbit",
        "Lens lids and paediatric eye",
    },
    "postmbbs": {
        "Applied ocular anatomy and development",
        "Ocular physiology and molecular function",
        "Optics refraction and instrument physics",
        "Cellular mechanisms of ocular disease",
        "Therapeutics investigations and quantitative evidence",
        "Cornea and ocular surface a layered decision framework",
        "Uveitis phenotype treatment burden and surgical readiness",
        "Glaucoma mechanism surgery and postoperative reasoning",
        "Cataract surgery planning fluidics and complication control",
        "Optics and refractive outcomes from measurement to symptoms",
        "Medical retina and macular decisions",
        "Vitreoretinal surgery and trauma",
        "Advanced neuro ophthalmology",
        "Orbit lids and lacrimal selection",
        "Paediatric ophthalmology and strabismus",
    },
}


def _every_note():
    return [note for level in LEVELS for note in resources.all_for(level)]


@pytest.mark.parametrize("level", LEVELS)
def test_both_kinds_of_sheet_are_present(level):
    overview, focused = COUNTS[level]
    assert len(resources.overview(level)) == overview
    assert len(resources.focused(level)) == focused
    assert len(resources.all_for(level)) == overview + focused


def test_the_whole_catalogue_is_accounted_for():
    assert len(_every_note()) == sum(a + b for a, b in COUNTS.values())


@pytest.mark.parametrize("level", LEVELS)
def test_unsent_skips_what_has_already_been_sent(level):
    """Delivery history is passed in rather than looked up, so this module stays
    free of database imports. It replaced a scheme that reserved six focused
    sheets per level for the monthly drop: tracking deliveries means nothing is
    ever sent twice without needing a curated list."""
    sheets = resources.focused(level)
    already = {n.code for n in sheets[:3]}

    remaining = resources.unsent(level, "b", already)

    assert len(remaining) == len(sheets) - 3
    assert not ({n.code for n in remaining} & already)
    assert resources.unsent(level, "b") == sheets          # nothing sent yet
    assert resources.unsent(level, "b", {n.code for n in sheets}) == []


@pytest.mark.parametrize("level", LEVELS)
def test_next_unsent_walks_the_catalogue_in_code_order(level):
    """`/notes` hands over one sheet at a time, so the order has to be stable and
    has to advance."""
    handed: list[str] = []
    for _ in range(len(resources.overview(level))):
        note = resources.next_unsent(level, "a", handed)
        assert note is not None
        handed.append(note.code)

    assert handed == [n.code for n in resources.overview(level)]
    # and once they have had them all, there is nothing left to hand over
    assert resources.next_unsent(level, "a", handed) is None


def test_codes_are_matched_case_insensitively():
    """Delivery rows come back from Postgres as stored; a case difference must not
    quietly re-send a sheet."""
    sent = {n.code.lower() for n in resources.focused("clin")[:5]}
    assert len(resources.unsent("clin", "b", sent)) == len(resources.focused("clin")) - 5


@pytest.mark.parametrize("level", LEVELS)
def test_random_focused_never_repeats_and_eventually_runs_out(level):
    """Drawing until the level is exhausted must terminate, never repeat, and then
    return None so the caller can say the syllabus is finished."""
    rng = random.Random(0)
    handed: list[str] = []
    total = len(resources.focused(level))

    for _ in range(total):
        note = resources.random_focused(level, handed, rng)
        assert note is not None
        assert note.code not in handed
        handed.append(note.code)

    assert sorted(handed) == sorted(n.code for n in resources.focused(level))
    assert resources.random_focused(level, handed, rng) is None


def test_every_listed_file_exists_and_is_really_a_pdf():
    for note in _every_note():
        assert note.path.exists(), note.relpath
        assert note.path.suffix == ".pdf"
        assert note.size > 1000, note.relpath
        assert note.path.read_bytes()[:5] == b"%PDF-", note.relpath


@pytest.mark.parametrize("level", LEVELS)
def test_codes_are_unique_within_a_level_and_in_scan_order(level):
    codes = [note.code for note in resources.all_for(level)]
    assert len(codes) == len(set(codes)), "a code is repeated at this level"
    for tier_code in ("a", "b"):
        notes = resources.tier(level, tier_code)
        first, last = CODE_RANGE[(level, tier_code)]
        assert notes[0].code == first
        assert notes[-1].code == last
        assert [n.code for n in notes] == sorted(n.code for n in notes)


def test_the_same_code_means_different_sheets_at_different_levels():
    """B01 exists at all three levels. A lookup that forgets its level would hand
    a clinical student a preclinical sheet, which is the bug this guards."""
    found = {level: resources.find(level, "B01") for level in LEVELS}

    assert all(note is not None for note in found.values())
    assert len({note.topic for note in found.values()}) == 3
    for level, note in found.items():
        assert note.level == level
        assert f"/{level}/" in note.relpath


@pytest.mark.parametrize("level", LEVELS)
def test_overview_sheets_cover_the_expected_topics(level):
    assert set(resources.topics(level)) == OVERVIEW_TOPICS[level]


def test_topics_are_readable():
    for note in _every_note():
        assert "_" not in note.topic, note.relpath
        assert note.topic == note.topic.strip()
        assert note.topic[0].isupper(), note.relpath


def test_students_never_see_the_internal_tier_names():
    """The content team's folders are tier_a/tier_b, but that is internal
    shorthand - students are told what the sheet *is*. The clinical sheets
    arrived named "..._Clinical_Type_B_...", so "Type B" is checked too."""
    assert resources.TIERS == {"a": "Overview", "b": "Focused"}
    for note in _every_note():
        for shown in (note.caption, note.label, note.topic):
            assert "Tier" not in shown, note.relpath
            assert "Type B" not in shown, note.relpath


@pytest.mark.parametrize("level", LEVELS)
def test_links_point_at_the_repo_including_the_level(level):
    note = resources.overview(level)[0]
    assert note.relpath.startswith(f"resources/notes/{level}/tier_a/")
    assert "github.com/" in note.url and note.relpath in note.url
    assert note.raw_url.startswith("https://raw.githubusercontent.com/")
    assert note.relpath in note.raw_url


def test_lookup_by_code():
    assert resources.find("preclin", "b14").code == "B14"
    assert resources.find("preclin", "B14").code == "B14"
    assert resources.find("preclin", "14").code == "B14"
    assert resources.find("preclin", "03").code == "03"
    assert resources.find("clin", "c03").code == "C03"
    assert resources.find("postmbbs", "a15").code == "A15"
    assert resources.find("preclin", "") is None
    assert resources.find("preclin", "nope") is None
    # Real codes, but not at this level.
    assert resources.find("preclin", "B65") is None
    assert resources.find("preclin", "C01") is None


def test_exact_lookup_within_a_level_and_tier():
    assert resources.get("preclin", "b", "B07").topic == "Horizontal gaze VI and MLF"
    assert resources.get("preclin", "a", "03").topic == "Optics and visual transduction"
    assert resources.get("clin", "a", "C03").topic == "Glaucoma"
    assert resources.get("preclin", "a", "B07") is None   # right code, wrong kind
    assert resources.get("preclin", "a", "C01") is None   # right kind, wrong level


def test_an_unknown_level_falls_back_rather_than_raising():
    """Levels arrive from the database, so a stale or bad value must not blow up
    inside a handler."""
    assert resources.overview(None) == resources.overview("preclin")
    assert resources.overview("nonsense") == resources.overview("preclin")
    assert resources.find(None, "03") is not None
