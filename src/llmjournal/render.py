"""Viewing side: turn traces into self-contained HTML pages (no JS, no external assets).

All user data is HTML-escaped. Links are built by caller-supplied functions so the pages
work under any URL scheme (Lambda Function URL, API Gateway path, FastAPI route...).
"""

from __future__ import annotations

import json
from html import escape
from typing import Any, Callable, Iterable

from .models import DailyCost, Generation, Trace

TraceLink = Callable[[Trace], str]

_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e5e5e5;--card:#fafafa;--accent:#2563eb;--err:#b91c1c;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#999;--line:#2a2a2a;--card:#181818;--accent:#60a5fa;--err:#f87171;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1200px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}
.muted{color:var(--muted)}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px}.tile b{display:block;font-size:18px}
.error{color:var(--err)}.completed{color:var(--ok)}details{border:1px solid var(--line);border-radius:6px;margin:8px 0;background:var(--card)}
summary{cursor:pointer;padding:8px 10px}pre{margin:0;padding:10px;overflow:auto;max-height:480px;white-space:pre-wrap;word-break:break-word;border-top:1px solid var(--line)}
.bar{background:var(--accent);height:8px;border-radius:2px;min-width:1px}
"""


def money(x: float) -> str:
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def _n(x: int) -> str:
    return f"{x:,}"


def _ms(x: float) -> str:
    return f"{x / 1000:.1f}s" if x >= 1000 else f"{x:.0f}ms"


def _pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>{body}</main></body></html>"
    )


def render_trace_list(
    traces: Iterable[Trace],
    link: TraceLink = lambda t: f"?trace_id={t.id}",
    title: str = "Traces",
    next_link: str | None = None,
) -> str:
    rows = []
    for t in traces:
        tot = t.totals
        rows.append(
            "<tr>"
            f"<td>{escape(t.started_at.strftime('%Y-%m-%d %H:%M:%S'))}</td>"
            f'<td><a href="{escape(link(t))}">{escape(t.name)}</a></td>'
            f"<td>{escape(t.user_id or t.session_id)}</td>"
            f'<td class="{escape(t.status)}">{escape(t.status)}</td>'
            f'<td class="n">{tot.call_count}</td>'
            f'<td class="n">{_n(tot.usage.total_tokens)}</td>'
            f'<td class="n">{money(tot.cost.total)}</td>'
            f'<td class="n">{_ms(t.duration_ms)}</td>'
            "</tr>"
        )
    table = (
        '<div class=wrap><table><thead><tr><th>Time (UTC)</th><th>Name</th><th>User / session</th><th>Status</th>'
        '<th class=n>Calls</th><th class=n>Tokens</th><th class=n>Cost</th><th class=n>Duration</th></tr></thead>'
        f"<tbody>{''.join(rows) or '<tr><td colspan=8 class=muted>No traces</td></tr>'}</tbody></table></div>"
    )
    more = f'<p><a href="{escape(next_link)}">Older →</a></p>' if next_link else ""
    return page(title, f"<h1>{escape(title)}</h1>{table}{more}")


def _gen_block(g: Generation) -> str:
    cost = money(g.cost.total) if g.cost else "no price"
    err = f' <span class="error">{escape(g.error)}</span>' if g.error else ""
    head = (
        f"<summary><b>{escape(g.name)}</b> · {escape(g.model)} · {_n(g.usage.input_tokens)} in / "
        f"{_n(g.usage.output_tokens)} out"
        + (f" / {_n(g.usage.cache_read_tokens)} cache-read / {_n(g.usage.cache_write_tokens)} cache-write"
           if g.usage.cache_read_tokens or g.usage.cache_write_tokens else "")
        + f" · {cost} · {_ms(g.duration_ms)}{err}</summary>"
    )
    if g.payload_ref and g.input is None and g.output is None:
        body = f"<pre class=muted>Payload stored separately: {escape(g.payload_ref)}</pre>"
    else:
        body = f"<pre>INPUT\n{escape(_pretty(g.input))}</pre><pre>OUTPUT\n{escape(_pretty(g.output))}</pre>"
    return f"<details>{head}{body}</details>"


def render_trace(trace: Trace | None, back_link: str = "?") -> str:
    if trace is None:
        return page("Not found", f'<h1>Trace not found</h1><p><a href="{escape(back_link)}">← Back</a></p>')
    t, tot = trace, trace.totals
    tiles = [
        ("Cost", money(tot.cost.total)),
        ("Calls", str(tot.call_count)),
        ("Input tokens", _n(tot.usage.input_tokens)),
        ("Output tokens", _n(tot.usage.output_tokens)),
        ("Cache read / write", f"{_n(tot.usage.cache_read_tokens)} / {_n(tot.usage.cache_write_tokens)}"),
        ("Duration", _ms(t.duration_ms)),
    ]
    tiles_html = "".join(f"<div class=tile><span class=muted>{k}</span><b>{escape(v)}</b></div>" for k, v in tiles)
    meta = " · ".join(
        escape(x)
        for x in (
            t.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            f"tenant {t.tenant_id}" if t.tenant_id else "",
            f"user {t.user_id}" if t.user_id else "",
            f"session {t.session_id}" if t.session_id else "",
            ", ".join(t.tags),
        )
        if x
    )
    err = f'<p class="error">{escape(t.error)}</p>' if t.error else ""
    io = ""
    if t.input is not None:
        io += f"<details><summary>Trace input</summary><pre>{escape(_pretty(t.input))}</pre></details>"
    if t.output is not None:
        io += f"<details><summary>Trace output</summary><pre>{escape(_pretty(t.output))}</pre></details>"
    if t.metadata:
        io += f"<details><summary>Metadata</summary><pre>{escape(_pretty(t.metadata))}</pre></details>"
    gens = "".join(_gen_block(g) for g in t.generations) or "<p class=muted>No LLM calls recorded</p>"
    body = (
        f'<p><a href="{escape(back_link)}">← All traces</a></p>'
        f'<h1>{escape(t.name)} <span class="{escape(t.status)}">· {escape(t.status)}</span></h1>'
        f"<p class=muted>{meta}</p>{err}<div class=tiles>{tiles_html}</div>{io}"
        f"<h2>LLM calls ({len(t.generations)})</h2>{gens}"
    )
    return page(t.name, body)


def render_daily_cost(rows: Iterable[DailyCost], title: str = "Daily cost") -> str:
    rows = list(rows)
    top = max((r.cost for r in rows), default=0) or 1
    total = sum(r.cost for r in rows)
    trs = "".join(
        "<tr>"
        f"<td>{escape(r.day)}</td>"
        f'<td class="n">{r.trace_count}</td><td class="n">{r.call_count}</td>'
        f'<td class="n">{_n(r.usage.total_tokens)}</td><td class="n">{money(r.cost)}</td>'
        f'<td style="width:40%"><div class=bar style="width:{r.cost / top * 100:.1f}%"></div></td>'
        "</tr>"
        for r in rows
    )
    table = (
        "<div class=wrap><table><thead><tr><th>Day (UTC)</th><th class=n>Traces</th><th class=n>Calls</th>"
        "<th class=n>Tokens</th><th class=n>Cost</th><th></th></tr></thead>"
        f"<tbody>{trs or '<tr><td colspan=6 class=muted>No data</td></tr>'}</tbody></table></div>"
    )
    return page(title, f"<h1>{escape(title)}</h1><p class=muted>Total {money(total)}</p>{table}")
