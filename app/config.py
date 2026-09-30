import os

MYSQL = dict(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", "root"),
    database=os.getenv("MYSQL_DB", "bookmyshow"),
)
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "32"))
DB_POOL_WAIT_S = float(os.getenv("DB_POOL_WAIT_S", "3"))   # queue wait before we shed load (HTTP 503)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

HOLD_TTL_S = int(os.getenv("HOLD_TTL_S", "600"))            # seat hold: 10 minutes
PAYMENT_WINDOW_S = int(os.getenv("PAYMENT_WINDOW_S", "300"))  # extra time once a booking is created
MAX_SEATS_PER_HOLD = int(os.getenv("MAX_SEATS_PER_HOLD", "6"))
REAPER_INTERVAL_S = float(os.getenv("REAPER_INTERVAL_S", "5"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dev-webhook-secret").encode()
LOCK_MODE = os.getenv("LOCK_MODE", "cas")                   # 'cas' (optimistic) | 'pessimistic'
