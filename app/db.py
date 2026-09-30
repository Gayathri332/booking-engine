"""Tiny thread-safe MySQL pool + transaction runner with deadlock retry."""
import queue
import random
import time
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from . import config


class PoolExhausted(Exception):
    pass


def _connect():
    c = pymysql.connect(
        **config.MYSQL,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )
    with c.cursor() as cur:
        # READ COMMITTED => no gap locks => far fewer deadlocks on hot seat rows
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute("SET time_zone = '+00:00'")
    c.commit()
    return c


class Pool:
    def __init__(self, size):
        self._q = queue.LifoQueue()
        for _ in range(size):
            self._q.put(_connect())

    @contextmanager
    def conn(self):
        try:
            c = self._q.get(timeout=config.DB_POOL_WAIT_S)
        except queue.Empty:
            raise PoolExhausted()
        try:
            yield c
        finally:
            try:
                c.rollback()
            except Exception:
                try:
                    c = _connect()
                except Exception:
                    pass
            self._q.put(c)


_pool = None


def pool():
    global _pool
    if _pool is None:
        _pool = Pool(config.DB_POOL_SIZE)
    return _pool


RETRYABLE = (1213, 1205)   # deadlock, lock wait timeout


def run_tx(fn, retries=5):
    """Run fn(cursor) in one transaction; retry on deadlock / lock-wait timeout."""
    for attempt in range(retries):
        with pool().conn() as c:
            try:
                with c.cursor() as cur:
                    res = fn(cur)
                c.commit()
                return res
            except pymysql.err.OperationalError as e:
                c.rollback()
                if e.args and e.args[0] in RETRYABLE and attempt < retries - 1:
                    time.sleep(random.uniform(0, 0.02) * (attempt + 1))
                    continue
                raise
            except Exception:
                c.rollback()
                raise


def query(sql, args=None):
    with pool().conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows
