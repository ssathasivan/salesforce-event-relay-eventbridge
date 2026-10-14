#!/usr/bin/env bash
# Stop and delete the relay, then delete the channel member and the channel.
# Deleting the relay also removes the partner event source in AWS.
#
# Usage: ./salesforce/scripts/07-teardown-relay.sh
#
# Verify: the DeveloperName filters below follow the Tooling API reference.
# Check them against the reference for your API version.
set -euo pipefail
source "$(dirname "$0")/env.sh"
: "${RELAY_ID:?export RELAY_ID first (see 02-create-relay.sh)}"

if [[ ! "$RELAY_ID" =~ ^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$ ]]; then
  echo "RELAY_ID doesn't look like a Salesforce Id: $RELAY_ID" >&2
  exit 1
fi

read -r -p "Delete relay $RELAY_ID and channel Member_Relay__chn? [y/N] " answer
if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

TOOLING="/services/data/$SF_API_VERSION/tooling/sobjects"

echo "Stopping relay..."
code="$(sfpatch "$TOOLING/EventRelayConfig/$RELAY_ID" \
  '{"Metadata":{"label":"Member Relay","state":"STOP","relayOption":"{\"ReplayRecovery\":\"EARLIEST\"}"}}')"
[[ "$code" == "204" ]] || { echo "Stop failed (HTTP $code)" >&2; exit 1; }

echo "Deleting relay..."
code="$(sfdelete "$TOOLING/EventRelayConfig/$RELAY_ID")"
[[ "$code" == "204" ]] || { echo "Relay delete failed (HTTP $code)" >&2; exit 1; }

member_id="$(sftooling_query "SELECT Id FROM PlatformEventChannelMember WHERE DeveloperName = 'Member_Relay_chn_Member_Registered_e'" \
  | jq -r '.records[0].Id // empty')"
if [[ -n "$member_id" ]]; then
  echo "Deleting channel member $member_id..."
  code="$(sfdelete "$TOOLING/PlatformEventChannelMember/$member_id")"
  [[ "$code" == "204" ]] || { echo "Member delete failed (HTTP $code)" >&2; exit 1; }
else
  echo "Channel member not found, skipping."
fi

channel_id="$(sftooling_query "SELECT Id FROM PlatformEventChannel WHERE DeveloperName = 'Member_Relay'" \
  | jq -r '.records[0].Id // empty')"
if [[ -n "$channel_id" ]]; then
  echo "Deleting channel $channel_id..."
  code="$(sfdelete "$TOOLING/PlatformEventChannel/$channel_id")"
  [[ "$code" == "204" ]] || { echo "Channel delete failed (HTTP $code)" >&2; exit 1; }
else
  echo "Channel not found, skipping."
fi

echo "Done."
