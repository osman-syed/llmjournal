import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import llmjournal as lj
from llmjournal.backends import dynamodb, postgres

PRICES = lj.Pricing({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}})
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


# ── pricing / usage ─────────────────────────────────────────────────────────

def test_normalize_model_id():
    assert lj.normalize_model_id("us.anthropic.claude-sonnet-4-6-v1:0") == "claude-sonnet-4-6"
    assert lj.normalize_model_id("claude-sonnet-4-6") == "claude-sonnet-4-6"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 2}, (10, 5, 3, 2)),
        ({"inputTokens": 10, "outputTokens": 5, "cacheWriteInputTokens": 3, "cacheReadInputTokens": 2}, (10, 5, 3, 2)),
        ({"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}}, (60, 7, 0, 40)),
        (None, (0, 0, 0, 0)),
    ],
)
def test_extract_usage(raw, expected):
    u = lj.extract_usage(raw)
    assert (u.input_tokens, u.output_tokens, u.cache_write_tokens, u.cache_read_tokens) == expected


def test_cost_matches_existing_formula():
    c = PRICES.cost("us.anthropic.claude-sonnet-4-6-v1:0", lj.Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000))
    assert c.input == pytest.approx(3.0)
    assert c.output == pytest.approx(15.0)
    assert c.cache_write == pytest.approx(3.75)
    assert c.cache_read == pytest.approx(0.30)
    assert c.total == pytest.approx(22.05)


def test_unpriced_model_returns_none():
    assert PRICES.cost("mystery-model", lj.Usage(10, 10)) is None


# ── tracer ──────────────────────────────────────────────────────────────────

def make_tracer():
    saved = []
    return lj.Tracer(sink=saved.append, pricing=PRICES), saved


def test_trace_collects_calls_and_calls_sink_once():
    tracer, saved = make_tracer()
    with tracer.trace("summarise_document", tenant_id="t1", session_id="p1") as t:
        lj.record_generation("claude-sonnet-4-6", usage={"input_tokens": 1000, "output_tokens": 100}, input="hi", output="yo")
        lj.record_generation("claude-sonnet-4-6", usage={"input_tokens": 1000, "output_tokens": 100})
        assert lj.current_trace() is t
    assert lj.current_trace() is None
    assert saved == [t]
    assert t.status == "completed" and t.ended_at is not None
    assert t.totals.call_count == 2
    assert t.totals.cost.total == pytest.approx(2 * (0.003 + 0.0015))


def test_trace_records_error_and_reraises():
    tracer, saved = make_tracer()
    with pytest.raises(ValueError):
        with tracer.trace("x"):
            lj.record_generation("claude-sonnet-4-6", usage={"input_tokens": 1})
            raise ValueError("boom")
    assert saved[0].status == "error" and "boom" in saved[0].error
    assert saved[0].totals.call_count == 1  # partial cost is kept


def test_sink_failure_does_not_break_caller():
    def bad_sink(_):
        raise RuntimeError("db down")

    with lj.Tracer(sink=bad_sink).trace("x"):
        pass  # no exception


def test_record_outside_trace_is_noop():
    assert lj.record_generation("m", usage={"input_tokens": 1}) is None


def test_nested_traces_restore_outer():
    tracer, saved = make_tracer()
    with tracer.trace("outer") as outer:
        with tracer.trace("inner"):
            lj.record_generation("claude-sonnet-4-6", usage={"input_tokens": 1})
        lj.record_generation("claude-sonnet-4-6", usage={"input_tokens": 1})
    assert outer.totals.call_count == 1 and len(saved) == 2


# ── fixtures for storage ────────────────────────────────────────────────────

def sample_traces():
    out = []
    for i in range(3):
        t = lj.Trace(name=f"op{i}", tenant_id="t1", user_id="u1", started_at=T0 + timedelta(days=i), status="completed")
        t.ended_at = t.started_at + timedelta(seconds=2)
        for j in range(2):
            u = lj.Usage(1000, 100, 10, 20)
            t.generations.append(
                lj.Generation(
                    model="claude-sonnet-4-6",
                    started_at=t.started_at + timedelta(milliseconds=j),
                    ended_at=t.started_at + timedelta(seconds=1),
                    usage=u,
                    cost=PRICES.cost("claude-sonnet-4-6", u),
                    input={"messages": [{"role": "user", "content": f"q{j}"}]},
                    output="answer",
                )
            )
        out.append(t)
    # other tenant must never show up
    out.append(lj.Trace(name="other", tenant_id="t2", started_at=T0))
    return out


# ── postgres shape, executed on sqlite ──────────────────────────────────────

@pytest.fixture
def sql_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(postgres.SCHEMA_SQL)
    for t in sample_traces():
        postgres.apply(con.cursor(), lj.to_record(t, "postgres", paramstyle="qmark"))
    return con


def run_sql(con, name, **p):
    qs = lj.build_query(name, "postgres", paramstyle="qmark", **p)
    return lj.from_rows(name, "postgres", [con.execute(sql, params).fetchall() for sql, params in qs])


def test_postgres_roundtrip(sql_db):
    recent = run_sql(sql_db, "recent_traces", tenant_id="t1", limit=2)
    assert [t.name for t in recent] == ["op2", "op1"]
    assert recent[0].totals.call_count == 2

    older = run_sql(sql_db, "recent_traces", tenant_id="t1", before=recent[-1].started_at)
    assert [t.name for t in older] == ["op0"]

    t = run_sql(sql_db, "trace_detail", trace_id=recent[0].id)
    assert len(t.generations) == 2 and t.generations[0].input["messages"][0]["content"] == "q0"
    assert t.totals.cost.total == pytest.approx(recent[0].totals.cost.total)

    days = run_sql(sql_db, "daily_cost", tenant_id="t1", start_day="2026-10-15", end_day="2026-10-16")
    assert [d.day for d in days] == ["2026-10-15", "2026-10-16"]
    assert days[0].call_count == 2 and days[0].trace_count == 1


