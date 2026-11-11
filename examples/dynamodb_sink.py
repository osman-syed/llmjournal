"""Recording side for a DynamoDB + S3 setup. Import `tracer` wherever LLM operations start.

Env: LLMJOURNAL_TABLE, LLMJOURNAL_BUCKET (for payloads > 100 KB), LLM_PRICING.
"""

import os

import boto3

import llmjournal as lj
from llmjournal.backends import dynamodb

_table = boto3.resource("dynamodb").Table(os.environ["LLMJOURNAL_TABLE"])
_s3 = boto3.client("s3")
_bucket = os.environ.get("LLMJOURNAL_BUCKET", "")


def save(trace: lj.Trace) -> None:
    records = lj.to_record(trace, "dynamodb", ttl_days=90)
    for key, body in records.blobs.items():  # upload big payloads first so refs never dangle
        _s3.put_object(Bucket=_bucket, Key=key, Body=body, ContentType="application/json")
    dynamodb.apply(_table, records)


tracer = lj.Tracer(sink=save, pricing=lj.Pricing.from_env())


# Usage where an operation starts:
#
#   with tracer.trace("summarise_document", tenant_id=tenant, user_id=email, session_id=doc_id) as t:
#       t.input = {"doc_id": doc_id}
#       ...
#
# And right after each LLM provider call:
#
#   lj.record_generation(model_id, usage=response["usage"], input=messages, output=text,
#                        started_at=start, ended_at=lj.models.utcnow())
