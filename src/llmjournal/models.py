"""Plain data objects. Every other module takes and returns these."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Usage:
    input_tokens: int = 0  # non-cached input
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens + self.cache_read_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass
class Cost:
    input: float = 0.0
    output: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_write + self.cache_read

    def __add__(self, other: Cost) -> Cost:
        return Cost(
            self.input + other.input,
            self.output + other.output,
            self.cache_write + other.cache_write,
            self.cache_read + other.cache_read,
        )


@dataclass
class Generation:
    """One LLM call."""

    model: str
    name: str = "llm_call"
    id: str = field(default_factory=new_id)
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    usage: Usage = field(default_factory=Usage)
    cost: Cost | None = None  # None = model has no price configured
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    payload_ref: str | None = None  # set when input/output were offloaded (e.g. to S3)

    @property
    def duration_ms(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds() * 1000


@dataclass
class Trace:
    """One user-facing operation, grouping any number of LLM calls."""

    name: str
    id: str = field(default_factory=new_id)
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    status: str = "running"  # running | completed | error
    input: Any = None
    output: Any = None
    error: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    generations: list[Generation] = field(default_factory=list)
    # Filled when a trace is loaded from storage without its generations.
    stored_totals: Totals | None = None

    @property
    def totals(self) -> Totals:
        if self.stored_totals is not None and not self.generations:
            return self.stored_totals
        usage, cost = Usage(), Cost()
        for g in self.generations:
            usage = usage + g.usage
            if g.cost:
                cost = cost + g.cost
        return Totals(call_count=len(self.generations), usage=usage, cost=cost)

    @property
    def duration_ms(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds() * 1000


@dataclass
class Totals:
    call_count: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: Cost = field(default_factory=Cost)


@dataclass
class DailyCost:
    """One row of the daily cost rollup."""

    day: str  # YYYY-MM-DD (UTC)
    tenant_id: str
    call_count: int = 0
    trace_count: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
