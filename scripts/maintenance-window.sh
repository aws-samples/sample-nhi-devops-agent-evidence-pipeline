#!/usr/bin/env bash
# Open or close the maintenance window that suppresses escalation.
#
# Usage:
#   ./maintenance-window.sh open 30     # suppress for 30 minutes
#   ./maintenance-window.sh close
set -euo pipefail

STACK_NAME="${STACK_NAME:-nhi-evidence-lab}"
REGION="${AWS_REGION:-us-east-1}"
ACTION="${1:-}"
MINUTES="${2:-30}"

TABLE="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='StateTableName'].OutputValue" --output text)"

case "$ACTION" in
  open)
    NOW="$(date +%s)"
    TTL=$(( NOW + MINUTES * 60 ))
    aws dynamodb put-item --table-name "$TABLE" --region "$REGION" \
      --item "{\"pk\":{\"S\":\"MAINT#window\"},\"ttl\":{\"N\":\"$TTL\"}}"
    echo "Maintenance window open for $MINUTES min (auto-expires via DynamoDB TTL at epoch $TTL)."
    ;;
  close)
    aws dynamodb delete-item --table-name "$TABLE" --region "$REGION" \
      --key '{"pk":{"S":"MAINT#window"}}'
    echo "Maintenance window closed."
    ;;
  *)
    echo "Usage: $0 {open [minutes]|close}" >&2
    exit 1
    ;;
esac
