"""End-to-end tests against real engines. Skipped unless the env vars are set.

    docker run -d --name llmjournal-pg  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=lj -p 127.0.0.1:55432:5432 postgres:16-alpine
    docker run -d --name llmjournal-ddb -p 127.0.0.1:58000:8000 amazon/dynamodb-local
    LJ_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/lj LJ_DDB_ENDPOINT=http://127.0.0.1:58000 pytest
"""

import os
import uuid
from datetime import timedelta

import pytest

import llmjournal as lj
from llmjournal.backends import dynamodb, postgres

PG_DSN = os.environ.get("LJ_PG_DSN")
DDB_ENDPOINT = os.environ.get("LJ_DDB_ENDPOINT")

PRICES = lj.Pricing({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}})
BIG = "x" * 150_000


def run_workload(tracer, tenant):
    """Same realistic workload for every backend. Returns the traces as recorded."""
    saved = []
    original = tracer.sink
    tracer.sink = lambda t: (saved.append(t), original(t))
    with tracer.trace("summarise", tenant_id=tenant, user_id="u@example.com", session_id="doc-1", tags=["a", "b"]) as t:
        t.input = {"doc": "café ☕ — 日本語", "nested": {"n": [1, 2.5, None]}}
        lj.record_generation(
            "us.anthropic.claude-sonnet-4-6-v1:0",
            usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 50},
            input=[{"role": "user", "content": "résumé please"}],
            output="Voilà ✓",
        )
        lj.record_generation("unknown-model", usage={"prompt_tokens": 10, "completion_tokens": 2}, input="q", output=BIG)
        t.output = {"ok": True}
    with pytest.raises(RuntimeError):
        with tracer.trace("failing", tenant_id=tenant):
            lj.record_generation("claude-sonnet-4-6", usage={"inputTokens": 100, "outputTokens": 10})
            raise RuntimeError("model timeout")
    for i in range(5):  # for pagination
        with tracer.trace(f"bulk{i}", tenant_id=tenant):
            lj.record_generation("claude-sonnet-4-6", usage={"input_tokens": 10, "output_tokens": 1})
    tracer.sink = original
    return saved


def check_results(run, saved, tenant, blobs):
    first, failing = saved[0], saved[1]

    recent = run("recent_traces", tenant_id=tenant, limit=3)
    assert len(recent) == 3 and recent[0].name == "bulk4"
    page2 = run("recent_traces", tenant_id=tenant, limit=10, before=f"{recent[-1].started_at.isoformat()}#{recent[-1].id}")
    assert [t.name for t in page2] == ["bulk1", "bulk0", "failing", "summarise"]
    assert run("recent_traces", tenant_id="someone-else") == []

    s = next(t for t in page2 if t.name == "summarise")
    assert s.totals.call_count == 2
    assert s.totals.cost.total == pytest.approx(first.totals.cost.total)

    d = run("trace_detail", trace_id=first.id)
    assert d.input == {"doc": "café ☕ — 日本語", "nested": {"n": [1, 2.5, None]}}
    assert d.tags == ["a", "b"] and d.status == "completed" and d.output == {"ok": True}
    g1, g2 = d.generations
    assert g1.output == "Voilà ✓" and g1.usage.cache_read_tokens == 800 and g1.usage.cache_write_tokens == 50
    assert g1.cost.total == pytest.approx(first.generations[0].cost.total)
    assert g2.cost is None  # unpriced model survives the round trip as "no price", not $0
    assert g2.payload_ref and g2.output is None and g2.payload_ref in blobs

    f = run("trace_detail", trace_id=failing.id)
    assert f.status == "error" and "model timeout" in f.error and f.totals.call_count == 1

    assert run("trace_detail", trace_id="does-not-exist") is None

    day = first.started_at.date().isoformat()
    rows = run("daily_cost", tenant_id=tenant, start_day=day, end_day=day)
    assert len(rows) == 1
    assert rows[0].trace_count == 7 and rows[0].call_count == 8
    assert rows[0].cost == pytest.approx(sum(t.totals.cost.total for t in saved))

    html = lj.render_trace(d) + lj.render_trace_list(recent) + lj.render_daily_cost(rows)
    assert "Voilà ✓" in html and "Payload stored separately" in html


@pytest.mark.skipif(not PG_DSN, reason="LJ_PG_DSN not set")
def test_postgres_end_to_end():
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(PG_DSN, row_factory=dict_row, autocommit=True) as con:
        con.execute("DROP TABLE IF EXISTS lj_generations, lj_traces")
        for stmt in filter(str.strip, postgres.SCHEMA_SQL.split(";")):
            con.execute(stmt)
        con.execute(postgres.SCHEMA_SQL.split(";")[0])  # schema is re-runnable

        blobs = {}

        def save(trace):
            rec = lj.to_record(trace, "postgres")
            blobs.update(rec.blobs)
            with con.transaction():
                postgres.apply(con.cursor(), rec)

        tenant = f"t-{uuid.uuid4().hex[:6]}"
        saved = run_workload(lj.Tracer(sink=save, pricing=PRICES), tenant)

        def run(name, **p):
            return lj.from_rows(name, "postgres", [con.execute(sql, params).fetchall() for sql, params in lj.build_query(name, "postgres", **p)])

        check_results(run, saved, tenant, blobs)

        save(saved[0])  # re-saving is a no-op, not a duplicate-key error
        assert con.execute("SELECT COUNT(*) AS n FROM lj_traces WHERE id = %s", [saved[0].id]).fetchone()["n"] == 1


@pytest.mark.skipif(not DDB_ENDPOINT, reason="LJ_DDB_ENDPOINT not set")
def test_dynamodb_end_to_end():
    import boto3

    kw = dict(endpoint_url=DDB_ENDPOINT, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    client, resource = boto3.client("dynamodb", **kw), boto3.resource("dynamodb", **kw)
    name = f"lj-test-{uuid.uuid4().hex[:6]}"
    client.create_table(TableName=name, **dynamodb.TABLE_DEFINITION)
    table = resource.Table(name)
    try:
        blobs = {}

        def save(trace):
            rec = lj.to_record(trace, "dynamodb", ttl_days=30)
            blobs.update(rec.blobs)
            dynamodb.apply(table, rec)

        tenant = f"t-{uuid.uuid4().hex[:6]}"
        saved = run_workload(lj.Tracer(sink=save, pricing=PRICES), tenant)

        def run(name, **p):
            return lj.from_rows(name, "dynamodb", [table.query(**q)["Items"] for q in lj.build_query(name, "dynamodb", **p)])

        check_results(run, saved, tenant, blobs)

        item = table.get_item(Key={"PK": f"TRACE#{saved[0].id}", "SK": "TRACE"})["Item"]
        assert item["expires_at"] > 0
    finally:
        client.delete_table(TableName=name)
