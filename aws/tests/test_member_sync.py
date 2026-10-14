import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import member_sync

NEW_ID = "11111111-2222-4333-8444-555555555555"
MEMBER_ID = "a005g00000AbCdEAAV"
SF_EVENT_ID = "118dab75-7b45-498e-8478-52d0324c1060"


@pytest.fixture(autouse=True)
def fixed_new_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(member_sync, "new_external_member_id", lambda: NEW_ID)


def _expected_put_item_params(item: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "TableName": "members",
        "Item": item
        or {
            "salesforce_member_id": {"S": MEMBER_ID},
            "external_member_id": {"S": NEW_ID},
            "email": {"S": "member@example.com"},
            "membership_type": {"S": "Standard"},
        },
        "ConditionExpression": "attribute_not_exists(salesforce_member_id)",
    }


def _expected_get_item_params() -> dict[str, Any]:
    return {
        "TableName": "members",
        "Key": {"salesforce_member_id": {"S": MEMBER_ID}},
        "ConsistentRead": True,
    }


def _expected_put_events_params(external_member_id: str) -> dict[str, Any]:
    return {
        "Entries": [
            {
                "EventBusName": "member-results",
                "Source": "com.example.member-sync",
                "DetailType": "MemberSynced",
                "Detail": json.dumps({
                    "Member_Id__c": MEMBER_ID,
                    "External_Member_Id__c": external_member_id,
                    "Sync_Status__c": "Synced",
                    "Source_Event_Id__c": SF_EVENT_ID,
                }),
            }
        ]
    }


def test_build_result_detail(relayed_event: dict[str, Any]) -> None:
    assert member_sync.build_result_detail(relayed_event, NEW_ID) == {
        "Member_Id__c": MEMBER_ID,
        "External_Member_Id__c": NEW_ID,
        "Sync_Status__c": "Synced",
        "Source_Event_Id__c": SF_EVENT_ID,
    }


def test_handler_registers_new_member(relayed_event: dict[str, Any]) -> None:
    with Stubber(member_sync._dynamodb) as ddb, Stubber(member_sync._events) as events:
        ddb.add_response("put_item", {}, _expected_put_item_params())
        events.add_response(
            "put_events",
            {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]},
            _expected_put_events_params(NEW_ID),
        )
        member_sync.handler(relayed_event, None)
        ddb.assert_no_pending_responses()
        events.assert_no_pending_responses()


def test_handler_reuses_existing_id_on_duplicate(relayed_event: dict[str, Any]) -> None:
    with Stubber(member_sync._dynamodb) as ddb, Stubber(member_sync._events) as events:
        ddb.add_client_error(
            "put_item",
            service_error_code="ConditionalCheckFailedException",
            expected_params=_expected_put_item_params(),
        )
        ddb.add_response(
            "get_item",
            {
                "Item": {
                    "salesforce_member_id": {"S": MEMBER_ID},
                    "external_member_id": {"S": "existing-id"},
                }
            },
            _expected_get_item_params(),
        )
        events.add_response(
            "put_events",
            {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]},
            _expected_put_events_params("existing-id"),
        )
        member_sync.handler(relayed_event, None)
        ddb.assert_no_pending_responses()
        events.assert_no_pending_responses()


def test_register_member_reraises_other_errors(relayed_event: dict[str, Any]) -> None:
    with Stubber(member_sync._dynamodb) as ddb:
        ddb.add_client_error(
            "put_item",
            service_error_code="ProvisionedThroughputExceededException",
            expected_params=_expected_put_item_params(),
        )
        with pytest.raises(ClientError):
            member_sync.register_member(relayed_event["detail"]["payload"])
        ddb.assert_no_pending_responses()


def test_register_member_omits_missing_optional_fields() -> None:
    item = {
        "salesforce_member_id": {"S": MEMBER_ID},
        "external_member_id": {"S": NEW_ID},
    }
    with Stubber(member_sync._dynamodb) as ddb:
        ddb.add_response("put_item", {}, _expected_put_item_params(item))
        assert member_sync.register_member({"Member_Id__c": MEMBER_ID, "Email__c": None}) == NEW_ID
        ddb.assert_no_pending_responses()


def test_handler_raises_on_partial_failure(relayed_event: dict[str, Any]) -> None:
    with Stubber(member_sync._dynamodb) as ddb, Stubber(member_sync._events) as events:
        ddb.add_response("put_item", {}, _expected_put_item_params())
        events.add_response(
            "put_events",
            {
                "FailedEntryCount": 1,
                "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "boom"}],
            },
            _expected_put_events_params(NEW_ID),
        )
        with pytest.raises(RuntimeError, match="PutEvents failed: InternalFailure boom"):
            member_sync.handler(relayed_event, None)
        ddb.assert_no_pending_responses()
        events.assert_no_pending_responses()
