#!/usr/bin/env bash
# Shared environment for every script in this folder.
#
# Usage (from the repo root):
#   source salesforce/scripts/env.sh
#
# The Salesforce session token expires. If calls start returning 401
# INVALID_SESSION_ID, source this file again.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "env.sh must be sourced, not executed: source salesforce/scripts/env.sh" >&2
  exit 1
fi

export SF_ORG="${SF_ORG:-relay-dev}"            # your sf CLI org alias

# One API version for every Salesforce call in this repo.
# v68.0 = Winter '27. Bump to your org's latest:
#   curl -s "$SF_URL/services/data/" | jq -r '.[-1].version'
export SF_API_VERSION="${SF_API_VERSION:-v68.0}"

export AWS_REGION="${AWS_REGION:-us-east-1}"
export STACK_NAME="${STACK_NAME:-sf-member-relay}"
AWS_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export AWS_ACCOUNT

# Pull a session token and instance URL from the CLI for raw REST and Tooling API calls.
_sf_org_json="$(sf org display --target-org "$SF_ORG" --json)"
SF_TOKEN="$(jq -r .result.accessToken <<<"$_sf_org_json")"
SF_URL="$(jq -r .result.instanceUrl <<<"$_sf_org_json")"
export SF_TOKEN SF_URL
unset _sf_org_json

# POST JSON to a Salesforce REST path, pretty-print the response.
sfpost() {
  curl -sS -X POST "$SF_URL$1" -H "Authorization: Bearer $SF_TOKEN" \
    -H "Content-Type: application/json" -d "$2" | jq
}

# PATCH JSON to a Salesforce REST path, print only the HTTP status code.
sfpatch() {
  curl -sS -o /dev/null -X PATCH "$SF_URL$1" -H "Authorization: Bearer $SF_TOKEN" \
    -H "Content-Type: application/json" -d "$2" -w "%{http_code}\n"
}

# DELETE a Salesforce REST path, print only the HTTP status code.
sfdelete() {
  curl -sS -o /dev/null -X DELETE "$SF_URL$1" -H "Authorization: Bearer $SF_TOKEN" \
    -w "%{http_code}\n"
}

# Run a Tooling API SOQL query, print the JSON response.
sftooling_query() {
  curl -sS -G "$SF_URL/services/data/$SF_API_VERSION/tooling/query/" \
    -H "Authorization: Bearer $SF_TOKEN" --data-urlencode "q=$1"
}
