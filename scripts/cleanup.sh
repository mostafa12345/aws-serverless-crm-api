#!/usr/bin/env bash
# ==============================================================================
# Resource Teardown Script for Serverless CRM Infrastructure
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"

cd "$TF_DIR"
if [ -d ".terraform" ]; then
  read -p "Are you sure you want to DESTROY all AWS resources via Terraform? (y/N): " -r CONFIRM
  if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Destroying AWS Serverless resources..."
    terraform destroy -auto-approve
    echo "Teardown complete."
  else
    echo "Teardown cancelled."
  fi
else
  echo "Terraform has not been initialized in $TF_DIR."
fi
