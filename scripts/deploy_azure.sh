#!/usr/bin/env bash
# Build in Azure and deploy to Container Apps. Reads secrets from .env; nothing secret goes into the image.
# Usage: scripts/deploy_azure.sh            (first deploy and every redeploy)
set -euo pipefail
cd "$(dirname "$0")/.."

RG="${RG:-rg-bookingagent}"
LOC="${LOC:-francecentral}"
APP="${APP:-bookingagent}"
ENVNAME="${ENVNAME:-bookingagent-env}"

[[ -f .env ]] || { echo "Missing .env (copy .env.example)"; exit 1; }
set -a; source .env; set +a
: "${BA_DATABASE_URL:?set BA_DATABASE_URL in .env}"
: "${BA_ACCESS_CODE:?set BA_ACCESS_CODE in .env (the public URL needs it)}"
LLM_KEY="${OPENROUTER_API_KEY:-${OPENAI_API_KEY:-}}"
: "${LLM_KEY:?set OPENAI_API_KEY in .env}"

# containerapp commands are in the core CLI; the extension is not needed.
az provider register -n Microsoft.App --wait >/dev/null
az provider register -n Microsoft.OperationalInsights --wait >/dev/null
az group create -n "$RG" -l "$LOC" -o none

# Builds the Dockerfile in Azure Container Registry (created on first run) and deploys it.
az containerapp up -n "$APP" -g "$RG" -l "$LOC" --environment "$ENVNAME" --source . \
  --ingress external --target-port 8000

SECRETS=(db-url="$BA_DATABASE_URL" access-code="$BA_ACCESS_CODE")
ENVVARS=(BA_DATABASE_URL=secretref:db-url BA_ACCESS_CODE=secretref:access-code
         BA_LLM_BASE_URL="${BA_LLM_BASE_URL:-https://api.openai.com/v1}" BA_LLM_MODEL="${BA_LLM_MODEL:-gpt-6-luna}"
         BA_LLM_PRICE_IN_PER_M="${BA_LLM_PRICE_IN_PER_M:-0.10}" BA_LLM_PRICE_OUT_PER_M="${BA_LLM_PRICE_OUT_PER_M:-0.50}"
         BA_LLM_REASONING_EFFORT="${BA_LLM_REASONING_EFFORT:-none}" BA_LOG_LEVEL="${BA_LOG_LEVEL:-INFO}")
if [[ -n "$LLM_KEY" ]]; then SECRETS+=(llm-key="$LLM_KEY"); ENVVARS+=(BA_LLM_API_KEY=secretref:llm-key); fi
if [[ -n "${BA_ADMIN_CODE:-}" ]]; then SECRETS+=(admin-code="$BA_ADMIN_CODE"); ENVVARS+=(BA_ADMIN_CODE=secretref:admin-code); fi

az containerapp secret set -n "$APP" -g "$RG" --secrets "${SECRETS[@]}" -o none
# One replica: session and user locks are in-process (see docs/design-note.md, "What breaks first").
az containerapp update -n "$APP" -g "$RG" --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --set-env-vars "${ENVVARS[@]}" -o none

URL="https://$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
echo "Deployed: $URL"
curl -fsS "$URL/healthz" && echo
