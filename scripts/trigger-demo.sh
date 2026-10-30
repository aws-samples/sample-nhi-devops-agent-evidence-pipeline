#!/usr/bin/env bash
# Fire the NHI pipeline in the lab.
#
# Real NFM HealthIndicator cannot be forced to 100 on demand, so we drive the
# demo by pushing the CloudWatch alarm straight into ALARM. This fans out to the
# hook installer + investigation Lambdas exactly as a real NHI-degraded event
# would. CloudWatch will later reset the alarm to its real state on its own.
set -euo pipefail

STACK_NAME="${STACK_NAME:-nhi-evidence-lab}"
REGION="${AWS_REGION:-us-east-1}"

ALARM_NAME="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='NhiAlarmName'].OutputValue" --output text)"
ASG_NAME="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AsgName'].OutputValue" --output text)"

echo ">> Driving alarm '$ALARM_NAME' to ALARM (simulated NHI=100)"
aws cloudwatch set-alarm-state \
  --alarm-name "$ALARM_NAME" \
  --state-value ALARM \
  --state-reason "Lab demo: simulated NHI degraded" \
  --region "$REGION"

echo ">> Branch 1 (hook installer) + Branch 2 (investigation) now triggered."
echo ">> Forcing instance churn so the lifecycle hook fires..."
# Terminate-and-replace one instance so EC2_INSTANCE_TERMINATING fires for real.
INSTANCE_ID="$(aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "$ASG_NAME" --region "$REGION" \
  --query "AutoScalingGroups[0].Instances[0].InstanceId" --output text)"

if [[ "$INSTANCE_ID" != "None" && -n "$INSTANCE_ID" ]]; then
  echo ">> Terminating $INSTANCE_ID (ASG will replace it; hook intercepts the termination)"
  aws autoscaling terminate-instance-in-auto-scaling-group \
    --instance-id "$INSTANCE_ID" \
    --no-should-decrement-desired-capacity \
    --region "$REGION" >/dev/null
else
  echo "!! No running instances in $ASG_NAME yet - wait for DesiredCapacity to launch, then re-run."
fi

echo ">> Done. Watch Lambda logs and the S3 evidence bucket for results."
