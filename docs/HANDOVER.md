# Handover — what exists, and how to finish it

Everything here is written for whoever picks this up next. No prior context needed.

**Status at handover.** 283 tests, four CI steps, six migrations. All three banks
are loaded — **Pre-Clinical** 120
questions, **Clinical** 103, **Post-MBBS** 170, so 393 in total, every one with a
written explanation and none of them needing the LLM.
[§2](#2-adding-a-question-bank) is the step-by-step for regenerating a bank or
adding a fourth level. Everything else is either working or listed as still open
in [§7](#7-what-was-built-and-what-is-still-open).

> **One caveat, and it is the easiest thing here to forget.** The M3 document
> holds 170 questions but only **152** are loaded. Eighteen are built around an
> embedded clinical photograph and the bot sends text-only question cards, so the
> generator holds them back. All eighteen name the picture in their own stem, so
> none is answerable as written: each needs the image uploaded or the stem
> rewritten. Full list and both routes:
> [§2.6](#26-the-eighteen-clinical-questions-that-are-held-back).

---

## 1. What is running

```
Telegram      @lkceyebot  ("LKC OphSoc Bot")
Droplet       DigitalOcean, Singapore (sgp1), 1 GB RAM, Ubuntu 24.04
              IP in the DigitalOcean account (the one holding the LKC OphSoc key)
Service       systemd unit `studybot`, running as user `deploy` from /opt/studybot
Database      PostgreSQL on the same box — role `studybot` owns db `studybot`
Backups       nightly pg_dump 17:04 into /var/backups/studybot (14-day rotation)
```

Working now: `/quizme` (a set of five, options numbered 1–5, score at the end),
`/notes` (the sheet progression), `/review`, `/stats`, `/resources`,
`/changestreams`, `/weeklyquiz`, `/subscribenotes`, `/stopweekly`, `/stopmonthly`,
`/tournament`, `/leaderboard`, and the four `/admin_*` commands. The older names
(`/practice`, `/subscribe`, `/notes_sub`, `/level` and so on) still work as
aliases.

Only IDs in `ADMIN_IDS` can run admin commands.

---

## 2. Adding a question bank

All three levels now have one, so this is the procedure for **regenerating** a
bank from edited sources, or adding a fourth level. It is **four steps**, and only
§2.3 touches code.

### 2.1 Get the questions written in the right format

The generator parses a `.docx` (or two) laid out like this. Anything it cannot
account for, it refuses to write — so a malformed file fails loudly rather than
silently dropping a question.

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
- A trailing "Coverage and Resources" / "Resource Guide" / "Clinical Sources and
  References" section is fine; the parser stops there (`STOP_RE`).

The six documents already in the repo disagree on nearly every detail of this
layout, and the parser tolerates all of it — so a new document does not have to
match any one of them exactly:

| Varies | Accepted |
|---|---|
| Header | `Question 1`, `Question 01`, `Sample question 01` |
| Field separator | `Topic: X` and `Topic  X` |
| Options header | `Options:`, `Options`, or **absent** — the first `a)`/`A)` line opens the list |
| Option letters | `a)`–`e)` or `A)`–`D)` |
| Question type | the `Question type:` label, or an unlabelled `Part 1 \| Anatomy \| applied inference` line under the header |
| After the answer | the **first** paragraph is the explanation; any further paragraphs are treated as a reading list and dropped |

One thing it will not accept silently: a question whose block contains an
**inline image**. Those are flagged and held back from the seed, because the bot
sends text-only question cards — see
[§2.6](#26-the-eighteen-clinical-questions-that-are-held-back).

### 2.2 Put the file in the repo

```bash
cp "Clinical_Ophthalmology_MCQs.docx" resources/questions/
```

### 2.3 Register it and regenerate

Edit `LEVELS` in `tools/build_question_seed.py` — add the file to that level's
list. All three are populated today:

```python
LEVELS = {
    "preclin":  ("01", [Preclinical_Ophthalmology_20_MCQs.docx,
                        Preclinical_Ophthalmology_100_Additional_MCQs.docx]),
    "clin":     ("02", [Clinical_Ophthalmology_M3_M5_20_Case_MCQs.docx,
                        Clinical_Ophthalmology_M3_M5_100_Additional_Cases_Q21_Q120.docx]),
    "postmbbs": ("03", [FRCOphth_Post_MBBS_20_Sample_MCQ.docx,
                        FRCOphth_Post_MBBS_150_Additional_MCQ_Q21_Q170.docx]),
}
```

Question numbering has to run `1..N` across a level's files together, not restart
per file — the two documents in each pair already do (`1..20` then `21..120`).

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

### 2.6 The eighteen clinical questions that are held back

**This is the one piece of loaded content that is deliberately incomplete, so it
is the thing most likely to be forgotten.** The M3 document holds 170 questions.
Eighteen of them are built around an embedded clinical photograph, and
`bot/sender.py` sends question cards as **text messages** — there is no code path
that uploads an image with a question. A student would be asked to interpret a
photograph that never arrives, so the generator holds them back and the loaded
clinical bank is **152, not 170**.

They are not dropped silently. `parse_document` flags any question whose block
contains an inline image (`graphicData` in the paragraph XML — the detection is
structural, so it cannot drift out of step with the document), validates it like
every other question, and then `build_level` removes it and prints exactly which:

```
held back 18 figure-dependent question(s): Q39, Q44, Q63, Q81, Q86, Q94, Q95,
Q96, Q101, Q102, Q107, Q113, Q124, Q130, Q148, Q156, Q168, Q169
```

| # | Topic | How the stem refers to it |
|---|---|---|
| Q39 | Red eye cornea and uveitis | "Clinical photograph from the ophthalmology Anki collection" |
| Q44 | Red eye cornea and uveitis | "Clinical photograph from the ophthalmology Anki collection" |
| Q63 | Glaucoma | "Clinical photograph from the ophthalmology Anki collection" |
| Q81 | Glaucoma | "Clinical photograph from the ophthalmology Anki collection" |
| Q86 | Retina macula and vitreous | "The attached photograph shows a dark collection of blood..." |
| Q94 | Retina macula and vitreous | "photograph shows sharply defined yellow deposits" |
| Q95 | Retina macula and vitreous | "The fundus photograph is shown" |
| Q96 | Retina macula and vitreous | "Fundus photograph" |
| Q101 | Retina macula and vitreous | "The fundus photograph shows widespread retinal pallor..." |
| Q102 | Retina macula and vitreous | "The attached photograph shows a sharply regional area of retinal whitening" |
| Q107 | Retina macula and vitreous | "The attached photograph shows yellow macular drusen" |
| Q113 | Retina macula and vitreous | "as shown in the photograph" |
| Q124 | Neuro ophthalmology and orbit | "the attached photograph shows a radial arrangement of hard exudates" |
| Q130 | Neuro ophthalmology and orbit | "fundus photograph shows both optic discs" |
| Q148 | Lens lids lacrimal and paediatric eye | "in the figure" |
| Q156 | Lens lids lacrimal and paediatric eye | "The figure shows a central ulcer with a raised, pearly rolled edge" |
| Q168 | Lens lids lacrimal and paediatric eye | "the family notices a white pupil in photographs" |
| Q169 | Lens lids lacrimal and paediatric eye | "Figure for this question" |

**All eighteen name the picture in their own stem**, so unlike the bank this
replaced there is no caption-only group to rescue: each one needs the image
uploaded or its stem rewritten. The quick win that the old bank offered is not
there, and the honest figure is 152 of 170.

#### Two ways to finish them

1. **Upload the figure with the card.** Extract the images to
   `resources/questions/figures/`, carry the reference through the seed (a
   `questions.figure` column, or a naming convention keyed on question id), and
   teach `sender.send_question` to `send_photo` first. Note the Bot API caps a
   photo caption at 1024 characters and these cards routinely exceed that, so it
   has to be photo-then-text, and the answer buttons must stay attached to the
   text message.
2. **Rewrite the eighteen stems** so the finding is described in words
   ("diffuse haemorrhage in all four quadrants with a swollen disc"). Cheaper,
   loses the image-interpretation skill the cases were written to test.

> **Copyright, before either route.** The document's own source appendix lists
> what its material is drawn from: a senior "Eye" document credited as *largely
> adapted from Jin Wei*, `Mega-WITI (M3 20_21)`, an `Eye M3 EOP Quiz 1 Stream`
> PDF, and an Anki deck. The content policy in the README and `schema.sql` keeps
> iRAT/tRAT, AMBOSS, PassMedicine and school or senior material out of this repo
> until it has been rewritten — rewritten prose is fine, an unmodified photograph
> is not. The four questions captioned *"Clinical photograph from the
> ophthalmology Anki collection"* are the clearest example. Clear the images with
> the content team before shipping them, whichever route you take. The stems and
> explanations are the society's own written work and are not affected.

#### Re-enabling them

One line, `tools/build_question_seed.py`:

```python
SKIP_FIGURE_QUESTIONS = False
```

Then `python tools/build_question_seed.py --level clin`, and update the
`EXPECTED["clin"]` count and topic split in the same file plus `TOPICS["clin"]`
in `tests/test_seed_data.py` — all three assert 152 today and will fail loudly,
which is the intended behaviour rather than something to work around.

---

## 3. Adding revision sheets

The sheets are PDFs; the bot sends the file itself, so there is nothing to host.

Sheets are **per level**, so the folder you drop a PDF into is what decides who
sees it:

```
resources/notes/<level>/tier_a/   one broad sheet per topic        01_<Topic>.pdf
resources/notes/<level>/tier_b/   deeper sheets on single points   B01_<Topic>.pdf

                         tier_a   tier_b   codes
  preclin                     6       20   01-06      / B01-B20
  clin                        6       36   A01-A06    / B01-B36
  postmbbs                   15       65   A01-A15    / B01-B65
```

To add one: name it `B41_Some_Topic.pdf` (`CODE_Name_With_Underscores.pdf`), put
it in the right level folder, commit it, and `git pull` on the server. **That is
the whole process** — the catalogue is built by scanning the directory, so there
is no code change and no database row.

Two things to know before you touch this:

- **Codes repeat across levels.** `B01` exists three times. Every lookup in
  `bot/resources.py` takes a level for that reason, and a bare number like `14`
  only matches within the student's own level. A code spelled with its letter
  (`A01`) is taken literally, so it will not fall through to another level's `01`.
- **The filename is the student-facing title.** `B01_Clinical_Type_B_Vision_...`
  would have been shown as "Clinical Type B Vision ...", so that shorthand was
  stripped when the clinical sheets were imported. Students see **"Overview"**
  and **"Focused"**; "Tier A / Tier B" is internal and never shown.

`tests/test_resources.py` fails if a file is misnamed or missing, if `B01` stops
resolving to three different sheets, or if the preclinical and clinical overview
sheets stop matching their question topics. Post-MBBS has eighteen question
topics against fifteen overview sheets, so those are pinned rather than matched
(`test_post_mbbs_sheets_do_not_line_up_with_its_topics` explains why).

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
(`ACUITY_CREDIT`, `SOCIETY_CREDIT`, `DISCLAIMER`) and the `/start` screen in
`bot/handlers.py` (`TAGLINE`, `FUNCTIONS`, and `welcome()` which assembles them).
Editing either needs a `git pull` + restart.

Four rules are enforced by tests, so a well-meant edit can fail CI. Each exists
because the thing it forbids actually shipped:

- **No em dashes** anywhere a student can see
  (`tests/test_handlers.py::test_no_em_dashes_in_anything_students_see`). Use
  `->` or a comma, as the existing copy does.
- **Every `/command` named in the welcome must be registered, and be in the
  menu** (`test_welcome_only_names_commands_that_exist`). It walks the router
  rather than a hand-kept list. A draft of the welcome once advertised
  `/subscribeqn`, `/subscribenotes`, `/unsub_qns` and `/unsub_notes`, none of
  which existed; a student tapping one would have got silence.
- **Any message containing markup must pass `parse_mode="HTML"`**
  (`test_html_is_never_sent_without_parse_mode`). Telegram does not guess: it
  shows the literal `<b>`. This test originally inspected only method calls, so
  every bare `safe_send(...)` in `bot/` escaped it and the tournament
  announcement went out with visible tags.
- **The sheet catalogue must match what is on disk** (`tests/test_resources.py`).
  Adding a sheet is dropping a PDF in and committing it, so a misnamed file fails
  here rather than quietly disappearing from the list students see.

If you are changing copy, the fastest check is not to read it. Drive the real
handlers against a real database with a fake bot and print what comes out, the
way `tests/test_handlers.py` does. Several bugs that survived careful reading
were obvious on the first render.

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

**After a schema change, run the migrations.** `schema.sql` builds a *fresh*
database; an existing one is brought forward by the numbered files in
`migrations/`, in order. They are idempotent, so running them all is always safe
and is the correct response to not being sure which have been applied:

```bash
cd /opt/studybot
for f in migrations/*.sql; do
    psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -v ON_ERROR_STOP=1 -f "$f"
done
```

| Migration | What it adds | Needed by |
|---|---|---|
| `001` | `level` columns, `tournament_answers` | levels, tournament dedup |
| `002` | `questions.tag` | the per-question-type half of `/stats` |
| `003` | `note_deliveries` | `/notes` as a progression, the fortnightly drop |
| `004` | `'review'` as an answer mode | `/review`, and keeping it out of scoring |
| `005` | one scoring answer per question; `level` CHECKs | two live cards for one question |
| `006` | one active tournament | two concurrent `/admin_tournament_start` taps |

`005` reclassifies any existing duplicate answers rather than deleting them, and
both `005` and `006` abort with a readable message if the data already breaks
the rule they are about to enforce. Nothing is applied half-way: each runs in a
transaction.

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
| Sets of five: topic-diverse, adaptive, soft to leave, score at the end | `db.pick_question` / `db.current_set` / `db.last_set_score`, `sender.send_question_for_level`, `handlers._report_set_if_finished` |
| Marker A: the running total, by topic and by question type | `db.stats`, behind `/stats` |
| Marker B: the tournament-window score, shown as standings only | `db.tournament_mark`, surfaced by `/tournament` and `/leaderboard` |
| Tournament auto-enrolment and an announcement to every user | `db.enrol_everyone` / `db.all_users`, `jobs.announce_tournament`, wired into `/admin_tournament_start` and `/start` |
| `/weeklyquiz` (a set every Monday) and `/subscribenotes` (the next few sheets, 1st and 15th) | `jobs.weekly_quiz`, `jobs.fortnightly_notes` |
| Topic-diverse, adaptive question selection | `db.pick_question`, behind `sender.send_question_for_level` |
| `/notes` as a progression, and `/review` for missed questions | `db.notes_delivered` / `db.record_notes_sent`, `db.wrong_questions` |
| Per-student delivery history, so no sheet is ever sent twice | `note_deliveries`, `handlers._deliver` |

All three question banks are loaded as of this update: Pre-Clinical 120,
Clinical 103, Post-MBBS 170. The parser was widened to take the clinical and
FRCOphth layouts (§2.1), and `tests/test_seed_data.py` now checks all three.

Still open, and nothing here is blocked by anything else:

| Open | Notes |
|---|---|
| **Content is shipped by git, not by the content team** | The biggest architectural limitation here: every question, sheet and line of copy is baked into the deploy artifact, so changing any of it is a developer task. Three incremental steps out of it, and the traps to avoid, in [§11](#11-the-content-pipeline-needs-to-stop-being-the-git-repo) |
| **Eighteen clinical questions need their photographs** | The clinical bank is 152 of 170 questions: the rest are built around an embedded photograph the bot cannot send, and every one of them names it in the stem, so none stands alone as text. Each needs `sender.send_question` to upload a figure, or a rewritten stem. The images are drawn from M3 Anki, quiz-PDF and senior-note material, so the README's content policy has to be cleared first. Everything — the list, both routes, and the one-line switch — is in [§2.6](#26-the-eighteen-clinical-questions-that-are-held-back) |
| **Dropping the old command names** | `/practice`, `/subscribe`, `/unsubscribe`, `/notes_sub`, `/notes_unsub` and `/level` still work as aliases so nothing in a student's existing chat breaks. They are the extra names on each `Command(...)` decorator |
| **Nothing shows a student their question history** | Intentional for marker B: other students cannot see anyone's activity. Every answer is in `attempts` if a per-question view is ever wanted |
| **A restart can skip a weekly push** | APScheduler computes `next_run_time` from boot with the default in-memory job store, so a restart at 09:05 on a Monday schedules the push for the *following* Monday, silently. Both fixes are costed in the comment at `jobs.register`; neither looked worth the dependency |
| **A shutdown can truncate a fan-out** | `main.py` drains for up to 5s before the scheduler goes down. Subscribers not reached keep their queue, so the cost is a fortnight's nudge rather than a sheet |

### What four audits found, and why it is worth knowing

After the banks landed, four read-only audits went over the scheduled jobs, the
message copy, the SQL and the handler flows. They found 20-odd real defects and
every one was reproduced before it was fixed. The pattern is worth carrying
forward, because it will repeat:

- **Numbers that were right but described wrongly.** `/admin_weekly_now` reported
  "Sent 15 question(s)" for a five-question set, because the total was summed
  across subscribers while the sentence read as one student's. Arithmetic is not
  the hard part; saying what a number counts is.
- **Code with no test at all.** `weekly_quiz` and `fortnightly_notes` fan out to
  every subscriber and had zero coverage, which is how both of the bugs a live
  user hit got there. `tests/test_jobs_live.py` exists now.
- **Guards with holes.** The test that forbids unmarked HTML read only method
  calls, so every bare `safe_send(...)` in `bot/` went unchecked, and the
  tournament announcement shipped literal `<b>` tags to every user.
- **Copy that outlived its code.** A docstring still described sets as a fixed
  benchmark months after they became adaptive. Treat a comment that disagrees
  with the code as a bug report.
- **Races that only a database can settle.** Two admin taps opened two
  tournaments; two job runs sent the same PDF twice. Check-then-act in Python
  does not survive concurrency, and this bot answers updates as tasks.

---

## 8. Where things are

```
bot/config.py       credit block, levels, tuning, repo links
bot/commands.py     the Telegram command menu and its per-chat scopes
bot/handlers.py     commands and callbacks (the /start copy is here)
bot/db.py           every SQL statement: question selection, sets, notes, tournament
bot/resources.py    the note catalogue, built by scanning resources/notes
bot/text.py         rendering, option numbering, HTML-safe splitting (pure)
bot/sender.py       outbound Telegram: retry, rate limiting, uploads, outstanding cards
bot/jobs.py         the scheduled pushes, tournament closing, single-flight guards
bot/main.py         wiring, the error handler, graceful shutdown and drain
schema.sql          a fresh database
migrations/         bringing an existing one forward, 001 to 006
seeds/              the question banks, one file per level
resources/          the .docx sources and 148 note PDFs, per level
tools/              regenerates the seeds from the .docx files
scripts/check_sql.py  parses every statement with the real PostgreSQL grammar
docs/SETUP.md       full server runbook, troubleshooting table, event-day checklist
```

**The tests, 283 of them.** `pytest` runs everything; the live-database ones skip
unless `TEST_DATABASE_URL` is set, and CI provides one.

| File | Covers |
|---|---|
| `test_text.py` | rendering, numbering, masking, HTML-safe splitting |
| `test_handlers.py` | every command and callback, against in-memory fakes |
| `test_sender.py` | retry, flood control, the rate limiter |
| `test_sender_live.py` | outstanding cards, against a real database |
| `test_db_live.py` | the SQL, the constraints, the tournament dedup |
| `test_sets_live.py` | set arithmetic, the adaptive picker, the review pile |
| `test_jobs_live.py` | both scheduled fan-outs |
| `test_seed_data.py` | the three loaded banks, rendered through the real path |
| `test_resources.py` | the catalogue against what is on disk |
| `test_llm.py` | the optional explanation fallback |

**Push a branch and let CI run** rather than relying on a local pass: CI stands up
a real PostgreSQL, so the live suites actually execute there.

---

## 9. Open questions

1. **Is one sheet per fortnightly drop the right pace?** `jobs.SHEETS_PER_DROP`
   is one constant. At one, preclinical (26 sheets) takes about a year and
   post-MBBS (80) far longer, so for most students `/notes` on demand will be the
   real route and the drop is a nudge. Raising it is a one-line change; the
   earlier six felt like a wall of PDFs in a phone client, which is why it is one.
2. **Is a set of five the right size?** One constant (`db.SET_SIZE`). Pre-Clinical
   is 24 sets, Clinical 21, Post-MBBS 34. A bigger set is a one-line change and
   progress carries over, because a set is derived from the answer count rather
   than stored.
3. **Should the Clinical and Post-MBBS banks have their own sheets?** The
   catalogue is per level now, and Pre-Clinical and Clinical overview sheets match
   their question topics one-to-one. Post-MBBS does not: eighteen question topics
   against fifteen overview sheets, organised differently
   (`test_post_mbbs_sheets_do_not_line_up_with_its_topics` pins what is actually
   there and explains why).
4. **Should a student be able to start the sheets again?** `db.reset_notes` is
   written and tested but nothing calls it. Someone who has finished their level
   has no way to re-read the set as a course.
5. **Should `/review` ever expire?** The pile is "every question whose latest
   answer was wrong", with no sense of when. A question missed in September and
   never revisited sits there forever alongside one missed yesterday.

---

## 10. Access you need

| Thing | Who has it |
|---|---|
| DigitalOcean droplet + SSH key | the account holding the LKC OphSoc key |
| Telegram bot token | @BotFather, under whoever created `@lkceyebot` |
| GitHub repo | [github.com/hongpenggg/acuity123-bot](https://github.com/hongpenggg/acuity123-bot) |

Hand the bot token on through BotFather's transfer, not by pasting it about — and
rotate it (`/revoke`) if it ever lands in a chat log.

---

## 11. The content pipeline needs to stop being the git repo

**This is the biggest architectural limitation in the project, and it is worth
fixing before the content team grows.**

### What happens today

Every piece of content is baked into the deploy artifact:

| Content | Where it lives | To change it |
|---|---|---|
| Question banks | `.docx` in `resources/questions/` → `tools/build_question_seed.py` → `seeds/*.sql` | edit the .docx, regenerate, commit, `git pull` on the server, `psql -f` the seed |
| Revision sheets | 148 PDFs in `resources/notes/<level>/tier_<a\|b>/` | commit the PDF, `git pull` on the server |
| Bot copy | string constants in `bot/config.py` and `bot/handlers.py` | commit, `git pull`, `systemctl restart studybot` |

`sender.send_note` uploads the file straight from the deployed working copy, and
falls back to a `raw.githubusercontent.com` link built from `REPO_SLUG` /
`REPO_REF` when a file is missing or too large.

### Why that is a problem

- **The content team cannot ship anything without a developer.** Writing a new
  cheat sheet is the society's work; `git pull` on a DigitalOcean droplet is not.
  Every sheet, every typo fix and every new question is a developer task today.
- **Content changes need a deploy.** A question bank reload is a manual `psql`
  against production. There is no way to preview, stage or roll back one sheet.
- **The repo carries the payload.** It is roughly 17 MB of PDFs and `.docx`
  already, and the M3 document alone is 1.4 MB of embedded images, for eighteen
  questions that are not even loaded (§2.6). This only grows.
- **The fallback links assume a public repo.** If `acuity123-bot` is ever made
  private, every `raw.githubusercontent.com` fallback silently 404s for students,
  and nothing in the code notices.
- **Nothing is per-environment.** Staging and production read the same files from
  the same commit, so there is no way to try content on a test bot first.

### What "dynamic" should mean

Content lives **outside** the deploy artifact, the bot reads it at runtime, and a
non-developer can change it without a release.

Three steps, smallest first, each useful on its own:

1. **Cache Telegram's own `file_id`s.** The first time a sheet is uploaded,
   Telegram returns a `file_id`; store it and resend by id instead of re-uploading
   the bytes. Cheap, no new infrastructure, and it makes the fortnightly fan-out
   dramatically faster. Caveat worth knowing: **`file_id`s are per bot**, so the
   move to `@lkceyebot` invalidates any that were cached under the old token.
   This speeds delivery up but does not solve authoring.

2. **Move the catalogue into the database.** `schema.sql` already creates a
   `notes` table with `level`, `tier`, `topic`, `title` and `body` — and
   **nothing has ever inserted a row into it** (`bot/db.py` has
   `note_topics`/`get_notes`/`all_notes` ready and `handlers.notes` already falls
   through to the PDFs when it is empty). Repurpose it: swap `body` for a
   storage URL plus the cached `file_id`, point `bot/resources.py` at the table
   instead of `Path.glob`, and keep the directory scan only as a local-development
   fallback. The level/tier/code model the catalogue uses now maps onto it
   directly.

3. **Let an admin upload.** `/admin_addnote` taking a Telegram document: store
   the file in object storage (Supabase Storage is already an option in the
   deployment appendix, and S3 or Cloudflare R2 are equivalent), write the `notes`
   row, cache the `file_id`. At that point the society ships content by sending the
   bot a PDF, and the repo holds code only.

For the question banks the same logic applies but the generator earns its keep:
keep `.docx` authoring and the strict parser (it is what catches a mis-lettered
answer), but make the import something an admin triggers against the database
rather than a `psql` command someone runs over SSH.

### What to be careful about

- Keep the **validation**. The reason the question pipeline is trustworthy is that
  `tools/build_question_seed.py` refuses to emit anything it cannot account for
  and `tests/test_seed_data.py` checks the loaded result. A dynamic upload path
  must run the same checks, or it is a downgrade dressed up as a feature.
- Keep a **local fallback** so the test suite and a developer laptop do not need
  network or credentials. `tests/test_resources.py` asserts the catalogue matches
  what is on disk; that test is cheap insurance and should survive in some form.
- PDFs are **student-facing medical content**. Whatever replaces the repo needs
  the same review gate the content policy describes, not a free-for-all upload.
