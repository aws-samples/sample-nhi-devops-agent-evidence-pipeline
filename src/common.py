"""Shared helpers for the NHI evidence-automation lab pipeline.

Everything in this module is intentionally dependency-light: only boto3 (which
is present in the Lambda runtime) and the standard library. The helpers here
are shared by all four Lambda handlers.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os

import boto3

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Environment configuration (set by the CloudFormation template)
# ---------------------------------------------------------------------------
EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")
STATE_TABLE = os.environ.get("STATE_TABLE", "")
SAMPLE_CAP = int(os.environ.get("SAMPLE_CAP", "3"))

# When true (the default for the lab) the DevOps Agent and Support API calls are
# simulated: their would-be payloads are written to S3 instead of hitting real
# APIs. Set to "false" only in an account that actually has DevOps Agent access
# and a Business/Enterprise Support plan.
MOCK_EXTERNAL = os.environ.get("MOCK_EXTERNAL", "true").lower() == "true"

# Impact-assessment escalation thresholds (see blog Step 4 / Branch 2).
FLEET_DEGRADED_PCT_THRESHOLD = float(os.environ.get("FLEET_DEGRADED_PCT_THRESHOLD", "10"))


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(ts: _dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def incident_id_from_alarm_time(alarm_time: str) -> str:
    """Derive a stable per-incident key from the alarm state-change time.

    The NHI alarm's onset timestamp anchors the whole incident window, so every
    Lambda in the pipeline derives the same incident id from it. We bucket to
    the minute so the two branches (evidence + investigation) agree even if
    their events arrive a few seconds apart.
    """
    # alarm_time looks like 2026-10-07T12:34:56.789+0000
    cleaned = alarm_time.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = _dt.datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue
    else:
        parsed = utcnow()
    return "incident-" + parsed.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M")


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
_s3 = boto3.client("s3")


def put_evidence(incident_id: str, key_suffix: str, body: dict | str) -> str:
    """Write an evidence artifact to S3, partitioned by incident id."""
    key = f"{incident_id}/{key_suffix}"
    payload = body if isinstance(body, str) else json.dumps(body, indent=2, default=str)
    _s3.put_object(
        Bucket=EVIDENCE_BUCKET,
        Key=key,
        Body=payload.encode("utf-8"),
        ContentType="application/json",
    )
    LOG.info("Wrote evidence s3://%s/%s", EVIDENCE_BUCKET, key)
    return f"s3://{EVIDENCE_BUCKET}/{key}"


def list_evidence(incident_id: str) -> list[str]:
    resp = _s3.list_objects_v2(Bucket=EVIDENCE_BUCKET, Prefix=f"{incident_id}/")
    return [obj["Key"] for obj in resp.get("Contents", [])]


def wait_for_evidence(incident_id: str, max_wait_s: int = 90, poll_s: int = 5) -> list[str]:
    """Wait for Branch 1 (evidence collector) to finish before reading evidence.

    The investigation branch runs immediately and dispatches impact assessment,
    but the evidence collector needs ~30-60s per sampled instance to run SSM and
    write its instances/<id>.json object. Without waiting, the Support case is
    built before the per-host forensics land and omits them.

    We can't simply wait for SAMPLE_CAP files: in a real incident fewer than the
    cap (even zero) instances may terminate, so a fixed count would hang. Instead
    we poll until the per-instance evidence count holds steady across two polls
    (collection has quiesced) or max_wait_s elapses. This settles quickly when a
    few instances are sampled and degrades gracefully to "no host evidence" when
    none terminate.
    """
    import time

    def _instance_count(keys: list[str]) -> int:
        return sum(1 for k in keys if "/instances/" in k)

    waited = 0
    prev_count = -1
    keys = list_evidence(incident_id)
    while waited < max_wait_s:
        count = _instance_count(keys)
        # Quiesced: we have at least one host sample and it didn't grow last poll.
        if count > 0 and count == prev_count:
            LOG.info("Evidence settled for %s: %d instance sample(s)", incident_id, count)
            return keys
        prev_count = count
        # Intentional bounded poll (max_wait_s): no AWS waiter exists for
        # "S3 prefix stopped growing", so we sleep between list calls.
        time.sleep(poll_s)  # nosemgrep: arbitrary-sleep
        waited += poll_s
        keys = list_evidence(incident_id)

    LOG.info("wait_for_evidence timed out after %ds for %s (%d instance sample(s))",
             max_wait_s, incident_id, _instance_count(keys))
    return keys


# ---------------------------------------------------------------------------
# DynamoDB: strict sample cap + maintenance window
# ---------------------------------------------------------------------------
_ddb = boto3.client("dynamodb")


def try_claim_sample_slot(incident_id: str, instance_id: str) -> bool:
    """Atomically claim one of SAMPLE_CAP evidence slots for this incident.

    This is the crux of the collect-and-release pattern. An S3 counter cannot
    enforce a strict cap under the burst of concurrent terminations a real
    network event produces, so we use a DynamoDB conditional UpdateItem as an
    atomic counter: increment `sample_count` only while it is below the cap.
    DynamoDB serializes the conditional update, so exactly SAMPLE_CAP slots are
    ever handed out, no matter how many Lambdas race.

    Returns True if this invocation won a slot (and should collect evidence),
    False if the cap is already reached (release the instance immediately).
    """
    pk = f"SAMPLE#{incident_id}"
    try:
        _ddb.update_item(
            TableName=STATE_TABLE,
            Key={"pk": {"S": pk}},
            UpdateExpression="SET sample_count = if_not_exists(sample_count, :zero) + :one, "
            "last_instance = :iid",
            ConditionExpression="attribute_not_exists(sample_count) OR sample_count < :cap",
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":cap": {"N": str(SAMPLE_CAP)},
                ":iid": {"S": instance_id},
            },
        )
        LOG.info("Claimed sample slot for %s (incident %s)", instance_id, incident_id)
        return True
    except _ddb.exceptions.ConditionalCheckFailedException:
        LOG.info("Sample cap %d reached for incident %s; releasing %s", SAMPLE_CAP, incident_id, instance_id)
        return False


def set_active_incident(incident_id: str) -> None:
    """Record the current incident id when the NHI alarm fires.

    Both branches must anchor to the SAME incident key — the NHI alarm onset —
    so that per-host evidence (collected as instances terminate at various later
    times) and the investigation findings land in one consolidated S3 prefix.
    The evidence collector reads this value rather than deriving a key from each
    instance's termination time, which would scatter evidence across prefixes.
    """
    _ddb.put_item(
        TableName=STATE_TABLE,
        Item={"pk": {"S": "ACTIVE#incident"}, "incident_id": {"S": incident_id}},
    )
    LOG.info("Recorded active incident %s", incident_id)


def get_active_incident() -> str | None:
    resp = _ddb.get_item(TableName=STATE_TABLE, Key={"pk": {"S": "ACTIVE#incident"}})
    item = resp.get("Item")
    return item["incident_id"]["S"] if item else None


def clear_active_incident() -> None:
    _ddb.delete_item(TableName=STATE_TABLE, Key={"pk": {"S": "ACTIVE#incident"}})
    LOG.info("Cleared active incident")


def maintenance_window_active() -> bool:
    """Return True if a maintenance window is currently active.

    The item is written with a DynamoDB TTL so a forgotten window self-expires.
    We treat an unexpired MAINT#window item as 'active'.
    """
    resp = _ddb.get_item(TableName=STATE_TABLE, Key={"pk": {"S": "MAINT#window"}})
    item = resp.get("Item")
    if not item:
        return False
    expires = int(item.get("ttl", {}).get("N", "0"))
    active = expires > int(utcnow().timestamp())
    if active:
        LOG.info("Maintenance window is active (expires epoch %d) - suppressing escalation", expires)
    return active
