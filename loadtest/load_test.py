"""
Self-designed load test for the High-Concurrency Booking Engine.

Two scenarios, both against a LIVE running instance (default http://127.0.0.1:8000):

  A) HOT-SEAT STAMPEDE
     N concurrent "users" all try to hold the SAME small block of seats
     (the worst case: a front-row block on a Friday premiere). Correctness
     bar: exactly ONE request may win; everyone else gets 409.

  B) REALISTIC BURST
     N concurrent users spread across a big show's seat map, each hoisting
     a random small group of seats, then confirming + paying immediately.
     Measures throughput/latency and confirms zero double-bookings across
     the whole show.

Usage:
    python3 load_test.py hot   --n 200
    python3 load_test.py burst --n 500 --show-id 1
"""
import argparse
import concurrent.futures as cf
import random
import statistics
import time

import httpx
import pymysql

BASE = "http://127.0.0.1:8000"
MYSQL = dict(host="127.0.0.1", user="root", password="root", database="bookmyshow")


def db():
    return pymysql.connect(**MYSQL, cursorclass=pymysql.cursors.DictCursor)


def reset_redis(show_id, seat_ids):
    import redis
    r = redis.Redis(host="127.0.0.1", port=6379, db=0)
    if seat_ids:
        r.delete(*[f"seat:{{{show_id}}}:{s}" for s in seat_ids])


def ensure_load_test_users(n=25):
    """The seed data only ships 3 demo users; a load test needs many concurrent
    *distinct* users, so make sure load-test-user-1..n exist (idempotent)."""
    with db() as c, c.cursor() as cur:
        cur.executemany(
            "INSERT IGNORE INTO app_user (user_id, name, email) VALUES (%s,%s,%s)",
            [(1000 + i, f"Load Test User {i}", f"loadtest{i}@example.com") for i in range(1, n + 1)],
        )
        c.commit()


def reset_show(show_id):
    ensure_load_test_users()
    reset_redis(show_id, all_seat_ids(show_id))
    with db() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE show_seat SET status='AVAILABLE', hold_id=NULL, booking_id=NULL WHERE show_id=%s",
            (show_id,),
        )
        cur.execute(
            "DELETE p FROM payment p JOIN booking b ON b.booking_id=p.booking_id WHERE b.show_id=%s",
            (show_id,),
        )
        cur.execute("DELETE FROM booking WHERE show_id=%s", (show_id,))
        cur.execute("DELETE FROM seat_hold WHERE show_id=%s", (show_id,))
        c.commit()


def all_seat_ids(show_id):
    with db() as c, c.cursor() as cur:
        cur.execute("SELECT seat_id FROM show_seat WHERE show_id=%s", (show_id,))
        return [r["seat_id"] for r in cur.fetchall()]


# ------------------------------------------------------------------ A
def hot_seat_stampede(show_id, n):
    reset_show(show_id)
    seats = sorted(all_seat_ids(show_id))[:2]        # the 2 "best" seats everyone wants
    print(f"[hot] {n} concurrent users all racing for seats {seats} on show {show_id}")

    def attempt(i):
        t0 = time.perf_counter()
        try:
            r = httpx.post(f"{BASE}/shows/{show_id}/hold",
                            json={"user_id": 1000 + (i % 20) + 1, "show_id": show_id, "seat_ids": seats},
                            timeout=10)
            return r.status_code, time.perf_counter() - t0
        except Exception:
            return "ERR", time.perf_counter() - t0

    with cf.ThreadPoolExecutor(max_workers=min(n, 200)) as ex:
        results = list(ex.map(attempt, range(n)))

    codes = [r[0] for r in results]
    lat = sorted(r[1] for r in results)
    wins = codes.count(200)
    conflicts = codes.count(409)

    with db() as c, c.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) n FROM show_seat WHERE show_id=%s AND seat_id IN %s AND status='HELD'",
            (show_id, tuple(seats)),
        )
        held_now = cur.fetchone()["n"]

    other_codes = [c for c in codes if c not in (200, 409)]
    print(f"  requests={n}  winners={wins}  conflicts={conflicts}  other={n-wins-conflicts} {other_codes[:10]}")
    print(f"  seats actually HELD in DB afterwards: {held_now} (must equal {len(seats)}, never more)")
    print(f"  latency ms  p50={lat[len(lat)//2]*1000:.1f}  p95={lat[int(len(lat)*0.95)]*1000:.1f}  max={lat[-1]*1000:.1f}")
    assert wins == 1, "CORRECTNESS FAILURE: more than one winner for the same seats!"
    assert held_now == len(seats), "CORRECTNESS FAILURE: seat count mismatch after race!"
    print("  RESULT: PASS - no double booking under concurrent stampede\n")


