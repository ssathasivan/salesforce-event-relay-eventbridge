"""Reports whether the Salesforce Event Relay is RUNNING.

Runs every 5 minutes (EventBridge Scheduler). Authenticates with its own
External Client App (client credentials, Run As = monitoring user), queries
EventRelayFeedback, and publishes RelayHealthy = 1 or 0 to CloudWatch.
Any failure (missing secret, bad credentials, query error) reports 0.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any

import boto3

SALESFORCE_ID = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
HTTP_TIMEOUT_SECONDS = 10

logger = logging.getLogger()
_secrets = boto3.client("secretsmanager")
_cloudwatch = boto3.client("cloudwatch")


def is_healthy(query_result: dict[str, Any]) -> bool:
    """Healthy only when exactly one feedback record exists and it is RUNNING."""
    records = query_result.get("records")
    if not isinstance(records, list) or len(records) != 1:
        return False
    record = records[0]
    return isinstance(record, dict) and record.get("Status") == "RUNNING"


def _require_https(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Only https URLs are allowed")


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    _require_https(url)
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def _get_json(url: str, token: str) -> dict[str, Any]:
    _require_https(url)
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def query_relay_feedback(relay_id: str) -> dict[str, Any]:
    my_domain = os.environ["SF_MY_DOMAIN"]
    api_version = os.environ["SF_API_VERSION"]

    secret = _secrets.get_secret_value(SecretId=os.environ["MONITOR_SECRET_NAME"])
    credentials = json.loads(secret["SecretString"])

    # Client-credentials token. Never log the token or the secret.
    token = _post_form(
        f"https://{my_domain}/services/oauth2/token",
        {
            "grant_type": "client_credentials",
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
        },
    )

    soql = (
        "SELECT Status, LastRelayedEventTime, ErrorCode, ErrorMessage "
        f"FROM EventRelayFeedback WHERE EventRelayConfigId = '{relay_id}'"
    )
    url = (
        f"{token['instance_url']}/services/data/{api_version}/query"
        f"?q={urllib.parse.quote(soql)}"
    )
    return _get_json(url, token["access_token"])


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    relay_id = os.environ["RELAY_ID"]
    namespace = os.environ.get("METRIC_NAMESPACE", "SalesforceEventRelay")

    healthy = False
    try:
        # Validating the Id keeps it safe to place inside the SOQL string.
        if not SALESFORCE_ID.match(relay_id):
            raise ValueError("RELAY_ID is not a valid Salesforce Id")

        result = query_relay_feedback(relay_id)
        healthy = is_healthy(result)
        for record in result.get("records") or []:
            logger.info(
                "Relay feedback",
                extra={
                    "relay_id": relay_id,
                    "status": record.get("Status"),
                    "last_relayed_event_time": record.get("LastRelayedEventTime"),
                    "error_code": record.get("ErrorCode"),
                    "error_message": record.get("ErrorMessage"),
                },
            )
    except Exception:
        logger.exception("Relay health check failed", extra={"relay_id": relay_id})
        healthy = False

    _cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": "RelayHealthy",
                "Value": 1.0 if healthy else 0.0,
                "Unit": "Count",
                "Dimensions": [{"Name": "RelayId", "Value": relay_id}],
            }
        ],
    )

    logger.info("Relay health checked", extra={"relay_id": relay_id, "healthy": healthy})
    return {"relay_id": relay_id, "healthy": healthy}
