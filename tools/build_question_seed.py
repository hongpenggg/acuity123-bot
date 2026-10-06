#!/usr/bin/env python3
"""Regenerate the per-level question seeds in seeds/ from the MCQ documents.

Usage
-----
    pip install -r requirements-dev.txt
    python tools/build_question_seed.py                    # all levels
    python tools/build_question_seed.py --level clin       # one level
    python tools/build_question_seed.py --check            # verify, write nothing

Files written (one per audience level, loaded in order):
    seeds/01_preclin_mcqs.sql
    seeds/02_clin_mcqs.sql
    seeds/03_postmbbs_mcqs.sql

A level with no source documents still gets a valid file: the double-load guard
and nothing else. Loading all three on a fresh database is therefore always
safe, and dropping a new .docx into resources/ plus re-running this script is
the entire workflow for adding a level.

Why a script rather than an edited SQL file: the bank will keep changing, and
hand-editing hundreds of rows of medical text is how you lose the answer
letters. This parses the sources, asserts the shape of every question, refuses
to emit anything it cannot fully account for, and prints a summary you can
eyeball against the document's own coverage table.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import docx

ROOT = Path(__file__).resolve().parent.parent

# level -> (order prefix, source documents). Adding a level is: drop the .docx
# into resources/questions/ and add it here; nothing else needs changing.
LEVELS: dict[str, tuple[str, list[Path]]] = {
    "preclin": (
        "01",
        [
            ROOT / "resources" / "questions" / "Preclinical_Ophthalmology_20_MCQs.docx",
            ROOT / "resources" / "questions" / "Preclinical_Ophthalmology_100_Additional_MCQs.docx",
        ],
    ),
    "clin": (
        "02",
        [
            ROOT / "resources" / "questions" / "Clinical_Ophthalmology_M3_M5_20_Case_MCQs.docx",
            ROOT / "resources" / "questions" / "Clinical_Ophthalmology_M3_M5_100_Additional_Cases_Q21_Q120.docx",
        ],
    ),
    "postmbbs": (
        "03",
        [
            ROOT / "resources" / "questions" / "FRCOphth_Post_MBBS_20_Sample_MCQ.docx",
            ROOT / "resources" / "questions" / "FRCOphth_Post_MBBS_150_Additional_MCQ_Q21_Q170.docx",
        ],
    ),
}

# Seventeen of the clinical cases are built around an embedded fundus or lid
# photograph ("The fundus photograph is shown"), and the bot sends text-only
# question cards - so they cannot be answered as delivered. They are parsed and
# validated like every other question, then held back from the seed. Flip this to
# False to emit them, once sender.send_question can upload the figure first.
SKIP_FIGURE_QUESTIONS = True

TEXT_TAG = "$q$"
JSON_TAG = "$o$"

# Each bank's own coverage table, asserted so a bad regeneration fails here
# rather than in front of students. These are counts of what is *emitted*, so
# the clinical figures are the 120 parsed cases less the 17 held back by
# SKIP_FIGURE_QUESTIONS.
EXPECTED: dict[str, dict] = {
    "preclin": {
        "count": 120,
        "topics": {
            "Development and ocular histology": 21,
            "Orbit and eye movements": 21,
            "Optics and visual transduction": 20,
            "Visual pathways and pupil reflexes": 20,
            "Aqueous humour and glaucoma mechanisms": 16,
            "Retinal and anterior segment pathology": 22,
        },
    },
    "clin": {
        "count": 103,
        "topics": {
            "Neuro ophthalmology and orbit": 20,
            "Red eye cornea and uveitis": 19,
            "Lens lids and paediatric eye": 17,
            "Clinical assessment and vision loss": 14,
            "Glaucoma": 12,
            "Retinal vascular disease": 11,
            "Macular and vitreoretinal disease": 10,
        },
    },
    "postmbbs": {
        "count": 170,
        "topics": {
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
    },
}

# "Question 1", the clinical banks' zero-padded "Question 01", and the FRCOphth
# sample document's "Sample question 01".
QUESTION_RE = re.compile(r"^(?:Sample\s+question|Question)\s+(\d+)\s*$")
TYPE_RE = re.compile(r"^Question type\s*:?\s*(.+)$")
TOPIC_RE = re.compile(r"^Topic\s*:?\s*(.+)$")
# The preclinical banks write "Options:", the clinical ones "Options", and the
# FRCOphth ones omit the header entirely - see parse_document.
OPTIONS_RE = re.compile(r"^Options\s*:?\s*$")
# Preclinical and clinical letter their options a)-e); FRCOphth uses A)-D).
OPTION_RE = re.compile(r"^([a-eA-E])\)\s*(.+)$")
CORRECT_RE = re.compile(r"^Correct option\s*:?\s*([a-eA-E])\)\s*(.+)$")
# Anything after the last question is appendix, not content.
STOP_RE = re.compile(
    r"^(Resource Guide|Coverage and Resources|Teaching Resources"
    r"|Supplied resources|Supplemental references"
    r"|Clinical Sources and References)\s*$"
)


def parse_document(path: Path) -> tuple[list[dict], list[str]]:
    """Parse one .docx into question dicts.

    The six source documents agree on shape but not on punctuation, so every
    separator here is deliberately loose:

    * headers read ``Question 1``, ``Question 01`` or ``Sample question 01``
    * ``Topic  X`` and ``Topic: X`` both occur, as do ``Options`` and ``Options:``
    * the FRCOphth documents omit the options header altogether, so the first
      ``a)``/``A)`` line opens the list instead
    * preclinical and clinical options are lettered ``a)``-``e)``, FRCOphth
      ``A)``-``D)``
    * the FRCOphth documents carry no ``Question type:`` label - the tag is a
      bare ``FRCOphth Part 1 | Anatomy | applied inference`` line under the
      header, which is why an unlabelled line containing a pipe is read as one
    * only the *first* paragraph after the answer is the explanation; the
      FRCOphth documents then list one to three reading references, which are
      not part of it and are dropped

    A question whose block contains an inline image is flagged ``figure``, since
    a card the bot can only render as text cannot carry a fundus photograph.
    """
    questions: list[dict] = []
    problems: list[str] = []
    current: dict | None = None
    section: str | None = None

    def finish(q: dict | None) -> None:
        if q is None:
            return
        missing = [k for k in ("number", "tag", "topic", "stem", "options",
                               "correct_idx", "explanation") if k not in q]
        if missing:
            problems.append(f"Q{q.get('number', '?')}: missing {missing}")
            return
        if not 2 <= len(q["options"]) <= 6:
            problems.append(f"Q{q['number']}: {len(q['options'])} options")
        if not 0 <= q["correct_idx"] < len(q["options"]):
            problems.append(f"Q{q['number']}: correct_idx out of range")
        elif q["options"][q["correct_idx"]].strip() != q["correct_text"].strip():
            problems.append(
                f"Q{q['number']}: the answer letter and its text disagree "
                f"({q['options'][q['correct_idx']][:40]!r} vs {q['correct_text'][:40]!r})")
        if not q["stem"].strip():
            problems.append(f"Q{q['number']}: empty stem")
        if not q["explanation"].strip():
            problems.append(f"Q{q['number']}: empty explanation")
        questions.append(q)

    for para in docx.Document(str(path)).paragraphs:
        line = para.text.strip()
        if STOP_RE.match(line):
            break           # the appendix, and the figure map that follows it
        # The drawing lives in its own paragraph next to the stem, so this has to
        # be checked before the empty-line skip below.
        if current is not None and "graphicData" in para._p.xml:
            current["figure"] = True
        if not line:
            continue

        if (match := QUESTION_RE.match(line)):
            finish(current)
            current, section = {"number": int(match.group(1))}, None
            continue
        if current is None:
            continue  # document title / blurb

        if (match := TYPE_RE.match(line)):
            current["tag"] = match.group(1).strip()
        elif (match := TOPIC_RE.match(line)):
            current["topic"] = match.group(1).strip()
        elif OPTIONS_RE.match(line):
            section = "options"
        elif (match := CORRECT_RE.match(line)):
            current["correct_idx"] = ord(match.group(1).lower()) - ord("a")
            current["correct_text"] = match.group(2).strip()
            section = "explanation"
        elif (match := OPTION_RE.match(line)) and (
                section == "options"
                or ("options" not in current and match.group(1).lower() == "a")):
            # No "Options" header in the FRCOphth documents: the first a)/A) line
            # opens the list. Anchoring on "a" keeps a stem that happens to start
            # "b) ..." from being mistaken for one.
            section = "options"
            current.setdefault("options", []).append(match.group(2).strip())
        elif section == "explanation":
            if "explanation" in current:
                current.setdefault("references", []).append(line)
            else:
                current["explanation"] = line
        elif section == "options":
            problems.append(f"Q{current['number']}: stray line in options: {line[:60]!r}")
        elif "|" in line and not {"tag", "topic", "stem"} & current.keys():
            current["tag"] = line           # FRCOphth's unlabelled tag line
        else:
            current["stem"] = (current.get("stem", "") + " " + line).strip()

    finish(current)
    return questions, problems


def literal(value: str) -> str:
    for tag in (TEXT_TAG, JSON_TAG):
        assert tag not in value, f"value contains {tag}: {value[:60]!r}"
    assert "\x00" not in value, "NUL byte in text"
    return f"{TEXT_TAG}{value}{TEXT_TAG}"


def render_sql(level: str, questions: list[dict], sources: list[Path]) -> str:
    label = {"preclin": "Preclinical", "clin": "Clinical",
             "postmbbs": "Post-MBBS"}[level]

    if questions:
        topics = sorted({q["topic"] for q in questions})
        summary = "\n".join(
            f"--   {count:3}  {topic}"
            for topic, count in sorted(Counter(q["topic"] for q in questions).items()))
        topic_block = f"\n-- Topics covered ({len(topics)}):\n{summary}\n"
        source_block = "\n".join(f"--   {p.relative_to(ROOT)}" for p in sources)
        body = ",\n".join(
            "  ('{level}', {topic}, {tag}, {stem}, {options}::jsonb, {idx}, {expl})".format(
                level=level,
                topic=literal(q["topic"]),
                tag=literal(q["tag"]),
                stem=literal(q["stem"]),
                options=f"{JSON_TAG}{json.dumps(q['options'], ensure_ascii=False)}{JSON_TAG}",
                idx=q["correct_idx"],
                expl=literal(q["explanation"]),
            )
            for q in questions
        )
        load = f"""insert into questions (level, topic, tag, text, options, correct_idx, explanation) values
{body};

