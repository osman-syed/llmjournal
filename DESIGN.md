# llmjournal — design

## Why

Hosted LLM observability tools (Langfuse, etc.) need a server running all the time plus a database,
and the newer versions need even more (ClickHouse, Redis, S3). Most teams only use three things:
record LLM calls with their cost, store them, and look at them.

llmjournal does those three jobs as a library. There's no server to run.

## Principle

**The package never opens a database connection.** It:

1. **Records:** collects LLM calls into a trace and calculates cost
2. **Shapes:** turns a trace into exactly what to write for a given database (`to_record`)
3. **Queries:** builds the query to read it back (`build_query`) and parses the rows (`from_rows`)
4. **Renders:** turns the results into HTML pages

The caller owns connections, credentials, hosting and auth. That keeps the package free of
`boto3`/`psycopg` (zero dependencies) and lets it run in Lambda, EC2, containers or locally.

## Flow

```
   your code                        llmjournal                     your infra
 ───────────────────────────────────────────────────────────────────────────────
 with tracer.trace(...)  ───────►  Trace (ContextVar)
   LLM call
   record_generation(...) ──────►  + Generation (usage, cost)
 (block exits)           ───────►  sink(trace)  ───► to_record() ───► put/insert ──► DynamoDB / Postgres
                                                                  └─► blobs ──────► S3 (payloads > 100 KB)

 viewer Lambda           ───────►  build_query() ──────────────────► query ───────► DB
                         ◄───────  from_rows() ◄──────────────────── rows
                         ───────►  render_*()  ───► HTML response
```

## Public API (v1)

| Function | Purpose |
|---|---|
| `Tracer(sink, pricing)` / `tracer.trace(name, tenant_id=…, user_id=…, session_id=…)` | Open a trace. On exit, `sink(trace)` is called once, even on error |
| `record_generation(model, usage=raw, input=…, output=…)` | Attach one LLM call to the active trace. No-op outside a trace |
| `Pricing(table)` / `Pricing.from_env()` | USD per 1M tokens; cache write ×1.25, cache read ×0.10 (configurable) |
| `extract_usage(raw)` | Normalises Anthropic, Bedrock Converse and OpenAI usage dicts |
| `to_record(trace, backend)` | Returns `Records(writes, blobs)` |
| `build_query(name, backend, **params)` | Returns a list of queries to run |
| `from_rows(name, backend, results)` | Rows → `Trace` / `list[Trace]` / `list[DailyCost]` |
| `render_trace_list`, `render_trace`, `render_daily_cost` | Self-contained HTML, no JS |

Backends: `"dynamodb"`, `"postgres"` (works on SQLite with `paramstyle="qmark"`).

## The three screens (v1)

| Screen | Query | DynamoDB | Postgres |
|---|---|---|---|
| Recent traces for a tenant, paged | `recent_traces(tenant_id, limit, before)` | GSI1 `TENANT#t`, sorted by time | index `(tenant_id, started_at)` |
| One trace, every call with prompt and response | `trace_detail(trace_id)` | `PK = TRACE#id` | 2 selects |
| Daily cost for a tenant | `daily_cost(tenant_id, start_day, end_day)` | pre-summed counters (`ADD` on write) | `GROUP BY day` |

**Anything not in this table is out of scope for v1.** Each new screen means a new query for each
backend, and for DynamoDB usually a new index or counter that only covers data written after it
was added.

## DynamoDB layout (single table, on-demand)

```
Trace       PK=TRACE#<id>          SK=TRACE                    GSI1PK=TENANT#<t>  GSI1SK=<iso>#<id>
Generation  PK=TRACE#<id>          SK=GEN#<iso>#<gen_id>
Daily cost  PK=TENANT#<t>#DAILY    SK=DAY#<YYYY-MM-DD>         counters via ADD
```

- Payloads over 100 KB go to `Records.blobs` (upload to S3) and the item keeps `payload_ref`.
  This keeps items well under the 400 KB limit.
- Optional `ttl_days` sets `expires_at` for DynamoDB TTL.
- Numbers are `Decimal`, so items go straight into boto3's `Table` resource.

## Decisions and trade-offs

- **Failures never break the business call.** Errors in the sink or pricing are logged and swallowed.
  The cost: if the write fails, that trace is lost, with no retry queue in v1.
- **The trace is written once, at the end.** You can't watch a long-running trace live. A crashed Lambda
  (timeout, out of memory) loses the trace because `finally` never runs. Langfuse streams events, so it
  doesn't have this problem.
- **Postgres schema uses TEXT for timestamps and JSON** so one schema works on any SQL database. Postgres
  users lose JSONB querying, which v1 doesn't use.
- **DynamoDB daily counters aren't idempotent.** If the sink retries the rollup `update`, cost is counted
  twice. Trace and generation `put`s are idempotent.
- **No search.** "Find traces whose response contains X" isn't supported. DynamoDB can't do it cheaply.
- **Auth is the caller's job.** Pages show full prompts. The example viewer takes the tenant from the
  Cognito token and refuses to show another tenant's trace.

## Open questions

- Retry queue for failed writes, or keep "best effort"?
- Streaming partial traces so crashed Lambdas don't lose them?
- Which backend comes next, if any (only add one when someone needs it)?
