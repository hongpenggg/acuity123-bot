import asyncpg, random

pool: asyncpg.Pool | None = None

async def init(dsn):
    global pool
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, statement_cache_size=0)

async def upsert_user(uid, username):
    await pool.execute("""insert into users(telegram_id, username) values($1,$2)
        on conflict (telegram_id) do update set username=$2, active=true""", uid, username)

async def deactivate(uid):
    await pool.execute("update users set active=false where telegram_id=$1", uid)

async def set_flag(uid, col, val):
    assert col in ("weekly_sub", "notes_sub")
    await pool.execute(f"update users set {col}=$2 where telegram_id=$1", uid, val)

async def subscribers(col):
    assert col in ("weekly_sub", "notes_sub")
    rows = await pool.fetch(f"select telegram_id from users where {col} and active")
    return [r["telegram_id"] for r in rows]

async def topics():
    rows = await pool.fetch("select distinct topic from questions order by topic")
    return [r["topic"] for r in rows]

async def get_question(qid):
    return await pool.fetchrow("select * from questions where id=$1", qid)

async def pick_question(uid, topic=None, adaptive=True):
    from .config import MIN_ATTEMPTS_FOR_ADAPTIVE as MIN_N, WEIGHT_FLOOR as FLOOR
    if topic is None:
        all_topics = await topics()
        if not all_topics:
            return None
        stats = {r["topic"]: (r["n"], r["c"]) for r in await pool.fetch(
            """select topic, count(*) n, sum(correct::int) c
               from attempts where user_id=$1 group by topic""", uid)}
        total = sum(n for n, _ in stats.values())
        if adaptive and total >= MIN_N:
            def w(t):
                n, c = stats.get(t, (0, 0))
                return (1 - (c + 1) / (n + 2)) + FLOOR   # smoothed error rate + floor
            weights = [w(t) for t in all_topics]
        else:
            weights = [1] * len(all_topics)
        topic = random.choices(all_topics, weights)[0]
    return await pool.fetchrow(
        """select q.* from questions q where topic=$2
           order by exists(select 1 from attempts a where a.user_id=$1 and a.question_id=q.id),
                    random() limit 1""", uid, topic)

async def record_attempt(uid, q, idx, correct, mode, msg_id):
    r = await pool.fetchval(
        """insert into attempts(user_id,question_id,topic,chosen_idx,correct,mode,msg_id)
           values($1,$2,$3,$4,$5,$6,$7) on conflict (user_id,msg_id) do nothing returning id""",
        uid, q["id"], q["topic"], idx, correct, mode, msg_id)
    return r is not None

async def save_explanation(qid, text):
    await pool.execute("update questions set explanation=$2 where id=$1", qid, text)

async def active_tournament():
    return await pool.fetchrow(
        "select * from tournaments where active and now() between starts_at and ends_at limit 1")

async def start_tournament(days=14):
    return await pool.fetchval(
        """insert into tournaments(starts_at, ends_at)
           values(now(), now() + make_interval(days => $1)) returning id""", days)

async def join_tournament(uid):
    t = await active_tournament()
    if not t:
        return False
    await pool.execute("""insert into tournament_points(tournament_id,user_id) values($1,$2)
                          on conflict do nothing""", t["id"], uid)
    return True

async def leave_tournament(uid):
    t = await active_tournament()
    if t:
        await pool.execute("delete from tournament_points where tournament_id=$1 and user_id=$2",
                           t["id"], uid)

async def award_point(uid):
    await pool.execute(
        """update tournament_points set points=points+1
           where user_id=$1 and tournament_id=(select id from tournaments
             where active and now() between starts_at and ends_at limit 1)""", uid)

async def leaderboard():
    t = await active_tournament()
    if not t:
        return None
    return await pool.fetch(
        """select p.user_id, u.username, p.points,
                  rank() over (order by p.points desc) rk
           from tournament_points p join users u on u.telegram_id=p.user_id
           where p.tournament_id=$1 order by rk, p.user_id""", t["id"])

async def close_expired_tournaments():
    return await pool.fetch(
        "update tournaments set active=false where active and ends_at < now() returning id")

async def note_topics(tier):
    rows = await pool.fetch("select distinct topic from notes where tier=$1 order by 1", tier)
    return [r["topic"] for r in rows]

async def get_notes(topic, tier):
    return await pool.fetch(
        "select title, body from notes where lower(topic)=lower($1) and tier=$2 order by id", topic, tier)

async def all_notes(tier):
    return await pool.fetch("select topic, title, body from notes where tier=$1 order by topic, id", tier)