#!/usr/bin/env bash
# =============================================================================
# Register the Entra app used as the OAuth client for the Work IQ MCP
# connection in Foundry ("Connect the Work IQ MCP tool" dialog).
#
# It creates an app registration, adds the WorkIQAgent.Ask delegated permission,
# creates a client secret and grants admin consent. At the end it prints every
# value you need to paste into the Foundry connection dialog.
#
# Requirements:
#   - az login  (with rights to register apps AND grant admin consent)
#   - This script only uses the Azure CLI (no jq needed).
#
# Usage:
#   bash setup/register_workiq_app.sh
#
# Optional overrides (environment variables):
#   APP_NAME               Display name of the app (default: WorkIQMCP-Client)
#   REDIRECT_URI           Web redirect URI for the OAuth callback (see notes)
#   WORKIQ_RESOURCE_APP_ID Work IQ API app id (default from the Foundry dialog)
#   WORKIQ_SCOPE_NAME      Delegated permission (default: WorkIQAgent.Ask)
#
# Notes on the redirect URI:
#   The Foundry "Connect" button runs an OAuth authorization-code flow, so the
#   app needs the redirect URI that Foundry uses registered on it. If you don't
#   know it yet, run the script without REDIRECT_URI. If the connect step later
#   fails with "AADSTS50011: redirect URI mismatch", copy the redirect_uri shown
#   in the error and add it with:
#       az ad app update --id <CLIENT_ID> --web-redirect-uris <URI>
# =============================================================================

set -euo pipefail

APP_NAME="${APP_NAME:-WorkIQMCP-Client}"
WORKIQ_RESOURCE_APP_ID="${WORKIQ_RESOURCE_APP_ID:-fdcc1f02-fc51-4226-8753-f668596af7f7}"
WORKIQ_SCOPE_NAME="${WORKIQ_SCOPE_NAME:-WorkIQAgent.Ask}"
MCP_ENDPOINT="https://workiq.svc.cloud.microsoft/mcp"
REDIRECT_URI="${REDIRECT_URI:-}"

TENANT_ID="$(az account show --query tenantId -o tsv)"
echo "Tenant: $TENANT_ID"

# ---------------------------------------------------------------------------
# 1. Resolve the WorkIQAgent.Ask permission id. This needs the Work IQ
#    enterprise app (service principal) to exist in your tenant; if it does
#    not, we try to provision it. If it still cannot be resolved, we create the
#    client app anyway and rely on interactive consent at Connect time.
# ---------------------------------------------------------------------------
resolve_scope_id() {
  az ad sp show --id "$WORKIQ_RESOURCE_APP_ID" \
    --query "oauth2PermissionScopes[?value=='$WORKIQ_SCOPE_NAME'].id | [0]" \
    -o tsv 2>/dev/null || true
}

echo "Looking up the '$WORKIQ_SCOPE_NAME' delegated permission..."
SCOPE_ID="$(resolve_scope_id)"

if [[ -z "$SCOPE_ID" || "$SCOPE_ID" == "None" ]]; then
  echo "Work IQ enterprise app not found in this tenant. Trying to provision it..."
  az ad sp create --id "$WORKIQ_RESOURCE_APP_ID" >/dev/null 2>&1 || true
  SCOPE_ID="$(resolve_scope_id)"
fi

DECLARE_PERMISSION=true
if [[ -z "$SCOPE_ID" || "$SCOPE_ID" == "None" ]]; then
  DECLARE_PERMISSION=false
  echo "WARNING: could not resolve the Work IQ scope in this tenant."
  echo "The client app will be created WITHOUT a pre-declared permission; you will"
  echo "consent to '$WORKIQ_SCOPE_NAME' interactively when you click Connect in Foundry."
else
  echo "Scope id: $SCOPE_ID"
fi

# ---------------------------------------------------------------------------
# 2. Create the client app registration (with the permission if we resolved it).
# ---------------------------------------------------------------------------
echo "Creating app registration '$APP_NAME'..."
if [[ "$DECLARE_PERMISSION" == "true" ]]; then
  CLIENT_ID="$(az ad app create \
    --display-name "$APP_NAME" \
    --required-resource-accesses \
    "[{\"resourceAppId\":\"$WORKIQ_RESOURCE_APP_ID\",\"resourceAccess\":[{\"id\":\"$SCOPE_ID\",\"type\":\"Scope\"}]}]" \
    --query appId -o tsv)"
else
  CLIENT_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
fi
echo "Client ID (appId): $CLIENT_ID"

# A service principal is required before consent can be granted.
az ad sp create --id "$CLIENT_ID" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 3. Optional redirect URI for the OAuth authorization-code flow.
# ---------------------------------------------------------------------------
if [[ -n "$REDIRECT_URI" ]]; then
  echo "Adding web redirect URI: $REDIRECT_URI"
  az ad app update --id "$CLIENT_ID" --web-redirect-uris "$REDIRECT_URI"
fi

# ---------------------------------------------------------------------------
# 4. Create a client secret (valid 1 year).
# ---------------------------------------------------------------------------
echo "Creating client secret..."
CLIENT_SECRET="$(az ad app credential reset --id "$CLIENT_ID" --append \
  --display-name "foundry-workiq" --years 1 --query password -o tsv)"

# ---------------------------------------------------------------------------
# 5. Grant admin consent (only if we pre-declared the permission).
# ---------------------------------------------------------------------------
if [[ "$DECLARE_PERMISSION" == "true" ]]; then
  echo "Granting admin consent..."
  if ! az ad app permission admin-consent --id "$CLIENT_ID"; then
    echo "WARNING: admin-consent failed (often a replication delay). Retry in ~1 min:"
    echo "  az ad app permission admin-consent --id $CLIENT_ID"
  fi
else
  echo "Skipping admin consent; you will consent interactively at Connect time."
fi

# ---------------------------------------------------------------------------
# 6. Print everything for the Foundry connection dialog.
# ---------------------------------------------------------------------------
cat <<EOF

=============================================================
Paste these into the "Connect the Work IQ MCP tool" dialog:
-------------------------------------------------------------
Name                       : WorkIQMCP
Remote MCP Server endpoint : $MCP_ENDPOINT
Client ID                  : $CLIENT_ID
Client secret              : $CLIENT_SECRET
Auth URL                   : https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/authorize
Token URL                  : https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token
Refresh URL                : https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token
Scopes                     : offline_access $WORKIQ_RESOURCE_APP_ID/$WORKIQ_SCOPE_NAME
-------------------------------------------------------------
Then set in your .env:
  WORKIQ_MCP_CONNECTION_NAME=WorkIQMCP
  WORKIQ_MCP_SERVER_URL=$MCP_ENDPOINT
=============================================================
IMPORTANT: copy the client secret now, it will not be shown again.
The secret goes ONLY into the Foundry dialog, never into .env or git.
EOF
