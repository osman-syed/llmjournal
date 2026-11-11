"""SQL shape (Postgres; also runs on SQLite with paramstyle="qmark").

Two tables: lj_traces and lj_generations. Timestamps are ISO-8601 text and JSON is text,
so the same schema works on any SQL database. Daily cost is a GROUP BY, no rollup table.

Writes are (sql, params) tuples. Queries are (sql, params) tuples. Rows must be dict-like
(psycopg: row_factory=dict_row; sqlite3: row_factory=sqlite3.Row).
"""

from __future__ import annotations

from typing import Any

from ..models import Cost, DailyCost, Generation, Totals, Trace
from ._common import (
    DEFAULT_MAX_INLINE_BYTES,
    Records,
    apply_payload,
    cost_from,
    dumps,
    iso,
    loads,
    parse_dt,
    payload,
    usage_from,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lj_traces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '',
    user_id TEXT,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    day TEXT NOT NULL,
    status TEXT,
    error TEXT,
    tags TEXT,
    metadata TEXT,
    input TEXT,
    output TEXT,
    call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    cost_input DOUBLE PRECISION,
    cost_output DOUBLE PRECISION,
    cost_cache_write DOUBLE PRECISION,
    cost_cache_read DOUBLE PRECISION,
    cost DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lj_traces_tenant_time ON lj_traces (tenant_id, started_at);
CREATE INDEX IF NOT EXISTS lj_traces_tenant_day ON lj_traces (tenant_id, day);
CREATE TABLE IF NOT EXISTS lj_generations (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES lj_traces(id) ON DELETE CASCADE,
    name TEXT,
    model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error TEXT,
    metadata TEXT,
    payload TEXT,
    payload_ref TEXT,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    cost_input DOUBLE PRECISION,
    cost_output DOUBLE PRECISION,
    cost_cache_write DOUBLE PRECISION,
    cost_cache_read DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS lj_generations_trace ON lj_generations (trace_id, started_at);
"""

_TRACE_COLS = (
    "id name tenant_id user_id session_id started_at ended_at day status error tags metadata input output "
    "call_count input_tokens output_tokens cache_write_tokens cache_read_tokens "
    "cost_input cost_output cost_cache_write cost_cache_read cost"
).split()
_GEN_COLS = (
    "id trace_id name model started_at ended_at error metadata payload payload_ref "
    "input_tokens output_tokens cache_write_tokens cache_read_tokens "
    "cost_input cost_output cost_cache_write cost_cache_read"
).split()


def _ph(paramstyle: str) -> str:
    return "?" if paramstyle == "qmark" else "%s"


def _sql(sql: str, paramstyle: str) -> str:
    return sql.replace("%s", "?") if paramstyle == "qmark" else sql


def _insert(table: str, cols: list[str], paramstyle: str) -> str:
    ph = ", ".join(_ph(paramstyle) for _ in cols)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) ON CONFLICT (id) DO NOTHING"


def _cost_vals(c: Cost | None) -> list[float | None]:
    return [None] * 4 if c is None else [c.input, c.output, c.cache_write, c.cache_read]


def to_record(
    trace: Trace,
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
    blob_prefix: str = "llmjournal/",
    paramstyle: str = "format",
) -> Records:
    rec = Records()
    tot = trace.totals
    u = tot.usage
    rec.writes.append(
        (
            _insert("lj_traces", _TRACE_COLS, paramstyle),
            [
                trace.id, trace.name, trace.tenant_id, trace.user_id, trace.session_id,
                iso(trace.started_at), iso(trace.ended_at), trace.started_at.date().isoformat(),
                trace.status, trace.error, dumps(trace.tags), dumps(trace.metadata),
                dumps(trace.input), dumps(trace.output),
                tot.call_count, u.input_tokens, u.output_tokens, u.cache_write_tokens, u.cache_read_tokens,
                *_cost_vals(tot.cost), tot.cost.total,
            ],
        )
    )
    for g in trace.generations:
        inline, ref = payload(trace, g, max_inline_bytes, blob_prefix, rec)
        gu = g.usage
        rec.writes.append(
            (
                _insert("lj_generations", _GEN_COLS, paramstyle),
                [
                    g.id, trace.id, g.name, g.model, iso(g.started_at), iso(g.ended_at), g.error,
                    dumps(g.metadata), inline, ref,
                    gu.input_tokens, gu.output_tokens, gu.cache_write_tokens, gu.cache_read_tokens,
                    *_cost_vals(g.cost),
                ],
            )
        )
    return rec


def build_query(name: str, paramstyle: str = "format", **p: Any) -> list[tuple[str, list]]:
    if name == "recent_traces":
        sql = f"SELECT {', '.join(_TRACE_COLS)} FROM lj_traces WHERE tenant_id = %s"
        params: list[Any] = [p.get("tenant_id", "")]
        if p.get("before"):
            sql += " AND started_at < %s"
            b = p["before"]
            # accept the DynamoDB-style cursor "<iso>#<id>" too, so viewers can share one pagination scheme
            params.append(b.split("#", 1)[0] if isinstance(b, str) else b.isoformat())
        sql += " ORDER BY started_at DESC LIMIT %s"
        params.append(int(p.get("limit", 50)))
        return [(_sql(sql, paramstyle), params)]
    if name == "trace_detail":
        tid = p["trace_id"]
        return [
            (_sql(f"SELECT {', '.join(_TRACE_COLS)} FROM lj_traces WHERE id = %s", paramstyle), [tid]),
            (_sql(f"SELECT {', '.join(_GEN_COLS)} FROM lj_generations WHERE trace_id = %s ORDER BY started_at", paramstyle), [tid]),
        ]
    if name == "daily_cost":
        sql = (
            "SELECT day, tenant_id, COUNT(*) AS trace_count, SUM(call_count) AS call_count, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_write_tokens) AS cache_write_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cost) AS cost FROM lj_traces WHERE tenant_id = %s AND day BETWEEN %s AND %s "
            "GROUP BY day, tenant_id ORDER BY day"
        )
        return [(_sql(sql, paramstyle), [p.get("tenant_id", ""), p["start_day"], p["end_day"]])]
    raise ValueError(f"unknown query {name!r}")


def _trace(r: dict) -> Trace:
    t = Trace(
        name=r["name"],
        id=r["id"],
        tenant_id=r.get("tenant_id") or "",
        user_id=r.get("user_id") or "",
        session_id=r.get("session_id") or "",
        started_at=parse_dt(r["started_at"]),
        ended_at=parse_dt(r.get("ended_at")),
        status=r.get("status") or "",
        input=loads(r.get("input")),
        output=loads(r.get("output")),
        error=r.get("error"),
        tags=loads(r.get("tags")) or [],
        metadata=loads(r.get("metadata")) or {},
    )
    t.stored_totals = Totals(int(r.get("call_count") or 0), usage_from(r), cost_from(r) or Cost())
    return t


def _gen(r: dict) -> Generation:
    g = Generation(
        model=r.get("model") or "",
        name=r.get("name") or "",
        id=r["id"],
        started_at=parse_dt(r["started_at"]),
        ended_at=parse_dt(r.get("ended_at")),
        usage=usage_from(r),
        cost=cost_from(r),
        error=r.get("error"),
        metadata=loads(r.get("metadata")) or {},
        payload_ref=r.get("payload_ref"),
    )
    apply_payload(g, r.get("payload"))
    return g


def from_rows(name: str, results: list[list[Any]]) -> Any:
    rows = [[dict(r) for r in res] for res in results]
    if name == "recent_traces":
        return [_trace(r) for r in rows[0]]
    if name == "trace_detail":
        if not rows or not rows[0]:
            return None
        t = _trace(rows[0][0])
        t.generations = [_gen(r) for r in (rows[1] if len(rows) > 1 else [])]
        if t.generations:
            t.stored_totals = None
        return t
    if name == "daily_cost":
        return [
            DailyCost(
                day=r["day"],
                tenant_id=r.get("tenant_id") or "",
                call_count=int(r.get("call_count") or 0),
                trace_count=int(r.get("trace_count") or 0),
                usage=usage_from(r),
                cost=float(r.get("cost") or 0),
            )
            for r in rows[0]
        ]
    raise ValueError(f"unknown query {name!r}")


def apply(cursor: Any, records: Records) -> None:
    """Convenience: run the writes on a DB-API cursor you created. Commit is up to you. Blobs are not uploaded."""
    for sql, params in records.writes:
        cursor.execute(sql, params)
