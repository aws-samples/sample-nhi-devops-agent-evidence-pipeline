#!/usr/bin/env bash
# Package + deploy the lab stack with the plain aws CLI (no SAM required).
#
# `aws cloudformation package` zips the local src/ directory, uploads it to an
# artifact S3 bucket, and rewrites the template's Code property to the uploaded
# object. `aws cloudformation deploy` then creates/updates the stack via a
# change set.
#
# Usage:
#   ARTIFACT_BUCKET=amzn-s3-demo-bucket \
#   VPC_ID=vpc-xxxx SUBNET_IDS=subnet-aaaa,subnet-bbbb \
#   ./scripts/deploy.sh
set -euo pipefail

STACK_NAME="${STACK_NAME:-nhi-evidence-lab}"
REGION="${AWS_REGION:-us-east-1}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:?Set ARTIFACT_BUCKET to an existing S3 bucket for Lambda code uploads}"
VPC_ID="${VPC_ID:?Set VPC_ID (e.g. vpc-0abc...)}"
SUBNET_IDS="${SUBNET_IDS:?Set SUBNET_IDS (comma-separated, e.g. subnet-a,subnet-b)}"
SAMPLE_CAP="${SAMPLE_CAP:-3}"
MOCK_EXTERNAL="${MOCK_EXTERNAL:-true}"
AGENT_SPACE_ID="${AGENT_SPACE_ID:-}"
DEVOPS_AGENT_POLL_SECONDS="${DEVOPS_AGENT_POLL_SECONDS:-0}"

# When the real DevOps Agent path is enabled, an Agent Space id is mandatory.
if [ "$MOCK_EXTERNAL" = "false" ] && [ -z "$AGENT_SPACE_ID" ]; then
  echo "ERROR: MOCK_EXTERNAL=false requires AGENT_SPACE_ID to be set." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# Write the packaged template to a temp dir, not the project folder. It embeds
# your artifact bucket name and should never end up in a zip or a scan of this repo.
PACKAGED_DIR="$(mktemp -d)"
trap 'rm -rf "$PACKAGED_DIR"' EXIT
PACKAGED="$PACKAGED_DIR/packaged.yaml"

echo ">> Packaging Lambda code from src/ to s3://$ARTIFACT_BUCKET"
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --output-template-file "$PACKAGED" \
  --region "$REGION"

echo ">> Deploying stack $STACK_NAME"
aws cloudformation deploy \
  --template-file "$PACKAGED" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --parameter-overrides \
    "VpcId=$VPC_ID" \
    "SubnetIds=$SUBNET_IDS" \
    "SampleCap=$SAMPLE_CAP" \
    "MockExternal=$MOCK_EXTERNAL" \
    "AgentSpaceId=$AGENT_SPACE_ID" \
    "DevOpsAgentPollSeconds=$DEVOPS_AGENT_POLL_SECONDS"

echo ">> Stack outputs:"
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs" --output table
