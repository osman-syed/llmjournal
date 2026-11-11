"""llmjournal: record LLM calls and cost, shape them for your own database, render them as HTML.

Recording : Tracer, record_generation, current_trace
Pricing   : Pricing, extract_usage, normalize_model_id
Storage   : to_record, build_query, from_rows   (backends: "dynamodb", "postgres")
Viewing   : render_trace_list, render_trace, render_daily_cost
"""

from .backends import build_query, from_rows, to_record
from .models import Cost, DailyCost, Generation, Totals, Trace, Usage
from .pricing import Pricing, extract_usage, normalize_model_id
from .render import render_daily_cost, render_trace, render_trace_list
from .tracer import Tracer, current_trace, record_generation

__version__ = "0.1.0"

__all__ = [
    "Cost", "DailyCost", "Generation", "Pricing", "Totals", "Trace", "Tracer", "Usage",
    "build_query", "current_trace", "extract_usage", "from_rows", "normalize_model_id",
    "record_generation", "render_daily_cost", "render_trace", "render_trace_list", "to_record",
]
