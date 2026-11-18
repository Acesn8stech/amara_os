"""Atomic cross-machine work-order locks.

One row per (tract_id, scope) in `tract_locks`. Acquisition is a single atomic
INSERT .. ON CONFLICT DO UPDATE .. WHERE <expired or mine> RETURNING owner statement, so two
machines racing for the same tract can never both win (works on SQLite >= 3.35 and Postgres).
Expired locks (crashed worker) are taken over automatically.
"""
from __future__ import annotations

import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .store import Store


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class LockHeld(Exception):
    def __init__(self, tract_id: str, scope: str, holder: str):
        super().__init__(f"{tract_id}/{scope} is locked by {holder}")
        self.holder = holder


class LockManager:
    def __init__(self, store: Store, ttl_seconds: int = 900):
        self.store = store
        self.ttl = ttl_seconds

    @staticmethod
    def owner_id(agent_id: str) -> str:
        return f"{agent_id}@{socket.gethostname()}"

    def acquire(self, tract_id: str, scope: str, owner: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        params = {"t": tract_id, "s": scope, "o": owner, "now": _iso(now),
                  "exp": _iso(now + timedelta(seconds=self.ttl))}
        sql = text("""
            INSERT INTO tract_locks (tract_id, scope, owner, acquired_at, expires_at)
            VALUES (:t, :s, :o, :now, :exp)
            ON CONFLICT (tract_id, scope) DO UPDATE
               SET owner = excluded.owner, acquired_at = excluded.acquired_at, expires_at = excluded.expires_at
             WHERE tract_locks.expires_at < :now OR tract_locks.owner = excluded.owner
            RETURNING owner
        """)
        with self.store.engine.begin() as conn:
            row = conn.execute(sql, params).first()
        return bool(row) and row[0] == owner

    def holder(self, tract_id: str, scope: str) -> str | None:
        row = self.store.scalar("SELECT owner FROM tract_locks WHERE tract_id=:t AND scope=:s", t=tract_id, s=scope)
        return row

    def release(self, tract_id: str, scope: str, owner: str) -> bool:
        n = self.store.execute("DELETE FROM tract_locks WHERE tract_id=:t AND scope=:s AND owner=:o",
                               t=tract_id, s=scope, o=owner)
        return n > 0

    @contextmanager
    def hold(self, tract_id: str, scope: str, owner: str):
        if not self.acquire(tract_id, scope, owner):
            raise LockHeld(tract_id, scope, self.holder(tract_id, scope) or "unknown")
        try:
            yield
        finally:
            self.release(tract_id, scope, owner)
