# Handover — what exists, and how to finish it

Everything here is written for whoever picks this up next. No prior context needed.

**Status at handover.** The bot is live for the **Pre-Clinical** bank (120
questions). The **Clinical** and **Post-MBBS** question banks are the main thing
still to be done, and the step-by-step for those is
[§2](#2-adding-a-question-bank). Everything else is either working or listed as
not-built in [§7](#7-not-built-yet).

---

## 1. What is running

```
Telegram      @acuity123_bot  ("Acuity Bot")
Droplet       DigitalOcean, Singapore (sgp1), 1 GB RAM, Ubuntu 24.04
              IP in the DigitalOcean account (the one holding the LKC OphSoc key)
Service       systemd unit `studybot`, running as user `deploy` from /opt/studybot
Database      PostgreSQL on the same box — role `studybot` owns db `studybot`
Backups       nightly pg_dump 17:04 into /var/backups/studybot (14-day rotation)
```

Working now: `/quizme` (a set of five, options numbered 1–5, score at the end),
`/stats`, `/topicalnotes`, `/randomnotes`, `/resources` (the sheets, as PDFs),
`/changestreams`, `/weeklyquiz`, `/monthlynotes`, `/stopweekly`, `/stopmonthly`,
`/tournament`, `/leaderboard`, and the four `/admin_*` commands. The older names
(`/practice`, `/subscribe`, `/notes_sub`, `/level` and so on) still work as
aliases.

Only IDs in `ADMIN_IDS` can run admin commands.

---

## 2. Adding a question bank

This is the main outstanding piece of work. It is **four steps** and needs no code
changes.

### 2.1 Get the questions written in the right format

The generator parses a `.docx` (or two) laid out exactly like this. Anything it
cannot account for, it refuses to write — so a malformed file fails loudly rather
than silently dropping a question.

```
Question 1
Question type: Physiology | experimental interpretation
Topic: Optics and visual transduction
<the question stem, as a single paragraph>

Options:
a) first option
b) second option
c) third option
d) fourth option
e) fifth option

Correct option: c) third option
<the explanation, as a single paragraph>

Question 2
...
```

Rules that matter:

- **2–6 options per question.** The database enforces it. The existing bank mostly
  uses five.
- **The answer letter and its text must agree.** The generator compares `c)` with
  the text after `Correct option:` and rejects the file if they differ.
- **One `Topic` per question**, spelled the same way each time. Topics are the
  buckets adaptive practice weights between, so a typo creates a new bucket.
- **`Question type:`** is optional context (`Discipline | what it tests`). It is
  stored in `questions.tag` and is not currently used for anything.
- **Numbering must be 1..N with no gaps** across the file(s).
- A trailing "Coverage and Resources" / "Resource Guide" section is fine; the
  parser stops there.

### 2.2 Put the file in the repo

```bash
cp "Clinical_Ophthalmology_MCQs.docx" resources/questions/
```

### 2.3 Register it and regenerate

Edit `LEVELS` in `tools/build_question_seed.py` — add the file to the `clin` list
(the `postmbbs` list for the other one):

```python
LEVELS = {
    "preclin": ("01", [ ...two files... ]),
    "clin":    ("02", [ROOT / "resources" / "questions" / "Clinical_Ophthalmology_MCQs.docx"]),
    "postmbbs":("03", []),
}
```

Then:

```bash
pip install -r requirements-dev.txt
python tools/build_question_seed.py --level clin
```

It prints the question count and the topic split, and **refuses to write** if
anything is inconsistent. Sanity-check that printout against the source document
before continuing.

Optional: add the expected count and topic split to `EXPECTED` in the same file so
a later regeneration that drops questions fails instead of quietly shrinking the
bank.

### 2.4 Commit, and let CI verify

```bash
git checkout -b add-clinical-bank
git add resources/questions/Clinical_Ophthalmology_MCQs.docx seeds/02_clin_mcqs.sql tools/build_question_seed.py
git commit -m "Add the Clinical question bank"
git push -u origin add-clinical-bank
```

Open a PR. CI runs the parser check (`--check` fails if a committed seed no longer
matches its source), the SQL check, ruff, and the test suite against a real
PostgreSQL. **Wait for it to go green before merging.**

### 2.5 Load it on the server

```bash
ssh root@<droplet-ip>
cd /opt/studybot
sudo -u deploy git pull
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -f seeds/02_clin_mcqs.sql
```

The seed file holds a guard that refuses to load a tier twice, so re-running it is
safe — it will tell you the bank is already present rather than duplicating it.

**Verify:**

```bash
psql "$DATABASE_URL" -c "select level, count(*) from questions group by level order by level;"
```

Then in Telegram: `/changestreams` → Clinical → `/quizme`, and a Clinical
question should arrive. The "that bank is still being written" message disappears
by itself once the tier has questions.

> `provision.sh` also loads every `seeds/*.sql` automatically, but only for tiers
> that have nothing in them yet — so a fresh server needs no manual step.

---

## 3. Adding revision sheets

The sheets are PDFs; the bot sends the file itself, so there is nothing to host.

```
resources/notes/tier_a/     one broad sheet per topic        01_<Topic>.pdf
resources/notes/tier_b/     deeper sheets on single points   B01_<Topic>.pdf
```

To add one: name it `B21_Some_Topic.pdf` (`CODE_Name_With_Underscores.pdf`),
commit it, and `git pull` on the server. **That is the whole process** — the
catalogue is built by scanning the directory, so there is no code change and no
database row.

Students see the two kinds as **"Overview"** and **"Focused"** sheets. "Tier A /
Tier B" is internal shorthand and is never shown to them.

`tests/test_resources.py` fails if a file is misnamed, missing, or if the six
overview sheets stop matching the six question topics.

---

## 4. Configuring the bot

Everything comes from `/opt/studybot/.env` (mode 600, owned by `deploy`). Nothing
needs editing to deploy.

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | from @BotFather |
| `DATABASE_URL` | `postgresql://studybot:<pw>@127.0.0.1:5432/studybot` |
| `ADMIN_IDS` | comma-separated numeric Telegram IDs allowed to run `/admin_*` |
| `LLM_API_KEY`, `LLM_MODEL` | **leave blank.** Every question ships with a written explanation, so the bot never calls a provider. Setting both enables generation for any question that has none |
| `BOT_TZ` | display/schedule timezone, `Asia/Singapore` |

Quiz behaviour is not configured by env any more: sets of five are fixed blocks in
question-id order, controlled by `db.SET_SIZE`. See `docs/SETUP.md` §4.5.

**Who is an admin** is the one thing people usually want to change. Add the numeric
ID (not the `@username` — get it from @userinfobot), then restart:

```bash
sudo systemctl restart studybot
```

**Wording** lives in two places: the credit block at the top of `bot/config.py`
(`ACUITY_CREDIT`, `SOCIETY_CREDIT`, `DISCLAIMER`) and the feature blurb in
`bot/handlers.py` (`FEATURES`). Editing either needs a `git pull` + restart.

---

## 5. Running it day to day

```bash
ssh root@<droplet-ip>
systemctl is-active studybot                     # active
systemctl show -p NRestarts --value studybot     # 0 means it is not crash-looping
journalctl -u studybot -f                        # live logs, Ctrl+C to stop
```

**Deploying a change:**

```bash
cd /opt/studybot && sudo -u deploy git pull
sudo -u deploy .venv/bin/pip install -r requirements.txt   # if dependencies changed
sudo systemctl restart studybot
```

**After a schema change** also run the matching file in `migrations/`.

> **`systemctl is-active` lies on a crash loop.** With `Restart=always`, a unit
> that is dying and restarting still reports `active`. Always check `NRestarts`
> and the journal. This cost an hour during setup.

**Backups are the database.** Check them, and once restore one:

```bash
sudo -u postgres ls -la /var/backups/studybot | tail -3
```

---

## 6. Tournament day

```bash
/admin_tournament_start      # 2 weeks. Enters everyone and announces it
/leaderboard                 # top 3, masked, plus the caller's own rank
/admin_tournament_end        # announces winners and sends you the full table
```

**Entry is automatic.** `/admin_tournament_start` adds every active user to the
tournament and announces it to all of them, so a student never has to opt in to a
competition they are already answering questions for. Anyone who sends `/start`
while a tournament is running is entered too.

Only `/quizme` scores, and **once per question**, so the board cannot be farmed by
repeating an easy question and the Monday set cannot be used to climb it. Ties
break by join order.

Winners are told to contact the LKC OphSoc EXCO at `@lkceye` for their award. Keep
the admin table the closing command sends you: it is the only place with real
usernames and Telegram IDs.

Checklist the day before is in `docs/SETUP.md` §5.7.

---

## 7. What was built, and what is still open

The redesign specified after the first release is **implemented**:

| Feature | Where it lives |
|---|---|
| Sets of five: a shared benchmark, soft to leave, score at the end | `db.set_board` / `db.quiz_set` / `db.set_score`, `sender.send_question_for_level`, `handlers._report_set_if_finished` |
| Marker A: the running total, by topic and by question type | `db.stats`, behind `/stats` |
| Marker B: the tournament-window score, shown as standings only | `db.tournament_mark`, surfaced by `/tournament` and `/leaderboard` |
| Tournament auto-enrolment and an announcement to every user | `db.enrol_everyone` / `db.all_users`, `jobs.announce_tournament`, wired into `/admin_tournament_start` and `/start` |
| `/weeklyquiz` (a set every Monday) and `/monthlynotes` (six overview sheets plus the six reserved focused ones, monthly) | `jobs.weekly_quiz`, `jobs.monthly_notes` |
| `/quizme`, `/topicalnotes`, `/randomnotes` | handlers, with the menu in `bot/commands.py` |
| Six focused sheets reserved for the monthly drop | `resources.MONTHLY_CODES` |

Still open, and nothing here is blocked by anything else:

| Open | Notes |
|---|---|
| **The Clinical and Post-MBBS banks** | §2. This is the main outstanding work |
| **Dropping the old command names** | `/practice`, `/subscribe`, `/unsubscribe`, `/notes_sub`, `/notes_unsub` and `/level` still work as aliases so nothing in a student's existing chat breaks. They are the extra names on each `Command(...)` decorator |
| **Sets are deliberately not adaptive** | A shared benchmark needs the same five questions for everyone, so per-student topic weighting was removed. The adaptive selector is in git history, at the commit before the sets landed |
| **Nothing shows a student their question history** | Intentional for marker B: other students cannot see anyone's activity. Every answer is in `attempts` if a per-question view is ever wanted |

---

## 8. Where things are

```
bot/config.py       credit block, levels, tuning, repo links
bot/commands.py     the command menu and its per-chat scopes
bot/handlers.py     commands and callbacks (FEATURES text is here)
bot/db.py           every SQL statement, including topic weighting and no-repeats
bot/resources.py    the note catalogue, built by scanning resources/notes
bot/text.py         option numbering and rendering (pure, easy to unit test)
bot/jobs.py         the scheduled pushes and tournament closing
seeds/              the question banks, one file per tier
resources/          the .docx sources and the note PDFs
tools/              regenerates the seeds from the .docx files
docs/SETUP.md       full server runbook, troubleshooting table, event-day checklist
tests/              unit, handler and live-database suites — all run in CI
```

`pytest` runs everything. The live-database tests skip unless `TEST_DATABASE_URL`
is set; CI sets one up, so **push a branch and let CI run** rather than testing
locally.

---

## 9. Open questions

1. **Should the Clinical and Post-MBBS banks have their own notes?** The sheets in
   the repo are topic-based and shared across levels today.
2. **Is a set of five the right size?** It is one constant (`db.SET_SIZE`). A
   student clearing 120 questions walks 24 sets; if that feels long for an event,
   a bigger set is a one-line change, and progress carries over.
3. **Should the reserved six rotate?** `resources.MONTHLY_CODES` is fixed, so the
   same six focused sheets go out every month. If the drop should walk through the
   whole bank over several months, that becomes a rotation keyed on the month.

---

## 10. Access you need

| Thing | Who has it |
|---|---|
| DigitalOcean droplet + SSH key | the account holding the LKC OphSoc key |
| Telegram bot token | @BotFather, under whoever created `@acuity123_bot` |
| GitHub repo | [github.com/hongpenggg/acuity123-bot](https://github.com/hongpenggg/acuity123-bot) |

Hand the bot token on through BotFather's transfer, not by pasting it about — and
rotate it (`/revoke`) if it ever lands in a chat log.
