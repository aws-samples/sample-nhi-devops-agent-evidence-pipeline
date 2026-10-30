"""Impact Assessment Lambda (Branch 2, Step 4).

Invoked by the investigation Lambda with the DevOps Agent findings. Evaluates
the escalation thresholds and, if any is met, opens a single Support case for
the incident with the S3 evidence package attached.

The AWS Support API requires a Business or Enterprise Support plan, which lab
accounts rarely have, so when MOCK_EXTERNAL is true the would-be case payload is
written to S3 instead of calling CreateCase. Flip MOCK_EXTERNAL to false in a
Business/Enterprise account to create real cases.
"""

from __future__ import annotations

import json

import boto3

from common import (
    FLEET_DEGRADED_PCT_THRESHOLD,
    LOG,
    MOCK_EXTERNAL,
    maintenance_window_active,
    put_evidence,
    wait_for_evidence,
)

# Support API lives only in us-east-1.
_support = boto3.client("support", region_name="us-east-1")


def _should_escalate(findings: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    azs = findings.get("affected_azs", [])
    if len(azs) > 1:
        reasons.append(f"impact spans {len(azs)} AZs ({', '.join(azs)})")
    pct = float(findings.get("fleet_degraded_pct", 0))
    if pct > FLEET_DEGRADED_PCT_THRESHOLD:
        reasons.append(f"{pct}% of fleet degraded (threshold {FLEET_DEGRADED_PCT_THRESHOLD}%)")
    if findings.get("correlated_health_event"):
        reasons.append(f"correlated AWS Health event: {findings['correlated_health_event']}")
    return (len(reasons) > 0, reasons)


def _build_case_body(incident_id: str, findings: dict, reasons: list[str], evidence_keys: list[str]) -> str:
    lines = [
        f"Automated network-impairment incident report ({incident_id})",
        "",
        "NHI (Network Health Indicator) degraded - AWS-confirmed network impairment.",
        "",
        "Escalation reasons:",
        *[f"  - {r}" for r in reasons],
        "",
        f"Affected AZs: {', '.join(findings.get('affected_azs', [])) or 'n/a'}",
        f"Fleet degraded: {findings.get('fleet_degraded_pct', 'n/a')}%",
        f"Correlated AWS Health event: {findings.get('correlated_health_event') or 'none'}",
        "",
        "DevOps Agent summary:",
        f"  {findings.get('summary', '')}",
        "",
        "Attached S3 evidence package:",
        *[f"  - {k}" for k in evidence_keys],
    ]
    return "\n".join(lines)


def handler(event: dict, _ctx) -> dict:
    incident_id = event["incident_id"]
    findings = event["findings"]
    LOG.info("Impact assessment for incident %s", incident_id)

    if maintenance_window_active():
        LOG.info("Maintenance window active - not creating Support case")
        return {"action": "suppressed-maintenance-window", "incident": incident_id}

    escalate, reasons = _should_escalate(findings)
    # Wait for Branch 1's per-host forensics to land so the case references them,
    # rather than racing ahead and attaching only the investigation findings.
    evidence_keys = wait_for_evidence(incident_id)
    case_body = _build_case_body(incident_id, findings, reasons, evidence_keys)

    if not escalate:
        LOG.info("Thresholds not met for %s - preserving evidence, no case", incident_id)
        put_evidence(incident_id, "assessment/no-escalation.json",
                     {"incident_id": incident_id, "escalate": False, "evidence": evidence_keys})
        return {"action": "no-escalation", "incident": incident_id}

    if MOCK_EXTERNAL:
        # Simulate the Support API: persist exactly what we WOULD have submitted.
        mock_case = {
            "subject": f"[AUTOMATED][NHI] Network impairment {incident_id}",
            "serviceCode": "amazon-ec2-networking",
            "severityCode": "urgent",
            "categoryCode": "network",
            "communicationBody": case_body,
            "evidence_attached": evidence_keys,
        }
        loc = put_evidence(incident_id, "assessment/mock-support-case.json", mock_case)
        LOG.info("MOCK Support case written to %s", loc)
        return {"action": "mock-case-created", "incident": incident_id, "case": loc, "reasons": reasons}

    # Real Support API path (requires Business/Enterprise support). pragma: no cover
    case = _support.create_case(
        subject=f"[AUTOMATED][NHI] Network impairment {incident_id}",
        serviceCode="amazon-ec2-networking",
        severityCode="urgent",
        categoryCode="network",
        communicationBody=case_body,
        issueType="technical",
    )
    LOG.info("Created Support case %s", case["caseId"])
    return {"action": "case-created", "incident": incident_id, "caseId": case["caseId"], "reasons": reasons}
