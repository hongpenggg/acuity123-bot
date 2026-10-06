"""Load-test the LKC OphSoc bot: the real Dispatcher and handlers, a real
PostgreSQL with the full question bank, and FakeTelegram in place of the API.
See docs/LOADTEST.md for what it measures and the results.

    docker compose up -d db
    python tools/loadtest/harness.py interactive --students 200 --read 10 30 --think 1 3 --guard
    python tools/loadtest/harness.py broadcast --users 1000 --live 100 --guard

It creates (and drops) its own databases on that server, never the bot's.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import itertools
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

REPO = os.environ.get("REPO", os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
# The docker-compose test database (docker-compose.yml) by default.
PG = os.environ.get("LOADTEST_PG", "postgresql://postgres:postgres@localhost:5433")
ADMIN = 42
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", str(ADMIN))
DBNAME = os.environ.get("STRESS_DB", "stress_run")
os.environ.setdefault("DATABASE_URL", f"{PG}/{DBNAME}")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(__file__))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.types import (CallbackQuery, Chat, Message, MessageEntity,  # noqa: E402
                           Update, User)

from fake_telegram import Config, FakeSession, FakeTelegram  # noqa: E402

LEVELS = ("preclin", "clin", "postmbbs")


# ---------------------------------------------------------------- database
def psql(db: str, sql: str) -> str:
    return subprocess.run(["psql", f"{PG}/{db}", "-qtAX", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=True, capture_output=True, text=True).stdout.strip()


def fresh_db(name: str = DBNAME) -> None:
    tpl_exists = psql("postgres", "select 1 from pg_database where datname='stress_tpl'")
    if not tpl_exists:
        psql("postgres", "create database stress_tpl")
        for f in ["schema.sql", "seeds/01_preclin_mcqs.sql", "seeds/02_clin_mcqs.sql",
                  "seeds/03_postmbbs_mcqs.sql"]:
            subprocess.run(["psql", f"{PG}/stress_tpl", "-q", "-v", "ON_ERROR_STOP=1",
                            "-f", os.path.join(REPO, f)], check=True, capture_output=True)
    psql("postgres", f"select pg_terminate_backend(pid) from pg_stat_activity where datname='{name}'")
    psql("postgres", f"drop database if exists {name}")
    psql("postgres", f"create database {name} template stress_tpl")


# ------------------------------------------------------------------ probes
class DBProbe:
    """Wraps the asyncpg pool: counts and times every call, samples saturation."""
    METHODS = ("fetch", "fetchrow", "fetchval", "execute", "executemany")

    def __init__(self, pool):
        self._pool = pool
        self.calls = 0
        self.time = 0.0
        self.slowest = 0.0
        self.samples = 0
        self.saturated = 0

    def __getattr__(self, name):
        attr = getattr(self._pool, name)
        if name not in self.METHODS:
            return attr

        async def timed(*a, **kw):
            t = time.perf_counter()
            try:
                return await attr(*a, **kw)
            finally:
                dt = time.perf_counter() - t
                self.calls += 1
                self.time += dt
                self.slowest = max(self.slowest, dt)
        return timed

    async def sample(self, stop: asyncio.Event):
        while not stop.is_set():
            self.samples += 1
            if self._pool.get_idle_size() == 0 and self._pool.get_size() >= self._pool.get_max_size():
                self.saturated += 1
            await asyncio.sleep(0.05)


async def loop_lag(stop: asyncio.Event, out: list):
    while not stop.is_set():
        t = time.perf_counter()
        await asyncio.sleep(0.05)
        out.append(time.perf_counter() - t - 0.05)


def rss_mb() -> tuple[float, float]:
    vals = {}
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(("VmRSS", "VmHWM")):
                k, v = line.split(":")
                vals[k] = int(v.split()[0]) / 1024
    return vals.get("VmRSS", 0), vals.get("VmHWM", 0)


class ErrorLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.errors = collections.Counter()
        self.warnings = collections.Counter()

    def emit(self, record):
        msg = record.getMessage().splitlines()[0][:110]
        msg = re.sub(r"\d{3,}", "N", msg)
        if record.exc_info and record.exc_info[1] is not None:
            msg += f" [{type(record.exc_info[1]).__name__}: {str(record.exc_info[1])[:80]}]"
        (self.errors if record.levelno >= logging.ERROR else self.warnings)[msg] += 1


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


# ------------------------------------------------------------------ the rig
class Rig:
    def __init__(self, cfg: Config, pool_size: int | None = None, seed: int = 7,
                 guard: bool = False):
        self.tg = FakeTelegram(cfg, seed=seed)
        self.bot = Bot(os.environ["BOT_TOKEN"], session=FakeSession(self.tg))
        self.guard = None
        if guard:
            from bot import throttle
            self.guard = throttle.FloodGuard()
            self.bot.session.middleware(self.guard)
        self.dp = Dispatcher()
        self.update_ids = itertools.count(1)
        self.cb_ids = itertools.count(1)
        self.lat = collections.defaultdict(list)
        self.raw_exceptions = collections.Counter()
        self.errlog = ErrorLog()
        self.pool_size = pool_size

    async def start(self):
        from bot import db, handlers, main as botmain
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger().addHandler(self.errlog)
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)
        if self.pool_size:
            import asyncpg
            db.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1,
                                                max_size=self.pool_size, statement_cache_size=0)
        else:
            await db.init(os.environ["DATABASE_URL"])
        self.probe = DBProbe(db.pool)
        db.pool = self.probe
        self.dp.include_router(handlers.router)
        self.dp.errors.register(botmain.on_error)
        self.db = db

    async def stop(self):
        await self.db.pool._pool.close()

    def user(self, uid: int) -> User:
        return User(id=uid, is_bot=False, first_name=f"S{uid}", username=f"stu{uid}")

    async def feed(self, kind: str, update: Update):
        t = time.perf_counter()
        try:
            await self.dp.feed_update(self.bot, update)
        except Exception as exc:  # anything the bot's own error handler let through
            self.raw_exceptions[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
        self.lat[kind].append(time.perf_counter() - t)

    async def command(self, uid: int, text: str):
        mid = self.tg.user_message(uid, text)
        cmd = text.split()[0]
        msg = Message(message_id=mid, date=datetime.now(timezone.utc),
                      chat=Chat(id=uid, type="private"), from_user=self.user(uid), text=text,
                      entities=[MessageEntity(type="bot_command", offset=0, length=len(cmd))])
        await self.feed(cmd, Update(update_id=next(self.update_ids), message=msg))

    async def tap(self, uid: int, mid: int, data: str, kind: str):
        qid = f"cb{next(self.cb_ids)}"
        self.tg.callbacks[qid] = time.monotonic()
        cq = CallbackQuery(id=qid, from_user=self.user(uid), chat_instance=f"ci{uid}",
                           message=self.tg.as_message(uid, mid, self.user(uid)), data=data)
        await self.feed(kind, Update(update_id=next(self.update_ids), callback_query=cq))


def buttons(stored) -> list[str]:
    if stored is None or stored.markup is None:
        return []
    return [b.callback_data for row in stored.markup.inline_keyboard for b in row if b.callback_data]


class Student:
    def __init__(self, rig: Rig, uid: int, level: str, rng: random.Random, answers: dict,
                 think: tuple[float, float], sets: int, extras: bool,
                 read: tuple[float, float] | None = None):
        self.rig, self.uid, self.level, self.rng = rig, uid, level, rng
        self.answers, self.think, self.sets, self.extras = answers, think, sets, extras
        self.read = read or think
        self.answered = 0
        self.exhausted = False
        self.no_verdict = 0       # tapped an answer, card never changed
        self.silent = 0           # sent a command, got nothing back
        self.dead: set[int] = set()  # cards whose verdict never came; not re-tapped

    async def pause(self, span=None):
        lo, hi = span or self.think
        if hi > 0:
            await asyncio.sleep(self.rng.uniform(lo, hi))

    def latest_with(self, prefix: str):
        for m in reversed(self.rig.tg.bot_messages(self.uid)):
            if m.mid in self.dead:
                continue
            if any(d.startswith(prefix) for d in buttons(m)):
                return m
        return None

    async def run(self):
        r, tg = self.rig, self.rig.tg
        await r.command(self.uid, "/start")
        await self.pause()
        if self.level != "preclin":
            welcome = self.latest_with("lv:")
            if welcome:
                await r.tap(self.uid, welcome.mid, f"lv:{self.level}", "tap:level")
                await self.pause()
        while self.answered < self.sets * 5:
            card = self.latest_with("a:")
            if card is None:
                nxt = self.latest_with("next")
                before = len(tg.bot_messages(self.uid))
                if nxt is not None and "next" in buttons(nxt):
                    await r.tap(self.uid, nxt.mid, "next", "tap:next")
                else:
                    await r.command(self.uid, "/quizme")
                card = self.latest_with("a:")
                if card is None:
                    if len(tg.bot_messages(self.uid)) == before:
                        # Silence. A real student tries once more, then gives up.
                        self.silent += 1
                        await self.pause()
                        await r.command(self.uid, "/quizme")
                        card = self.latest_with("a:")
                        if card is None:
                            if len(tg.bot_messages(self.uid)) == before:
                                self.silent += 1
                            self.exhausted = True
                            break
                    else:
                        self.exhausted = True
                        break
            await self.pause(self.read)
            opts = [d for d in buttons(card) if d.startswith("a:")]
            qid = int(opts[0].split(":")[1])
            correct = self.answers.get(qid)
            if correct is not None and self.rng.random() < 0.6:
                choice = next((d for d in opts if int(d.split(":")[2]) == correct), opts[0])
            else:
                choice = self.rng.choice(opts)
            if self.rng.random() < 0.05:   # impatient double tap
                await asyncio.gather(r.tap(self.uid, card.mid, choice, "tap:answer"),
                                     r.tap(self.uid, card.mid, choice, "tap:answer(dup)"))
            else:
                await r.tap(self.uid, card.mid, choice, "tap:answer")
            self.answered += 1
            answered = tg.chats[self.uid].get(card.mid)
            if any(d.startswith("a:") for d in buttons(answered)):
                # The verdict never arrived. A student re-tapping would be told
                # "already answered", so they move on with /quizme instead.
                self.no_verdict += 1
                self.dead.add(card.mid)
                newest = tg.bot_messages(self.uid)
                if not newest or newest[-1].mid == card.mid:
                    await self.pause()
                    await r.command(self.uid, "/quizme")
                continue
            await self.pause()
            explain = next((d for d in buttons(answered) if d.startswith("e:")), None)
            if explain and self.rng.random() < 0.3:
                await r.tap(self.uid, card.mid, explain, "tap:explain")
                await self.pause()
            if self.extras and self.answered % 5 == 0:
                for cmd in self.rng.sample(["/stats", "/notes", "/leaderboard", "/review",
                                            "/tournament"], k=2):
                    await r.command(self.uid, cmd)
                    await self.pause()
            if "next" in buttons(tg.chats[self.uid].get(card.mid)):
                await r.tap(self.uid, card.mid, "next", "tap:next")
                await self.pause()


# ------------------------------------------------------------ integrity
def integrity(rig: Rig, uids: list[int]) -> dict:
    tg = rig.tg
    out = {}
    # every callback answered exactly once, and in time
    unanswered = [q for q in tg.callbacks if not tg.cb_answers.get(q)]
    multi = [q for q, a in tg.cb_answers.items() if len(a) > 1]
    ack = [tg.cb_answers[q][0][0] - tg.callbacks[q] for q in tg.callbacks if tg.cb_answers.get(q)]
    out["callbacks"] = len(tg.callbacks)
    out["callbacks_unanswered"] = len(unanswered)
    out["callbacks_answered_twice"] = len(multi)
    out["callback_ack_p95_s"] = round(pct(ack, 95), 3)
    out["callback_ack_max_s"] = round(max(ack) if ack else 0, 3)
    out["callback_ack_over_15s"] = sum(1 for a in ack if a > 15)
    # practice cards: no student is served the same question twice
    dup_cards = 0
    for uid in uids:
        seen = collections.Counter()
        for m in tg.bot_messages(uid):
            for d in buttons(m):
                if d.startswith("a:") and d.endswith(":practice"):
                    seen[int(d.split(":")[1])] += 1
                    break
        dup_cards += sum(c - 1 for c in seen.values() if c > 1)
    # answered cards keep their a: buttons only if unanswered; count served cards
    out["practice_cards_served_twice"] = dup_cards
    db = DBNAME
    out["attempt_rows"] = int(psql(db, "select count(*) from attempts"))
    out["duplicate_scoring_answers"] = int(psql(db, """select count(*) from (select 1 from attempts
        where mode <> 'review' group by user_id, question_id having count(*) > 1) d"""))
    out["points_vs_answers_mismatch"] = int(psql(db, """select count(*) from tournament_points p
        where p.points <> (select count(*) from tournament_answers a
                           where a.tournament_id = p.tournament_id and a.user_id = p.user_id)"""))
    out["active_tournaments"] = int(psql(db, "select count(*) from tournaments where active"))
    return out


def summary(rig: Rig, wall: float, cpu: float, lag: list, extra: dict) -> dict:
    lat = {k: {"n": len(v), "p50": round(pct(v, 50), 3), "p95": round(pct(v, 95), 3),
               "p99": round(pct(v, 99), 3), "max": round(max(v), 3)}
           for k, v in sorted(rig.lat.items())}
    total = sum(len(v) for v in rig.lat.values())
    st = rig.tg.stats
    rss, hwm = rss_mb()
    peak_sends = max(st.accepted_by_second.values()) if st.accepted_by_second else 0
    return {
        **extra,
        "wall_s": round(wall, 1), "updates": total, "updates_per_s": round(total / wall, 1),
        "cpu_s": round(cpu, 1), "cpu_util": round(cpu / wall, 2),
        "loop_lag_p99_ms": round(pct(lag, 99) * 1000, 1), "loop_lag_max_ms": round(max(lag) * 1000 if lag else 0, 1),
        "rss_mb": round(rss, 1), "rss_peak_mb": round(hwm, 1),
        "db_calls": rig.probe.calls, "db_calls_per_update": round(rig.probe.calls / max(total, 1), 1),
        "db_time_avg_ms": round(rig.probe.time / max(rig.probe.calls, 1) * 1000, 2),
        "db_slowest_ms": round(rig.probe.slowest * 1000, 1),
        "db_pool_saturated_pct": round(100 * rig.probe.saturated / max(rig.probe.samples, 1), 1),
        "api_calls": dict(st.calls), "api_max_inflight": st.max_inflight,
        "flood_429_global": st.flood_global, "flood_429_chat": st.flood_chat,
        "peak_sends_per_s": peak_sends, "bad_requests": dict(st.bad_request),
        "forbidden": st.forbidden,
        "guard_retries": rig.guard.retries if rig.guard else None,
        "latency_s": lat,
        "errors_logged": dict(rig.errlog.errors.most_common(12)),
        "warnings_logged": dict(rig.errlog.warnings.most_common(8)),
        "raw_exceptions": dict(rig.raw_exceptions),
    }


# ------------------------------------------------------------ scenarios
async def interactive(a) -> dict:
    fresh_db()
    cfg = Config(limits=a.limits, latency=tuple(a.latency), edits_count=a.edits_count)
    rig = Rig(cfg, pool_size=a.pool, guard=a.guard)
    await rig.start()
    answers = {int(r.split("|")[0]): int(r.split("|")[1]) for r in
               psql(DBNAME, "select id || '|' || correct_idx from questions").splitlines()}
    if a.tournament:
        psql(DBNAME, "insert into tournaments (starts_at, ends_at) values (now(), now() + interval '14 days')")
    rng = random.Random(11)
    uids = [100000 + i for i in range(a.students)]
    stop = asyncio.Event()
    lag: list = []
    tasks = [asyncio.create_task(loop_lag(stop, lag)), asyncio.create_task(rig.probe.sample(stop))]
    students = [Student(rig, uid, rng.choices(LEVELS, weights=(5, 3, 2))[0], random.Random(uid),
                        answers, tuple(a.think), a.sets, a.extras,
                        read=tuple(a.read) if a.read else None) for uid in uids]

    async def launch(s, delay):
        await asyncio.sleep(delay)
        await s.run()
    c0, t0 = time.process_time(), time.perf_counter()
    await asyncio.gather(*(launch(s, rng.uniform(0, a.ramp)) for s in students))
    wall, cpu = time.perf_counter() - t0, time.process_time() - c0
    stop.set()
    await asyncio.gather(*tasks)
    res = summary(rig, wall, cpu, lag, {"scenario": "interactive", "students": a.students,
                                        "pool": a.pool or 5, "limits": a.limits,
                                        "answers": sum(s.answered for s in students),
                                        "exhausted": sum(s.exhausted for s in students),
                                        "answers_without_verdict": sum(s.no_verdict for s in students),
                                        "commands_met_with_silence": sum(s.silent for s in students)})
    res["integrity"] = integrity(rig, uids)
    await rig.stop()
    return res


async def broadcast(a) -> dict:
    fresh_db()
    cfg = Config(limits=a.limits, latency=tuple(a.latency), edits_count=a.edits_count)
    rig = Rig(cfg, pool_size=a.pool, guard=a.guard)
    await rig.start()
    from bot import jobs
    rng = random.Random(5)
    users = [200000 + i for i in range(a.users)]
    rows = ",".join(f"({u},'u{u}','{rng.choices(LEVELS, weights=(5, 3, 2))[0]}',true,true)" for u in users)
    psql(DBNAME, f"insert into users (telegram_id, username, level, weekly_sub, notes_sub) values {rows}")
    psql(DBNAME, f"insert into users (telegram_id, username) values ({ADMIN}, 'admin')")
    blocked = set(rng.sample(users, k=int(len(users) * a.blocked)))
    rig.tg.blocked |= blocked
    stop = asyncio.Event()
    lag: list = []
    probes = [asyncio.create_task(loop_lag(stop, lag)), asyncio.create_task(rig.probe.sample(stop))]
    answers = {int(r.split("|")[0]): int(r.split("|")[1]) for r in
               psql(DBNAME, "select id || '|' || correct_idx from questions").splitlines()}
    live_uids = [300000 + i for i in range(a.live)]
    live = [Student(rig, u, "preclin", random.Random(u), answers, (1, 3), 50, False,
                    read=(10, 30)) for u in live_uids]
    live_tasks = [asyncio.create_task(s.run()) for s in live]
    await asyncio.sleep(3)       # live students settle in before the push

    phases = {}
    c0, t0 = time.process_time(), time.perf_counter()
    for name, coro in (("weekly_quiz", lambda: jobs.weekly_quiz(rig.bot)),
                       ("fortnightly_notes", lambda: jobs.fortnightly_notes(rig.bot))):
        before = sum(rig.tg.stats.calls.values())
        f0 = (rig.tg.stats.flood_global, rig.tg.stats.flood_chat)
        p0 = time.perf_counter()
        result = await coro()
        phases[name] = {"seconds": round(time.perf_counter() - p0, 1), "result": result,
                        "api_calls": sum(rig.tg.stats.calls.values()) - before,
                        "429s": (rig.tg.stats.flood_global - f0[0], rig.tg.stats.flood_chat - f0[1])}
    # tournament: two admins tapping start at the same moment
    p0 = time.perf_counter()
    await asyncio.gather(rig.command(ADMIN, "/admin_tournament_start"),
                         rig.command(ADMIN, "/admin_tournament_start"))
    phases["tournament_start_x2"] = {"seconds": round(time.perf_counter() - p0, 1)}
    for s in live:
        s.sets = 0                # let the live students wind down
    live_tasks = [t for t in live_tasks if not t.done()] or live_tasks
    await asyncio.gather(*live_tasks)
    wall, cpu = time.perf_counter() - t0, time.process_time() - c0
    stop.set()
    await asyncio.gather(*probes)

    # what each subscriber actually received
    tg = rig.tg
    cards = collections.Counter()
    docs = collections.Counter()
    announce = 0
    for u in users:
        msgs = tg.bot_messages(u)
        cards[sum(1 for m in msgs if any(d.startswith("a:") for d in buttons(m)))] += 1
        docs[sum(1 for m in msgs if m.kind == "document")] += 1
        announce += any("tournament" in (m.text or "").lower() for m in msgs)
    reachable = len(users) - len(blocked)
    res = summary(rig, wall, cpu, lag, {"scenario": "broadcast", "users": a.users,
                                        "live_answers_without_verdict": sum(s.no_verdict for s in live),
                                        "live_commands_met_with_silence": sum(s.silent for s in live),
                                        "blocked": len(blocked), "live_students": a.live,
                                        "limits": a.limits, "phases": phases})
    res["delivery"] = {"reachable": reachable,
                       "weekly_cards_per_user": dict(sorted(cards.items())),
                       "sheets_per_user": dict(sorted(docs.items())),
                       "got_tournament_announcement": announce,
                       "deactivated_after": int(psql(DBNAME, "select count(*) from users where not active"))}
    live_lat = [x for k in ("tap:answer", "tap:next", "/quizme") for x in rig.lat.get(k, [])]
    res["live_student_latency_during_push"] = {"p50": round(pct(live_lat, 50), 2),
                                               "p95": round(pct(live_lat, 95), 2),
                                               "max": round(max(live_lat), 2) if live_lat else 0}
    res["integrity"] = integrity(rig, live_uids)
    await rig.stop()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["interactive", "broadcast"])
    ap.add_argument("--students", type=int, default=50)
    ap.add_argument("--users", type=int, default=500)
    ap.add_argument("--live", type=int, default=0)
    ap.add_argument("--blocked", type=float, default=0.03)
    ap.add_argument("--sets", type=int, default=2)
    ap.add_argument("--think", type=float, nargs=2, default=(1.0, 4.0))
    ap.add_argument("--ramp", type=float, default=10.0)
    ap.add_argument("--read", type=float, nargs=2, default=None)
    ap.add_argument("--edits-count", dest="edits_count", action="store_true",
                    help="count edits against the global flood budget too")
    ap.add_argument("--latency", type=float, nargs=2, default=(0.12, 0.25))
    ap.add_argument("--pool", type=int, default=None)
    ap.add_argument("--no-limits", dest="limits", action="store_false")
    ap.add_argument("--no-extras", dest="extras", action="store_false")
    ap.add_argument("--tournament", action="store_true")
    ap.add_argument("--guard", action="store_true", help="install bot.throttle.FloodGuard")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = asyncio.run(interactive(a) if a.scenario == "interactive" else broadcast(a))
    text = json.dumps(res, indent=1, default=str)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
