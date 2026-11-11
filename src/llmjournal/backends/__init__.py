"""Storage shapes. The package never opens a DB connection: it builds what to write and what to query,
the caller runs it, and hands the rows back to `from_rows`.

    records = to_record(trace, "dynamodb")            # what to save
    queries = build_query("recent_traces", "dynamodb", tenant_id="t1")
    results = [table.query(**q)["Items"] for q in queries]
    traces  = from_rows("recent_traces", "dynamodb", results)

Queries (v1): recent_traces(tenant_id, limit=50, before=None), trace_detail(trace_id),
daily_cost(tenant_id, start_day, end_day).
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from ..models import Trace
from . import dynamodb, postgres
from ._common import Records

BACKENDS: dict[str, ModuleType] = {"dynamodb": dynamodb, "postgres": postgres}
QUERIES = ("recent_traces", "trace_detail", "daily_cost")


def _backend(name: str) -> ModuleType:
    try:
        return BACKENDS[name]
    except KeyError:
        raise ValueError(f"unknown backend {name!r}; choose from {sorted(BACKENDS)}") from None


def to_record(trace: Trace, backend: str, **opts: Any) -> Records:
    return _backend(backend).to_record(trace, **opts)


def build_query(name: str, backend: str, **params: Any) -> list[Any]:
    return _backend(backend).build_query(name, **params)


def from_rows(name: str, backend: str, results: list[list[Any]]) -> Any:
    return _backend(backend).from_rows(name, results)


__all__ = ["BACKENDS", "QUERIES", "Records", "build_query", "from_rows", "to_record"]
