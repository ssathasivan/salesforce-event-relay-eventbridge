#!/usr/bin/env bash
# Test an External Client App's client-credentials flow before you involve
# EventBridge. It splits "OAuth is wrong" from "EventBridge is wrong".
#
# Usage: ./salesforce/scripts/test-client-credentials.sh <my-domain> [client-id]
#   my-domain  e.g. acme.my.salesforce.com
#   client-id  the ECA consumer key. Or export CID instead.
#
# The consumer secret is read from a hidden prompt. Passing secrets as command
# arguments leaves them in shell history and in the process list.
# This script doesn't source env.sh, because it needs no sf CLI session.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <my-domain> [client-id]" >&2
  exit 1
fi

MY_DOMAIN="$1"
CID="${2:-${CID:-}}"
if [[ -z "$CID" ]]; then
  echo "Pass the consumer key as the second argument or export CID." >&2
  exit 1
fi
if [[ ! "$MY_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Invalid My Domain host: $MY_DOMAIN" >&2
  exit 1
fi

read -rs -p "Consumer secret: " CSECRET
echo

response="$(curl -sS "https://$MY_DOMAIN/services/oauth2/token" \
  -d grant_type=client_credentials \
  --data-urlencode "client_id=$CID" \
  --data-urlencode "client_secret=$CSECRET")"
unset CSECRET

token="$(jq -r '.access_token // empty' <<<"$response")"
if [[ -n "$token" ]]; then
  echo "Token issued: ${token:0:10}..."
else
  echo "No token. Response:" >&2
  echo "$response" >&2
  exit 1
fi
