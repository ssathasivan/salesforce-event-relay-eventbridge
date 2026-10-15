import json
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.customer_relay_stack import CustomerRelayStack, RelayConfig

PARTNER = "aws.partner/salesforce.com/00D5g000000XXXXEAA/0YL5g000000XXXXGAA"
SAMPLE = RelayConfig(partner_event_source_name=PARTNER)


def _synth(config: RelayConfig) -> Template:
    stack = CustomerRelayStack(cdk.App(), "sf-customer-relay", config=config)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _synth(SAMPLE)


def _logical_id(template: Template, resource_type: str, props: dict[str, Any]) -> str:
    found = template.find_resources(resource_type, {"Properties": props})
    assert len(found) == 1, f"expected one {resource_type} matching {props}, found {len(found)}"
    return next(iter(found))


def _queue_ids(template: Template) -> tuple[str, str]:
    """Return (main queue, DLQ) logical IDs. The main queue is the one with a redrive policy."""
    queues = template.find_resources("AWS::SQS::Queue")
    main = [qid for qid, q in queues.items() if "RedrivePolicy" in q["Properties"]]
    dlq = [qid for qid, q in queues.items() if "RedrivePolicy" not in q["Properties"]]
    assert len(main) == 1 and len(dlq) == 1
    return main[0], dlq[0]


def test_only_the_expected_resources(template: Template) -> None:
    template.resource_count_is("AWS::Events::EventBus", 1)
    template.resource_count_is("AWS::Events::Rule", 1)
    template.resource_count_is("AWS::SQS::Queue", 2)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.resource_count_is("AWS::Events::ApiDestination", 0)
    template.resource_count_is("AWS::Scheduler::Schedule", 0)


def test_partner_bus_name_matches_event_source(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Events::EventBus", {"Name": PARTNER, "EventSourceName": PARTNER}
    )


def test_rule_routes_customer_created_to_queue(template: Template) -> None:
    main, dlq = _queue_ids(template)
    rule = next(iter(template.find_resources("AWS::Events::Rule").values()))["Properties"]
    assert rule["Name"] == "customer-created-to-queue"
    assert rule["EventPattern"] == {
        "source": [{"prefix": "aws.partner/salesforce.com"}],
        "detail-type": ["Customer_Created__e"],
    }
    (target,) = rule["Targets"]
    assert target["Arn"] == {"Fn::GetAtt": [main, "Arn"]}
    assert target["DeadLetterConfig"]["Arn"] == {"Fn::GetAtt": [dlq, "Arn"]}
    assert target["RetryPolicy"] == {"MaximumRetryAttempts": 185, "MaximumEventAgeInSeconds": 86400}
    # No input transformer: the SQS message body is the full EventBridge event.
    assert "InputPath" not in target and "InputTransformer" not in target


def test_queue_redrives_to_dlq(template: Template) -> None:
    main, dlq = _queue_ids(template)
    props = template.find_resources("AWS::SQS::Queue")[main]["Properties"]
    assert props["VisibilityTimeout"] == 180  # 6x the 30 s function timeout
    assert props["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": [dlq, "Arn"]},
        "maxReceiveCount": 5,
    }
    dlq_props = template.find_resources("AWS::SQS::Queue")[dlq]["Properties"]
    assert dlq_props["MessageRetentionPeriod"] == 1209600


def test_queues_enforce_ssl(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SQS::QueuePolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Effect": "Deny",
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    })
                ])
            }
        },
    )


def test_customer_table(template: Template) -> None:
    template.has_resource(
        "AWS::DynamoDB::Table",
        {
            "Properties": {
                "TableName": "customers",
                "BillingMode": "PAY_PER_REQUEST",
                "KeySchema": [{"AttributeName": "account_id", "KeyType": "HASH"}],
            },
            "DeletionPolicy": "Delete",
        },
    )


def test_function(template: Template) -> None:
    table = _logical_id(template, "AWS::DynamoDB::Table", {"TableName": "customers"})
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "customer-sync",
            "Handler": "customer_sync.handler",
            "Runtime": "python3.14",
            "Architectures": ["arm64"],
            "Timeout": 30,
            "LoggingConfig": Match.object_like({"LogFormat": "JSON"}),
            "Environment": {"Variables": {"CUSTOMER_TABLE_NAME": {"Ref": table}}},
        },
    )


def test_event_source_mapping_reports_batch_failures(template: Template) -> None:
    main, _ = _queue_ids(template)
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "EventSourceArn": {"Fn::GetAtt": [main, "Arn"]},
            "BatchSize": 10,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "ScalingConfig": {"MaximumConcurrency": 5},
        },
    )


def test_dynamodb_access_is_put_item_on_the_table_only(template: Template) -> None:
    table = _logical_id(template, "AWS::DynamoDB::Table", {"TableName": "customers"})
    statements = [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "dynamodb:" in json.dumps(statement["Action"])
    ]
    assert statements == [
        {"Action": "dynamodb:PutItem", "Effect": "Allow", "Resource": {"Fn::GetAtt": [table, "Arn"]}}
    ]


def test_dlq_alarm(template: Template) -> None:
    _, dlq = _queue_ids(template)
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "ApproximateNumberOfMessagesVisible",
            "Dimensions": [{"Name": "QueueName", "Value": {"Fn::GetAtt": [dlq, "QueueName"]}}],
            "Threshold": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        },
    )


def test_no_email_subscription_by_default(template: Template) -> None:
    template.resource_count_is("AWS::SNS::Subscription", 0)


def test_email_subscription_when_alert_email_set() -> None:
    template = _synth(RelayConfig(partner_event_source_name=PARTNER, alert_email="ops@example.com"))
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"}
    )


# RelayConfig.from_context


def _from_context(**context: Any) -> RelayConfig:
    return RelayConfig.from_context(cdk.App(context=context).node)


def test_from_context() -> None:
    assert _from_context(partnerEventSourceName=PARTNER) == SAMPLE


def test_from_context_requires_partner_source() -> None:
    with pytest.raises(ValueError, match="Missing CDK context 'partnerEventSourceName'"):
        _from_context()


@pytest.mark.parametrize(
    ("key", "value"),
    [("partnerEventSourceName", "aws.partner/other.com/x"), ("alertEmail", "not-an-email")],
)
def test_from_context_rejects_invalid_values(key: str, value: str) -> None:
    context = {"partnerEventSourceName": PARTNER, key: value}
    with pytest.raises(ValueError, match=f"Invalid CDK context '{key}'"):
        _from_context(**context)
