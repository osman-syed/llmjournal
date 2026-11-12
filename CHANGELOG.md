# Changelog

## 0.1.0 — 2026-11-12

First usable release.

- `Tracer` / `record_generation`: collect LLM calls into a trace and calculate cost
  (Anthropic, Bedrock Converse and OpenAI-compatible usage shapes; cache read/write pricing).
- `to_record` / `build_query` / `from_rows` for **DynamoDB** (single table + one GSI, daily cost
  counters, optional TTL) and **Postgres** (also runs on SQLite). The package never opens a connection.
- Payloads over 100 KB are returned as blobs for object storage (e.g. S3) instead of stored inline.
- `render_trace_list`, `render_trace`, `render_daily_cost`: self-contained HTML, no JavaScript.
- Examples: DynamoDB + S3 sink, and a Lambda viewer with per-tenant access.

Known limits: a trace is saved once at the end (a crashed process loses it); DynamoDB daily counters
double-count if the rollup write is retried; no full-text search.

## 0.0.1

Name placeholder.
