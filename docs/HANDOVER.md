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

Working now: `/start`, `/practice` (options numbered 1–5, no repeats once you get
one right), `/resources` (the revision sheets, as PDFs), `/changestreams`,
`/subscribe`, `/tournament`, `/leaderboard`, `/notes`, `/notes_sub`, and the four
`/admin_*` commands.

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

Then in Telegram: `/changestreams` → Clinical → `/practice`, and a Clinical
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
| `MIN_ATTEMPTS_FOR_ADAPTIVE` | answers before topic weighting starts (default 10) |
| `WEIGHT_FLOOR` | floor on a topic's error rate (default 0.15) |

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

```
/admin_tournament_start      # 2 weeks; students opt in with /tournament
/leaderboard                 # top 3, masked, plus the caller's own rank
/admin_tournament_end        # announces winners and sends you the full table
```

Correct `/practice` answers score **once per question**, so the board cannot be
farmed by repeating an easy question. Ties break by join order.

Winners are told to contact the society for their award — keep the admin table the
closing command sends you, since it is the only place with real usernames and
Telegram IDs.

Checklist the day before is in `docs/SETUP.md` §5.7.

---

## 7. Not built yet

These were requested but are **not implemented**. They are listed so the next
person does not assume they exist.

| Wanted | Notes for whoever builds it |
|---|---|
| **Practice in sets of 5**, with the score for the set, then a running total | Today `/practice` serves one question at a time. A set needs per-set state (which of the 5 are answered) — either a `sets` table or a session keyed on the message ids |
| **Marker A: long-run tracking** of how many are correct and of what *kind*, by topic and tag | `attempts` already stores `level`, `topic`, `correct` per answer, so this is a reporting query plus a `/stats` command. `questions.tag` is stored and currently unused |
| **Marker B: a tournament-window count**, shown on top of Marker A | The tournament tables already score only during the window. Surfacing it as a separate counter, and hiding question counts from students, is the remaining work |
| **Tournament auto-enrolment** — admin switches it on and everyone is in | Today students opt in with `/tournament`. Auto-entry means inserting membership rows for every active user on start, plus an announcement to all chats (mind Telegram's rate limit — `jobs.FANOUT_PAUSE` already exists for this) |
| **Announce the tournament to all users** when it starts | Needs an "all active users" query; `db.subscribers(flag)` is the existing pattern |
| **Subscription shape**: `/weeklyquiz` (a set of 5, weekly) and `/monthlynotes` (overview sheets + a few focused ones, monthly) | Today it is one question weekly and the overview sheets fortnightly. The cadence is a one-line change in `jobs.register`; the weekly *set* depends on the set-of-5 work above |
| **`/quizme`, `/topicalnotes`, `/randomnotes`** as the command names | `/resources` is the current entry point. Renaming is a menu change in `bot/commands.py` plus handler decorators |
| **A `/randomnotes` that excludes some sheets** | The intent was "a random focused sheet, excluding the ones the monthly push sends". Which sheets are reserved needs deciding — see the open question in §9 |

Nothing above is blocked by the others except the subscriptions, which need the
sets-of-5 work first.

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

1. **Which sheets does `/randomnotes` exclude?** The spec said "a random focused
   sheet, excluding the six that the monthly push sends". If the monthly push sends
   a fixed six, that list needs naming.
2. **Does practice ever repeat a cleared question?** Today: no, until the whole
   tier is answered correctly, and then the bot says so and serves the
   least-repeated one. If revision-by-repetition is wanted instead, that is one
   query in `bot/db.py`.
3. **Should the Clinical and Post-MBBS banks have their own notes?** The sheets in
   the repo are topic-based and shared across tiers today.

---

## 10. Access you need

| Thing | Who has it |
|---|---|
| DigitalOcean droplet + SSH key | the account holding the LKC OphSoc key |
| Telegram bot token | @BotFather, under whoever created `@acuity123_bot` |
| GitHub repo | [github.com/hongpenggg/acuity123-bot](https://github.com/hongpenggg/acuity123-bot) |

Hand the bot token on through BotFather's transfer, not by pasting it about — and
rotate it (`/revoke`) if it ever lands in a chat log.
