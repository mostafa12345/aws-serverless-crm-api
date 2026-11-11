#!/usr/bin/env bash
# ==============================================================================
# Full AWS Serverless Deployment Script (Terraform + S3 SPA Sync + CloudFront)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="$PROJECT_DIR/terraform"
FRONTEND_DIR="$PROJECT_DIR/frontend"

echo "============================================================"
echo " Deploying Serverless CRM Infrastructure to AWS"
echo "============================================================"

cd "$TF_DIR"
terraform init
terraform validate
terraform apply -auto-approve

S3_BUCKET=$(terraform output -raw s3_frontend_bucket)
CF_URL=$(terraform output -raw cloudfront_url)
HOSTED_UI=$(terraform output -raw cognito_hosted_ui_url)

# Generate production config.json for the static SPA
TMP_FRONTEND=$(mktemp -d)
trap 'rm -rf "$TMP_FRONTEND"' EXIT
cp -r "$FRONTEND_DIR/"* "$TMP_FRONTEND/"

cat > "$TMP_FRONTEND/config.json" <<EOF
{
  "mode": "cognito",
  "apiBase": "$CF_URL",
  "hostedUiUrl": "$HOSTED_UI",
  "region": "us-east-1",
  "wafEnabled": true
}
EOF

echo "Uploading static frontend assets to s3://$S3_BUCKET ..."
aws s3 sync "$TMP_FRONTEND" "s3://$S3_BUCKET" --delete

echo "Deployment Complete!"
echo "Live CloudFront URL: $CF_URL"
echo "Cognito Hosted UI:   $HOSTED_UI"
