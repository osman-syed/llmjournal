"""Helpers shared by the storage shapes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models import Cost, Generation, Trace, Usage

DEFAULT_MAX_INLINE_BYTES = 100_000  # keeps DynamoDB items well under the 400 KB limit


@dataclass
class Records:
    """What to write, in order. The caller runs `writes` against their DB and uploads `blobs` (e.g. to S3)."""

    writes: list[Any] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def loads(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def payload(trace: Trace, gen: Generation, max_inline_bytes: int, blob_prefix: str, records: Records) -> tuple[str | None, str | None]:
    """Return (inline_payload_json, payload_ref). Large payloads go to records.blobs."""
    body = dumps({"input": gen.input, "output": gen.output})
    if len(body.encode()) <= max_inline_bytes:
        return body, None
    key = f"{blob_prefix}{trace.id}/{gen.id}.json"
    records.blobs[key] = body.encode()
    return None, key


def apply_payload(gen: Generation, raw: Any) -> None:
    data = loads(raw) or {}
    if isinstance(data, dict):
        gen.input, gen.output = data.get("input"), data.get("output")


def usage_from(row: dict, prefix: str = "") -> Usage:
    return Usage(
        int(row.get(f"{prefix}input_tokens") or 0),
        int(row.get(f"{prefix}output_tokens") or 0),
        int(row.get(f"{prefix}cache_write_tokens") or 0),
        int(row.get(f"{prefix}cache_read_tokens") or 0),
    )


def cost_from(row: dict) -> Cost | None:
    if row.get("cost_input") is None:
        return None
    return Cost(
        float(row["cost_input"]),
        float(row.get("cost_output") or 0),
        float(row.get("cost_cache_write") or 0),
        float(row.get("cost_cache_read") or 0),
    )
