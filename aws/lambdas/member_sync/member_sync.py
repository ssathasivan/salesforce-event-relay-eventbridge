"""Registers relayed Member_Registered__e events in our AWS application and
emits a MemberSynced result event.

Subscribed to the Salesforce partner event bus. The member is written to a
DynamoDB table with a conditional put, so duplicates reuse the first external
id. The result goes to the member-results bus, where a rule forwards it to
Salesforce through an API destination.
"""

import json
import logging
import os
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

SOURCE = "com.example.member-sync"
DETAIL_TYPE = "MemberSynced"
SYNC_STATUS = "Synced"

# Fail fast at cold start if the stack didn't set the bus or table name.
RESULT_BUS_NAME = os.environ["RESULT_BUS_NAME"]
MEMBER_TABLE_NAME = os.environ["MEMBER_TABLE_NAME"]

logger = logging.getLogger()
_dynamodb = boto3.client("dynamodb")
_events = boto3.client("events")


def new_external_member_id() -> str:
    return str(uuid.uuid4())


def register_member(payload: dict[str, Any]) -> str:
    member_id = payload["Member_Id__c"]
    external_member_id = new_external_member_id()
    item = {
        "salesforce_member_id": {"S": member_id},
        "external_member_id": {"S": external_member_id},
    }
    # DynamoDB rejects null strings, so only add fields that have a value.
    if payload.get("Email__c"):
        item["email"] = {"S": payload["Email__c"]}
    if payload.get("Membership_Type__c"):
        item["membership_type"] = {"S": payload["Membership_Type__c"]}

    try:
        # First write wins. A replay or retry doesn't overwrite it.
        _dynamodb.put_item(
            TableName=MEMBER_TABLE_NAME,
            Item=item,
            ConditionExpression="attribute_not_exists(salesforce_member_id)",
        )
        return external_member_id
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    # Already registered. Reuse the id from the first write.
    existing = _dynamodb.get_item(
        TableName=MEMBER_TABLE_NAME,
        Key={"salesforce_member_id": {"S": member_id}},
        ConsistentRead=True,
    )
    return existing["Item"]["external_member_id"]["S"]


def build_result_detail(event: dict[str, Any], external_member_id: str) -> dict[str, Any]:
    detail = event["detail"]
    payload = detail["payload"]
    # Keys are Salesforce field API names on Member_Synced__e.
    # InputPath $.detail forwards this object as the REST body.
    return {
        "Member_Id__c": payload["Member_Id__c"],
        "External_Member_Id__c": external_member_id,
        "Sync_Status__c": SYNC_STATUS,
        "Source_Event_Id__c": detail["id"],
    }


def handler(event: dict[str, Any], context: Any) -> None:
    detail = event["detail"]
    logger.info(
        "Member received",
        extra={
            "detail_type": event.get("detail-type"),
            "sf_event_id": detail.get("id"),
            "schema_id": detail.get("schemaId"),
            "member_id": detail["payload"].get("Member_Id__c"),
        },
    )

    external_member_id = register_member(detail["payload"])
    result = build_result_detail(event, external_member_id)

    response = _events.put_events(
        Entries=[
            {
                "EventBusName": RESULT_BUS_NAME,
                "Source": SOURCE,
                "DetailType": DETAIL_TYPE,
                "Detail": json.dumps(result),
            }
        ]
    )

    # PutEvents reports partial failure in the body, not as an exception.
    if response.get("FailedEntryCount", 0) > 0:
        entry = response["Entries"][0]
        raise RuntimeError(f"PutEvents failed: {entry.get('ErrorCode')} {entry.get('ErrorMessage')}")
