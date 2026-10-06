# Load test

What happens when a lot of students use the bot at once, where it breaks, and the
fix. Run on `main` at 9ac8501 (all three banks loaded, 393 questions), 6 Oct 2026.

## Summary

- **The server is not the bottleneck.** On one CPU core (the droplet has one),
  1,000 students tapping as fast as they can used about half a core. The
  database and memory were fine, and no data went wrong.
- **Telegram is the bottleneck.** A bot can send roughly 30 messages a second in
  total. Above that, Telegram answers "429 Too Many Requests".
- **The bot handled a 429 badly.** Only `sender.safe_send` and the PDF upload
  retried. The other ~40 replies (`m.answer(...)`, the answer verdict, the set
  report) were dropped, so the student got silence. At 200 students answering
  quickly, that was 342 commands with no reply.
- **Fix: `bot/throttle.py`.** Every Telegram call now goes through one queue. It
  keeps sends just under Telegram's limit, retries a 429, and stops broadcasts
  crowding out students. With it installed, nothing was lost in any run.
- **Capacity.** With the fix, about 200 students can answer at the same moment
  and 95% of replies arrive within about a second. If Telegram also counts message
  edits against its limit, which it does not publish, that figure is nearer 150.
  Beyond it, replies queue instead of disappearing.

## How it was tested

`tools/loadtest/` runs the real `Dispatcher` and handlers against a real
PostgreSQL 16 loaded with all three banks. Only the HTTP layer is replaced, by
`FakeTelegram`, which:

- adds 120–250 ms latency per call and caps connections at aiohttp's 100;
- enforces flood control: 30 sends a second overall and 4 a second per chat,
  refused with a real `TelegramRetryAfter`;
- validates every message the way Telegram does: HTML parsing, the
  4,096-character cap, 64-byte callback data, "message is not modified", and
  callback answers that come twice or after 15 s;
- simulates blocked users (`TelegramForbiddenError`).

Each simulated student reads the latest card and taps its real buttons. Every
run does `/start`, picks a level, then answers 10 questions. Along the way it
taps Explain 30% of the time, double-taps 5% of the time, and every 5 answers
sends two of `/stats`, `/notes`, `/leaderboard`, `/review` and `/tournament`.

- **Fast** students wait 1–4 s between actions.
- **Realistic** students take 10–30 s to read each question.

The bot and Postgres were pinned to one CPU core to mimic the droplet.

Two flood-control models, because Telegram does not say whether **edits** (every
verdict and explanation is one) count against the 30/s:

- **sends-only**: only new messages count (the optimistic case);
- **edits count**: edits count too (`--edits-count`, the pessimistic case).

## Results

### Before and after the fix

"Commands with no reply" counts commands that ended in Telegram's error and were
never answered. "Messages abandoned" counts sends that gave up after their retries.

| Scenario | Version | Commands with no reply | Answers with no verdict | Messages abandoned | "Next" p95 |
|---|---|---|---|---|---|
| 50 fast students | main | 0 | 0 | 0 | 0.7 s |
| 200 fast | main | 342 | 0 | 93 + 20 PDFs | 6.8 s |
| 200 fast | **fix** | **0** | **0** | **0** | 5.5 s |
| 200 realistic | main | 16 | 0 | 2 + 1 PDF | 0.7 s |
| 200 realistic | **fix** | **0** | **0** | **0** | 1.2 s |
| 300 realistic | main | 212 | 0 | 35 + PDFs | 3.8 s |
| 300 realistic | **fix** | **0** | **0** | **0** | 4.6 s |
| 500 realistic | main | 832 | 0 | 322 + PDFs | 7.0 s |
| 500 realistic | **fix** | **0** | **0** | **0** | 12 s |
| 500 realistic, edits count | main | 473 | 2,332 of 4,357 | 1,287 | 7.1 s |
| 500 realistic, edits count | **fix** | **0** | **0** | **0** | 33 s |

On `main` the latencies look better under heavy load only because failures
return fast. The fixed version delivers everything, so once the bot is at
Telegram's ceiling, its latency is honest queueing.

