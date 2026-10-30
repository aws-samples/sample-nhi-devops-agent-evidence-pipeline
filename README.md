# NHI + DevOps Agent Evidence Pipeline

This sample is a deployable, end-to-end pipeline that collects evidence of AWS
network impairments and escalates them to AWS Support. It monitors the **Network
Health Indicator (NHI)** from Amazon CloudWatch Network Flow Monitor. When NHI
reports a degradation, the pipeline:

1. Captures short-lived, per-host network diagnostics from Auto Scaling instances
   before they terminate.
2. Starts an **AWS DevOps Agent** investigation.
3. Opens an AWS Support case with the collected evidence attached, if the
   escalation thresholds are met.


## ⚠️ Important Notice

These tools are provided as sample code. If you intend to deploy them in
production, start with a non-production environment first, and before deploying
to production:

- Test thoroughly in a non-production environment first
- Review IAM permissions and security configurations against your organization's policies
- Validate that behavior meets your operational requirements

See the LICENSE file for terms of use.

## Folder layout

```
nhi-devops-agent-evidence-pipeline/
├── template.yaml            CloudFormation stack (alarm, SNS, 4 Lambdas, evidence + access-log S3 buckets, DynamoDB, KMS key, ASG, SSM endpoints, IAM)
├── src/
│   ├── common.py            S3 helpers, DynamoDB atomic sample cap, incident anchor, maintenance window
│   ├── hook_installer.py    Branch 1 entry: install/remove lifecycle hook on alarm ALARM/OK
│   ├── evidence_collector.py  Hook target: collect-and-release, SSM Run Command, NFM metrics → S3
│   ├── investigation.py     Branch 2: DevOps Agent CreateBacklogTask → impact assessment
│   └── impact_assessment.py Escalation thresholds → Support CreateCase
└── scripts/
    ├── deploy.sh            package + deploy via aws CLI
    ├── trigger-demo.sh      drive NHI alarm to ALARM + force a termination
    ├── maintenance-window.sh open/close the suppression window
    └── teardown.sh          empty both buckets (all versions), drop hook, delete stack
```

## What's real vs mocked

| Piece | Behavior |
|---|---|
| CloudWatch NHI alarm (`HealthIndicator >= 100`) | **Real** alarm. Driven manually via `set-alarm-state` since real NFM can't be forced on demand. |
| SNS fan-out | **Real** — two KMS-encrypted topics (NHI alarm + lifecycle-hook target). |
| Hook installer / evidence collector / investigation / impact-assessment Lambdas | **Real** Python 3.12 functions. |
| S3 evidence bucket, access-log bucket, DynamoDB state table | **Real**. Evidence bucket is versioned (noncurrent versions expire after 30 days) and writes server access logs to a separate log bucket (which logs to itself); table has point-in-time recovery and KMS encryption. |
| Auto Scaling group + lifecycle hook + SSM Run Command | **Real** — a tiny SSM-managed ASG so hooks and `SendCommand` actually run (via SSM interface VPC endpoints). |
| Strict sample cap | **Real** DynamoDB atomic `UpdateItem` counter (not an S3 counter — see note below). |
| DevOps Agent investigation | **Real** when `MockExternal=false` — calls `CreateBacklogTask` against your Agent Space. Synthesized findings when `MockExternal=true` (default, for accounts without DevOps Agent). |
| AWS Support `CreateCase` | **Real** when `MockExternal=false` (requires Business/Enterprise Support). Writes the would-be case payload to S3 when `MockExternal=true`. |

> **One incident key across both branches.** The NHI alarm onset anchors the
> incident. The hook installer records it in DynamoDB (`ACTIVE#incident`) when
> the alarm fires; the evidence collector reads that key instead of deriving one
> from each instance's termination time. Otherwise per-host evidence and the
> investigation findings would scatter into different `incident-...` S3 prefixes.
>
> **Why DynamoDB for the sample cap.** Under a real event, many instances
> terminate at once and their Evidence Lambda invocations race on the counter —
> an S3 read/modify/write overshoots the cap. A DynamoDB conditional `UpdateItem`
> is serialized by the service, so exactly N slots are ever handed out
> (`common.try_claim_sample_slot`).

## Prerequisites

- AWS CLI v2 and credentials for a **sandbox** account (no SAM CLI needed).
  The scripts pass `--region` explicitly, but some commands below (such as
  `aws logs tail`) are v2-only. AWS CLI v1 ignores `AWS_REGION`; if you're on
  v1, also export `AWS_DEFAULT_REGION`.
