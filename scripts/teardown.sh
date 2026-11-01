#!/usr/bin/env bash
# Delete the stack and everything it created.
# Left behind on purpose: the final RDS snapshot (DeletionPolicy: Snapshot) and the RDS log
# groups under /aws/rds/instance/. Delete those by hand if you no longer need them.
set -euo pipefail

STACK="${STACK_NAME:-webapp}"
REGION="${AWS_REGION:-us-east-1}"

read -r -p "Type the stack name ($STACK) to delete it in $REGION: " answer
[ "$answer" = "$STACK" ] || { echo "Aborted."; exit 1; }

BUCKET=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='StaticBucketName'].OutputValue" --output text)
DB_ID=$(aws cloudformation describe-stack-resource --region "$REGION" --stack-name "$STACK" \
  --logical-resource-id Database --query 'StackResourceDetail.PhysicalResourceId' --output text)
# CloudFormation cannot delete a non-empty bucket.
aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
echo "Waiting for deletion (10-20 minutes)..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"

echo "Stack deleted. Snapshots left for DB instance $DB_ID:"
aws rds describe-db-snapshots --region "$REGION" --db-instance-identifier "$DB_ID" \
  --query 'DBSnapshots[].[DBSnapshotIdentifier,SnapshotType,SnapshotCreateTime]' --output table
