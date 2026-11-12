#!/usr/bin/env bash
# ==============================================================================
# Full AWS Serverless Deployment Script (Terraform + S3 SPA Sync + CloudFront)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="$PROJECT_DIR/terraform"

echo "============================================================"
echo " Deploying Serverless CRM Infrastructure to AWS"
echo "============================================================"

cd "$TF_DIR"
terraform init
terraform validate
terraform apply -auto-approve

S3_BUCKET=$(terraform output -raw s3_frontend_bucket)
CF_URL=$(terraform output -raw cloudfront_url)
CF_DIST_ID=$(terraform output -raw cloudfront_distribution_id)
HOSTED_UI=$(terraform output -raw cognito_hosted_ui_url)

echo "Uploading static frontend assets to s3://$S3_BUCKET ..."
python3 "$SCRIPT_DIR/upload_frontend.py" "$S3_BUCKET" "$CF_URL" "$HOSTED_UI" "$CF_DIST_ID"

echo "Deployment Complete!"
echo "Live CloudFront URL: $CF_URL"
echo "Cognito Hosted UI:   $HOSTED_UI"
