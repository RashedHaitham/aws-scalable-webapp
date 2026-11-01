#!/usr/bin/env bash
# Create or update the stack, then publish ./static to the CloudFront origin bucket.
#
#   scripts/deploy.sh you@example.com                                  # HTTP on the ALB DNS name
#   scripts/deploy.sh you@example.com DomainName=app.example.com HostedZoneId=Z123...  # HTTPS + Route 53
#   STACK_NAME=webapp-dev AWS_REGION=eu-west-1 scripts/deploy.sh you@example.com NatGatewayMode=Single
#
# Takes ~25-35 minutes on first create (RDS Multi-AZ is the slow part).
set -euo pipefail
cd "$(dirname "$0")/.."

STACK="${STACK_NAME:-webapp}"
REGION="${AWS_REGION:-us-east-1}"
EMAIL="${1:?usage: scripts/deploy.sh <alert-email> [ParameterKey=Value ...]}"
shift

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file cloudformation/webapp.yaml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "AlertEmail=$EMAIL" "$@"

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

aws s3 sync static "s3://$(output StaticBucketName)/static" --region "$REGION"
# CloudFront caches assets for up to 24h; refresh them after every upload.
aws cloudfront create-invalidation --distribution-id "$(output CloudFrontDistributionId)" \
  --paths '/static/*' --query 'Invalidation.Id' --output text

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
echo "Confirm the SNS subscription email sent to $EMAIL to receive alarms."
