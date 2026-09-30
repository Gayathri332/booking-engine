"""Redis fast-path seat locks (SET NX PX semantics, all-or-nothing via Lua).

Redis is an *accelerator and timer*, not the source of truth: if it is down or
flushed, MySQL (conditional UPDATE + CHECK + UNIQUE) still prevents double booking.
"""
import logging

import redis

from . import config

log = logging.getLogger("seatlock")
_r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True, socket_timeout=0.5)

# KEYS = seat keys, ARGV[1] = owner token, ARGV[2] = ttl ms.  Returns 0 or index of first taken key.
_ACQUIRE = _r.register_script("""
for i = 1, #KEYS do
  if redis.call('EXISTS', KEYS[i]) == 1 then return i end
end
for i = 1, #KEYS do
  redis.call('SET', KEYS[i], ARGV[1], 'PX', ARGV[2])
end
return 0
""")
_RELEASE = _r.register_script("""
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then redis.call('DEL', KEYS[i]) end
end
return 1
""")
_EXTEND = _r.register_script("""
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then redis.call('PEXPIRE', KEYS[i], ARGV[2]) end
end
return 1
""")
_BOOKED = _r.register_script("""
for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then
    redis.call('SET', KEYS[i], 'BOOKED', 'PX', ARGV[2])
  end
end
return 1
""")


def key(show_id, seat_id):
    # {show_id} hash-tag => all seats of a show live in one Redis Cluster slot
    return f"seat:{{{show_id}}}:{seat_id}"


def _keys(show_id, seat_ids):
    return [key(show_id, s) for s in seat_ids]


def acquire(show_id, seat_ids, token, ttl_s):
    """True = locked, False = at least one seat already locked, None = Redis unavailable."""
    try:
        return _ACQUIRE(keys=_keys(show_id, seat_ids), args=[token, int(ttl_s * 1000)]) == 0
    except redis.RedisError as e:
        log.warning("redis acquire failed, falling back to DB only: %s", e)
        return None


def release(show_id, seat_ids, token):
    try:
        _RELEASE(keys=_keys(show_id, seat_ids), args=[token])
    except redis.RedisError as e:
        log.warning("redis release failed: %s", e)


def extend(show_id, seat_ids, token, ttl_s):
    try:
        _EXTEND(keys=_keys(show_id, seat_ids), args=[token, int(ttl_s * 1000)])
    except redis.RedisError as e:
        log.warning("redis extend failed: %s", e)


def mark_booked(show_id, seat_ids, token, ttl_s=86400):
    try:
        _BOOKED(keys=_keys(show_id, seat_ids), args=[token, int(ttl_s * 1000)])
    except redis.RedisError as e:
        log.warning("redis mark_booked failed: %s", e)


def restore(show_id, seat_ids, token, ttl_ms):
    """Rebuild a lock after a Redis flush/restart (NX so we never overwrite a newer owner)."""
    try:
        pipe = _r.pipeline()
        for k in _keys(show_id, seat_ids):
            pipe.set(k, token, px=max(int(ttl_ms), 1), nx=True)
        pipe.execute()
    except redis.RedisError as e:
        log.warning("redis restore failed: %s", e)


def ping():
    try:
        return _r.ping()
    except redis.RedisError:
        return False
