"""Token usage normalisation and cost calculation. Pure functions, no I/O."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import Cost, Usage

logger = logging.getLogger(__name__)


def normalize_model_id(model_id: str) -> str:
    """'us.anthropic.claude-sonnet-4-6-v1:0' -> 'claude-sonnet-4-6'."""
    m = re.sub(r"^(us|eu|ap|apac|global)\.", "", model_id or "")
    m = re.sub(r"^anthropic\.", "", m)
    m = re.sub(r"-v\d+:\d+$", "", m)
    return m


def _int(d: Mapping[str, Any], *keys: str) -> int:
    for k in keys:
        v = d.get(k)
        if v:
            return int(v)
    return 0


def extract_usage(raw: Mapping[str, Any] | None) -> Usage:
    """Normalise a usage dict from Anthropic (invoke_model), Bedrock Converse or OpenAI-compatible APIs.

    Anthropic/Bedrock report input tokens *excluding* cache tokens.
    OpenAI reports prompt_tokens *including* cached tokens, so cached ones are subtracted.
    """
    raw = raw or {}
    nested = raw.get("cache_metrics") or {}
    cache_write = _int(raw, "cache_creation_input_tokens", "cacheWriteInputTokens", "cache_creation_tokens") or _int(
        nested, "cache_creation_input_tokens"
    )
    cache_read = _int(raw, "cache_read_input_tokens", "cacheReadInputTokens", "cache_read_tokens") or _int(
        nested, "cache_read_input_tokens"
    )
    output = _int(raw, "output_tokens", "outputTokens", "completion_tokens")

    if "prompt_tokens" in raw and "input_tokens" not in raw and "inputTokens" not in raw:
        details = raw.get("prompt_tokens_details") or {}
        cached = _int(details, "cached_tokens")
        cache_read = cache_read or cached
        return Usage(max(int(raw["prompt_tokens"] or 0) - cached, 0), output, cache_write, cache_read)

    return Usage(_int(raw, "input_tokens", "inputTokens"), output, cache_write, cache_read)


@dataclass
class Pricing:
    """Prices in USD per 1M tokens, keyed by normalised model id.

    >>> Pricing({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}})
    """

    table: dict[str, dict[str, float]] = field(default_factory=dict)
    cache_write_multiplier: float = 1.25
    cache_read_multiplier: float = 0.10
    _warned: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def from_env(cls) -> Pricing:
        """Read LLM_PRICING (JSON), CACHE_WRITE_PRICE_MULTIPLIER, CACHE_READ_PRICE_MULTIPLIER."""
        try:
            table = json.loads(os.environ.get("LLM_PRICING") or "{}")
        except json.JSONDecodeError:
            logger.warning("LLM_PRICING is not valid JSON; cost tracking disabled")
            table = {}
        return cls(
            table=table,
            cache_write_multiplier=float(os.environ.get("CACHE_WRITE_PRICE_MULTIPLIER") or 1.25),
            cache_read_multiplier=float(os.environ.get("CACHE_READ_PRICE_MULTIPLIER") or 0.10),
        )

    def cost(self, model_id: str, usage: Usage) -> Cost | None:
        key = normalize_model_id(model_id)
        rates = self.table.get(key)
        if rates is None:
            if key not in self._warned:
                self._warned.add(key)
                logger.warning("No price configured for model %r (normalised %r)", model_id, key)
            return None
        per_in = rates["input"] / 1_000_000
        per_out = rates["output"] / 1_000_000
        return Cost(
            input=usage.input_tokens * per_in,
            output=usage.output_tokens * per_out,
            cache_write=usage.cache_write_tokens * per_in * self.cache_write_multiplier,
            cache_read=usage.cache_read_tokens * per_in * self.cache_read_multiplier,
        )
