"""Evidence Collector Lambda (Branch 1 target).

This is the hook's target. It is invoked once per terminating instance (via the
lifecycle-hook SNS notification). It implements the collect-and-release pattern:

  * Claim one of SAMPLE_CAP slots via an atomic DynamoDB counter.
  * If the cap is reached, release the instance immediately (CONTINUE, <1s).
  * Otherwise run SSM diagnostic commands, pull NFM metrics, write to S3, and
    then release the instance.

Either way the instance is ALWAYS released, even on error, so termination is
never permanently blocked.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

import boto3
from botocore.exceptions import WaiterError

from common import (
    LOG,
    get_active_incident,
    incident_id_from_alarm_time,
    put_evidence,
    try_claim_sample_slot,
    utcnow,
)

# The on-host network forensic commands (blog: "What evidence gets collected").
DIAGNOSTIC_COMMANDS = [
    "echo '=== netstat -s ==='; netstat -s || true",
    "echo '=== ss -tnp ==='; ss -tnp || true",
    "echo '=== ip -s link ==='; ip -s link || true",
    "echo '=== /proc/net/snmp ==='; cat /proc/net/snmp || true",
    "echo '=== dmesg (net) ==='; dmesg | tail -n 200 || true",
]

NFM_METRICS = ["RoundTripTime", "Timeouts", "Retransmissions", "HealthIndicator"]
SSM_POLL_SECONDS = int(os.environ.get("SSM_POLL_SECONDS", "60"))

_asg = boto3.client("autoscaling")
_ssm = boto3.client("ssm")
_cw = boto3.client("cloudwatch")


def _complete(hook: dict, result: str = "CONTINUE") -> None:
    """Release the instance from the Terminating:Wait state."""
    _asg.complete_lifecycle_action(
        LifecycleHookName=hook["LifecycleHookName"],
        AutoScalingGroupName=hook["AutoScalingGroupName"],
        LifecycleActionResult=result,
        InstanceId=hook["EC2InstanceId"],
        LifecycleActionToken=hook.get("LifecycleActionToken", ""),
    )
    LOG.info("CompleteLifecycleAction result=%s for %s", result, hook["EC2InstanceId"])


def _run_ssm(instance_id: str) -> dict:
    """Run the diagnostic commands via SSM Run Command and return the output."""
    cmd = _ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": DIAGNOSTIC_COMMANDS},
        TimeoutSeconds=SSM_POLL_SECONDS,
    )
    command_id = cmd["Command"]["CommandId"]

    # Use the built-in SSM waiter instead of a hand-rolled sleep loop. It also
    # retries InvocationDoesNotExist, which is common right after send_command.
    # A terminal non-success status (Failed/TimedOut/Cancelled) raises
    # WaiterError, but we still want that invocation's output as evidence.
    delay = 3
    try:
        _ssm.get_waiter("command_executed").wait(
            CommandId=command_id,
            InstanceId=instance_id,
            WaiterConfig={"Delay": delay, "MaxAttempts": max(1, SSM_POLL_SECONDS // delay)},
        )
    except WaiterError as exc:
        LOG.info("SSM waiter ended without Success for %s: %s", instance_id, exc)

    try:
        inv = _ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except _ssm.exceptions.InvocationDoesNotExist:
        return {"status": "Pending-timeout", "stdout": "", "stderr": "SSM invocation not found"}

    if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
        return {
            "status": inv["Status"],
            "stdout": inv.get("StandardOutputContent", ""),
            "stderr": inv.get("StandardErrorContent", ""),
        }
    return {"status": "Pending-timeout", "stdout": "", "stderr": "SSM poll timed out in Lambda"}


def _pull_nfm_metrics(window_start: _dt.datetime) -> dict:
    """Pull NFM metrics around the incident window via GetMetricStatistics."""
    out: dict = {}
    end = utcnow()
    for metric in NFM_METRICS:
        try:
            resp = _cw.get_metric_statistics(
                Namespace="AWS/NetworkFlowMonitor",
                MetricName=metric,
                StartTime=window_start,
                EndTime=end,
                Period=60,
                Statistics=["Average", "Maximum"],
            )
            out[metric] = resp.get("Datapoints", [])
        except Exception as exc:  # pragma: no cover - lab resilience
            out[metric] = {"error": str(exc)}
    return out


def handler(event: dict, _ctx) -> dict:
    # The lifecycle-hook notification is delivered via SNS -> Lambda.
    record = event["Records"][0]["Sns"]
    hook = json.loads(record["Message"])

    # Test/heartbeat notifications have no instance to act on.
    if hook.get("Event", "").endswith("TEST_NOTIFICATION") or "EC2InstanceId" not in hook:
        LOG.info("Ignoring lifecycle test notification")
        return {"action": "ignored-test"}

    instance_id = hook["EC2InstanceId"]
    # Anchor to the NHI alarm onset recorded by the hook installer so evidence
    # and investigation findings share one incident prefix. Fall back to the
    # lifecycle event time only if no active incident is recorded (e.g. a stray
    # termination outside an NHI window).
    incident_id = get_active_incident() or incident_id_from_alarm_time(hook.get("Time", ""))
    LOG.info("Evidence collector: instance=%s incident=%s", instance_id, incident_id)

    # Strict, atomic sample cap. Losers release immediately.
    if not try_claim_sample_slot(incident_id, instance_id):
        _complete(hook, "CONTINUE")
        return {"action": "released-over-cap", "instance": instance_id}

    # Winner: collect, then ALWAYS release.
    try:
        window_start = utcnow() - _dt.timedelta(minutes=15)
        ssm_out = _run_ssm(instance_id)
        nfm = _pull_nfm_metrics(window_start)
        evidence = {
            "incident_id": incident_id,
            "instance_id": instance_id,
            "collected_at": utcnow().isoformat(),
            "ssm": ssm_out,
            "nfm_metrics": nfm,
        }
        loc = put_evidence(incident_id, f"instances/{instance_id}.json", evidence)
        return {"action": "collected", "instance": instance_id, "evidence": loc}
    except Exception as exc:  # noqa: BLE001 - we must never let this block termination
        LOG.exception("Evidence collection failed for %s: %s", instance_id, exc)
        return {"action": "collect-failed", "instance": instance_id, "error": str(exc)}
    finally:
        _complete(hook, "CONTINUE")
