"""Investigation Lambda (Branch 2).

Triggered by the same NHI alarm SNS message as the hook installer. In
production this would call the AWS DevOps Agent (CreateBacklogTask) to run one
autonomous investigation per incident. There is no public DevOps Agent API to
call from a lab, so when MOCK_EXTERNAL is true we synthesize a realistic
findings document, write it to the shared S3 evidence package, and then invoke
the impact-assessment step.

Set MOCK_EXTERNAL=false (and wire a real client) only in an account that has
DevOps Agent access.
"""

from __future__ import annotations

import json
import os
import random

import boto3

from common import (
    LOG,
    MOCK_EXTERNAL,
    incident_id_from_alarm_time,
    maintenance_window_active,
    put_evidence,
)

IMPACT_FUNCTION_NAME = os.environ["IMPACT_FUNCTION_NAME"]
# Agent Space id to file the investigation under (required for the real API).
AGENT_SPACE_ID = os.environ.get("AGENT_SPACE_ID", "")
# How long to wait for the agent's async investigation before proceeding (0 = don't poll).
DEVOPS_AGENT_POLL_SECONDS = int(os.environ.get("DEVOPS_AGENT_POLL_SECONDS", "0"))

_lambda = boto3.client("lambda")


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def _mock_devops_agent_findings(incident_id: str) -> dict:
    """Produce a plausible DevOps Agent investigation log for the lab.

    The shape mirrors what the impact-assessment step expects: affected AZs,
    fleet-degraded percentage, and any correlated AWS Health event.
    """
    azs = random.choice([["use1-az1"], ["use1-az1", "use1-az2"], ["use1-az1", "use1-az2", "use1-az4"]])
    return {
        "incident_id": incident_id,
        "source": "mock-devops-agent",
        "summary": "Autonomous investigation of NHI-degraded network event.",
        "affected_azs": azs,
        "fleet_degraded_pct": round(random.uniform(3, 25), 1),
        "correlated_health_event": random.choice([None, "AWS_EC2_OPERATIONAL_ISSUE"]),
        "analyzed_signals": [
            "AWS/NetworkFlowMonitor HealthIndicator",
            "AWS/NetworkFlowMonitor Retransmissions",
            "AWS/NetworkFlowMonitor RoundTripTime",
        ],
        "recommended_next_steps": [
            "Correlate NHI onset timestamp with internal NSE tooling.",
            "Attach sampled per-host network state to the Support case.",
        ],
    }


