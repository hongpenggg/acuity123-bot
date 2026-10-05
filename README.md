# LKC OphSoc Tele Bot

Telegram revision bot for the LKC Ophthalmology Society — adaptive practice
questions, weekly pushes, cheat-sheet notes and a two-week tournament
leaderboard, for students at three levels: **Pre-Clinical**, **Clinical** and
**Post-MBBS**.

LKC Ophthalmology Society · Made by **Zhong Han** (Vice-Chairperson, LKC OphSoc 26/27)
and the **Acuity Team** — Zhong Han, Rahul, Hongpeng, Jeromy.

> Educational revision only — not clinical advice.

## What it does

| Area | Behaviour |
|---|---|
| `/practice` | Sends a question at the student's level, preferring ones they haven't seen |
| Adaptive weighting | After 10 answers, topics are picked in proportion to the student's error rate, with a floor so nothing is starved |
| `/level` | Switch Pre-Clinical / Clinical / Post-MBBS; questions *and* notes follow it |
| `/subscribe` | A question every Monday, one topic per week |
| `/notes`, `/notes_sub` | Tier B cheat sheets on demand; Tier A sets pushed fortnightly |
| Tournament | Toggled on by an admin for 2 weeks; correct `/practice` answers score **once per question**; `/leaderboard` shows the top 3 with the last two characters of each username hidden |
| Explanations | 💡 button; generated once per question and cached, so the LLM bill stays flat |

Weakness detection is plain SQL (per-topic accuracy) — the LLM only writes
explanations, so a slow or broken provider can never block practice.

## Layout

```
bot/
  config.py    environment + level definitions
  text.py      option letters, question rendering, name masking, message splitting
  sender.py    outbound Telegram plumbing (retry, flood control, 4096-char splitting)
  db.py        every SQL statement
  llm.py       explanation generation + cache
  handlers.py  commands and callbacks
  jobs.py      the weekly / fortnightly / tournament-close cron entries
  main.py      wiring, error handler, graceful shutdown
schema.sql                 full schema + placeholder seed
migrations/001_*.sql       upgrade path for a database on the older schema
scripts/check_sql.py       parse every statement with the real Postgres grammar
tests/                     unit tests + a live-database suite
deploy/studybot.service    systemd unit for the VPS
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # fill in the values
psql "$DATABASE_URL" -f schema.sql          # fresh database
python -m bot.main                         # Ctrl+C when it looks alive
```

Already have the database from the earlier schema? Run
`migrations/001_levels_and_tournament_answers.sql` instead of `schema.sql`.

The database URL must be Supabase's **Session pooler** string (port 5432). The
direct host is IPv6-only and will not resolve from an IPv4-only VPS. `db.init`
sets `statement_cache_size=0` because a transaction pooler cannot keep prepared
statements pinned across transactions.

Register the command list with BotFather once, or just let `main.py` call
`set_my_commands` on startup.

## Commands

Student-facing: `/start` `/help` `/practice` `/level` `/subscribe` `/unsubscribe`
`/notes` `/notes_sub` `/notes_unsub` `/tournament` `/leaderboard`

Admin-only (`ADMIN_IDS`): `/admin_tournament_start` `/admin_tournament_end`
`/admin_weekly_now` `/admin_notes_now`

The `admin_*_now` commands exist so you never have to wait for a Monday to
demonstrate the push — useful on demo day.

## Development

```bash
pip install -r requirements-dev.txt

python -m pytest -q              # unit tests + handler flows, no database needed
python scripts/check_sql.py      # parse every SQL statement with libpg_query

# Live database tests (real Postgres, real constraints):
docker compose up -d db
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/studybot_test \
    python -m pytest tests/test_db_live.py -v
```

`scripts/check_sql.py` catches typos and code/schema drift without a database;
`tests/test_db_live.py` proves the constraints actually behave (double-tap
idempotency, tournament scoring, standings surviving the close).

## Deploy

Contabo VPS, Ubuntu LTS, systemd. Full walkthrough — user setup, SSH hardening,
`ufw`, Fail2ban, Supabase, the deploy key — is in the team's build guide; the
short version:

```bash
sudo mkdir -p /opt/studybot && sudo chown deploy:deploy /opt/studybot
git clone <repo> /opt/studybot && cd /opt/studybot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
nano .env && chmod 600 .env

sudo cp deploy/studybot.service /etc/systemd/system/studybot.service
sudo systemctl daemon-reload && sudo systemctl enable --now studybot
journalctl -u studybot -f
```

### Operating notes

- **One instance only.** Two processes polling the same token produce `Conflict`
  errors. Don't run it locally while the VPS service is up.
- **Backups.** Supabase's free tier has little point-in-time recovery. A weekly
  `pg_dump` of `questions` and `notes` is enough — those are the real assets.
- **Cost.** Explanations are generated once per question and cached in the DB.
  Put a hard monthly spend cap on the LLM key anyway.
- **Cron caveat.** `misfire_grace_time` is set to an hour: APScheduler's
  1-second default silently drops a job whose scheduled minute passed while the
  process was restarting, which is exactly how a weekly push goes missing.
- **Timezone.** Schedules and displayed dates are `Asia/Singapore`; timestamps
  are stored as `timestamptz` so this is presentation only.

## Content and privacy

Questions and notes live in the database, not in this repo. The seed rows in
`schema.sql` are placeholders — do **not** paste iRAT/tRAT, AIMBOSS, PassMedicine
or school/senior material in until it has been rewritten, per the team's
copyright plan.

The bot stores Telegram IDs, usernames and answer history, which is personal
data under Singapore's PDPA. Explanations send the question text to the LLM
provider. Keep the RLS lockdown in `schema.sql` in place, and add a
`/delete_me` command before this goes in front of anyone outside the society.
