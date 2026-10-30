"""Hook Installer Lambda (Branch 1 entry point).

Triggered by the SNS topic when the NHI alarm changes state. On ALARM it
attaches an EC2_INSTANCE_TERMINATING lifecycle hook (DefaultResult=CONTINUE) to
the target ASG; on OK it removes the hook, restoring normal scaling with zero
overhead.

The same SNS message also drives the investigation Lambda in parallel; this
handler only owns the lifecycle-hook lifecycle.
"""

from __future__ import annotations

import json
import os

import boto3

from common import (
    LOG,
    clear_active_incident,
    incident_id_from_alarm_time,
    maintenance_window_active,
    set_active_incident,
)

ASG_NAME = os.environ["ASG_NAME"]
HOOK_NAME = os.environ.get("HOOK_NAME", "nhi-evidence-hook")
HOOK_ROLE_ARN = os.environ["HOOK_ROLE_ARN"]
HOOK_TARGET_SNS_ARN = os.environ["HOOK_TARGET_SNS_ARN"]
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "120"))

_asg = boto3.client("autoscaling")


def _parse_alarm(event: dict) -> dict:
    record = event["Records"][0]["Sns"]
    # CloudWatch alarm notifications carry NewStateValue = ALARM | OK | INSUFFICIENT_DATA
    return json.loads(record["Message"])


def _install_hook() -> None:
    _asg.put_lifecycle_hook(
        LifecycleHookName=HOOK_NAME,
        AutoScalingGroupName=ASG_NAME,
        LifecycleTransition="autoscaling:EC2_INSTANCE_TERMINATING",
        # DefaultResult=CONTINUE: if the Evidence Lambda fails or the heartbeat
        # expires, the ASG proceeds with termination. Evidence collection can
        # never permanently block scaling. CONTINUE (vs ABANDON) also lets any
        # other lifecycle hooks the team runs complete normally.
        DefaultResult="CONTINUE",
        HeartbeatTimeout=HEARTBEAT_TIMEOUT,
        NotificationTargetARN=HOOK_TARGET_SNS_ARN,
        RoleARN=HOOK_ROLE_ARN,
    )
    LOG.info("Installed lifecycle hook %s on ASG %s", HOOK_NAME, ASG_NAME)


def _remove_hook() -> None:
    try:
        _asg.delete_lifecycle_hook(LifecycleHookName=HOOK_NAME, AutoScalingGroupName=ASG_NAME)
        LOG.info("Removed lifecycle hook %s from ASG %s", HOOK_NAME, ASG_NAME)
    except _asg.exceptions.ClientError as exc:  # pragma: no cover - lab convenience
        LOG.warning("Could not remove hook (may not exist): %s", exc)


def handler(event: dict, _ctx) -> dict:
    msg = _parse_alarm(event)
    state = msg.get("NewStateValue", "UNKNOWN")
    LOG.info("Hook installer invoked; NHI alarm state=%s", state)

    if state == "ALARM":
        # Anchor the incident to the NHI alarm onset and record it so the
        # evidence branch uses the SAME incident key (see common.set_active_incident).
        incident_id = incident_id_from_alarm_time(msg.get("StateChangeTime", ""))
        set_active_incident(incident_id)

        if maintenance_window_active():
            # Blog guardrail: during planned work we can suppress hook install.
            LOG.info("Maintenance window active - skipping hook installation")
            return {"action": "suppressed-maintenance-window", "incident": incident_id}
        _install_hook()
        return {"action": "installed", "hook": HOOK_NAME, "asg": ASG_NAME, "incident": incident_id}

    if state == "OK":
        # NHI healthy again: remove the hook and close the incident window.
        _remove_hook()
        clear_active_incident()
        return {"action": "removed", "hook": HOOK_NAME, "asg": ASG_NAME}

    LOG.info("No action for alarm state %s", state)
    return {"action": "noop", "state": state}
