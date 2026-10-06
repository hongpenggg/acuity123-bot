# LKC OphSoc Tele Bot

[![CI](https://github.com/hongpenggg/acuity123-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/hongpenggg/acuity123-bot/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3-2CA5E0.svg)](https://docs.aiogram.dev/)
[![postgresql](https://img.shields.io/badge/postgresql-14%2B-336791.svg)](https://www.postgresql.org/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

A Telegram revision bot for the **LKC Ophthalmology Society** (`@lkceye`).
Students answer adaptive quiz questions, pull the society's revision sheets
straight out of the chat as PDFs, get a question and notes pushed to them, and
compete on a tournament leaderboard.

Built by the **Acuity Team** — Zhong Han, Hongpeng, Rahul, Jeromy.

> Educational revision only — not clinical advice.

---

## Contents

- [What it does](#what-it-does)
- [Revision notes](#revision-notes)
- [The question bank](#the-question-bank)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Commands](#commands)
- [Architecture](#architecture)
- [Development](#development)
- [Deployment](#deployment)
- [Content and privacy](#content-and-privacy)
- [Contributing](#contributing)

## What it does

| Area | Behaviour |
|---|---|
| `/quizme` | A **set of five** questions, options numbered 1–5, picked for you: spread across topics first, then weighted toward the topics you keep getting wrong. The score is reported when the fifth is answered |
| Sets, softly | Stop whenever you like and pick up where you left off. A set is simply your next five answers, derived from the `attempts` table rather than stored, so there is no session state to go stale |
| `/weeklyquiz` scoring | The Monday push answers count toward your sets, but **never** toward the tournament |
| `/stats` | Marker A: the running total, then accuracy broken down by **topic** and by **question type** (`questions.tag`), weakest first |
| `/notes` | Your next unseen cheat sheet: overview sheets first, then focused, then "syllabus complete" |
| Notes under a question | Every question card carries a **📘 Notes for this topic** button under the options, so the sheet for whatever is being tested is one tap away. It stays on the card after it is answered, and it sends the sheet for *that* question rather than the next one in the queue |
| `/review` | Re-serves the questions you got wrong, with their explanations |
| `/resources` | Browse every sheet, or fetch one by code (`/resources b14`) |
| `/changestreams` | Pre-Clinical / Clinical / Post-MBBS. Questions *and* sheets follow it |
| `/weeklyquiz` | Five questions every Monday, picked the same way. Deliberately does **not** score for the tournament |
| `/subscribenotes` | On the 1st and 15th: the next few sheets you have not had, as PDFs |
| Tournament | An admin switches it on and **everyone who has used `/start` is entered automatically**; only `/quizme` scores, once per question; `/leaderboard` shows the top 3 with the last two characters of each username hidden |
| Marker B | Tournament points, shown as standings only. Question and activity counts are never shown to students |
| 💡 Explain | Serves the written explanation stored with the question; the LLM is only a fallback and is optional |
| Menu | Telegram keeps a command list per chat. The default shows only `/start`; sending `/start` sets that chat's full menu, which is what makes `/quizme` and the rest appear |

**Sets are adaptive, and that is a deliberate trade.** An earlier version served
a fixed block of five in id order so every student's set 1 was identical and the
scores were directly comparable. Variety and revisiting weak topics were judged
worth more than that comparability, so `db.pick_question` now ranks every
unattempted question by: a topic not already in the set being built, then an
unused question type, then the topics the student is weakest on, then random. An
untouched topic sits exactly between weak and mastered, so new material still
comes round.

Two students therefore do not walk the same path, and set numbers are **not**
comparable between students. Compare them with `/stats` or the tournament
instead. `/review` exists because of this too: `pick_question` only ever serves
questions a student has never attempted, so without it a missed question would
never come back.

## Revision notes

The sheets live in the repo as PDFs and are delivered **from the deployed working
copy**, so Telegram receives the actual document and nothing needs hosting. If a
sheet is missing locally, or is too large for the Bot API to upload, the bot falls
back to the GitHub link rather than failing.

**153 sheets, scoped per audience level** exactly like the question banks, so a
Post-MBBS student is never handed a preclinical sheet:

```
resources/
  questions/                    the .docx MCQ sources
  notes/preclin/tier_a/    6    one broad sheet per topic       01–06
  notes/preclin/tier_b/   20    deeper sheets on single points  B01–B20
  notes/clin/tier_a/       7    C01–C07
  notes/clin/tier_b/      40    B01–B40
  notes/postmbbs/tier_a/  15    A01–A15
  notes/postmbbs/tier_b/  65    B01–B65
```

A bare code is only unique **within** a level — `B01` exists in all three — so
every lookup in `bot/resources.py` takes a level, and there is no level-free
accessor left in the module. `/notes`, `/resources` and the fortnightly drop all
resolve the student's stream first.

The catalogue is built by **scanning the directory**, not hard-coded — adding a
sheet is: drop the PDF in the right level folder, commit it. No code change, no
database row. `tests/test_resources.py` asserts the catalogue matches what is on
disk, and that `B01` resolves to three different sheets, so a lookup that forgets
its level fails CI.

To students these are "Overview" and "Focused" sheets, never "Tier A" and
"Tier B" — that is internal shorthand for the content team. The clinical sheets
arrived named `B01_Clinical_Type_B_...`; that shorthand was stripped on import,
and the clinical overview sheets were renamed to the seven clinical question
topics so the sheets and the bank describe the same seven things.

**Every question points at the sheet for its own topic.** The button under the
options comes from `resources.for_topic(level, question["topic"])`, resolved from
the question's own level and topic, so the same card points at the same sheet
however it was sent and the button survives the card being edited into its
answered state.

**It is a button rather than a `/notes 03` line in the card text, and that is not
a style choice.** Telegram only makes the *command word* tappable: a tapped
`/notes 03` arrives as a bare `/notes`, which hands the student the first sheet in
their queue rather than the one on the card. The button's callback data carries
the level, kind and code, so the tap cannot lose the sheet — and it reuses the
`/resources` delivery path, so the sheet still goes through the one place that
records a delivery.

Matching is an exact topic match first; failing that, shared words carry it,
because the post-MBBS overview sheets are descriptive titles ("Applied ocular
anatomy and development") rather than the bank's shorter topic names ("Anatomy
and embryology"). A shared word that appears in only one sheet's title is enough
on its own, otherwise two shared words are needed. Where the sheets genuinely do
not cover a topic — the post-MBBS bank has questions on pathology, pharmacology,
genetics and microbiology with no sheet for any of them — the question carries no
button rather than a wrong one.

**`/notes` is a progression, not a picker.** It hands over the next sheet the
student has not had — overview sheets first, then focused ones — and when they
have had every sheet at their level it tells them they have finished the
syllabus. Deliveries are recorded per student in `note_deliveries`, so nothing is
ever sent twice, a student can stop and resume, and the fortnightly drop
(`/subscribenotes`, the 1st and 15th) works through the focused catalogue rather
than resending a fixed bundle.

That replaced a scheme where six focused sheets per level were reserved for a
monthly drop and random draws came from the rest. Tracking deliveries properly
makes a reserved subset unnecessary and removes the per-level curation it would
have needed. `/resources` remains the browser for anyone who wants to jump
straight to a sheet, by code or by phrase (`/resources b14`,
`/resources glaucoma`).

## The question bank

**393 single best answer questions** across all three audience levels, every one
of them with a written explanation. One seed file per level, loaded in order:

```
seeds/01_preclin_mcqs.sql    120 questions    6 topics
seeds/02_clin_mcqs.sql       103 questions    7 topics
seeds/03_postmbbs_mcqs.sql   170 questions   18 topics
```

<details>
<summary><b>Topic split per bank</b> (the generator asserts these, so a bad regeneration fails CI)</summary>

| Pre-Clinical — 120 | | Clinical — 103 | |
|---|--:|---|--:|
| Retinal and anterior segment pathology | 22 | Neuro ophthalmology and orbit | 20 |
| Development and ocular histology | 21 | Red eye cornea and uveitis | 19 |
| Orbit and eye movements | 21 | Lens lids and paediatric eye | 17 |
| Optics and visual transduction | 20 | Clinical assessment and vision loss | 14 |
| Visual pathways and pupil reflexes | 20 | Glaucoma | 12 |
| Aqueous humour and glaucoma mechanisms | 16 | Retinal vascular disease | 11 |
| | | Macular and vitreoretinal disease | 10 |

| Post-MBBS — 170 | | | |
|---|--:|---|--:|
| Physiology and biochemistry | 17 | Paediatric ophthalmology and strabismus | 9 |
| Cornea and ocular surface | 15 | Anatomy and embryology | 7 |
| Optics and refraction | 14 | Biostatistics and evidence | 5 |
| Medical retina and macular decisions | 13 | Genetics | 4 |
| Cataract and lens surgery | 12 | Microbiology and immunology | 4 |
| Optics and refractive surgery | 12 | Pharmacology | 4 |
| Vitreoretinal surgery and trauma | 12 | Pathology | 2 |
| Orbit lids and lacrimal selection | 12 | | |
| Advanced neuro ophthalmology | 10 | | |
| Uveitis and inflammatory medicine | 9 | | |
| Glaucoma | 9 | | |

</details>

**Seventeen clinical cases are deliberately not loaded.** Their documents hold
120 cases, but seventeen are built around an embedded fundus or lid photograph
("the fundus photograph is shown") and the bot sends text-only question cards, so
they cannot be answered as delivered. The generator parses and validates them
like any other question and then holds them back — `SKIP_FIGURE_QUESTIONS` in
`tools/build_question_seed.py`, one line to reverse once `sender.send_question`
can upload the figure first.

Four of the seventeen carry only a caption and look answerable as written, so
they are the quick way to 107. The full list, which thirteen genuinely need the
picture, both routes to finishing them, and the copyright position on the images
are all in
[docs/HANDOVER.md §2.6](docs/HANDOVER.md#26-the-seventeen-clinical-cases-that-are-held-back).

Because every question carries an explanation, the bot serves them from the
database and **never calls the LLM for these banks** — you can run the whole
event with no API key and zero spend.

The banks are generated from the `.docx` sources in `resources/questions/`.
Editing those documents does not change the database; regenerate the seeds
instead (`tools/build_question_seed.py`). The three source formats disagree on
punctuation, option lettering (`a)`–`e)` versus `A)`–`D)`), whether there is an
`Options:` header at all, and whether the question type is labelled — the parser
tolerates all of it and refuses to emit anything it cannot fully account for.
`tests/test_seed_data.py` verifies the loaded banks — the row counts, the topic
splits above, one content anchor per bank, and that every question renders — so a
bad regeneration fails CI rather than shipping.

Options are rendered **1–5**, not A–E, matching how the society writes its
questions. One function (`bot/text.py:letter`) feeds both the rendered list and
the answer buttons, so the two can never disagree.

## Quickstart

```bash
git clone https://github.com/hongpenggg/acuity123-bot.git && cd acuity123-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env && chmod 600 .env      # set BOT_TOKEN, DATABASE_URL, ADMIN_IDS

psql "$DATABASE_URL" -f schema.sql                  # tables
for f in seeds/*.sql; do psql "$DATABASE_URL" -f "$f"; done   # the question banks

python -m bot.main                          # Ctrl+C once it looks alive
```

Full walkthrough — VPS provisioning, Postgres, systemd, backups, tournament day
— is in **[docs/SETUP.md](docs/SETUP.md)**.

## Configuration

Everything comes from the environment; nothing needs editing to deploy.

| Variable | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather) |
| `DATABASE_URL` | yes | Local Postgres or Supabase pooler |
| `ADMIN_IDS` | yes | Numeric Telegram IDs allowed to run `/admin_*`, comma-separated |
| `LLM_BASE_URL` | no | OpenAI-compatible endpoint, defaults to OpenRouter |
| `LLM_API_KEY` | no | **Leave blank for zero LLM spend** |
| `LLM_MODEL` | no | Set together with the key to enable generation |
| `BOT_TZ` | no | Display/schedule timezone, defaults to `Asia/Singapore` |
| `REPO_SLUG`, `REPO_REF` | no | Which repo the fallback sheet links point at — set these if you fork |

## Commands

**Students**, in the order Telegram shows them — `/quizme` `/notes` `/review`
`/stats` `/resources` `/weeklyquiz` `/subscribenotes` `/tournament`
`/leaderboard` `/changestreams` `/help`

Unlisted but working: `/start`, `/topicalnotes`, `/randomnotes`, `/stopweekly`,
`/stopmonthly`. The two subscriptions each carry an inline **Turn off** button on
their confirmation, which is why the stop commands do not take up a menu slot.

Older names still work as aliases, so nothing already sitting in a student's chat
breaks:

`/practice` = `/quizme` · `/subscribe` = `/weeklyquiz` · `/unsubscribe` =
`/stopweekly` · `/notes_sub` = `/subscribenotes` · `/notes_unsub` = `/stopmonthly` ·
`/level` = `/changestreams`

**Admins** (`ADMIN_IDS`) — `/admin_tournament_start` `/admin_tournament_end`
`/admin_weekly_now` `/admin_notes_now`

The `admin_*_now` commands fire the scheduled jobs on demand, so you never have
to wait for a Monday to demonstrate a push.

## Architecture

```
bot/
  config.py    environment + level definitions
  commands.py  the command menu, and how it changes once someone has started
  text.py      option numbering, rendering, masking, message splitting   (pure)
  resources.py the note catalogue, built by scanning resources/notes
  sender.py    outbound Telegram: retry, flood control, document upload, 4096-char split
  db.py        every SQL statement
  llm.py       explanation fallback (optional)
  handlers.py  commands and callbacks
  jobs.py      weekly quiz / fortnightly sheets / tournament-close cron entries
  main.py      wiring, error handler, graceful shutdown
schema.sql                 structure only, nothing seeded
migrations/                upgrade path for an existing database
seeds/                     the question bank
resources/                 the .docx sources and the note PDFs
tools/                     regenerates the question seeds from the .docx files
scripts/check_sql.py       parse every statement with libpg_query
tests/                     unit, handler and live-database suites
deploy/studybot.service    systemd unit
deploy/provision.sh        one-shot server setup (idempotent)
```

Three design choices worth knowing:

- **`sender.py` exists to break an import cycle.** `handlers` and `jobs` both
  need to send messages, and importing each other would be circular.
- **Every callback path answers its callback**, or the user's client spins
  forever. There is a `dp.errors` handler as a backstop.
- **Tournament points are deduplicated in SQL** (`tournament_answers`), because
  a student could otherwise re-answer a question they already know to farm the
  leaderboard. Only `practice` scores, so the Monday set cannot be used either.

## Development

```bash
pip install -r requirements-dev.txt

python -m pytest -q              # unit + handler tests, no database needed
python scripts/check_sql.py      # parse all SQL with the real Postgres grammar
ruff check . --select F,E9,B,UP,SIM --ignore E501

# Live database tests — real constraints, real data:
docker compose up -d db
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/studybot_test \
    python -m pytest tests/ -v
```

CI runs all of the above against a real Postgres service on every push and pull
request (`.github/workflows/ci.yml`).

**Regenerating a question bank.** If the `.docx` files in `resources/` change,
regenerate the seeds rather than editing them by hand — the parser asserts the
shape of every question and reports anything it can't account for:

```bash
python tools/build_question_seed.py                 # all levels
python tools/build_question_seed.py --check         # CI-style: fail if out of date
python tools/build_question_seed.py --level clin    # one level

# then reload that level (the seed guards against double-loading)
psql "$DATABASE_URL" -c "delete from questions where level = 'preclin';"
psql "$DATABASE_URL" -f seeds/01_preclin_mcqs.sql
```

## Deployment

VPS (Contabo or DigitalOcean), Ubuntu LTS, systemd, Postgres on the same box.
Step-by-step in [docs/SETUP.md](docs/SETUP.md) — including per-provider notes and
the DigitalOcean $4-tier memory caveat.

The short version, once you have a droplet and your key is on it:

```bash
ssh root@YOUR.IP 'bash -s' < deploy/provision.sh   # swap, hardening, Postgres, venv, unit
# then write /opt/studybot/.env, load schema.sql + seeds/*.sql, and start the service
```

**One instance only** — two processes polling the same token produce `Conflict`
errors. Don't run the bot locally while the service is up.

## Content and privacy

Question text lives in the database and in `seeds/`, not in the application
code. Per the content plan: nothing from iRAT/tRAT, AIMBOSS, PassMedicine or
school/senior material goes into this repository until it has been rewritten, and
the "OphSoc QBank and Notes (ZH)" sheet is off limits.

The bot stores Telegram IDs, usernames and answer history, which is personal data
under Singapore's PDPA. The RLS lockdown in `schema.sql` matters if you put this
in front of Supabase's public API, and a `/delete_me` command should be added
before this goes to anyone outside the society.

## Contributing

Open an issue or a PR. Branches are checked by CI — run `python -m pytest -q`
and `python scripts/check_sql.py` before pushing.

## License

[MIT](LICENSE) © 2026 Hongpeng Wei
