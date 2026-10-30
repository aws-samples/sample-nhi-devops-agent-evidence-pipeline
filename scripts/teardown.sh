#!/usr/bin/env bash
# Tear down the lab stack cleanly.
#
# Order matters: stop access logging into the log bucket, empty both S3 buckets
# (CloudFormation can't delete a non-empty bucket), remove any lifecycle hook
# still attached to the ASG, then delete the stack. S3 delivers access logs
# late, so if a straggler log lands and blocks the delete, empty again and retry.
set -euo pipefail

STACK_NAME="${STACK_NAME:-nhi-evidence-lab}"
REGION="${AWS_REGION:-us-east-1}"

echo ">> Resolving stack outputs..."
BUCKET="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='EvidenceBucketName'].OutputValue" --output text 2>/dev/null || echo "")"
ASG_NAME="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AsgName'].OutputValue" --output text 2>/dev/null || echo "")"

LOGS_BUCKET="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AccessLogsBucketName'].OutputValue" --output text 2>/dev/null || echo "")"

# Both buckets are versioned, so `aws s3 rm` alone only adds delete markers.
# Delete every object version and delete marker (in batches of up to 1000).
empty_bucket() {
  local bucket="$1"
  [[ -z "$bucket" || "$bucket" == "None" ]] && return 0
  echo ">> Emptying s3://$bucket (all versions)"
  while true; do
    DELETE_JSON="$(aws s3api list-object-versions --bucket "$bucket" --region "$REGION" \
      --max-items 1000 --output json 2>/dev/null | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
resp = json.loads(raw) if raw else {}
objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
        for o in (resp.get("Versions") or []) + (resp.get("DeleteMarkers") or [])]
print(json.dumps({"Objects": objs[:1000], "Quiet": True}) if objs else "")
')"
    [[ -z "$DELETE_JSON" ]] && break
    aws s3api delete-objects --bucket "$bucket" --region "$REGION" \
      --delete "$DELETE_JSON" >/dev/null
  done
}

if [[ -n "$BUCKET" && "$BUCKET" != "None" ]]; then
  echo ">> Disabling access logging on s3://$BUCKET"
  aws s3api put-bucket-logging --bucket "$BUCKET" --region "$REGION" \
    --bucket-logging-status '{}' 2>/dev/null || true
fi
if [[ -n "$LOGS_BUCKET" && "$LOGS_BUCKET" != "None" ]]; then
  # The log bucket logs to itself; stop that so emptying it doesn't race new logs.
  echo ">> Disabling access logging on s3://$LOGS_BUCKET"
  aws s3api put-bucket-logging --bucket "$LOGS_BUCKET" --region "$REGION" \
    --bucket-logging-status '{}' 2>/dev/null || true
fi
empty_bucket "$BUCKET"
empty_bucket "$LOGS_BUCKET"

if [[ -n "$ASG_NAME" && "$ASG_NAME" != "None" ]]; then
  echo ">> Removing any lingering lifecycle hook from $ASG_NAME"
  aws autoscaling delete-lifecycle-hook \
    --lifecycle-hook-name nhi-evidence-hook \
    --auto-scaling-group-name "$ASG_NAME" --region "$REGION" 2>/dev/null || true
fi

for attempt in 1 2 3; do
  echo ">> Deleting stack $STACK_NAME (attempt $attempt)"
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  if aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"; then
    echo ">> Teardown complete."
    exit 0
  fi
  # Most likely a late access-log delivery refilled the log bucket.
  echo "!! Stack delete did not complete; re-emptying buckets and retrying in 60s"
  sleep 60
  empty_bucket "$BUCKET"
  empty_bucket "$LOGS_BUCKET"
done
echo "!! Stack delete still failing after 3 attempts. Check the stack events." >&2
exit 1
