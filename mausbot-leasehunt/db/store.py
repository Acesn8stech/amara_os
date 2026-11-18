"""Thin SQLAlchemy store shared by every agent (SQLite local dev, Postgres/Supabase in prod)."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

JSON_COLUMNS = {
    "lease_info", "deal_card", "payload", "entries", "nma_math", "defects", "field_tickets",
    "evidence", "reasons", "detail", "evidence_map",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


class Store:
    def __init__(self, url: str = "sqlite:///leasehunt.db"):
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite") and ":memory:" in url:
            kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
        elif url.startswith("sqlite"):
            kwargs.update(connect_args={"check_same_thread": False, "timeout": 30})
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        self.url = url

    # ------------------------------------------------------------------ schema
    def init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text()
        sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
        with self.engine.begin() as conn:
            for stmt in (s.strip() for s in sql.split(";\n")):
                stmt = stmt.rstrip(";").strip()
                if stmt:
                    conn.exec_driver_sql(stmt)

    # ------------------------------------------------------------------ generic CRUD
    @staticmethod
    def _enc(values: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in values.items():
            if k in JSON_COLUMNS and not isinstance(v, str) and v is not None:
                v = json.dumps(v, default=str, sort_keys=True)
            elif isinstance(v, bool):
                v = int(v)
            out[k] = v
        return out

    @staticmethod
    def _dec(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for k in JSON_COLUMNS & out.keys():
            if isinstance(out[k], str):
                try:
                    out[k] = json.loads(out[k])
                except ValueError:
                    pass
        return out

    def insert(self, table: str, **values: Any) -> None:
        values = self._enc(values)
        cols = ", ".join(_ident(c) for c in values)
        params = ", ".join(f":{c}" for c in values)
        with self.engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {_ident(table)} ({cols}) VALUES ({params})"), values)

    def update(self, table: str, key_col: str, key: Any, **values: Any) -> int:
        if not values:
            return 0
        values = self._enc(values)
        sets = ", ".join(f"{_ident(c)} = :v_{c}" for c in values)
        params = {f"v_{c}": v for c, v in values.items()}
        params["k"] = key
        with self.engine.begin() as conn:
            res = conn.execute(text(f"UPDATE {_ident(table)} SET {sets} WHERE {_ident(key_col)} = :k"), params)
            return res.rowcount

    def get(self, table: str, key_col: str, key: Any) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT * FROM {_ident(table)} WHERE {_ident(key_col)} = :k"), {"k": key}
            ).mappings().first()
        return self._dec(dict(row) if row else None)

    def select(self, table: str, where: dict[str, Any] | None = None, order_by: str | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        where = where or {}
        clause = " AND ".join(f"{_ident(c)} = :{c}" for c in where)
        sql = f"SELECT * FROM {_ident(table)}" + (f" WHERE {clause}" if clause else "")
        if order_by:
            sql += f" ORDER BY {_ident(order_by.split()[0])}" + (" DESC" if order_by.endswith(" DESC") else "")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), where).mappings().all()
        return [self._dec(dict(r)) for r in rows]

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def execute(self, sql: str, **params: Any) -> int:
        with self.engine.begin() as conn:
            return conn.execute(text(sql), params).rowcount

    # ------------------------------------------------------------------ domain helpers
    def audit(self, actor: str, event: str, tract_id: str | None = None, **detail: Any) -> None:
        self.insert("audit_log", id=new_id("a_"), ts=now_iso(), actor=actor, event=event,
                    tract_id=tract_id, detail=detail)

    def update_tract(self, tract_id: str, **values: Any) -> None:
        values["updated_at"] = now_iso()
        self.update("tracts", "tract_id", tract_id, **values)

    def get_tract(self, tract_id: str) -> dict[str, Any] | None:
        return self.get("tracts", "tract_id", tract_id)

    def latest_market(self) -> dict[str, Any] | None:
        rows = self.select("market_snapshots", order_by="created_at DESC", limit=1)
        return rows[0] if rows else None

    def expenses_total_cents(self, tract_id: str) -> int:
        return int(self.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM tract_expenses WHERE tract_id=:t",
                               t=tract_id) or 0)

    def active_count(self, statuses: Iterable[str]) -> int:
        marks = ", ".join(f":s{i}" for i, _ in enumerate(statuses))
        params = {f"s{i}": s for i, s in enumerate(statuses)}
        return int(self.scalar(f"SELECT COUNT(*) FROM tracts WHERE status IN ({marks})", **params) or 0)