- Permission to create a KMS customer managed key (the stack creates one; see
  [Security notes](#security-notes)).
- An existing S3 bucket to hold the packaged Lambda code (the `aws cloudformation
  package` artifact bucket). Any bucket you own works.
- A VPC with at least two subnets. The template adds SSM interface VPC endpoints,
  so the instances reach SSM privately (no NAT/IGW required), but the VPC must
  have `enableDnsHostnames` and `enableDnsSupport` turned on (required for the
  endpoints' private DNS). Pick subnets in AZs that support your
  `InstanceType`: for example, `t3.micro` isn't available in `us-east-1e`, and
  the ASG will keep failing launches there and pile instances into the other
  subnet, which also undercuts the multi-AZ part of the demo.
- Region: examples assume `us-east-1` (the Support API only lives there).
- For the **real** DevOps Agent path: an onboarded DevOps Agent **Agent Space**
  and its id, plus a Business/Enterprise Support plan for real Support cases.

---

## Running commands

All commands run from inside the `nhi-devops-agent-evidence-pipeline/` folder.
Set these once per shell:

```bash
cd nhi-devops-agent-evidence-pipeline
export AWS_REGION=us-east-1
export STACK_NAME=nhi-evidence-lab
export ARTIFACT_BUCKET=amzn-s3-demo-bucket            # replace with your artifact bucket
export VPC_ID=vpc-xxxxxxxx
export SUBNET_IDS=subnet-aaaa,subnet-bbbb             # comma-separated, NO spaces
```

### 0. Check credentials

```bash
aws sts get-caller-identity --region "$AWS_REGION"
```

### 1a. Deploy — mocked DevOps Agent + Support (default, no onboarding needed)

```bash
MOCK_EXTERNAL=true ./scripts/deploy.sh
```

### 1b. Deploy — REAL DevOps Agent + Support

```bash
MOCK_EXTERNAL=false \
AGENT_SPACE_ID=<your-agent-space-id> \
DEVOPS_AGENT_POLL_SECONDS=0 \
./scripts/deploy.sh
```

`deploy.sh` runs `aws cloudformation package` then `aws cloudformation deploy`.
Deploy parameters (env vars → CloudFormation params):

| Env var | CFN param | Default | Notes |
|---|---|---|---|
| `ARTIFACT_BUCKET` | — | (required) | S3 bucket for packaged Lambda code |
| `VPC_ID` | `VpcId` | (required) | VPC for the lab ASG |
| `SUBNET_IDS` | `SubnetIds` | (required) | 2+ subnets, comma-separated, no spaces |
| `SAMPLE_CAP` | `SampleCap` | `3` | max instances sampled per incident |
| `MOCK_EXTERNAL` | `MockExternal` | `true` | `false` = real DevOps Agent + Support |
| `AGENT_SPACE_ID` | `AgentSpaceId` | `""` | required when `MOCK_EXTERNAL=false` |
| `DEVOPS_AGENT_POLL_SECONDS` | `DevOpsAgentPollSeconds` | `0` | seconds to poll `GetBacklogTask` inline (0 = fire-and-forget) |

### 2. Wait for instances to register with SSM

```bash
aws ssm describe-instance-information --region "$AWS_REGION" \
  --query "InstanceInformationList[].{Id:InstanceId,Ping:PingStatus}" --output table
```

Wait until all 3 show `Online` before running the demo.

### 3. Fire the demo

```bash
./scripts/trigger-demo.sh
```

This pushes the NHI alarm to `ALARM` (simulated NHI=100), fanning out to both
branches, then terminates one ASG instance so the `EC2_INSTANCE_TERMINATING`
hook fires for real.

### 4. Inspect results

The stack outputs `EvidenceBucketName`, `AccessLogsBucketName`, `StateTableName`,
`AsgName`, `NhiAlarmName` and `NhiTopicArn`. The alarm name is generated by
CloudFormation (it isn't a fixed name), so always read it from the outputs; the
scripts do.

```bash
# Resolve the evidence bucket (survives the random suffix)
BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='EvidenceBucketName'].OutputValue" --output text)

# List all evidence, partitioned by incident
aws s3 ls "s3://$BUCKET/" --recursive

# Read a specific artifact (print to stdout)
aws s3 cp "s3://$BUCKET/<incident-id>/investigation/findings.json" -
aws s3 cp "s3://$BUCKET/<incident-id>/assessment/no-escalation.json" -

# Download a whole incident package
aws s3 cp "s3://$BUCKET/<incident-id>/" ./<incident-id>/ --recursive
```

Expected objects per incident:
- `incident-.../instances/<id>.json` — SSM network state + NFM metrics (first N instances only)
- `incident-.../investigation/findings.json` — DevOps Agent task/findings
- `incident-.../assessment/mock-support-case.json` — case that *would* be filed (mock), or
  `.../assessment/no-escalation.json` — thresholds not met

S3 server access logs for the evidence bucket go to the `AccessLogsBucketName`
bucket under `evidence-bucket/` (the log bucket's own logs go under `access-logs/`). S3 delivers them on a best-effort basis, often
minutes to hours late, so they won't show up right after a demo run.

### 5. Tail Lambda logs

```bash
# Investigation Lambda (shows the real CreateBacklogTask result / errors)
PHYS=$(aws cloudformation describe-stack-resource --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --logical-resource-id InvestigationFn --query 'StackResourceDetail.PhysicalResourceId' --output text)
aws logs tail "/aws/lambda/$PHYS" --follow --region "$AWS_REGION"
```

Swap `InvestigationFn` for `EvidenceCollectorFn`, `HookInstallerFn`, or
`ImpactAssessmentFn` to follow the other functions.

### 6. Inspect the lifecycle hook (optional)

The hook exists only while the NHI alarm is in `ALARM`. To see it:

```bash
ASG=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AsgName'].OutputValue" --output text)
ALARM=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='NhiAlarmName'].OutputValue" --output text)
aws cloudwatch set-alarm-state --alarm-name "$ALARM" --state-value ALARM \
  --state-reason "inspect hook" --region "$AWS_REGION"
aws autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" --region "$AWS_REGION" --output json
# return to steady state (also removes the hook):
aws cloudwatch set-alarm-state --alarm-name "$ALARM" --state-value OK \
  --state-reason "reset" --region "$AWS_REGION"
```

### 7. Maintenance window (escalation suppression)

```bash
./scripts/maintenance-window.sh open 30   # suppress for 30 min
./scripts/maintenance-window.sh close     # end early
```

With a window open, NHI alarms still fire and are recorded, but the pipeline
skips hook installation and Support-case creation. The DynamoDB item carries a
TTL so a forgotten window self-expires.

### 8. Teardown

```bash
./scripts/teardown.sh
```

Turns off access logging on the evidence bucket, empties both buckets (every
object version and delete marker, since they are versioned), removes any
lingering lifecycle hook, and deletes the stack. S3 can deliver an access log
after the buckets are emptied, which makes the stack delete fail on a non-empty
bucket, so the script re-empties both buckets and retries the delete up to 3 times.
The uploaded Lambda zip stays in your artifact bucket (not a stack resource);
delete it separately for a complete cleanup. `deploy.sh` writes the packaged
template to a temp directory (it embeds your artifact bucket name), so no
`packaged.yaml` is left in this folder.

Deleting the stack schedules the KMS key for deletion with a 7-day waiting
period (`PendingWindowInDays: 7`), so it shows as `Pending deletion` in KMS for
a week. That's expected. Cancel the deletion within that window if you need to
read anything still encrypted with it.

Until you tear down, the stack keeps running 3 instances and 3 interface VPC
endpoints, both billed hourly.

If you redeploy over an existing stack from an older version of this template,
the NHI alarm is replaced and gets a new generated name (it was previously fixed
as `nhi-degraded`). Update anything outside this repo that referenced the old name.

## Security notes

The template is scanned with Checkov, cfn_nag and the cfn-guard S3 rules. It has
no cfn_nag warnings, and the only Checkov failure left is `CKV_AWS_116` (below).
Controls:

- One customer managed KMS key (`PipelineKey`, rotation on) encrypts both SNS
  topics, the DynamoDB table, and the Lambda environment variables. It's a CMK
  rather than the AWS managed `aws/sns` key because CloudWatch alarms can only
  publish to an encrypted topic if the key policy grants
  `cloudwatch.amazonaws.com`. Each role gets only the KMS actions it needs.
- IAM is scoped to the lab resources: lifecycle-hook actions to the lab ASG's
  ARN, and `ssm:SendCommand` to the `AWS-RunShellScript` document on instances
  tagged `aws:autoscaling:groupName=<lab ASG>`.
- Instances require IMDSv2, and every security group rule has a description.
- Network egress is least-privilege. Instances can only reach the SSM endpoints
  over TCP 443, and the endpoint security group allows no outbound traffic. A
  match-nothing placeholder rule removes the default allow-all egress. Instances
  need no other egress: the diagnostics only run local commands.
- The `aidevops:CreateBacklogTask` / `GetBacklogTask` permissions apply only to
  your Agent Space ARN, and only when `AgentSpaceId` is set. In mock mode the
  role has no `aidevops:` permissions.
- The evidence bucket writes server access logs to a dedicated, versioned log
  bucket (logs expire after 30 days). The log bucket logs to itself, so both
  buckets have access logging on. Both bucket policies deny non-TLS requests.
  The log bucket has a fixed name (`<stack>-access-logs-<account id>`), because a
  bucket can't reference its own generated name. Keep the stack name lowercase
  and under 40 characters.
- The remaining `*` resources are on actions with no resource-level permissions
  (AWS Support, `ssm:GetCommandInvocation`, `cloudwatch:GetMetricStatistics`),
  each with a cfn_nag suppression explaining why.

These checks are intentionally suppressed, with the reason recorded in the
template's `Metadata` (`checkov.skip`, plus the matching `cfn_nag` and `guard` keys):

| Check | Why it's skipped |
|---|---|
| `CKV_AWS_117` Lambda in a VPC | The functions only call AWS APIs. The lab VPC has no NAT, so VPC attachment would need about 7 more interface endpoints. |
| `CKV_AWS_115` reserved concurrency | New and lab accounts often have a concurrency quota of 10, where any reservation fails the deploy. The sample cap is enforced in DynamoDB instead. |

`CKV_AWS_116` (Lambda dead-letter queue) is still reported on all four functions
and not yet addressed.

Tested in a sandbox account with `MockExternal=true`: the stack deployed, the demo
produced the `instances/`, `investigation/` and `assessment/` objects under one
incident prefix, the Lambda logs had no errors, and teardown completed. Access-log
delivery to the log bucket had not been observed yet when the test ended.

The two remaining `time.sleep()` calls (`common.wait_for_evidence` and the
optional DevOps Agent poll) are bounded polls with no AWS waiter equivalent and
carry `# nosemgrep: arbitrary-sleep`. The SSM poll in the evidence collector
uses boto3's `command_executed` waiter.

---

## Prove the sample cap holds

Raise `DesiredCapacity` (e.g. 6), fire the demo, and terminate several instances
at once. You'll see exactly `SampleCap` `instances/*.json` objects; all other
terminations log `released-over-cap` and complete in under a second.

## The DevOps Agent integration (real path)

When `MockExternal=false`, `investigation.py` calls the real `devops-agent`
boto3 client:

- `CreateBacklogTask` (`taskType=INVESTIGATION`) starts an autonomous
  investigation in your Agent Space and returns a `taskId` + `executionId`.
- The investigation is **asynchronous** — findings are not available on the call.
  By default the Lambda records the task as `PENDING_START` and returns; set
  `DEVOPS_AGENT_POLL_SECONDS > 0` to poll `GetBacklogTask` inline for a budget.

Two non-obvious details confirmed against the live service (both baked into the
code/template):
- The IAM action namespace is **`aidevops`** (`aidevops:CreateBacklogTask` /
  `aidevops:GetBacklogTask`), not `devops-agent`.
- The optional `reference` object's `system` field must be a service-known
  value, so it is omitted here.

> **Open item — async poller.** Because the investigation is async, the findings
> arrive as `PENDING_START` with empty escalation fields, so impact assessment
> currently sees no thresholds met and writes `no-escalation`. To let real
> findings drive escalation, add a poller (EventBridge-scheduled or Step
> Functions) that calls `GetBacklogTask` until completion, maps the agent's
> `metadata`/`supportMetadata` into `affected_azs` / `fleet_degraded_pct` /
> `correlated_health_event`, then triggers impact assessment. Not yet built.

## Going live (beyond the sandbox)

- `MockExternal=false` + a Business/Enterprise Support plan → real `CreateCase`.
  Wire `AddAttachmentsToSet` / `AddCommunicationToCase` to attach the S3 evidence
  (stubbed in `impact_assessment.py`).
- Build the async poller described above so investigation findings populate the
  escalation decision.
- Point the NHI alarm at a real NFM monitor scope and remove the manual
  `set-alarm-state` trigger.