commit;

-- Expect: {len(questions)}
--   select count(*) from questions where level = '{level}';
"""
    else:
        source_block = ("--   (none yet - drop the .docx into resources/ and re-run "
                        "the generator)")
        topic_block = "\n-- No source document for this level yet, so this file loads nothing.\n"
        load = f"""-- Nothing to load yet. This file is a placeholder so that loading every seed in
-- order is always safe. When the {label} questions arrive:
--
--   1. put the .docx in resources/
--   2. add it to LEVELS['{level}'] in tools/build_question_seed.py
--   3. python tools/build_question_seed.py --level {level}
--
commit;
"""

    return f"""-- {label} Ophthalmology - question bank for level '{level}'.
--
-- GENERATED FILE - do not edit by hand.
--   Regenerate with:  python tools/build_question_seed.py --level {level}
--   Source(s):
{source_block}
--
-- Load AFTER schema.sql, in file order:
--   psql "$DATABASE_URL" -f schema.sql
--   psql "$DATABASE_URL" -f seeds/01_preclin_mcqs.sql
--   psql "$DATABASE_URL" -f seeds/02_clin_mcqs.sql
--   psql "$DATABASE_URL" -f seeds/03_postmbbs_mcqs.sql
--
-- Re-running is blocked by the guard below. To reload from scratch:
--   delete from questions where level = '{level}';
{topic_block}
begin;

