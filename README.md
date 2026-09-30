# High-Concurrency Booking Engine

A BookMyShow-style ticketing backend: seat-level locking with timed holds,
idempotent payment webhooks, and a self-designed load test proving no
double-booked seats under concurrency.

See **`docs/submission.pdf`** for the full write-up (schema, normalization,
locking strategy, P2 query, and load-test results).

## Layout
```
sql/       P1 schema + seed data + P2 queries
app/       FastAPI service (hold / confirm / webhook / P2 endpoint)
loadtest/  the load test used to prove correctness under concurrency
docs/      submission.md / submission.pdf
```

## Running it locally

Requirements: MySQL 8.0.16+, Redis, Python 3.10+.

```bash
pip install -r requirements.txt

# 1) create schema + seed data
mysql -u root -p < sql/01_schema.sql
mysql -u root -p < sql/02_seed.sql

# 2) start Redis (defaults to redis://127.0.0.1:6379/0)
redis-server &

# 3) configure DB creds (defaults shown; override via env vars)
export MYSQL_USER=root MYSQL_PASSWORD=yourpass MYSQL_DB=bookmyshow

# 4) run the API
uvicorn app.main:app --reload
```
cd frontend && python3 -m http.server 5500
# open http://127.0.0.1:5500


## API

| Endpoint | Purpose |
|---|---|
| `POST /shows/{show_id}/hold` | Hold N seats for 10 min (Redis fast path + MySQL conditional UPDATE) |
| `POST /bookings/confirm` | Turn an active hold into a `PENDING_PAYMENT` booking + payment order |
| `POST /webhooks/payment` | Idempotent payment-gateway webhook (`payment.success` / `payment.failed`) |
| `GET /theatres/{id}/shows?show_date=YYYY-MM-DD` | P2: shows at a theatre on a date |
| `GET /health` | Liveness + Redis connectivity |

## Load test

```bash
# same 2 seats, 300 concurrent users -> exactly 1 winner
python3 loadtest/load_test.py hot --n 300 --show-id 1

# full show, 400 concurrent users, hold->confirm->pay chain -> zero double-bookings
python3 loadtest/load_test.py burst --n 400 --show-id 2
```

Both scenarios reset the target show's seats before running and assert
correctness (exactly one winner / zero duplicate-seat rows) at the end,
so a regression fails loudly instead of silently.

## Config (env vars, see `app/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `HOLD_TTL_S` | 600 | How long a seat hold lasts before the reaper releases it |
| `PAYMENT_WINDOW_S` | 300 | Extra grace period once a booking (payment order) is created |
| `MAX_SEATS_PER_HOLD` | 6 | Per-request seat cap |
| `DB_POOL_SIZE` | 32 | MySQL connection pool size |
| `LOCK_MODE` | cas | Reserved for future optimistic (`cas`) vs pessimistic-only toggling |
