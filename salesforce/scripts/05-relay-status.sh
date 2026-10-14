#!/usr/bin/env bash
# One-shot relay status check.
#
# Usage: ./salesforce/scripts/05-relay-status.sh
# Poll:  watch -n 15 ./salesforce/scripts/05-relay-status.sh
set -euo pipefail
source "$(dirname "$0")/env.sh"
: "${RELAY_ID:?export RELAY_ID first (see 02-create-relay.sh)}"

if [[ ! "$RELAY_ID" =~ ^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$ ]]; then
  echo "RELAY_ID doesn't look like a Salesforce Id: $RELAY_ID" >&2
  exit 1
fi

sf data query --target-org "$SF_ORG" -q \
  "SELECT Status, LastRelayedEventTime, ErrorCode, ErrorMessage FROM EventRelayFeedback WHERE EventRelayConfigId = '$RELAY_ID'"