def _real_devops_agent_findings(incident_id: str, alarm_msg: dict) -> dict:
    """Start a real AWS DevOps Agent investigation via CreateBacklogTask.

    Uses the public boto3 'devops-agent' client (API version 2026-01-01). The
    request shape here matches the verified CreateBacklogTask contract:

      * agentSpaceId  - URI path parameter, passed as a top-level kwarg (REQUIRED)
      * taskType      - INVESTIGATION | EVALUATION | RELEASE_READINESS_REVIEW | RELEASE_TESTING
      * priority      - CRITICAL | HIGH | MEDIUM | LOW | MINIMAL
      * title         - 1..400 chars
      * description   - 0..10000 chars
      * clientToken   - idempotency key (we use the incident id)
      * reference     - optional object linking the originating signal

    The response is { "task": { taskId, executionId, status, ... } }.

    The investigation runs ASYNCHRONOUSLY: CreateBacklogTask returns immediately
    with a task in a pending/running status; the agent then reads metrics, code,
    and deploy data over minutes. Findings are retrieved later via GetBacklogTask.
    By default (DEVOPS_AGENT_POLL_SECONDS=0) this function starts the task and
    returns a 'pending' findings doc. A production deployment decouples retrieval
    (an EventBridge-scheduled or Step Functions poller calls GetBacklogTask, writes
    the completed findings into the evidence package, and then triggers escalation).

    The returned dict is shaped for impact_assessment._should_escalate
    (affected_azs, fleet_degraded_pct, correlated_health_event). Until the agent
    completes, those are empty/zero so the impact step will not escalate on
    half-finished data.
    """
    if not AGENT_SPACE_ID:
        raise RuntimeError(
            "AGENT_SPACE_ID is required when MOCK_EXTERNAL=false. Deploy with "
            "--parameter-overrides AgentSpaceId=<your-agent-space-id>."
        )

    agent = boto3.client("devops-agent")
    alarm_name = alarm_msg.get("AlarmName", "nhi-degraded")
    description = (
        f"NHI (Network Health Indicator) degraded for monitor alarm '{alarm_name}'. "
        f"Incident {incident_id}. Investigate scope of impact: affected Availability "
        f"Zones, percentage of fleet degraded, and any correlated AWS Health events "
        f"for the EC2/network backbone."
    )

    resp = agent.create_backlog_task(
        agentSpaceId=AGENT_SPACE_ID,            # URI path param
        taskType="INVESTIGATION",
        priority="HIGH",
        title=f"NHI network impairment {incident_id}"[:400],
        description=description[:10000],
        clientToken=incident_id,                 # idempotent per incident
        # NOTE: `reference` is optional and omitted here. When supplied, its
        # `system` field must be one of the service's known data-plane source
        # systems (an arbitrary string like "CloudWatch-NetworkFlowMonitor" is
        # rejected with a ValidationException). Populate `reference` only with a
        # system value your Agent Space recognizes.
    )

    task = resp.get("task", {})
    task_id = task.get("taskId")
    execution_id = task.get("executionId")
    status = task.get("status", "PENDING")
    LOG.info("Started DevOps Agent investigation task=%s execution=%s status=%s incident=%s",
             task_id, execution_id, status, incident_id)

    findings: dict = {
        "incident_id": incident_id,
        "source": "aws-devops-agent",
        "agent_space_id": AGENT_SPACE_ID,
        "task_id": task_id,
        "execution_id": execution_id,
        "status": status,
        "summary": "DevOps Agent investigation started (async); findings pending.",
        # Impact assessment reads these; empty/zero until the agent reports back.
        "affected_azs": [],
        "fleet_degraded_pct": 0,
        "correlated_health_event": None,
    }

    # Optional bounded poll for a fast-completing investigation. In production
    # prefer a decoupled poller over blocking the Lambda.
    if DEVOPS_AGENT_POLL_SECONDS > 0 and task_id:
        import time
        deadline = time.time() + DEVOPS_AGENT_POLL_SECONDS
        while time.time() < deadline:
            # Intentional bounded poll (DEVOPS_AGENT_POLL_SECONDS): the DevOps
            # Agent client has no waiter for backlog task completion.
            time.sleep(10)  # nosemgrep: arbitrary-sleep
            got = agent.get_backlog_task(agentSpaceId=AGENT_SPACE_ID, taskId=task_id)
            t = got.get("task", {})
            state = t.get("status", "")
            findings["status"] = state
            if state in ("COMPLETED", "COMPLETE", "DONE", "SUCCEEDED"):
                findings["summary"] = t.get("statusReason") or findings["summary"]
                # The agent's structured result lives in metadata/supportMetadata;
                # map whatever your Agent Space emits into the escalation fields.
                findings["metadata"] = t.get("metadata")
                findings["support_metadata"] = t.get("supportMetadata")
                break
            if state in ("FAILED", "CANCELLED", "ERROR"):
                findings["summary"] = t.get("statusReason") or "investigation failed"
                break

    return findings


def handler(event: dict, _ctx) -> dict:
    record = event["Records"][0]["Sns"]
    msg = json.loads(record["Message"])
    if msg.get("NewStateValue") != "ALARM":
        return {"action": "noop", "state": msg.get("NewStateValue")}

    incident_id = incident_id_from_alarm_time(msg.get("StateChangeTime", ""))
    LOG.info("Investigation Lambda: incident=%s mock=%s", incident_id, MOCK_EXTERNAL)

    if maintenance_window_active():
        LOG.info("Maintenance window active - recording event but suppressing escalation")
        put_evidence(incident_id, "investigation/suppressed.json",
                     {"incident_id": incident_id, "suppressed": "maintenance-window"})
        return {"action": "suppressed-maintenance-window", "incident": incident_id}

    if MOCK_EXTERNAL:
        findings = _mock_devops_agent_findings(incident_id)
    else:
        findings = _real_devops_agent_findings(incident_id, msg)

    put_evidence(incident_id, "investigation/findings.json", findings)

    # Hand off to the impact-assessment step with the findings as its input.
    _lambda.invoke(
        FunctionName=IMPACT_FUNCTION_NAME,
        InvocationType="Event",  # async; branch 1 keeps collecting in parallel
        Payload=json.dumps({"incident_id": incident_id, "findings": findings}).encode("utf-8"),
    )
    LOG.info("Dispatched impact assessment for incident %s", incident_id)
    return {"action": "investigated", "incident": incident_id, "findings": findings}
