# LKC OphSoc Tele Bot

[![CI](https://github.com/hongpenggg/acuity123-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/hongpenggg/acuity123-bot/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3-2CA5E0.svg)](https://docs.aiogram.dev/)
[![postgresql](https://img.shields.io/badge/postgresql-14%2B-336791.svg)](https://www.postgresql.org/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

A Telegram revision bot built for the **LKC Ophthalmology Society**. Students
answer adaptive practice questions, get a weekly question and fortnightly notes
pushed to them, and compete on a two-week tournament leaderboard.

**LKC Ophthalmology Society** — made by **Zhong Han** (Vice-Chairperson, LKC OphSoc 26/27)
and the **Acuity Team**: Zhong Han, Rahul, Hongpeng, Jeromy.

> Educational revision only — not clinical advice.

---

## Contents

- [What it does](#what-it-does)
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
| `/practice` | A question at the student's level, preferring ones they haven't seen |
| Adaptive weighting | After 10 answers, topics are chosen in proportion to the student's error rate, with a floor so a mastered topic still resurfaces |
| `/level` | Pre-Clinical / Clinical / Post-MBBS — questions *and* notes follow it, changeable any time |
| `/subscribe` | A question every Monday, one topic per week, rotating through all topics |
| `/notes`, `/notes_sub` | Tier B cheat sheets on demand; Tier A sets pushed fortnightly |
| Tournament | An admin switches it on for 2 weeks; correct `/practice` answers score **once per question**; `/leaderboard` shows the top 3 with the last two characters of each username hidden |
| 💡 Explain | Serves the written explanation stored with the question — the LLM is only a fallback and is optional |

Weakness detection is plain SQL (per-topic accuracy), so a slow or missing LLM
provider can never block practice.

## The question bank

`seeds/01_preclin_mcqs.sql` loads **120 single best answer questions** across
six topics, every one of them with a written explanation:

| Topic | Questions |
|---|---|
| Development and ocular histology | 21 |
| Orbit and eye movements | 21 |
| Optics and visual transduction | 20 |
| Visual pathways and pupil reflexes | 20 |
| Aqueous humour and glaucoma mechanisms | 16 |
| Retinal and anterior segment pathology | 22 |

There is **one seed file per audience level**, loaded in order:

```
seeds/01_preclin_mcqs.sql    120 questions   (loaded)
seeds/02_clin_mcqs.sql       Clinical        (placeholder — loads nothing yet)
seeds/03_postmbbs_mcqs.sql   Post-MBBS       (placeholder — loads nothing yet)
```

The Clinical and Post-MBBS files are valid, load cleanly and add nothing, so
running all three against a fresh database is always safe. Adding a level later
is: drop the `.docx` into `resources/`, add it to `LEVELS` in
`tools/build_question_seed.py`, re-run the generator, load the file. The
`questions.level` column and the bot's `/level` command already handle all three.

Because every question carries an explanation, the bot serves them from the
database and **never calls the LLM for this bank** — you can run the whole event
with no API key and zero spend.

The bank is generated from the `.docx` sources in `resources/`. Editing those
documents does not change the database; regenerate the seeds instead
(`tools/build_question_seed.py`). `tests/test_seed_data.py` verifies the loaded
bank — 120 rows, the topic split above, and that every question renders — so a
bad regeneration fails CI rather than shipping.

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
| `MIN_ATTEMPTS_FOR_ADAPTIVE` | no | Answers before weighting kicks in, default `10` |
| `WEIGHT_FLOOR` | no | Error-rate floor, default `0.15` |

## Commands

**Students** — `/start` `/help` `/practice` `/level` `/subscribe` `/unsubscribe`
`/notes` `/notes_sub` `/notes_unsub` `/tournament` `/leaderboard`

**Admins** (`ADMIN_IDS`) — `/admin_tournament_start` `/admin_tournament_end`
`/admin_weekly_now` `/admin_notes_now`

The `admin_*_now` commands fire the scheduled jobs on demand, so you never have
to wait for a Monday to demonstrate a push.

## Architecture

```
bot/
  config.py    environment + level definitions
  text.py      option letters, rendering, masking, message splitting   (pure)
  sender.py    outbound Telegram: retry, flood control, 4096-char split
  db.py        every SQL statement
  llm.py       explanation fallback (optional)
  handlers.py  commands and callbacks
  jobs.py      weekly / fortnightly / tournament-close cron entries
  main.py      wiring, error handler, graceful shutdown
schema.sql                 structure only, nothing seeded
migrations/                upgrade path for an existing database
seeds/                     the question bank
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
  `pick_question` eventually re-serves a cleared topic and would otherwise let
  students farm the leaderboard.

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
