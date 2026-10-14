#!/usr/bin/env bash
# Insert one Member__c record with anonymous Apex, so the real
# MemberRegisteredPublisher trigger publishes Member_Registered__e.
#
# Usage: ./salesforce/scripts/06-create-test-member.sh <email> [Standard|Premium]
#   email  the member's email address
#   type   membership type, defaults to Standard
#
# Run as a user with the Member_Relay_Admin permission set. Anonymous Apex runs
# with that user's object and field permissions.
#
# Sync_Status__c starts at Pending and turns Synced when the return leg
# completes.
set -euo pipefail
source "$(dirname "$0")/env.sh"
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <email> [Standard|Premium]" >&2
  exit 1
fi
EMAIL="$1"
TYPE="${2:-Standard}"
# Validate before interpolating into Apex and SOQL (keeps quotes out).
if [[ ! "$EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
  echo "Not an email address: $EMAIL" >&2
  exit 1
fi
if [[ "$TYPE" != "Standard" && "$TYPE" != "Premium" ]]; then
  echo "Membership type must be Standard or Premium, got: $TYPE" >&2
  exit 1
fi
echo "insert new Member__c(Name='Test Member', Email__c='$EMAIL', Membership_Type__c='$TYPE');" \
  | sf apex run --target-org "$SF_ORG"
sf data query --target-org "$SF_ORG" \
  -q "SELECT Id, Sync_Status__c, External_Member_Id__c FROM Member__c WHERE Email__c = '$EMAIL' ORDER BY CreatedDate DESC LIMIT 1"