-- Refuse to double-load this level: a second run would otherwise silently
-- duplicate the bank. Scoped to '{level}' so the levels stay independent.
do $$
begin
  if exists (select 1 from questions where level = '{level}') then
    raise exception '{level} questions already present (% rows) - delete them first to reload',
      (select count(*) from questions where level = '{level}');
  end if;
end
$$;

{load}"""


def build_level(level: str, sources: list[Path], check: bool) -> tuple[bool, str]:
    prefix, _ = LEVELS[level]
    out = ROOT / "seeds" / f"{prefix}_{level}_mcqs.sql"

    questions: list[dict] = []
    problems: list[str] = []
    for path in sources:
        if not path.exists():
            problems.append(f"missing source document: {path.relative_to(ROOT)}")
            continue
        found, issues = parse_document(path)
        print(f"  {path.name}: {len(found)} question(s), {len(issues)} problem(s)")
        for issue in issues:
            print(f"     ! {issue}")
        questions.extend(found)
        problems.extend(issues)

    questions.sort(key=lambda q: q["number"])

    if questions:
        numbers = [q["number"] for q in questions]
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(f"question numbering is not 1..{len(numbers)}: {numbers[:5]}...")

        # Held back only after the checks above, so the source documents are
        # still validated as a complete 1..N bank.
        if SKIP_FIGURE_QUESTIONS:
            figures = [q["number"] for q in questions if q.get("figure")]
            if figures:
                questions = [q for q in questions if not q.get("figure")]
                print(f"  held back {len(figures)} figure-dependent question(s): "
                      + ", ".join(f"Q{n}" for n in figures))

        expected = EXPECTED.get(level)
        if expected:
            if len(questions) != expected["count"]:
                problems.append(f"expected {expected['count']} questions, parsed {len(questions)}")
            counts = Counter(q["topic"] for q in questions)
            if dict(counts) != expected["topics"]:
                problems.append(f"topic split differs from the source document: {dict(counts)}")

        print(f"  {len(questions)} questions")
        for topic, count in Counter(q["topic"] for q in questions).most_common():
            print(f"    {count:3}  {topic}")
        print(f"  options per question: {dict(Counter(len(q['options']) for q in questions))}")

    if problems:
        return False, f"{len(problems)} problem(s), refusing to write {out.name}"

    sql = render_sql(level, questions, sources)

    if check:
        existing = out.read_text(encoding="utf-8") if out.exists() else None
        if existing != sql:
            return False, f"{out.relative_to(ROOT)} is out of date (run without --check)"
        return True, f"{out.relative_to(ROOT)} is up to date"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sql, encoding="utf-8")
    return True, f"wrote {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", choices=sorted(LEVELS), action="append",
                        help="level to build (repeatable); default all")
    parser.add_argument("--source", type=Path, action="append",
                        help="override the source .docx for a single --level")
    parser.add_argument("--check", action="store_true",
                        help="verify the committed seeds match the sources; write nothing")
    args = parser.parse_args()

    levels = args.level or sorted(LEVELS)
    if args.source and len(levels) != 1:
        parser.error("--source requires exactly one --level")

    failed = False
    for level in levels:
        _, default_sources = LEVELS[level]
        sources = args.source or default_sources
        print(f"[{level}]")
        ok, message = build_level(level, sources, args.check)
        print(f"  {message}\n")
        failed |= not ok

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
