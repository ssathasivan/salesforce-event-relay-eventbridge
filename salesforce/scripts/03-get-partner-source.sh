#!/usr/bin/env bash
# Print the partner event source name Salesforce registered in your AWS account,
# then confirm it from the AWS side (it shows PENDING until the CDK stack
# associates it with a bus).
#
# Usage: ./salesforce/scripts/03-get-partner-source.sh
#
# EventRelayFeedback is a regular sObject, so this uses the data API, not Tooling.
set -euo pipefail
source "$(dirname "$0")/env.sh"
: "${RELAY_ID:?export RELAY_ID first (see 02-create-relay.sh)}"

if [[ ! "$RELAY_ID" =~ ^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$ ]]; then
  echo "RELAY_ID doesn't look like a Salesforce Id: $RELAY_ID" >&2
  exit 1
fi

source_name="$(sf data query --target-org "$SF_ORG" --json -q \
  "SELECT RemoteResource, Status FROM EventRelayFeedback WHERE EventRelayConfigId = '$RELAY_ID'" \
  | jq -r '.result.records[0].RemoteResource // empty')"

if [[ -z "$source_name" ]]; then
  echo "RemoteResource is empty. The field fills in after a short delay. Wait a minute and re-run." >&2
  exit 2
fi

echo "export PARTNER_SOURCE=$source_name"
echo "Run the line above, then pass it to CDK as -c partnerEventSourceName=\"\$PARTNER_SOURCE\"."
echo

aws events list-event-sources --region "$AWS_REGION" --name-prefix aws.partner/salesforce.com \
  --query 'EventSources[].{Name:Name,State:State}' --output table
