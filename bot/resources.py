"""The revision-note PDFs under resources/notes, and how a student gets one.

Sheets are **per audience level**, exactly like the question banks, so a
Post-MBBS student is never handed a preclinical sheet:

```
resources/notes/
  preclin/tier_a/   01-06     preclin/tier_b/   B01-B20
  clin/tier_a/      A01-A06   clin/tier_b/      B01-B36
  postmbbs/tier_a/  A01-A15   postmbbs/tier_b/  B01-B65
```

Note that a bare code is only unique *within* a level - `B01` exists in all
three - so every lookup here takes a level. Getting that wrong is how a clinical
student ends up with a preclinical sheet, which is why there is no level-free
accessor left in this module.

Two *kinds* of sheet. The folders and code names are the content team's
("tier_a" / "tier_b"), but students are never shown "Tier A" or "Tier B" - they
see what the sheet actually is:

* **Overview** - one broad sheet per topic.
* **Focused** - a deeper sheet on a single point.

Clinical sheets arrived named `B01_Clinical_Type_B_...`; that shorthand was
stripped when they were imported, because it would otherwise have been shown to
students verbatim. Clinical overview sheets were renamed to the seven clinical
question topics, so the overview sheets and the bank describe the same seven
things (asserted by tests/test_resources.py).

Who gets which sheet is **not** decided here. A student's delivery history lives
in the database (``db.notes_delivered``), so ``/notes`` can walk them through the
overview sheets and then the focused ones, ``/randomnotes`` can avoid repeating
itself, and the fortnightly drop can work through the focused catalogue. This
module only answers "what sheets exist at this level"; the caller passes in what
has already been sent.

That replaced an earlier scheme where six focused sheets were reserved for the
monthly drop and ``/randomnotes`` drew from the other fourteen. Tracking
deliveries properly makes a reserved subset unnecessary: nothing is ever sent
twice, at any level, without needing a curated list per level.

The catalogue is built by **scanning the directory**, not hard-coded, so adding a
note is: drop the PDF in the right level folder, commit it. No code change, no
database row. A test asserts the catalogue matches what is actually on disk, so a
commit that breaks the naming convention fails CI rather than silently hiding a
sheet from students.

Delivery: the files are served straight from the deployed working copy, so
Telegram receives the real document and nothing needs hosting. If a file is
missing locally, or is too big for the Bot API to upload, the caller falls back
to a GitHub link instead - see ``sender.send_note``.
"""
from __future__ import annotations

import random
import re
from collections.abc import Collection
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_LEVEL, LEVELS, REPO_REF, REPO_SLUG

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "resources" / "notes"

#: Telegram refuses bot uploads above 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Student-facing names. Never "Tier A"/"Tier B" - that is internal shorthand.
TIERS: dict[str, str] = {"a": "Overview", "b": "Focused"}

# "01_Development_and_ocular_histology", "A01_Assessment_refraction_and_vision_loss"
_CODE_RE = re.compile(r"^(?P<code>[A-Z]{0,2}\d{1,2})_(?P<topic>.+)$")


@dataclass(frozen=True)
class Note:
    """One note PDF on disk."""

    level: str         # "preclin", "clin" or "postmbbs"
    tier: str          # "a" or "b"
    code: str          # "03", "A01" or "B14"
    topic: str         # "Optics and visual transduction"
    path: Path

    @property
    def label(self) -> str:
        """How the file is listed to a student."""
        return f"{self.code} · {self.topic}"

    @property
    def caption(self) -> str:
        return f"📄 {TIERS[self.tier]} sheet: {self.topic}"

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    @property
    def relpath(self) -> str:
        return self.path.relative_to(ROOT).as_posix()

    @property
    def url(self) -> str:
        """A GitHub page a student can open in a browser, with the level and tier
        folders visible so it maps onto the same names they see in Telegram."""
        return f"https://github.com/{REPO_SLUG}/blob/{REPO_REF}/{self.relpath}"

    @property
    def raw_url(self) -> str:
        """Plain file bytes - the form Telegram itself fetches if we ever have to
        fall back to sending by URL instead of uploading."""
        return (f"https://raw.githubusercontent.com/{REPO_SLUG}/"
                f"{REPO_REF}/{self.relpath}")


def _scan(level: str, tier_code: str) -> list[Note]:
    """Every well-named PDF in one level's tier directory, in code order."""
    directory = NOTES_DIR / level / f"tier_{tier_code}"
    if not directory.is_dir():
        return []
    notes: list[Note] = []
    for path in sorted(directory.glob("*.pdf")):
        match = _CODE_RE.match(path.stem)
        if not match:
            continue          # ignore anything that is not `CODE_Topic.pdf`
        notes.append(Note(level=level,
                          tier=tier_code,
                          code=match.group("code"),
                          topic=match.group("topic").replace("_", " "),
                          path=path))
    return notes


#: level -> tier -> sheets. Built once at import, by scanning the directory.
CATALOGUE: dict[str, dict[str, list[Note]]] = {
    level: {tier_code: _scan(level, tier_code) for tier_code in TIERS}
    for level in LEVELS
}

