"""DynamoDB shape: one table, keys PK/SK, one GSI (GSI1PK/GSI1SK).

  Trace       PK=TRACE#<id>               SK=TRACE                       GSI1PK=TENANT#<t>  GSI1SK=<started_at>#<id>
  Generation  PK=TRACE#<id>               SK=GEN#<started_at>#<gen_id>
  Daily cost  PK=TENANT#<t>#DAILY         SK=DAY#<YYYY-MM-DD>            (counters, updated with ADD)

Items use Decimal for numbers so they can go straight into boto3's Table resource.
Writes are ("put", item) or ("update", update_item_kwargs). Queries are table.query(**kwargs) kwargs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..models import Cost, DailyCost, Generation, Totals, Trace, Usage
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

GSI_NAME = "GSI1"

TABLE_DEFINITION = {
    "AttributeDefinitions": [
        {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")
    ],
    "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
    "GlobalSecondaryIndexes": [
        {
            "IndexName": GSI_NAME,
            "KeySchema": [
                {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ],
    "BillingMode": "PAY_PER_REQUEST",
}
"""Pass to client.create_table(TableName=..., **TABLE_DEFINITION)."""


def _d(x: float | int) -> Decimal:
    return Decimal(str(x))


def _num(x: Any) -> float:
    return float(x) if x is not None else 0.0


def _usage_attrs(u: Usage) -> dict[str, Decimal]:
    return {
        "input_tokens": _d(u.input_tokens),
        "output_tokens": _d(u.output_tokens),
        "cache_write_tokens": _d(u.cache_write_tokens),
        "cache_read_tokens": _d(u.cache_read_tokens),
    }


def _cost_attrs(c: Cost) -> dict[str, Decimal]:
    return {
        "cost_input": _d(c.input),
        "cost_output": _d(c.output),
        "cost_cache_write": _d(c.cache_write),
        "cost_cache_read": _d(c.cache_read),
    }


def to_record(
    trace: Trace,
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
    blob_prefix: str = "llmjournal/",
    ttl_days: int | None = None,
) -> Records:
    rec = Records()
    totals = trace.totals
    pk = f"TRACE#{trace.id}"
    ttl = {"expires_at": int(trace.started_at.timestamp()) + ttl_days * 86400} if ttl_days else {}

    item = {
        "PK": pk,
        "SK": "TRACE",
        "GSI1PK": f"TENANT#{trace.tenant_id}",
        "GSI1SK": f"{iso(trace.started_at)}#{trace.id}",
        "type": "trace",
        "id": trace.id,
        "name": trace.name,
        "tenant_id": trace.tenant_id,
        "user_id": trace.user_id,
        "session_id": trace.session_id,
        "started_at": iso(trace.started_at),
        "ended_at": iso(trace.ended_at),
        "status": trace.status,
        "error": trace.error,
        "tags": dumps(trace.tags),
        "metadata": dumps(trace.metadata),
        "input": dumps(trace.input),
        "output": dumps(trace.output),
        "call_count": _d(totals.call_count),
        "cost": _d(totals.cost.total),
        **_cost_attrs(totals.cost),
        **_usage_attrs(totals.usage),
        **ttl,
    }
    rec.writes.append(("put", {k: v for k, v in item.items() if v is not None}))

    for g in trace.generations:
        inline, ref = payload(trace, g, max_inline_bytes, blob_prefix, rec)
        gi = {
            "PK": pk,
            "SK": f"GEN#{iso(g.started_at)}#{g.id}",
            "type": "generation",
            "id": g.id,
            "name": g.name,
            "model": g.model,
            "started_at": iso(g.started_at),
            "ended_at": iso(g.ended_at),
            "error": g.error,
            "metadata": dumps(g.metadata),
            "payload": inline,
            "payload_ref": ref,
            **_usage_attrs(g.usage),
            **ttl,
        }
        if g.cost is not None:
            gi.update(_cost_attrs(g.cost))
        rec.writes.append(("put", {k: v for k, v in gi.items() if v is not None}))

    day = trace.started_at.date().isoformat()
    rec.writes.append(
        (
            "update",
            {
                "Key": {"PK": f"TENANT#{trace.tenant_id}#DAILY", "SK": f"DAY#{day}"},
                "UpdateExpression": (
                    "SET tenant_id = :t, #day = :day "
                    "ADD trace_count :one, call_count :calls, cost :cost, input_tokens :in, "
                    "output_tokens :out, cache_write_tokens :cw, cache_read_tokens :cr"
                ),
                "ExpressionAttributeNames": {"#day": "day"},
                "ExpressionAttributeValues": {
                    ":t": trace.tenant_id,
                    ":day": day,
                    ":one": _d(1),
                    ":calls": _d(totals.call_count),
                    ":cost": _d(totals.cost.total),
                    ":in": _d(totals.usage.input_tokens),
                    ":out": _d(totals.usage.output_tokens),
                    ":cw": _d(totals.usage.cache_write_tokens),
                    ":cr": _d(totals.usage.cache_read_tokens),
                },
            },
        )
    )
    return rec


def build_query(name: str, **p: Any) -> list[dict]:
    if name == "recent_traces":
        kwargs: dict[str, Any] = {
            "IndexName": GSI_NAME,
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": f"TENANT#{p.get('tenant_id', '')}"},
            "ScanIndexForward": False,
            "Limit": int(p.get("limit", 50)),
        }
        before = p.get("before")  # a GSI1SK value, or an ISO timestamp
        if before:
            kwargs["KeyConditionExpression"] += " AND GSI1SK < :before"
            kwargs["ExpressionAttributeValues"][":before"] = before.isoformat() if isinstance(before, datetime) else before
        return [kwargs]
    if name == "trace_detail":
        return [
            {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": f"TRACE#{p['trace_id']}"},
            }
        ]
    if name == "daily_cost":
        return [
            {
                "KeyConditionExpression": "PK = :pk AND SK BETWEEN :a AND :b",
                "ExpressionAttributeValues": {
                    ":pk": f"TENANT#{p.get('tenant_id', '')}#DAILY",
                    ":a": f"DAY#{p['start_day']}",
                    ":b": f"DAY#{p['end_day']}",
                },
            }
        ]
    raise ValueError(f"unknown query {name!r}")


def _trace(i: dict) -> Trace:
    t = Trace(
        name=i.get("name", ""),
        id=i["id"],
        tenant_id=i.get("tenant_id", ""),
        user_id=i.get("user_id", ""),
        session_id=i.get("session_id", ""),
        started_at=parse_dt(i["started_at"]),
        ended_at=parse_dt(i.get("ended_at")),
        status=i.get("status", ""),
        input=loads(i.get("input")),
        output=loads(i.get("output")),
        error=i.get("error"),
        tags=loads(i.get("tags")) or [],
        metadata=loads(i.get("metadata")) or {},
    )
    t.stored_totals = Totals(int(i.get("call_count") or 0), usage_from(i), cost_from(i) or Cost())
    return t


def _gen(i: dict) -> Generation:
    g = Generation(
        model=i.get("model", ""),
        name=i.get("name", ""),
        id=i["id"],
        started_at=parse_dt(i["started_at"]),
        ended_at=parse_dt(i.get("ended_at")),
        usage=usage_from(i),
        cost=cost_from(i),
        error=i.get("error"),
        metadata=loads(i.get("metadata")) or {},
        payload_ref=i.get("payload_ref"),
    )
    apply_payload(g, i.get("payload"))
    return g


def from_rows(name: str, results: list[list[dict]]) -> Any:
    items = results[0] if results else []
    if name == "recent_traces":
        return [_trace(i) for i in items]
    if name == "trace_detail":
        head = next((i for i in items if i.get("SK") == "TRACE"), None)
        if head is None:
            return None
        t = _trace(head)
        t.generations = [_gen(i) for i in items if str(i.get("SK", "")).startswith("GEN#")]
        t.stored_totals = None if t.generations else t.stored_totals
        return t
    if name == "daily_cost":
        return [
            DailyCost(
                day=i["day"],
                tenant_id=i.get("tenant_id", ""),
                call_count=int(i.get("call_count") or 0),
                trace_count=int(i.get("trace_count") or 0),
                usage=usage_from(i),
                cost=_num(i.get("cost")),
            )
            for i in items
        ]
    raise ValueError(f"unknown query {name!r}")


def apply(table: Any, records: Records) -> None:
    """Convenience: run the writes against a boto3 Table resource you created. Blobs are not uploaded."""
    for op, arg in records.writes:
        if op == "put":
            table.put_item(Item=arg)
        else:
            table.update_item(**arg)
