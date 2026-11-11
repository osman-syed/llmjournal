"""Recording side: open a trace, log LLM calls into it, hand the finished trace to the caller's sink.

Tracing must never break the business call: sink and pricing failures are logged, not raised.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping

from .models import Generation, Trace, utcnow
from .pricing import Pricing, extract_usage

logger = logging.getLogger(__name__)

Sink = Callable[[Trace], None]

_current: ContextVar[tuple[Tracer, Trace] | None] = ContextVar("llmjournal_current", default=None)


class Tracer:
    """
    tracer = Tracer(sink=save_trace, pricing=Pricing.from_env())

    with tracer.trace("summarise_document", tenant_id="t1", session_id=doc_id) as t:
        ...
        record_generation(model=model_id, input=messages, output=text, usage=resp["usage"])
    # on exit: save_trace(t) is called once with the full trace
    """

    def __init__(self, sink: Sink, pricing: Pricing | None = None, enabled: bool = True):
        self.sink = sink
        self.pricing = pricing or Pricing()
        self.enabled = enabled

    @contextmanager
    def trace(self, name: str, **fields: Any) -> Iterator[Trace]:
        t = Trace(name=name, **fields)
        if not self.enabled:
            yield t
            return
        token = _current.set((self, t))
        try:
            yield t
            t.status = "completed"
        except BaseException as exc:
            t.status = "error"
            t.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _current.reset(token)
            t.ended_at = utcnow()
            self._emit(t)

    def _emit(self, t: Trace) -> None:
        try:
            self.sink(t)
        except Exception:
            logger.warning("llmjournal sink failed for trace %s (%s)", t.id, t.name, exc_info=True)


def current_trace() -> Trace | None:
    cur = _current.get()
    return cur[1] if cur else None


def record_generation(
    model: str,
    *,
    usage: Mapping[str, Any] | None = None,
    input: Any = None,
    output: Any = None,
    name: str = "llm_call",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Generation | None:
    """Attach one LLM call to the active trace. No-op (returns None) outside a trace.

    `usage` is the raw usage dict from the provider response, in any supported shape.
    """
    cur = _current.get()
    if cur is None:
        return None
    tracer, t = cur
    u = extract_usage(usage)
    try:
        cost = tracer.pricing.cost(model, u)
    except Exception:
        logger.warning("llmjournal pricing failed for %s", model, exc_info=True)
        cost = None
    now = utcnow()
    g = Generation(
        model=model,
        name=name,
        started_at=started_at or now,
        ended_at=ended_at or now,
        usage=u,
        cost=cost,
        input=input,
        output=output,
        error=error,
        metadata=metadata or {},
    )
    t.generations.append(g)
    return g
