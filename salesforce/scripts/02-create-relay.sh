#!/usr/bin/env bash
# Create the event relay in STOP state (the only state allowed on create).
#
# Usage: ./salesforce/scripts/02-create-relay.sh [named-credential]
#   named-credential  defaults to AWS_US_EAST_1
#
# After creation you can update only state and relayOption. A wrong channel or
# credential means delete and recreate (07-teardown-relay.sh).
set -euo pipefail
source "$(dirname "$0")/env.sh"

NAMED_CREDENTIAL="${1:-AWS_US_EAST_1}"
if [[ ! "$NAMED_CREDENTIAL" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]]; then
  echo "Invalid named credential name: $NAMED_CREDENTIAL" >&2
  exit 1
fi

# relayOption is a JSON string inside JSON, so the escaping is intentional.
response="$(sfpost "/services/data/$SF_API_VERSION/tooling/sobjects/EventRelayConfig" '{
  "FullName": "Member_Relay",
  "Metadata": {
    "label": "Member Relay",
    "destinationResourceName": "callout:'"$NAMED_CREDENTIAL"'",
    "eventChannel": "Member_Relay__chn",
    "relayOption": "{\"ReplayRecovery\":\"EARLIEST\"}",
    "state": "STOP"
  }
}')"

relay_id="$(jq -r '.id // empty' <<<"$response" 2>/dev/null || true)"
if [[ -z "$relay_id" ]]; then
  echo "Relay creation failed:" >&2
  echo "$response" >&2
  exit 1
fi

echo "Relay created. Run this in your shell:"
echo "export RELAY_ID=$relay_id"