### Broadcasts (1,000 subscribers, 3% of whom have blocked the bot, 100 students online)

| | main | fix |
|---|---|---|
| Monday push, 5 cards each | 6.6 min, 970/970 delivered | 8.2 min, 970/970 delivered |
| Fortnightly drop, 1 PDF each | 3.7 min, 970/970 delivered | 2.7 min, 970/970 delivered |
| Blocked users marked inactive | 30/30 | 30/30 |
| Two admins tap `/admin_tournament_start` together | 1 tournament, announced once | same |
| Online students' commands lost | 83 | 0 |

The push is slightly slower with the fix on purpose. Broadcasts are held to 12
of the 24 sends a second, so students online during a push keep fast replies
(p95 0.7 s throughout).

### Server headroom (Telegram limits switched off, 1,000 fast students)

146 updates a second at 53% of one core, with loop lag p99 at 42 ms. The 5-connection
database pool was fully busy 53% of the time, which pushed the median answer to
3.8 s. That is five times beyond what Telegram would let through, so the pool is
not worth changing. The bot process uses about 170 MB at startup, and the whole
harness peaked under 400 MB, which leaves room on the 1 GB droplet.

### Integrity: held in every run

- no question scored twice by the same student;
- tournament points always equal the deduplicated correct answers;
- no student was served the same practice question twice;
- every button tap was answered exactly once, within 2 s, and never past
  Telegram's 15 s cut-off;
- concurrent tournament starts never opened two tournaments.

## Capacity

One answered question costs about 2.1 sends with this harness's usage mix. With
the edit-related calls included, that rises to about 4.5: 1.3 `EditMessageText`
plus 1.05 `EditMessageReplyMarkup`. The bot budgets 24 sends a second, which
gives:

| Flood model | Answers per second | Students answering at once, before replies queue |
|---|---|---|
| sends-only | ~11 | ~300 |
| edits count | ~5 | ~150 |

These assume a realistic 25–30 s per question. Past those numbers nothing is
lost, but replies slow down in proportion.

## Changes in this PR

- `bot/throttle.py`: `FloodGuard`, an aiogram request middleware installed in
  `main.py`. It does three things:
  - FIFO send budget at 24/s, burst 4.
  - Retries a 429 up to 4 times and pauses the queue for the time Telegram asks.
  - Starts budgeting edits only after Telegram rate-limits one, so the
    optimistic case keeps full capacity.

  `BULK` caps scheduled fan-outs and the tournament announcement at 12/s.
- `main.on_error` now also replies to a failed command ("Something went wrong.
  Please try again.") instead of leaving silence.
- `tests/test_throttle.py`: 8 tests covering ordering, rate, broadcast priority,
  retry, give-up, the no-outage-sleep rule, adaptive edits and the error reply.
- `tools/loadtest/`: the harness, to re-run after future changes.

## Worth doing next (not in this PR)

1. **`/review` sends the whole pile at once** (a header plus every card). It is
   the slowest command under load, with p95 16 s at 300 students. Sending one
   card at a time with a Next button, as `/quizme` does, would fix that.
2. **The "remove used buttons" edit on Next** (`drop_buttons`) is one extra API
   call per question, purely cosmetic. If Telegram counts edits, it is about a
   quarter of the budget. Dropping it raises the pessimistic capacity from ~150
   to ~190 students.
3. **The tournament announcement is sequential.** It takes about 200 s for 1,000
   users, and the admin's command only confirms at the end. Running it through
   `jobs._fan_out` like the weekly push would take about 80 s.

## Re-running it

```bash
docker compose up -d db
pip install -r requirements.txt
python tools/loadtest/harness.py interactive --students 200 --read 10 30 --think 1 3 --guard --out r.json
python tools/loadtest/harness.py broadcast --users 1000 --live 100 --guard --out b.json
python tools/loadtest/show.py r.json b.json
```

Leave out `--guard` to see the behaviour without FloodGuard. Add `--edits-count`
for the pessimistic flood model, or `--no-limits` to measure the server alone.
The harness creates and drops its own `stress_*` databases on the docker-compose
server. It never touches the bot's database.