def test_postgres_write_is_idempotent(sql_db):
    t = sample_traces()[0]
    rec = lj.to_record(t, "postgres", paramstyle="qmark")
    postgres.apply(sql_db.cursor(), rec)
    postgres.apply(sql_db.cursor(), rec)
    assert sql_db.execute("SELECT COUNT(*) FROM lj_generations WHERE trace_id=?", [t.id]).fetchone()[0] == 2


def test_postgres_default_placeholders():
    sql, params = lj.build_query("trace_detail", "postgres", trace_id="x")[0]
    assert "%s" in sql and params == ["x"]


# ── dynamodb shape, executed on a tiny in-memory fake ───────────────────────

class FakeTable:
    """Implements exactly the put/update/query patterns the dynamodb shape emits."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        for v in Item.values():
            assert not isinstance(v, float), "boto3 rejects floats"
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        item = self.items.setdefault((Key["PK"], Key["SK"]), dict(Key))
        vals, names = ExpressionAttributeValues, ExpressionAttributeNames or {}
        set_part, add_part = UpdateExpression.split(" ADD ")
        for a in set_part.removeprefix("SET ").split(", "):
            k, v = a.split(" = ")
            item[names.get(k, k)] = vals[v]
        for a in add_part.split(", "):
            k, v = a.split()
            item[k] = item.get(k, Decimal(0)) + vals[v]

    def query(self, KeyConditionExpression, ExpressionAttributeValues, IndexName=None, ScanIndexForward=True, Limit=None):
        v = ExpressionAttributeValues
        pk_attr, sk_attr = ("GSI1PK", "GSI1SK") if IndexName else ("PK", "SK")
        rows = [i for i in self.items.values() if i.get(pk_attr) == v[":pk"]]
        if ":before" in v:
            rows = [i for i in rows if i[sk_attr] < v[":before"]]
        if ":a" in v:
            rows = [i for i in rows if v[":a"] <= i[sk_attr] <= v[":b"]]
        rows.sort(key=lambda i: i[sk_attr], reverse=not ScanIndexForward)
        return {"Items": rows[:Limit] if Limit else rows}


@pytest.fixture
def ddb():
    table = FakeTable()
    for t in sample_traces():
        dynamodb.apply(table, lj.to_record(t, "dynamodb"))
    return table


def run_ddb(table, name, **p):
    qs = lj.build_query(name, "dynamodb", **p)
    return lj.from_rows(name, "dynamodb", [table.query(**q)["Items"] for q in qs])


def test_dynamodb_roundtrip(ddb):
    recent = run_ddb(ddb, "recent_traces", tenant_id="t1", limit=2)
    assert [t.name for t in recent] == ["op2", "op1"]
    assert recent[0].totals.cost.total > 0

    t = run_ddb(ddb, "trace_detail", trace_id=recent[0].id)
    assert len(t.generations) == 2 and t.generations[1].output == "answer"
    assert t.totals.cost.total == pytest.approx(recent[0].totals.cost.total)

    days = run_ddb(ddb, "daily_cost", tenant_id="t1", start_day="2026-10-15", end_day="2026-10-17")
    assert len(days) == 3 and days[0].call_count == 2
    assert days[0].cost == pytest.approx(recent[0].totals.cost.total)


def test_dynamodb_large_payload_goes_to_blob():
    t = lj.Trace(name="big", started_at=T0)
    t.generations.append(lj.Generation(model="m", input="x" * 200_000, output="y"))
    rec = lj.to_record(t, "dynamodb", blob_prefix="traces/")
    gen_item = rec.writes[1][1]
    assert "payload" not in gen_item and gen_item["payload_ref"].startswith(f"traces/{t.id}/")
    assert len(rec.blobs[gen_item["payload_ref"]]) > 200_000


def test_dynamodb_missing_trace_returns_none(ddb):
    assert run_ddb(ddb, "trace_detail", trace_id="nope") is None


def test_unknown_backend_and_query():
    with pytest.raises(ValueError):
        lj.build_query("recent_traces", "mongo")
    with pytest.raises(ValueError):
        lj.build_query("nope", "dynamodb")


# ── rendering ───────────────────────────────────────────────────────────────

def test_render_escapes_user_content():
    t = sample_traces()[0]
    t.name = "<script>alert(1)</script>"
    t.generations[0].output = "<img src=x onerror=alert(1)>"
    for html in (lj.render_trace_list([t]), lj.render_trace(t)):
        assert "<script>alert" not in html and "<img src=x" not in html


def test_render_pages_have_content():
    traces = sample_traces()
    assert "op0" in lj.render_trace_list(traces)
    detail = lj.render_trace(traces[0])
    assert "LLM calls (2)" in detail and "answer" in detail
    assert "Trace not found" in lj.render_trace(None)
    rows = [lj.DailyCost(day="2026-10-15", tenant_id="t1", cost=1.5, call_count=3)]
    assert "2026-10-15" in lj.render_daily_cost(rows)


def test_postgres_accepts_dynamodb_style_cursor(sql_db):
    recent = run_sql(sql_db, "recent_traces", tenant_id="t1")
    cursor = f"{recent[1].started_at.isoformat()}#{recent[1].id}"
    assert [t.name for t in run_sql(sql_db, "recent_traces", tenant_id="t1", before=cursor)] == ["op0"]
