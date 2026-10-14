from typing import Any
from unittest.mock import patch

import pytest
from botocore.stub import Stubber

import relay_health_check

RELAY_ID = "7k2000000000001AAA"
RUNNING = {"totalSize": 1, "done": True, "records": [{"Status": "RUNNING"}]}


@pytest.mark.parametrize(
    ("query_result", "expected"),
    [
        (RUNNING, True),
        ({"records": [{"Status": "ERROR"}]}, False),
        ({"records": []}, False),
        ({"records": [{"Status": "RUNNING"}, {"Status": "RUNNING"}]}, False),
        ({}, False),
    ],
    ids=["running", "error", "no-records", "two-records", "missing-records"],
)
def test_is_healthy(query_result: dict[str, Any], expected: bool) -> None:
    assert relay_health_check.is_healthy(query_result) is expected


def _expected_metric(relay_id: str, value: float) -> dict[str, Any]:
    return {
        "Namespace": "SalesforceEventRelay",
        "MetricData": [
            {
                "MetricName": "RelayHealthy",
                "Value": value,
                "Unit": "Count",
                "Dimensions": [{"Name": "RelayId", "Value": relay_id}],
            }
        ],
    }


def test_handler_reports_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_ID", RELAY_ID)
    with (
        patch.object(relay_health_check, "query_relay_feedback", return_value=RUNNING),
        Stubber(relay_health_check._cloudwatch) as stubber,
    ):
        stubber.add_response("put_metric_data", {}, _expected_metric(RELAY_ID, 1.0))
        result = relay_health_check.handler({}, None)
        stubber.assert_no_pending_responses()
    assert result == {"relay_id": RELAY_ID, "healthy": True}


def test_handler_reports_unhealthy_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_ID", RELAY_ID)
    with (
        patch.object(
            relay_health_check, "query_relay_feedback", side_effect=RuntimeError("token failed")
        ),
        Stubber(relay_health_check._cloudwatch) as stubber,
    ):
        stubber.add_response("put_metric_data", {}, _expected_metric(RELAY_ID, 0.0))
        result = relay_health_check.handler({}, None)
        stubber.assert_no_pending_responses()
    assert result == {"relay_id": RELAY_ID, "healthy": False}


def test_handler_rejects_invalid_relay_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_ID", "bad'id")
    with (
        patch.object(relay_health_check, "query_relay_feedback") as query,
        Stubber(relay_health_check._cloudwatch) as stubber,
    ):
        stubber.add_response("put_metric_data", {}, _expected_metric("bad'id", 0.0))
        result = relay_health_check.handler({}, None)
        stubber.assert_no_pending_responses()
    query.assert_not_called()
    assert result == {"relay_id": "bad'id", "healthy": False}


def test_require_https_rejects_http() -> None:
    with pytest.raises(ValueError):
        relay_health_check._require_https("http://acme.my.salesforce.com/services/oauth2/token")
