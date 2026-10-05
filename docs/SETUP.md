# Setup — from nothing to a running bot

Everything below is copy-pasteable. Do the phases in order; each one ends with a
verification step, so you never carry a broken step forward.

**Target topology**

```
        students (Telegram)
                │  long polling (outbound only)
                ▼
   Contabo VPS · Ubuntu 24.04 LTS · Singapore region
   ┌──────────────────────────────────────────────┐
   │  systemd: studybot.service                   │
   │    └── python -m bot.main   (one process)    │
   │                                              │
   │  postgresql.service (localhost only)         │
   │    └── db: studybot · role: studybot         │
   └──────────────────────────────────────────────┘
                │
                └── optional: LLM provider for questions with no written
                    explanation. The preclinical bank ships with explanations,
                    so this is off by default and costs nothing.
```

**Time:** about an hour for phases 1–3, minus waiting for the VPS to provision.

**You will need:** a credit card for Contabo, a Telegram account, and the numeric
Telegram ID of whoever will be an admin.

---

## Phase 0 — Before you touch the server

Have these ready:

| Thing | Where | Needed for |
|---|---|---|
| Contabo account | contabo.com | The VPS |
| A bot token | [@BotFather](https://t.me/BotFather) → `/newbot` | Phase 3 |
| Your numeric Telegram ID | [@userinfobot](https://t.me/userinfobot) → send it anything | Phase 3 |
| An SSH key on your laptop | `ssh-keygen -t ed25519` | Phase 1 |

> **BotFather in advance (2 minutes).** Message @BotFather, send `/newbot`, give
> it a display name and a username ending in `bot`. It replies with a token like
> `1234567890:AAE...`. Treat it as a password — anyone with it controls the bot.

> **Get real numeric IDs.** @userinfobot replies with something like
> `Id: 5123456789`. That number — not your `@username` — goes in `ADMIN_IDS`.

---

## Phase 1 — The VPS

> **Shortcut.** `deploy/provision.sh` in this repo does all of Phase 1 and most of
> Phase 2 unattended: swap, updates, timezone, the `deploy` user, SSH hardening
> (validated with `sshd -t` before it restarts the daemon), ufw, Fail2ban,
> PostgreSQL tuned for the RAM it finds, the repo in a virtualenv, and the systemd
> unit enabled but not started. It is idempotent, so re-running it is the correct
> response to a failure part-way through.
>
> ```bash
> ssh root@YOUR.IP 'bash -s' < deploy/provision.sh
> ```
>
> Then go to Phase 2.4 (the `.env` and the database load) — the script stops
> before starting the service precisely so the bot never crash-loops on a missing
> token. The manual steps below are what it does, and are worth reading once.

### 1.1 Order it

1. Contabo → **Cloud VPS 10** (4 vCPU / 8 GB RAM / 75 GB NVMe). Around
   €4–6/month depending on the billing term; a one-month term costs more than a
   twelve-month one.
2. **Region: Singapore.** Latency to your users matters more than anything else
   on this list.
3. **OS: Ubuntu 24.04 LTS** (or the current LTS). Choose 64-bit.
4. Set a root password at checkout, and add your SSH **public** key if the form
   offers it. Confirm the price at checkout — Contabo also charges a one-off
   setup fee on this plan.

### 1.2 First login

Contabo emails you the IP. Log in as root:

```bash
ssh root@YOUR.SERVER.IP
```

**Verify:** you get a shell prompt. `cat /etc/os-release` shows Ubuntu.

### 1.3 Update and install the basics

```bash
apt update && apt upgrade -y
apt install -y git python3-venv python3-pip fail2ban ufw unattended-upgrades

timedatectl set-timezone Asia/Singapore
timedatectl                      # Verify: shows Asia/Singapore

reboot
```

Reconnect after the reboot if a kernel was installed.

> The timezone matters: the weekly question fires at 09:00 and the displayed
> tournament dates are formatted in this zone.

### 1.4 Create a deploy user

Never run the bot as root.

```bash
adduser deploy
usermod -aG sudo deploy
```

### 1.5 SSH key login

On **your laptop**, not the server:

```bash
ssh-keygen -t ed25519            # skip if you already have a key
ssh-copy-id deploy@YOUR.SERVER.IP
ssh deploy@YOUR.SERVER.IP        # Verify: logs in with no password prompt
```

### 1.6 Harden SSH

**Keep your existing root session open** until this is verified, or you can lock
yourself out.

```bash
sudo nano /etc/ssh/sshd_config
```

Set (or uncomment and change) these two lines:

```
PermitRootLogin no
PasswordAuthentication no
```

Cloud images sometimes override settings in a drop-in directory, so check what
the daemon will actually use before restarting:

```bash
sudo ls /etc/ssh/sshd_config.d/
sudo sshd -T | grep -E "permitrootlogin|passwordauthentication"
```

Both must read `no`. Then:

```bash
sudo systemctl restart ssh
```

**Verify from a second terminal, without closing the first:**
`ssh deploy@YOUR.SERVER.IP` still works, and `ssh root@YOUR.SERVER.IP` is refused.

### 1.7 Firewall

The bot only makes outbound connections, so SSH is the only inbound port you need.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable                   # type y
sudo ufw status verbose
```

**Verify:** `Status: active`, and `22/tcp ALLOW` present. Do **not** open 5432 —
Postgres stays on localhost.

### 1.8 Fail2ban and unattended upgrades

```bash
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd   # Verify: shows a jail

sudo dpkg-reconfigure -plow unattended-upgrades   # choose "Yes"
```

### 1.9 App directory

```bash
sudo mkdir -p /opt/studybot
sudo chown deploy:deploy /opt/studybot
```

**Phase 1 done.** You should now only be able to log in as `deploy` with a key.

---

## Phase 2 — The database

Postgres runs on the same box. This is deliberate: the workload is tiny, and it
removes the pooler, the free-tier project pause and the 500 MB cap from your
event-day risk. If you'd rather use Supabase, see
[the appendix](#appendix--using-supabase-instead).

### 2.1 Install

```bash
sudo apt install -y postgresql postgresql-contrib
sudo systemctl status postgresql   # Verify: active (running)
```

Ubuntu binds Postgres to localhost and allows password logins from 127.0.0.1 out
of the box, so **you do not need to touch `pg_hba.conf` or `postgresql.conf`.**
Leave `listen_addresses` as it is.

### 2.2 Role and database

Pick a real password and keep it — you'll paste it into `.env` next.

```bash
sudo -u postgres psql -c "create role studybot login password 'CHANGE_ME_STRONG';"
sudo -u postgres createdb -O studybot studybot
```

`-O studybot` makes the bot the **owner** of the database. That matters: the
schema enables row-level security with no policies (harmless for Supabase, but a
trap if the bot connects as a role that isn't the owner), and an owner bypasses
RLS. Keep it this way.

**Verify:**

```bash
psql "postgresql://studybot:CHANGE_ME_STRONG@127.0.0.1:5432/studybot" -c "select current_user, current_database();"
```

### 2.3 Load the schema and the question bank

```bash
cd /tmp
git clone https://github.com/hongpenggg/acuity123-bot.git
cd acuity123-bot

export DATABASE_URL="postgresql://studybot:CHANGE_ME_STRONG@127.0.0.1:5432/studybot"

psql "$DATABASE_URL" -f schema.sql
for f in seeds/*.sql; do echo "--- $f"; psql "$DATABASE_URL" -f "$f"; done
```

**Verify — this is the step to be fussy about:**

```bash
psql "$DATABASE_URL" -c "select level, count(*) from questions group by level order by level;"
```

Expected:

```
  level   | count
----------+-------
 preclin  |   120
```

And spot-check one question renders its five options:

```bash
psql "$DATABASE_URL" -c "select jsonb_array_length(options) as options, correct_idx, left(text, 60) from questions limit 4;"
```

If you see an error mentioning `already present`, the seed was already loaded —
that's the double-load guard, not a failure.

### 2.4 Backups

The database is now your only copy of the question bank. Set up a nightly dump
with a two-week rotation:

```bash
sudo mkdir -p /var/backups/studybot
sudo chown postgres:postgres /var/backups/studybot

sudo -u postgres crontab -l 2>/dev/null | { cat; echo '17 4 * * * pg_dump -Fc studybot > /var/backups/studybot/studybot-$(date +\%F).dump'; } | sudo -u postgres crontab -
sudo -u postgres crontab -l      # Verify: the line is there
```

**A dump on the same machine is not a backup.** Get it off the box. Cheapest
option that works: an rclone remote (Google Drive is free and you already use it).

```bash
sudo -u deploy -s                       # work as deploy
curl https://rclone.org/install.sh | sudo bash
rclone config                           # create a remote named "gdrive"
exit

# then add to deploy's crontab
crontab -e
```

```cron
30 4 * * * rclone copy /var/backups/studybot gdrive:studybot-backups --max-age 24h
```

`/var/backups` is root-owned and `deploy` can't read it, so either `chmod 755`
the directory or (better) run the rclone step as a root cron entry:

```bash
sudo crontab -e
```

```cron
30 4 * * * rclone copy /var/backups/studybot gdrive:studybot-backups --max-age 24h
```

**Restore drill — do this once, now, not during an incident.** A backup you have
never restored is a rumour:

```bash
psql "$DATABASE_URL" -c "create database restore_test;"
psql "postgresql://studybot:...@127.0.0.1:5432/restore_test" -c "select 1;"
pg_restore -d "postgresql://studybot:...@127.0.0.1:5432/restore_test" /var/backups/studybot/studybot-$(date +%F).dump
psql "postgresql://studybot:...@127.0.0.1:5432/restore_test" -c "select count(*) from questions;"
psql "$DATABASE_URL" -c "drop database restore_test;"
```

(You may need to `grant`/`create` the test DB as the postgres superuser first.)

---

## Phase 3 — Get the bot running

### 3.1 Deploy the code

```bash
sudo mkdir -p /opt/studybot && sudo chown deploy:deploy /opt/studybot

# as deploy
git clone https://github.com/hongpenggg/acuity123-bot.git /opt/studybot
cd /opt/studybot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> If the repo stays private, add a read-only **deploy key**: on the server run
> `ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""`, print the `.pub`, and add
> it under *Repo → Settings → Deploy keys*. Then
> `printf 'Host github.com\n  IdentityFile ~/.ssh/deploy_key\n' >> ~/.ssh/config`.

### 3.2 Configure

```bash
cd /opt/studybot
cp .env.example .env
nano .env
chmod 600 .env
```

Fill in:

```ini
BOT_TOKEN=1234567890:AAE...your token from BotFather
DATABASE_URL=postgresql://studybot:CHANGE_ME_STRONG@127.0.0.1:5432/studybot
ADMIN_IDS=5123456789

# Leave the LLM blank. The bank has written explanations, so there is nothing to pay for.
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=
LLM_MODEL=
```

`ADMIN_IDS` is a comma-separated list if several people need admin rights.

**Verify the bot can read its config without starting:**

```bash
.venv/bin/python -c "from bot.config import BOT_TOKEN, DATABASE_URL, ADMIN_IDS, LLM_ENABLED; print(bool(BOT_TOKEN), bool(DATABASE_URL), sorted(ADMIN_IDS), LLM_ENABLED)"
```

Expect `True True [5123456789] False`.

### 3.3 Smoke test before you daemonise

```bash
.venv/bin/python -m bot.main
```

Expect a line like `polling started (tz=Asia/Singapore)`. In Telegram, message
your bot:

1. **`/start`** → the credit block, your level (Pre-Clinical), the command list.
2. **`/quizme`** → a set of five questions, options numbered 1–5.
3. Tap an answer → ✅ or ❌ with the correct option, then **💡 Explain** and **Next**.
4. **`/changestreams`** → set it to Clinical. `/quizme` now says the Clinical bank is
   still being written and tells you what *is* available. Set it back.
5. **`/tournament`** → "No tournament is running right now." (expected — you'll
   start one in Phase 4.)

Press `Ctrl+C` when it works.

> **Check the database saw your answers:**
> ```bash
> psql "$DATABASE_URL" -c "select user_id, topic, correct, mode from attempts order by id desc limit 5;"
> ```

### 3.4 Run it as a service

```bash
sudo cp /opt/studybot/deploy/studybot.service /etc/systemd/system/studybot.service
sudo systemctl daemon-reload
sudo systemctl enable --now studybot
sudo systemctl status studybot            # Verify: active (running)
journalctl -u studybot -f                 # live logs; Ctrl+C to stop watching
```

The unit already declares `After=postgresql.service`, so the bot waits for the
database at boot instead of crash-looping.

**Verify it survives a reboot** — the step people skip and regret:

```bash
sudo reboot
# wait ~30s, reconnect
systemctl is-active studybot     # Verify: active
journalctl -u studybot -n 20     # Verify: "polling started"
```

Then send `/quizme` in Telegram to confirm end to end.

---

## Phase 4 — Configure each part

### 4.1 The credit block

Shown on `/start` and `/help`. Edit `CREDIT` in `bot/config.py`, push, and pull
on the server. Today it reads "LKC Ophthalmology Society / Made by Zhong Han … /
and the Acuity Team". The disclaimer line is `DISCLAIMER` in the same file.

Adding or changing text here needs a redeploy (Phase 5.1).

### 4.2 Levels

Three levels exist: `preclin`, `clin`, `postmbbs`, defined in `bot/config.py`
(`LEVELS`) and constrained in `schema.sql`. Students pick one with `/level` and
can change it any time; questions *and* notes follow it.

Adding a level is a code + schema change (`LEVELS`, the `check` constraints in
`schema.sql`, and the `check` in `tests/test_seed_data.py`). Ask before doing it
by hand — it touches the CHECK constraints on three tables.

### 4.3 Questions

**Load the bank you have** — done in Phase 2.3. To check what's loaded:

```bash
psql "$DATABASE_URL" -c "select topic, count(*) from questions where level='preclin' group by topic order by topic;"
```

**Add a question by hand:**

```bash
psql "$DATABASE_URL" <<'SQL'
insert into questions (level, topic, tag, text, options, correct_idx, explanation)
values (
  'preclin',
  'Orbit and eye movements',
  'Anatomy | lesion localisation',
  'A patient cannot abduct the right eye. Which nerve is affected?',
  '["III", "IV", "VI", "V1", "VII"]'::jsonb,
  2,
  'CN VI supplies lateral rectus, so an isolated abduction deficit points to it.'
);
SQL
```

The database **enforces** quality here: 2–6 options, and `correct_idx` must
point at a real option. A malformed row is rejected rather than shipped to
students.

**When the Clinical and Post-MBBS banks arrive:**

```bash
# 1. put the .docx files in resources/
# 2. add them to LEVELS['clin'] / LEVELS['postmbbs'] in tools/build_question_seed.py
# 3. regenerate
python tools/build_question_seed.py --level clin
python tools/build_question_seed.py --level postmbbs

# 4. load (the files guard against double-loading)
psql "$DATABASE_URL" -f seeds/02_clin_mcqs.sql
psql "$DATABASE_URL" -f seeds/03_postmbbs_mcqs.sql
```

`tools/build_question_seed.py --check` fails if the committed seeds no longer
match the sources, so CI catches a hand-edited seed.

The generator expects the source documents to follow the format of the existing
ones (`Question N` / `Question type` / `Topic` / `Options:` / `a)`–`e)` /
`Correct option` / explanation). If ZH's new files differ, the script says
exactly which question it couldn't parse rather than silently dropping it.

**Reload a level from scratch** (the seeds refuse a second load by design):

```bash
psql "$DATABASE_URL" -c "delete from questions where level = 'preclin';"
psql "$DATABASE_URL" -f seeds/01_preclin_mcqs.sql
```

> Deleting questions cascades to `attempts` for those questions, so student
> history for them goes too. Fine before the event; not fine mid-tournament.

### 4.4 Notes

Notes are **not** seeded yet — the 20 PDFs in `resources/` are lecture material,
not the cheat-sheet format. `/notes` will say "No Tier B notes for Pre-Clinical
yet" until you load some.

Two kinds, per the plan:

| Kind | Meaning | Delivered |
|---|---|---|
| **Overview** | One broad sheet per topic (`01`–`06`) | `/topicalnotes` on demand, and all six in the monthly drop |
| **Focused** | Deeper on a single point (`B01`–`B20`) | `/randomnotes` on demand; six are reserved for the monthly drop |

Both are per-level. Add them like this:

```bash
psql "$DATABASE_URL" <<'SQL'
insert into notes (level, topic, tier, title, body) values
  ('preclin', 'Orbit and eye movements', 'B',
   'Cranial nerve palsies at a glance',
   'CN III: ptosis, "down and out", mydriasis.
CN IV: vertical diplopia, worse on downgaze and head tilt.
CN VI: failure to abduct.'),
  ('preclin', 'Development and ocular histology', 'A',
   'Optic fissure closure',
   'The fissure closes in week 7. Failure gives coloboma, typically inferior.');
SQL
```

Long bodies are split automatically across Telegram messages, so a whole cheat
sheet can be one row. To check what a level has:

```bash
psql "$DATABASE_URL" -c "select level, tier, topic, count(*) from notes group by 1,2,3 order by 1,2,3;"
```

### 4.5 Sets of five

Progress is measured in sets of five. The sets are **consecutive blocks of five
questions in id order** (`db.SET_SIZE`), so set 1 is the same five questions for
every student and the scores are comparable. Nothing about the sets is stored:
`db.set_board` derives how far a student has got from the `attempts` table, which
is why there is no session state to go stale and why a student who stops halfway
through a set resumes it.

The numbers are all in `bot/db.py`: `set_board`, `quiz_set`, `set_no_for`,
`set_score` (progress and scoring) and `stats` (marker A, the per-topic and
per-question-type breakdown behind `/stats`).

**Changing the set size** is one constant (`db.SET_SIZE`). It takes effect on the
next answer: existing attempts simply fall into the new blocks, so nobody's
progress is lost.

### 4.6 Weekly quiz and monthly sheets

`weekly_quiz` fires **Mondays 09:00** (server timezone, Asia/Singapore from Phase
1.3) and sends each subscriber the rest of their current set, so a student who
stopped mid-set gets the remainder rather than a fresh five. Answers to the
Monday set use the `weekly` mode and **do not score for the tournament**, which is
what stops the push from becoming a leaderboard farm.

`monthly_notes` fires on the **1st at 10:00** and sends all six overview sheets
plus the six reserved focused ones, as PDFs.

Both can be fired on demand with `/admin_weekly_now` and `/admin_notes_now`.

`/weeklyquiz` and `/stopweekly` control the Monday set; `/monthlynotes` and
`/stopmonthly` control the sheets. To test without waiting for the schedule:

```
/admin_weekly_now
```

Job settings live in `jobs.register`: `misfire_grace_time=3600` — APScheduler's
1-second default would silently skip the push if the process happened to be
restarting at 09:00.

### 4.7 Monthly sheets

Fires on the **1st at 10:00**. Sends all six overview sheets plus the six focused
sheets reserved for the drop (`resources.MONTHLY_CODES`), as PDFs.
`/monthlynotes` and `/stopmonthly` control it; `/admin_notes_now` fires it on
demand.

`/randomnotes` deliberately draws from the *other* fourteen focused sheets, so a
student cannot be handed the monthly bundle at random before it goes out.

### 4.8 The tournament

Two weeks, admin-controlled, and **entry is automatic**.

**Start it:**

```
/admin_tournament_start
```

**Students do not join.** Everyone active is entered automatically when the
tournament opens, and anyone who sends `/start` while it runs is entered then, so
`/tournament` only reports the status. There is nothing to opt in or out of.

**Scoring:** every correct `/quizme` answer earns **one point, once per question**.
The `mode` on each attempt decides this, so the Monday set (`weekly`) contributes
nothing; the `tournament_answers` table enforces the once-per-question rule in SQL
so re-answering a known question cannot farm the board.

**Standings:** `/leaderboard` shows the top 3 to everyone with the last two
characters of each username hidden, plus the caller's own rank. Admins get the
full table with real usernames and Telegram IDs, which is what OphSoc needs to pay
out.

**Close it early:**

```
/admin_tournament_end
```

This reads the standings **before** deactivating, closes the tournament, DMs the
top 3 (they're told to contact LKC OphSoc), and sends the admin table. Automatic
closing happens within 30 minutes of expiry via the `close_tournaments` job.

**Rewards:** the bot only tells winners to get in touch. Keep your own note of
the top 3 — the admin DM has the IDs.

> **Ties** break by join order: the person who joined the tournament first ranks
> higher on equal points.

### 4.9 The explanation button

Every question in the preclinical bank has a **written explanation** stored in the
database. Pressing 💡 serves that text and never contacts an LLM — so the button
works with no API key and costs nothing.

The LLM is a fallback for questions with no explanation (which is all future
Clinical/Post-MBBS content until ZH writes explanations). To enable it, set
`LLM_API_KEY` and `LLM_MODEL` in `.env` and restart. Generated explanations are
cached in `questions.explanation`, so each question is paid for once, and the
write is single-writer (a concurrent second tap can't overwrite a good answer).

Rate limiting: one explanation per user per 10 seconds.

To see which questions still lack an explanation:

```bash
psql "$DATABASE_URL" -c "select level, count(*) from questions where explanation is null group by level;"
```

### 4.10 Admin commands

Only IDs in `ADMIN_IDS` can run these; anyone else is ignored (and logged).

| Command | Does |
|---|---|
| `/admin_tournament_start` | Starts a 2-week tournament |
| `/admin_tournament_end` | Announces winners and closes it early |
| `/admin_weekly_now` | Sends this week's question immediately |
| `/admin_notes_now` | Sends the Tier A notes immediately |

### 4.11 Message content

All student-facing copy is in `bot/handlers.py` (`HELP`) and `bot/config.py`
(`CREDIT`, `DISCLAIMER`). Edit, commit, push, then redeploy (Phase 5.1).

---

## Phase 5 — Running it

### 5.1 Deploying a change

```bash
cd /opt/studybot
git pull
.venv/bin/pip install -r requirements.txt    # only needed if deps changed
sudo systemctl restart studybot
journalctl -u studybot -n 30
```

After a schema change, also run the matching migration:

```bash
ls migrations/
psql "$DATABASE_URL" -f migrations/00X_name.sql
```

### 5.2 Daily checks

```bash
systemctl is-active studybot
journalctl -u studybot --since "24 hours ago" | grep -c ERROR
journalctl -u studybot --since "24 hours ago" -p warning
```

`unattended-upgrades` handles OS security patches. Postgres security patches come
with those too; the database restarts with the package, and the bot reconnects on
its own.

### 5.3 Logs

```bash
journalctl -u studybot -f                    # follow
journalctl -u studybot --since today         # today
journalctl -u studybot -p err                # errors only
```

Log rotation is automatic via journald. Nothing sensitive is logged above INFO —
keep it that way; question text and student answers must not go into logs.

### 5.4 Monitoring

A silent bot looks exactly like a working one. Either:

- **UptimeRobot** (free) — add an HTTP(s) monitor for
  `https://api.telegram.org/bot<TOKEN>/getMe`. It returns 200 while the token is
  valid, so it catches a revoked token, and it wakes you if the API is down. It
  does **not** catch a dead process.
- **A heartbeat** — a systemd `OnFailure=` unit that DMs you, or a cron job that
  `curl`s a healthcheck URL. To catch a dead process, point the monitor at
  something that depends on it.

Simplest reliable one: a cron entry that checks the service and messages you.

### 5.5 Cost

Running with the LLM disabled: **the VPS only.** If you enable it, put a hard
monthly spend cap on the key at the provider — a bot left in a loop with an
uncapped key is the classic way to wake up to a bill.

### 5.6 Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `TelegramConflictError: terminated by other getUpdates request` | Two processes polling one token. `systemctl status studybot`; don't also run it on your laptop |
| Bot silent, service `active` | Almost always the DB. `journalctl -u studybot -n 50`; check `psql "$DATABASE_URL" -c 'select 1'` |
| `password authentication failed for user "studybot"` | `.env` password doesn't match the role. Reset: `sudo -u postgres psql -c "alter role studybot password 'NEW';"` |
| `/quizme` says no questions for your level | Correct behaviour — that bank isn't loaded. `/changestreams` switches back |
| `/notes` finds nothing | No notes loaded for that level/tier yet. See 4.4 |
| A student taps an answer and the spinner hangs | Shouldn't happen — every callback path answers. Check `journalctl -u studybot -p err` and report it |
| Answer button says "Already answered" | That message was already scored. Tap **Next** for a new question |
| Everyone got the weekly question twice | Two bot instances, or the job ran twice after a manual `/admin_weekly_now` |
| `relation "questions" does not exist` | `schema.sql` was never applied, or `DATABASE_URL` points at a different database |
| Wrong date shown for tournament end | Server timezone. `timedatectl` should say Asia/Singapore |
| Bot crash-loops on startup: `PermissionError: [Errno 13] ... '/home/deploy/.postgresql/postgresql.key'` | `ProtectHome=true` in the unit. It makes `/home` inaccessible, so asyncpg's probe for its SSL key raises `EACCES` instead of returning "not found" — and asyncpg doesn't catch it. The shipped unit omits `ProtectHome` and sets `Environment=HOME=/opt/studybot` for exactly this reason. **Do not add `ProtectHome` back.** |
| `systemctl is-active` says `active` but the bot is clearly dead | With `Restart=always`, a crash-looping unit reports `active` between restarts. Check `systemctl show -p NRestarts --value studybot` — a climbing number means it is dying and restarting. Use `journalctl -u studybot -p err`. |
| `TelegramConflictError: terminated by other getUpdates request` | Two pollers on one token. Something else called `getUpdates` — a second instance, or a manual `curl` to the API from a terminal (which steals the bot's poll slot; aiogram recovers on its own within ~10s). |
| Locked out of SSH | Contabo's VNC console in the control panel, or DigitalOcean's *Access → Launch Droplet Console*, then re-check `sshd_config` |

### 5.7 Event-day checklist

Run this the day before, not the morning of.

```bash
systemctl is-active studybot postgresql      # both active
psql "$DATABASE_URL" -c "select level, count(*) from questions group by level;"
psql "$DATABASE_URL" -c "select count(*) from notes;"
sudo -u postgres ls -la /var/backups/studybot | tail -3
```

Then, in Telegram:

- [ ] `/start` on a phone you have never used — the credit block and `/level` show
- [ ] `/quizme` → answer a full set of five → the score lands → 💡 Explain works
- [ ] `/level` → Clinical → the honest "still being written" message
- [ ] `/subscribe` → `/admin_weekly_now` → the question arrives
- [ ] `/admin_tournament_start` → `/tournament` → `/leaderboard` shows you
- [ ] Answer 3 questions correctly → `/leaderboard` shows 3 points
- [ ] `/admin_tournament_end` → you get a winner DM and the admin table
- [ ] Register the bot's commands with BotFather (`/setcommands`) if the menu is
      empty — `main.py` also sets them on startup

**On the day:** start the tournament (`/admin_tournament_start`), tell students
the bot username, and keep `journalctl -u studybot -f` open in a terminal. Close
with `/admin_tournament_end` and screenshot the admin table for OphSoc.

---

## Appendix — using Supabase instead

Everything above still works; only Phase 2 changes.

1. Create a Supabase project in the **Singapore** region.
2. **Connect → Session pooler** (port 5432). The direct host is IPv6-only and
   won't resolve from Contabo's IPv4 address.
3. Run `schema.sql` and each `seeds/*.sql` in the SQL editor.
4. Set `DATABASE_URL` to the pooler string. `db.init` already sets
   `statement_cache_size=0`, which a transaction pooler requires.
5. Skip the local `postgresql.service` step — the systemd unit's `After=`
   line is harmless if Postgres isn't installed.

**Watch for:**

- **Free projects pause after 7 days of inactivity.** If the bot is down for a
  week, you must restore the project from the dashboard before it works again.
- **No point-in-time recovery** on the free tier. Keep your own `pg_dump`.
- **RLS.** The schema enables row-level security with no policies, so an API key
  can't read your tables. The bot is unaffected because it connects as the
  database owner, which bypasses RLS.

---

## Appendix — DigitalOcean instead of Contabo

Phase 1 differs slightly; everything from Phase 2 onwards is identical.

**At droplet creation**

| Field | Value |
|---|---|
| Image | Ubuntu 24.04 LTS |
| Region | Singapore (SGP1) |
| Size | See the warning below |
| Authentication | **SSH Key** — add your public key (see below) |
| Advanced | Enable monitoring; optionally paste an initialisation script |

**Which size.** The **$4/mo tier is 512 MB RAM**, and that is genuinely tight for
Postgres *plus* a Python process *plus* the OS. It runs, but you want **1 GB
($6/mo)** for comfort. `deploy/provision.sh` creates a 1 GB swapfile first thing,
which is what makes the 512 MB tier survivable at all — without swap the OOM
killer will take Postgres or the bot under load, and it will look like random
crashes rather than a memory problem. If you already built the small droplet, you
can resize CPU/RAM from *Resize → CPU and RAM only* (a reboot; disk size is the
one-way part). Do it before the event, not during.

**SSH keys.** Generate a pair, then paste the contents of the **`.pub`** file —
one line starting `ssh-ed25519`, never the private key and never the file path:

```bash
ssh-keygen -t ed25519 -C "your-name-phone"      # in Termux: pkg install openssh first
cat ~/.ssh/id_ed25519.pub                       # this is what DigitalOcean wants
```

Keys are applied **at creation**. Adding one to an existing droplet means editing
`~/.ssh/authorized_keys` by hand, so get it right in the create form. You can add
several keys — worth doing so you are not dependent on a single device.

**Recovery console.** *Droplet → Access → Launch Droplet Console* gets you a root
shell even when SSH is broken. That is your escape hatch from a bad
`sshd_config`, and it is why the Contabo runbook's "keep your session open" dance
matters less here. Use it if you ever lock yourself out.

**Cloud firewall.** DigitalOcean firewalls are applied outside the droplet, so
they survive you breaking `ufw`. Create one allowing inbound **22/tcp only**
(and 80/443 if you ever add a webhook), apply it to the droplet, then let `ufw`
do the same job inside. The bot only ever makes outbound connections.

**Then run the provisioning script** and continue from Phase 2.4:

```bash
ssh root@YOUR.DROPLET.IP 'bash -s' < deploy/provision.sh
```

Two Contabo-specific steps you can skip: there is no emailed root password (your
key works immediately), and there is no one-off setup fee.