def _lv(level: str | None) -> str:
    """Levels come from the database, so treat anything unknown as the default
    rather than raising inside a handler."""
    return level if level in CATALOGUE else DEFAULT_LEVEL


def tier(level: str | None, tier_code: str) -> list[Note]:
    """One kind of sheet at one level."""
    return CATALOGUE[_lv(level)].get(tier_code.lower(), [])


def overview(level: str | None) -> list[Note]:
    return tier(level, "a")


def focused(level: str | None) -> list[Note]:
    return tier(level, "b")


def all_for(level: str | None) -> list[Note]:
    return overview(level) + focused(level)


def topics(level: str | None) -> list[str]:
    """The topics the overview sheets cover at this level, in code order."""
    return [note.topic for note in overview(level)]


def _key(text: str) -> str:
    """A comparison key: letters and digits only, lowercased."""
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _words(text: str) -> set[str]:
    """The significant words in a topic name.

    Four characters and up, which drops the joining words ("and", "the", "of")
    without needing a stopword list, and also drops "eye", which is too common
    here to tell two sheets apart.
    """
    return set(re.findall(r"[a-z]{4,}", text.casefold()))


def for_topic(level: str | None, topic: str) -> Note | None:
    """The overview sheet that covers one question topic, or None.

    This is what puts a Notes pointer under every question. An exact match is
    tried first, ignoring case and punctuation.

    Failing that, the sheet titles and the bank's topics are matched on shared
    words. They really are written differently: the preclinical and clinical
    sheets carry the bank's own topic names, but the post-MBBS overview sheets
    are descriptive titles ("Applied ocular anatomy and development") against
    short bank topics ("Anatomy and embryology"). A shared word that appears in
    only one sheet's title is a strong enough signal by itself; otherwise two
    shared words are needed. A topic the sheets genuinely do not cover (the
    post-MBBS bank has questions on pathology and pharmacology, and no sheet for
    either) gets no pointer rather than a wrong one.
    """
    wanted = _key(topic)
    if not wanted:
        return None
    sheets = overview(level)
    for note in sheets:
        if _key(note.topic) == wanted:
            return note

    words = _words(topic)
    if not words:
        return None
    frequency = Counter(word for note in sheets for word in _words(note.topic))

    scored: list[tuple[tuple[int, int, int], Note]] = []
    for index, note in enumerate(sheets):
        shared = words & _words(note.topic)
        if not shared:
            continue
        distinctive = any(frequency[word] == 1 for word in shared)
        if distinctive or len(shared) >= 2:
            # Distinctive first, then the most overlap, then code order.
            scored.append(((1 if distinctive else 0, len(shared), -index), note))
    return max(scored)[1] if scored else None


def unsent(level: str | None, tier_code: str,
           already: Collection[str] = ()) -> list[Note]:
    """Sheets of one kind this student has not been sent, in code order.

    `already` is the set of codes from `db.notes_delivered`. Kept as a plain
    argument rather than a database call so this module stays free of db imports
    and testable without a database.
    """
    seen = {code.lower() for code in already}
    return [note for note in tier(level, tier_code)
            if note.code.lower() not in seen]


def next_unsent(level: str | None, tier_code: str,
                already: Collection[str] = ()) -> Note | None:
    """The next sheet of one kind to hand over, in code order, or None when this
    student has had them all."""
    remaining = unsent(level, tier_code, already)
    return remaining[0] if remaining else None


def random_focused(level: str | None, already: Collection[str] = (),
                   rng: random.Random | None = None) -> Note | None:
    """One focused sheet at random that this student has not been sent.

    Returns None once they have had every focused sheet at their level, which is
    what lets the caller congratulate them instead of repeating one.
    """
    pool = unsent(level, "b", already)
    if not pool:
        return None
    return (rng or random).choice(pool)


def find(level: str | None, code: str) -> Note | None:
    """Look a sheet up by its code within a level, accepting 'B14', 'b14' and
    '14'. Codes repeat across levels, so this never searches outside one."""
    wanted = code.strip().lower()
    if not wanted:
        return None
    pool = all_for(level)
    for note in pool:
        if note.code.lower() == wanted:
            return note
    # A bare number matches whichever code carries it, so "14" still finds "B14".
    # A query that spells out a letter prefix is taken literally: "A01" is a
    # clinical (and post-MBBS) code and must not fall through to the
    # preclinical "01".
    if not wanted.isdigit():
        return None
    loose = wanted.lstrip("0")
    if not loose:
        return None
    for note in pool:
        if re.sub(r"^[a-z]+", "", note.code.lower()).lstrip("0") == loose:
            return note
    return None


def get(level: str | None, tier_code: str, code: str) -> Note | None:
    """Exact lookup inside one level and one kind. Callback data carries both, so
    there is no need to guess which kind a bare code belongs to."""
    wanted = code.strip().lower()
    return next((n for n in tier(level, tier_code) if n.code.lower() == wanted),
                None)
