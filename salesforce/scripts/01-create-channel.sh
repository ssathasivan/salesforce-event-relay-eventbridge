#!/usr/bin/env bash
# Create the custom platform event channel and its single member (Tooling API).
#
# Usage: ./salesforce/scripts/01-create-channel.sh
#
# Naming: the channel is Member_Relay__chn (double underscore), but the
# member's FullName uses single underscores (Member_Relay_chn_Member_Registered_e).
# Member_Synced__e must never be added here, or results would relay back to AWS.
set -euo pipefail
source "$(dirname "$0")/env.sh"

# Channel: channelType "event" for platform events ("data" for CDC)
sfpost "/services/data/$SF_API_VERSION/tooling/sobjects/PlatformEventChannel" '{
  "FullName": "Member_Relay__chn",
  "Metadata": { "channelType": "event", "label": "Member Relay" }
}'

# Member: one per event type in the channel
sfpost "/services/data/$SF_API_VERSION/tooling/sobjects/PlatformEventChannelMember" '{
  "FullName": "Member_Relay_chn_Member_Registered_e",
  "Metadata": { "eventChannel": "Member_Relay__chn", "selectedEntity": "Member_Registered__e" }
}'
