# llmjournal

Record LLM calls and their cost, save them in **your** database, and view them as HTML pages.
There's no server to run and the package has no dependencies. See [DESIGN.md](https://github.com/osman-syed/llmjournal/blob/main/DESIGN.md).

```python
import llmjournal as lj
from llmjournal.backends import dynamodb

def save(trace):                       # you own the DB connection
    records = lj.to_record(trace, "dynamodb")
    dynamodb.apply(table, records)     # + upload records.blobs to S3

tracer = lj.Tracer(sink=save, pricing=lj.Pricing.from_env())

with tracer.trace("summarise_document", tenant_id="t1", session_id="doc-42"):
    resp = call_llm(...)
    lj.record_generation(model_id, usage=resp["usage"], input=messages, output=text)
```

Viewing:

```python
qs = lj.build_query("recent_traces", "dynamodb", tenant_id="t1")
traces = lj.from_rows("recent_traces", "dynamodb", [table.query(**q)["Items"] for q in qs])
html = lj.render_trace_list(traces)
```

Full examples: [`examples/dynamodb_sink.py`](https://github.com/osman-syed/llmjournal/blob/main/examples/dynamodb_sink.py), [`examples/lambda_viewer.py`](https://github.com/osman-syed/llmjournal/blob/main/examples/lambda_viewer.py).

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

Against real Postgres and DynamoDB Local (skipped unless the env vars are set):

```bash
docker run -d --name llmjournal-pg  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=lj -p 127.0.0.1:55432:5432 postgres:16-alpine
docker run -d --name llmjournal-ddb -p 127.0.0.1:58000:8000 amazon/dynamodb-local
.venv/bin/pip install -e '.[integration]'
LJ_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/lj LJ_DDB_ENDPOINT=http://127.0.0.1:58000 .venv/bin/pytest
```
