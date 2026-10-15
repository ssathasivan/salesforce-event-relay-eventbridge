"""Stores Customer_Created__e events from Salesforce in DynamoDB.

EventBridge sends each relayed event to an SQS queue, and this function reads
from the queue in batches. Each SQS message body is the full EventBridge event.

- Duplicates are expected (Event Relay recovery, EventBridge and SQS retries).
  A conditional put keyed on the Account ID makes a repeat delivery a no-op.
- Failed messages are reported individually (partial batch response), so only
  those are retried and, after the queue's maxReceiveCount, moved to the DLQ.
"""

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Fail fast at cold start if the stack didn't set the table name.
CUSTOMER_TABLE_NAME = os.environ["CUSTOMER_TABLE_NAME"]

logger = logging.getLogger()
_dynamodb = boto3.client("dynamodb")


def build_item(event: dict[str, Any]) -> dict[str, Any]:
    """Map a relayed Customer_Created__e event to a DynamoDB item."""
    detail = event["detail"]
    payload = detail["payload"]
    item = {
        "account_id": {"S": payload["Account_Id__c"]},
        "salesforce_event_id": {"S": detail["id"]},
    }
    # DynamoDB rejects null strings, so only add optional fields that have a value.
    optional = {
        "name": payload.get("Name__c"),
        "industry": payload.get("Industry__c"),
        "source_created_date": payload.get("CreatedDate"),
    }
    for key, value in optional.items():
        if value:
            item[key] = {"S": value}
    return item


def save_customer(event: dict[str, Any]) -> bool:
    """Write the customer once. Returns False if it was already stored."""
    try:
        _dynamodb.put_item(
            TableName=CUSTOMER_TABLE_NAME,
            Item=build_item(event),
            ConditionExpression="attribute_not_exists(account_id)",
        )
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Already stored: this is a duplicate delivery, not a failure.
        # Raising here would retry it until it lands in the DLQ.
        return False


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures = []
    for record in event["Records"]:
        message_id = record["messageId"]
        try:
            relayed = json.loads(record["body"])
            created = save_customer(relayed)
            logger.info(
                "Customer stored" if created else "Duplicate skipped",
                extra={
                    "message_id": message_id,
                    "sf_event_id": relayed["detail"].get("id"),
                    "account_id": relayed["detail"]["payload"].get("Account_Id__c"),
                },
            )
        except Exception:
            # Catch everything per message. Raising from the handler would fail
            # the whole batch, including messages that were already saved.
            logger.exception("Failed to process message", extra={"message_id": message_id})
            failures.append({"itemIdentifier": message_id})

    # An empty list tells Lambda the whole batch succeeded.
    return {"batchItemFailures": failures}
