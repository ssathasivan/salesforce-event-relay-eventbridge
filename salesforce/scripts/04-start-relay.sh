#!/usr/bin/env bash
# Start (or stop) the relay. state and relayOption are updated through the
# Metadata field. The label and current relayOption are sent along with state,
# so it's always clear what the relay is configured to do after the call.
#
# Usage: ./salesforce/scripts/04-start-relay.sh [RUN|STOP]   (default RUN)
#
# Run this only after cdk deploy has associated the partner event source.
# State changes can take several minutes. Poll with 05-relay-status.sh.
set -euo pipefail
source "$(dirname "$0")/env.sh"
: "${RELAY_ID:?export RELAY_ID first (see 02-create-relay.sh)}"

STATE="${1:-RUN}"
if [[ "$STATE" != "RUN" && "$STATE" != "STOP" ]]; then
  echo "State must be RUN or STOP, got: $STATE" >&2
  exit 1
fi
if [[ ! "$RELAY_ID" =~ ^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$ ]]; then
  echo "RELAY_ID doesn't look like a Salesforce Id: $RELAY_ID" >&2
  exit 1
fi

code="$(sfpatch "/services/data/$SF_API_VERSION/tooling/sobjects/EventRelayConfig/$RELAY_ID" \
  '{"Metadata":{"label":"Member Relay","state":"'"$STATE"'","relayOption":"{\"ReplayRecovery\":\"EARLIEST\"}"}}')"

if [[ "$code" != "204" ]]; then
  echo "Expected HTTP 204, got $code" >&2
  exit 1
fi
echo "Relay state set to $STATE (HTTP 204). Poll 05-relay-status.sh until it settles."