# ------------------------------------------------------------------ B
def realistic_burst(show_id, n):
    reset_show(show_id)
    seats = all_seat_ids(show_id)
    random.shuffle(seats)
    capacity = len(seats)
    print(f"[burst] {n} concurrent users, show {show_id} has {capacity} seats total")

    # Partition seats into small non-overlapping groups so we can also see
    # the "sold out" tail once capacity runs out - like a real premiere.
    groups, i = [], 0
    while i < len(seats):
        k = random.choice([1, 2, 2, 3])
        groups.append(seats[i:i + k])
        i += k
    random.shuffle(groups)
    # more users than groups on purpose -> guarantees a sold-out tail
    jobs = (groups * ((n // max(len(groups), 1)) + 1))[:n]

    def attempt(i, seat_group):
        t0 = time.perf_counter()
        user_id = 1000 + (i % 20) + 1
        try:
            r = httpx.post(f"{BASE}/shows/{show_id}/hold",
                            json={"user_id": user_id, "show_id": show_id, "seat_ids": seat_group},
                            timeout=10)
            if r.status_code != 200:
                return r.status_code, time.perf_counter() - t0
            hold = r.json()
            r2 = httpx.post(f"{BASE}/bookings/confirm",
                             json={"hold_id": hold["hold_id"], "lock_token": hold["lock_token"]}, timeout=10)
            if r2.status_code != 200:
                return r2.status_code, time.perf_counter() - t0
            order = r2.json()["gateway_order_id"]
            r3 = httpx.post(f"{BASE}/webhooks/payment",
                             json={"event_id": f"evt_{order}", "gateway_order_id": order,
                                   "type": "payment.success", "amount": r2.json()["amount"]},
                             timeout=10)
            return r3.status_code, time.perf_counter() - t0
        except Exception:
            return "ERR", time.perf_counter() - t0

    t_start = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=min(n, 300)) as ex:
        results = list(ex.map(lambda p: attempt(*p), enumerate(jobs)))
    wall = time.perf_counter() - t_start

    codes = [r[0] for r in results]
    lat = sorted(r[1] for r in results)
    ok = codes.count(200)
    conflict = codes.count(409)
    other = len(codes) - ok - conflict

    with db() as c, c.cursor() as cur:
        cur.execute(
            "SELECT seat_id, COUNT(*) c FROM show_seat WHERE show_id=%s AND status='BOOKED' "
            "GROUP BY seat_id HAVING c > 1", (show_id,),
        )
        dupes = cur.fetchall()
        cur.execute("SELECT COUNT(*) n FROM show_seat WHERE show_id=%s AND status='BOOKED'", (show_id,))
        booked = cur.fetchone()["n"]

    from collections import Counter
    other_dist = Counter(c for c in codes if c not in (200, 409))
    print(f"  jobs={len(jobs)}  confirmed+paid={ok}  seat-conflicts={conflict}  other={other} {dict(other_dist)}")
    print(f"  wall clock={wall:.2f}s   throughput={len(jobs)/wall:.1f} req/s (hold->confirm->webhook chain)")
    print(f"  latency ms  p50={lat[len(lat)//2]*1000:.1f}  p95={lat[int(len(lat)*0.95)]*1000:.1f}  max={lat[-1]*1000:.1f}")
    print(f"  seats booked in DB: {booked} / {capacity} capacity")
    print(f"  duplicate-seat rows (must be empty): {dupes}")
    assert not dupes, "CORRECTNESS FAILURE: a seat was booked more than once!"
    assert booked <= capacity
    print("  RESULT: PASS - zero double-bookings across full show capacity\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["hot", "burst"])
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--show-id", type=int, default=1)
    args = ap.parse_args()
    if args.scenario == "hot":
        hot_seat_stampede(args.show_id, args.n)
    else:
        realistic_burst(args.show_id, args.n)
