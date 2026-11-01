#!/usr/bin/env bash
# Burn CPU on every instance in the Auto Scaling group via SSM Run Command (no SSH needed),
# so you can watch the CpuHighAlarm fire, step scaling add instances, and target tracking
# scale back in afterwards.
#
#   scripts/stress.sh [minutes]      (default 10)
set -euo pipefail

STACK="${STACK_NAME:-webapp}"
REGION="${AWS_REGION:-us-east-1}"
MINUTES="${1:-10}"
[[ "$MINUTES" =~ ^[0-9]+$ ]] || { echo "minutes must be a whole number" >&2; exit 1; }

ASG=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AutoScalingGroupName'].OutputValue" --output text)

CMD="for i in \$(seq \$(nproc)); do timeout ${MINUTES}m yes > /dev/null & done; wait"
aws ssm send-command --region "$REGION" \
  --document-name AWS-RunShellScript \
  --comment "CPU stress for ${MINUTES} minutes" \
  --targets "Key=tag:aws:autoscaling:groupName,Values=$ASG" \
  --parameters "$(printf '{"commands":["%s"]}' "$CMD")" \
  --query 'Command.CommandId' --output text

echo "Watch: Auto Scaling activity, the '$STACK' CloudWatch dashboard, and your alarm email."
