import json
from typing import Any

import pytest
from botocore.stub import Stubber

import customer_sync

ACCOUNT_ID = "0015g00000AbCdEAAV"
SF_EVENT_ID = "118dab75-7b45-498e-8478-52d0324c1060"

FULL_ITEM = {
    "account_id": {"S": ACCOUNT_ID},
    "salesforce_event_id": {"S": SF_EVENT_ID},
    "name": {"S": "Example Corp"},
    "industry": {"S": "Manufacturing"},
    "source_created_date": {"S": "2026-10-13T19:23:22.686Z"},
}


def _put_params(item: dict[str, Any] = FULL_ITEM) -> dict[str, Any]:
    return {
        "TableName": "customers",
        "Item": item,
        "ConditionExpression": "attribute_not_exists(account_id)",
    }


def _sqs_event(*bodies: Any) -> dict[str, Any]:
    """Build an SQS event. Strings are used as raw bodies, dicts are JSON-encoded."""
    return {
        "Records": [
            {"messageId": f"msg-{i}", "body": body if isinstance(body, str) else json.dumps(body)}
            for i, body in enumerate(bodies, start=1)
        ]
    }


def test_build_item(relayed_event: dict[str, Any]) -> None:
    assert customer_sync.build_item(relayed_event) == FULL_ITEM


def test_build_item_omits_empty_optional_fields(relayed_event: dict[str, Any]) -> None:
    relayed_event["detail"]["payload"]["Industry__c"] = None
    item = customer_sync.build_item(relayed_event)
    assert "industry" not in item
    assert item["account_id"] == {"S": ACCOUNT_ID}


def test_new_customer_is_stored(relayed_event: dict[str, Any]) -> None:
    with Stubber(customer_sync._dynamodb) as ddb:
        ddb.add_response("put_item", {}, _put_params())
        assert customer_sync.handler(_sqs_event(relayed_event), None) == {"batchItemFailures": []}
        ddb.assert_no_pending_responses()


def test_duplicate_is_treated_as_success(relayed_event: dict[str, Any]) -> None:
    with Stubber(customer_sync._dynamodb) as ddb:
        ddb.add_client_error(
            "put_item",
            service_error_code="ConditionalCheckFailedException",
            expected_params=_put_params(),
        )
        assert customer_sync.handler(_sqs_event(relayed_event), None) == {"batchItemFailures": []}
        ddb.assert_no_pending_responses()


def test_only_failed_messages_are_reported(relayed_event: dict[str, Any]) -> None:
    with Stubber(customer_sync._dynamodb) as ddb:
        ddb.add_response("put_item", {}, _put_params())
        ddb.add_client_error(
            "put_item",
            service_error_code="ProvisionedThroughputExceededException",
            expected_params=_put_params(),
        )
        event = _sqs_event(relayed_event, relayed_event, "not json")
        assert customer_sync.handler(event, None) == {
            "batchItemFailures": [{"itemIdentifier": "msg-2"}, {"itemIdentifier": "msg-3"}]
        }
        ddb.assert_no_pending_responses()


def test_message_missing_account_id_is_reported(relayed_event: dict[str, Any]) -> None:
    del relayed_event["detail"]["payload"]["Account_Id__c"]
    assert customer_sync.handler(_sqs_event(relayed_event), None) == {
        "batchItemFailures": [{"itemIdentifier": "msg-1"}]
    }


def test_save_customer_returns_false_for_duplicate(relayed_event: dict[str, Any]) -> None:
    with Stubber(customer_sync._dynamodb) as ddb:
        ddb.add_client_error(
            "put_item",
            service_error_code="ConditionalCheckFailedException",
            expected_params=_put_params(),
        )
        assert customer_sync.save_customer(relayed_event) is False


def test_save_customer_reraises_other_errors(relayed_event: dict[str, Any]) -> None:
    with Stubber(customer_sync._dynamodb) as ddb:
        ddb.add_client_error(
            "put_item",
            service_error_code="InternalServerError",
            expected_params=_put_params(),
        )
        with pytest.raises(Exception):
            customer_sync.save_customer(relayed_event)
