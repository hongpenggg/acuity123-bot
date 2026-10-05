"""The revision-note PDFs under resources/notes, and how a student gets one.

Two *kinds* of sheet. The folders and code names are the content team's
("tier_a" / "tier_b", ``01``-``06`` / ``B01``-``B20``), but students are never
shown "Tier A" or "Tier B" - they see what the sheet actually is:

* **Overview** - one broad sheet per topic (``01``-``06``). The fortnightly push.
* **Focused** - a deeper sheet on a single point (``B01``-``B20``), on demand.

The catalogue is built by **scanning the directory**, not hard-coded, so adding a
note is: drop the PDF in, commit it. No code change, no database row. A test
asserts the catalogue matches what is actually on disk, so a commit that breaks
the naming convention fails CI rather than silently hiding a sheet from students.

Delivery: the files are served straight from the deployed working copy, so
Telegram receives the real document and nothing needs hosting. If a file is
missing locally, or is too big for the Bot API to upload, the caller falls back
to a GitHub link instead - see ``sender.send_note``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_REF, REPO_SLUG

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "resources" / "notes"

#: Telegram refuses bot uploads above 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Student-facing names. Never "Tier A"/"Tier B" - that is internal shorthand.
TIERS: dict[str, str] = {"a": "Overview", "b": "Focused"}

# "01_Development_and_ocular_histology" / "B14_Saccades_and_the_VOR"
_CODE_RE = re.compile(r"^(?P<code>[A-Z]{0,2}\d{1,2})_(?P<topic>.+)$")


@dataclass(frozen=True)
class Note:
    """One note PDF on disk."""

    tier: str          # "a" or "b"
    code: str          # "03" or "B14"
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
        """A GitHub page a student can open in a browser, with the tier folder
        visible so it maps onto the same names they see in Telegram."""
        return f"https://github.com/{REPO_SLUG}/blob/{REPO_REF}/{self.relpath}"

    @property
    def raw_url(self) -> str:
        """Plain file bytes - the form Telegram itself fetches if we ever have to
        fall back to sending by URL instead of uploading."""
        return (f"https://raw.githubusercontent.com/{REPO_SLUG}/"
                f"{REPO_REF}/{self.relpath}")


def _scan(tier: str) -> list[Note]:
    """Every well-named PDF in a tier directory, in code order."""
    directory = NOTES_DIR / f"tier_{tier}"
    if not directory.is_dir():
        return []
    notes: list[Note] = []
    for path in sorted(directory.glob("*.pdf")):
        match = _CODE_RE.match(path.stem)
        if not match:
            continue          # ignore anything that is not `CODE_Topic.pdf`
        notes.append(Note(tier=tier,
                          code=match.group("code"),
                          topic=match.group("topic").replace("_", " "),
                          path=path))
    return notes


TIER_A: list[Note] = _scan("a")
TIER_B: list[Note] = _scan("b")
ALL: list[Note] = TIER_A + TIER_B

#: Tier A topics are the same six topics the question bank is built around.
TIER_A_TOPICS: list[str] = [note.topic for note in TIER_A]


def tier(code: str) -> list[Note]:
    return TIER_A if code.lower() == "a" else TIER_B


def find(code: str) -> Note | None:
    """Look a sheet up by its code, accepting 'B14', 'b14' and '14'."""
    wanted = code.strip().lower()
    for note in ALL:
        if note.code.lower() == wanted:
            return note
    loose = wanted.lstrip("b").lstrip("0")
    for note in ALL:
        if note.code.lower().lstrip("b").lstrip("0") == loose:
            return note
    return None


def get(tier_code: str, code: str) -> Note | None:
    """Exact lookup inside one tier. Callback data always carries both, so there
    is no need to guess which tier a bare code belongs to."""
    wanted = code.strip().lower()
    return next((n for n in tier(tier_code) if n.code.lower() == wanted), None)
