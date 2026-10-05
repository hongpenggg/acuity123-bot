"""The note catalogue must match what is actually on disk.

The catalogue is built by scanning `resources/notes`, so these tests are what stop
a commit that renames or drops a file from silently hiding a sheet from students.
"""
from bot import resources

EXPECTED_TOPICS = {
    "Development and ocular histology",
    "Orbit and eye movements",
    "Optics and visual transduction",
    "Visual pathways and pupil reflexes",
    "Aqueous humour and glaucoma mechanisms",
    "Retinal and anterior segment pathology",
}


def test_both_kinds_of_sheet_are_present():
    assert len(resources.TIER_A) == 6
    assert len(resources.TIER_B) == 20
    assert len(resources.ALL) == 26


def test_every_listed_file_exists_and_is_really_a_pdf():
    for note in resources.ALL:
        assert note.path.exists(), note.relpath
        assert note.path.suffix == ".pdf"
        assert note.size > 1000, note.relpath
        assert note.path.read_bytes()[:5] == b"%PDF-", note.relpath


def test_codes_are_unique_and_in_order():
    codes = [note.code for note in resources.ALL]
    assert len(codes) == len(set(codes))
    assert [n.code for n in resources.TIER_A] == [f"{i:02d}" for i in range(1, 7)]
    assert [n.code for n in resources.TIER_B] == [f"B{i:02d}" for i in range(1, 21)]


def test_overview_sheets_cover_exactly_the_question_bank_topics():
    """The overview sheets and the 120 questions are built around the same six
    topics. Renaming one without the other fails here rather than in front of
    students."""
    assert set(resources.TIER_A_TOPICS) == EXPECTED_TOPICS


def test_topics_are_readable():
    for note in resources.ALL:
        assert "_" not in note.topic, note.relpath
        assert note.topic == note.topic.strip()
        assert note.topic[0].isupper(), note.relpath


def test_students_never_see_the_internal_tier_names():
    """The content team's folders are tier_a/tier_b, but that is internal
    shorthand - students are told what the sheet *is*."""
    assert resources.TIERS == {"a": "Overview", "b": "Focused"}
    for note in resources.ALL:
        assert "Tier" not in note.caption
        assert "Tier" not in note.label


def test_links_point_at_the_repo():
    note = resources.TIER_A[0]
    assert note.relpath.startswith("resources/notes/tier_a/")
    assert "github.com/" in note.url and note.relpath in note.url
    assert note.raw_url.startswith("https://raw.githubusercontent.com/")
    assert note.relpath in note.raw_url


def test_lookup_by_code():
    assert resources.find("b14").code == "B14"
    assert resources.find("B14").code == "B14"
    assert resources.find("14").code == "B14"
    assert resources.find("03").code == "03"
    assert resources.find("") is None
    assert resources.find("nope") is None


def test_exact_lookup_within_a_tier():
    assert resources.get("b", "B07").topic == "Horizontal gaze VI and MLF"
    assert resources.get("a", "03").topic == "Optics and visual transduction"
    assert resources.get("a", "B07") is None      # right code, wrong kind
