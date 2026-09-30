"""
High-Concurrency Booking Engine (BookMyShow-style) - FastAPI reference service.

Concurrency model
------------------
1. FAST PATH  (Redis):  Lua script atomically checks+sets one key per seat
   (SET NX PX). Rejects a competing request in microseconds without ever
   touching MySQL, so a sold-out burst does not fall through to the DB.
2. SOURCE OF TRUTH (MySQL): the hold is only durable once a conditional
   UPDATE flips show_seat rows AVAILABLE -> HELD and the row count matches
   the seat count requested. A UNIQUE(show_id, seat_id) + CHECK state-machine
   constraint on show_seat make double-booking impossible even if Redis is
   skipped, stale, or flushed - Redis is an accelerator, not the guarantee.
3. EXPIRY: holds carry expires_at; a background reaper releases expired
   holds back to AVAILABLE. Redis keys also carry a PX TTL as a second,
   independent expiry mechanism.
4. PAYMENT WEBHOOKS: payment_webhook_event.event_id is the PRIMARY KEY, so
   re-delivering the same gateway event is a no-op (idempotent) even under
   concurrent duplicate delivery.
"""
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta

import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import config, seatlock
from .db import PoolExhausted, query, run_tx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("booking")

app = FastAPI(title="High-Concurrency Booking Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # demo/reference frontend only - lock this down in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------
class HoldRequest(BaseModel):
    user_id: int
    show_id: int
    seat_ids: list[int] = Field(..., min_length=1, max_length=config.MAX_SEATS_PER_HOLD)


class HoldResponse(BaseModel):
    hold_id: int
    lock_token: str
    expires_at: str
    seat_ids: list[int]


class ConfirmRequest(BaseModel):
    hold_id: int
    lock_token: str


class ConfirmResponse(BaseModel):
    booking_id: int
    gateway_order_id: str
    amount: float
    status: str


class WebhookEvent(BaseModel):
    event_id: str
    gateway_order_id: str
    type: str            # payment.success | payment.failed
    amount: float | None = None


# ----------------------------------------------------------------------
# 1) HOLD SEATS  (Redis fast path + MySQL conditional UPDATE)
# ----------------------------------------------------------------------
@app.post("/shows/{show_id}/hold", response_model=HoldResponse)
def hold_seats(show_id: int, req: HoldRequest):
    if req.show_id != show_id:
        raise HTTPException(400, "show_id mismatch")
    seat_ids = sorted(set(req.seat_ids))          # fixed order => no lock-ordering deadlocks
    token = secrets.token_hex(16)
    ttl_s = config.HOLD_TTL_S

    redis_ok = seatlock.acquire(show_id, seat_ids, token, ttl_s)
    if redis_ok is False:
        raise HTTPException(409, "One or more seats are already held or booked")
    # redis_ok is True, or None (Redis down) -> fall through to MySQL, the real gate

    def tx(cur):
        expires_at = datetime.utcnow() + timedelta(seconds=ttl_s)
        cur.execute(
            "INSERT INTO seat_hold (user_id, show_id, seat_count, lock_token, status, expires_at) "
            "VALUES (%s,%s,%s,%s,'ACTIVE',%s)",
            (req.user_id, show_id, len(seat_ids), token, expires_at),
        )
        hold_id = cur.lastrowid

        # Conditional UPDATE: only rows that are still AVAILABLE flip to HELD.
        # If a racing request already took one, rowcount < len(seat_ids) and we abort.
        placeholders = ",".join(["%s"] * len(seat_ids))
        cur.execute(
            f"""UPDATE show_seat
                SET status='HELD', hold_id=%s
                WHERE show_id=%s AND seat_id IN ({placeholders}) AND status='AVAILABLE'""",
            (hold_id, show_id, *seat_ids),
        )
        if cur.rowcount != len(seat_ids):
            raise HTTPException(409, "One or more seats are no longer available")
        return hold_id, expires_at

    try:
        hold_id, expires_at = run_tx(tx)
    except PoolExhausted:
        seatlock.release(show_id, seat_ids, token)
        raise HTTPException(503, "System is busy, please retry shortly")
    except HTTPException:
        seatlock.release(show_id, seat_ids, token)   # undo the fast-path lock we took
        raise
    except pymysql.err.IntegrityError:
        seatlock.release(show_id, seat_ids, token)
        raise HTTPException(400, "Invalid show or seat id")

    return HoldResponse(
        hold_id=hold_id, lock_token=token,
        expires_at=expires_at.isoformat() + "Z", seat_ids=seat_ids,
    )


# ----------------------------------------------------------------------
# 2) CONFIRM HOLD -> create a booking + a payment order (still unpaid)
# ----------------------------------------------------------------------
@app.post("/bookings/confirm", response_model=ConfirmResponse)
def confirm_hold(req: ConfirmRequest):
    def tx(cur):
        cur.execute(
            "SELECT * FROM seat_hold WHERE hold_id=%s AND lock_token=%s FOR UPDATE",
            (req.hold_id, req.lock_token),
        )
        hold = cur.fetchone()
        if not hold:
            raise HTTPException(404, "Hold not found or token mismatch")
        if hold["status"] != "ACTIVE":
            raise HTTPException(409, f"Hold is {hold['status']}, not ACTIVE")
        if hold["expires_at"] < datetime.utcnow():
            raise HTTPException(410, "Hold expired")

        cur.execute(
            """SELECT ss.seat_id, sp.price FROM show_seat ss
               JOIN seat s ON s.seat_id = ss.seat_id
               JOIN show_price sp ON sp.show_id = ss.show_id AND sp.category_id = s.category_id
               WHERE ss.hold_id=%s""",
            (hold["hold_id"],),
        )
        seats = cur.fetchall()
        total = sum(float(r["price"]) for r in seats)
        seat_ids = [r["seat_id"] for r in seats]

        cur.execute(
            "INSERT INTO booking (user_id, show_id, hold_id, status, total_amount) "
            "VALUES (%s,%s,%s,'PENDING_PAYMENT',%s)",
            (hold["user_id"], hold["show_id"], hold["hold_id"], total),
        )
        booking_id = cur.lastrowid
        order_id = f"order_{uuid.uuid4().hex[:20]}"
        cur.execute(
            "INSERT INTO payment (booking_id, gateway_order_id, amount, status) "
            "VALUES (%s,%s,%s,'CREATED')",
            (booking_id, order_id, total),
        )
        cur.execute("UPDATE seat_hold SET status='CONFIRMED' WHERE hold_id=%s", (hold["hold_id"],))
        return booking_id, order_id, total, hold["show_id"], seat_ids, hold["lock_token"]

    booking_id, order_id, total, show_id, seat_ids, token = run_tx(tx)
    seatlock.extend(show_id, seat_ids, token, config.PAYMENT_WINDOW_S)
    return ConfirmResponse(booking_id=booking_id, gateway_order_id=order_id, amount=total, status="PENDING_PAYMENT")


# ----------------------------------------------------------------------
# 3) PAYMENT WEBHOOK - idempotent by PRIMARY KEY(event_id)
# ----------------------------------------------------------------------
@app.post("/webhooks/payment")
def payment_webhook(evt: WebhookEvent):
    def tx(cur):
        try:
            cur.execute(
                "INSERT INTO payment_webhook_event (event_id, gateway_order_id, event_type, payload) "
                "VALUES (%s,%s,%s,%s)",
                (evt.event_id, evt.gateway_order_id, evt.type, __import__("json").dumps(evt.model_dump())),
            )
        except pymysql.err.IntegrityError:
            return {"status": "duplicate_ignored", "event_id": evt.event_id}   # already processed

        cur.execute("SELECT * FROM payment WHERE gateway_order_id=%s FOR UPDATE", (evt.gateway_order_id,))
        pay = cur.fetchone()
        if not pay:
            raise HTTPException(404, "Unknown gateway_order_id")
        if pay["status"] != "CREATED":
            cur.execute("UPDATE payment_webhook_event SET processed_at=NOW(3) WHERE event_id=%s", (evt.event_id,))
            return {"status": "already_processed"}

        new_status = "SUCCESS" if evt.type == "payment.success" else "FAILED"
        cur.execute("UPDATE payment SET status=%s WHERE payment_id=%s", (new_status, pay["payment_id"]))
        cur.execute("SELECT * FROM booking WHERE booking_id=%s FOR UPDATE", (pay["booking_id"],))
        booking = cur.fetchone()

        cur.execute(
            """SELECT ss.seat_id FROM show_seat ss JOIN seat_hold h ON h.hold_id = ss.hold_id
               WHERE h.hold_id=%s""",
            (booking["hold_id"],),
        )
        seat_ids = [r["seat_id"] for r in cur.fetchall()]

        if new_status == "SUCCESS":
            cur.execute("UPDATE booking SET status='CONFIRMED' WHERE booking_id=%s", (booking["booking_id"],))
            cur.execute(
                "UPDATE show_seat SET status='BOOKED', booking_id=%s WHERE hold_id=%s",
                (booking["booking_id"], booking["hold_id"]),
            )
        else:
            cur.execute("UPDATE booking SET status='FAILED' WHERE booking_id=%s", (booking["booking_id"],))
            cur.execute(
                "UPDATE show_seat SET status='AVAILABLE', hold_id=NULL WHERE hold_id=%s",
                (booking["hold_id"],),
            )
            cur.execute("UPDATE seat_hold SET status='RELEASED' WHERE hold_id=%s", (booking["hold_id"],))

        cur.execute("UPDATE payment_webhook_event SET processed_at=NOW(3) WHERE event_id=%s", (evt.event_id,))
        return {"status": "ok", "booking_status": new_status, "show_id": booking["show_id"], "seat_ids": seat_ids}

    result = run_tx(tx)
    if result.get("show_id"):
        seat_ids, show_id = result.pop("seat_ids"), result.pop("show_id")
        if result["booking_status"] == "SUCCESS":
            seatlock.mark_booked(show_id, seat_ids, "n/a")
        else:
            seatlock.release(show_id, seat_ids, "n/a")
    return result


# ----------------------------------------------------------------------
# 4) P2 read endpoint - shows at a theatre on a date
# ----------------------------------------------------------------------
@app.get("/theatres/{theatre_id}/dates")
def theatre_dates(theatre_id: int):
    rows = query(
        """SELECT DISTINCT ms.show_date
           FROM screen sc JOIN movie_show ms ON ms.screen_id = sc.screen_id
           WHERE sc.theatre_id=%s AND ms.status='SCHEDULED'
           ORDER BY ms.show_date""",
        (theatre_id,),
    )
    return [str(r["show_date"]) for r in rows]


@app.get("/theatres/{theatre_id}/shows")
def shows_by_date(theatre_id: int, show_date: str):
    rows = query(
        """SELECT t.name AS theatre, m.title AS movie, l.name AS language, f.name AS format,
                  sc.name AS screen, ms.show_id, ms.show_date,
                  TIME_FORMAT(ms.start_time,'%%h:%%i %%p') AS show_time
           FROM theatre t
           JOIN screen sc ON sc.theatre_id = t.theatre_id
           JOIN movie_show ms ON ms.screen_id = sc.screen_id
           JOIN movie m ON m.movie_id = ms.movie_id
           JOIN language l ON l.language_id = ms.language_id
           JOIN show_format f ON f.format_id = ms.format_id
           WHERE t.theatre_id=%s AND ms.show_date=%s AND ms.status='SCHEDULED'
           ORDER BY m.title, ms.start_time""",
        (theatre_id, show_date),
    )
    return {"theatre_id": theatre_id, "show_date": show_date, "shows": rows}


@app.get("/theatres")
def list_theatres():
    return query(
        """SELECT t.theatre_id, t.name, t.address, c.name AS city
           FROM theatre t JOIN city c ON c.city_id = t.city_id ORDER BY t.name"""
    )


@app.get("/shows/{show_id}/seats")
def show_seat_map(show_id: int):
    show = query(
        """SELECT ms.show_id, ms.show_date, TIME_FORMAT(ms.start_time,'%%h:%%i %%p') AS show_time,
                  m.title AS movie, l.name AS language, f.name AS format,
                  t.name AS theatre, sc.name AS screen
           FROM movie_show ms
           JOIN screen sc ON sc.screen_id = ms.screen_id
           JOIN theatre t ON t.theatre_id = sc.theatre_id
           JOIN movie m ON m.movie_id = ms.movie_id
           JOIN language l ON l.language_id = ms.language_id
           JOIN show_format f ON f.format_id = ms.format_id
           WHERE ms.show_id=%s""",
        (show_id,),
    )
    if not show:
        raise HTTPException(404, "Show not found")

    seats = query(
        """SELECT s.seat_id, s.row_label, s.seat_number, cat.name AS category,
                  sp.price, ss.status
           FROM show_seat ss
           JOIN seat s ON s.seat_id = ss.seat_id
           JOIN seat_category cat ON cat.category_id = s.category_id
           JOIN show_price sp ON sp.show_id = ss.show_id AND sp.category_id = s.category_id
           WHERE ss.show_id=%s
           ORDER BY s.row_label, s.seat_number""",
        (show_id,),
    )
    return {"show": show[0], "seats": seats}


@app.get("/health")
def health():
    return {"ok": True, "redis": seatlock.ping()}


# ----------------------------------------------------------------------
# Background reaper: release expired holds every REAPER_INTERVAL_S
# ----------------------------------------------------------------------
def reap_expired_holds():
    def tx(cur):
        cur.execute(
            "SELECT hold_id, show_id, lock_token FROM seat_hold "
            "WHERE status='ACTIVE' AND expires_at < NOW(3) LIMIT 200 FOR UPDATE SKIP LOCKED",
        )
        holds = cur.fetchall()
        released = []
        for h in holds:
            cur.execute(
                "SELECT seat_id FROM show_seat WHERE hold_id=%s AND status='HELD'",
                (h["hold_id"],),
            )
            seat_ids = [r["seat_id"] for r in cur.fetchall()]
            cur.execute(
                "UPDATE show_seat SET status='AVAILABLE', hold_id=NULL "
                "WHERE hold_id=%s AND status='HELD'",
                (h["hold_id"],),
            )
            cur.execute("UPDATE seat_hold SET status='EXPIRED' WHERE hold_id=%s", (h["hold_id"],))
            if seat_ids:
                released.append((h["show_id"], seat_ids, h["lock_token"]))
        return released

    while True:
        try:
            released = run_tx(tx)
            # Release the Redis fast-path locks too, so the seats are immediately
            # re-holdable and don't wait out their own independent Redis TTL.
            for show_id, seat_ids, token in released:
                seatlock.release(show_id, seat_ids, token)
            if released:
                log.info("reaper released %d expired hold(s)", len(released))
        except Exception as e:
            log.warning("reaper tick failed: %s", e)
        time.sleep(config.REAPER_INTERVAL_S)


@app.on_event("startup")
def _start_reaper():
    threading.Thread(target=reap_expired_holds, daemon=True).start()
