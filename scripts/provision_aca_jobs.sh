#!/usr/bin/env bash
# scripts/provision_aca_jobs.sh
#
# Deploys all 18 Container App Jobs via infra/aca_jobs.bicep.
# Run once (or re-run to update image tag / env vars).
#
# Usage:
#   ./scripts/provision_aca_jobs.sh
#   ./scripts/provision_aca_jobs.sh --image-tag 0.2.2
#
# Prerequisites:
#   az login
#   az account set --subscription 8aee075f-c478-4da6-872c-ebcfef7a11c6

set -euo pipefail

RG="galaxyscanner-rg"
TEMPLATE="infra/aca_jobs.bicep"
IMAGE_TAG="${IMAGE_TAG:-0.2.1}"

# Parse --image-tag flag
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "==> Fetching secrets from Key Vault and ACR..."
ACR_PASSWORD=$(az acr credential show \
  --name galaxyscannercrd63cdd \
  --query "passwords[0].value" -o tsv)

STORAGE_KEY=$(az storage account keys list \
  --resource-group "$RG" \
  --account-name galaxyscannersa \
  --query "[0].value" -o tsv)

echo "==> Deploying infra/aca_jobs.bicep (image tag: ${IMAGE_TAG})..."
az deployment group create \
  --resource-group "$RG" \
  --template-file "$TEMPLATE" \
  --parameters \
    acrPassword="$ACR_PASSWORD" \
    storageAccountKey="$STORAGE_KEY" \
    imageTag="$IMAGE_TAG" \
  --verbose

echo ""
echo "==> Done. Verify with:"
echo "    az containerapp job list --resource-group $RG --query '[].name' -o tsv"
