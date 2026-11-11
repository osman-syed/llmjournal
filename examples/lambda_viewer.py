"""Viewer: one Lambda that renders the three pages from DynamoDB.

Routes (query string):  ?            recent traces
                        ?trace_id=…  one trace with every LLM call
                        ?view=cost   daily cost, last 30 days

SECURITY: these pages show full prompts and responses. Put the Lambda behind API Gateway with a
Cognito authorizer (tenant taken from the token, as below) or a Function URL with AuthType=AWS_IAM.
Never expose it publicly with a tenant chosen from the query string.

Env: LLMJOURNAL_TABLE
"""

import os
from urllib.parse import urlencode
from datetime import date, timedelta

import boto3

import llmjournal as lj

_table = boto3.resource("dynamodb").Table(os.environ["LLMJOURNAL_TABLE"])


def _run(name: str, **params):
    results = []
    for q in lj.build_query(name, "dynamodb", **params):
        items, resp = [], _table.query(**q)
        items += resp["Items"]
        # trace_detail can span pages when a trace has many calls; recent_traces is capped by Limit.
        while name == "trace_detail" and "LastEvaluatedKey" in resp:
            resp = _table.query(**q, ExclusiveStartKey=resp["LastEvaluatedKey"])
            items += resp["Items"]
        results.append(items)
    return lj.from_rows(name, "dynamodb", results)


def _tenant(event) -> str:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not tenant:
        raise PermissionError("no tenant in token")
    return tenant


def handler(event, _context):
    qs = event.get("queryStringParameters") or {}
    try:
        tenant = _tenant(event)
    except PermissionError:
        return {"statusCode": 403, "body": "forbidden"}

    if qs.get("trace_id"):
        trace = _run("trace_detail", trace_id=qs["trace_id"])
        if trace is not None and trace.tenant_id != tenant:  # never show another tenant's trace
            trace = None
        html = lj.render_trace(trace)
    elif qs.get("view") == "cost":
        end = date.today()
        rows = _run("daily_cost", tenant_id=tenant, start_day=(end - timedelta(days=30)).isoformat(), end_day=end.isoformat())
        html = lj.render_daily_cost(rows)
    else:
        traces = _run("recent_traces", tenant_id=tenant, limit=50, before=qs.get("before"))
        last = traces[-1] if len(traces) == 50 else None
        nxt = "?" + urlencode({"before": f"{last.started_at.isoformat()}#{last.id}"}) if last else None
        html = lj.render_trace_list(traces, next_link=nxt)

    return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": html}
