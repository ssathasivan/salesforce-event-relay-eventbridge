import dataclasses
import json
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.member_relay_stack import MemberRelayStack, RelayConfig

PARTNER = "aws.partner/salesforce.com/00D5g000000XXXXEAA/0YL5g000000XXXXGAA"
DOMAIN = "acme.my.salesforce.com"
RELAY_ID = "7k2000000000001AAA"

SAMPLE = RelayConfig(
    partner_event_source_name=PARTNER,
    salesforce_my_domain=DOMAIN,
    salesforce_client_id="3MVG9EXAMPLE",
    event_relay_config_id=RELAY_ID,
)

RETRY_POLICY = {"MaximumRetryAttempts": 185, "MaximumEventAgeInSeconds": 86400}


def _synth(config: RelayConfig) -> Template:
    app = cdk.App()
    stack = MemberRelayStack(app, "sf-member-relay", config=config)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _synth(SAMPLE)


@pytest.fixture(scope="module")
def template_without_health_check() -> Template:
    return _synth(dataclasses.replace(SAMPLE, event_relay_config_id=""))


def _single(template: Template, resource_type: str, props: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    found = template.find_resources(resource_type, {"Properties": props})
    assert len(found) == 1, f"expected one {resource_type} matching {props}, found {len(found)}"
    return next(iter(found.items()))


def _logical_id(template: Template, resource_type: str, props: dict[str, Any]) -> str:
    return _single(template, resource_type, props)[0]


# Buses


def test_partner_bus_name_matches_event_source(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Events::EventBus", {"Name": PARTNER, "EventSourceName": PARTNER}
    )


def test_results_bus(template: Template) -> None:
    template.has_resource_properties("AWS::Events::EventBus", {"Name": "member-results"})
    template.resource_count_is("AWS::Events::EventBus", 2)


# Lambdas


def test_functions_runtime_arch_and_json_logs(template: Template) -> None:
    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 2
    for resource in functions.values():
        props = resource["Properties"]
        assert props["Runtime"] == "python3.14"
        assert props["Architectures"] == ["arm64"]
        assert props["LoggingConfig"]["LogFormat"] == "JSON"
        assert props["LoggingConfig"]["ApplicationLogLevel"] == "INFO"


def test_member_sync_function(template: Template) -> None:
    results_bus = _logical_id(template, "AWS::Events::EventBus", {"Name": "member-results"})
    table = _logical_id(template, "AWS::DynamoDB::Table", {"TableName": "members"})
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "member-sync",
            "Handler": "member_sync.handler",
            "Environment": {
                "Variables": {
                    "RESULT_BUS_NAME": {"Ref": results_bus},
                    "MEMBER_TABLE_NAME": {"Ref": table},
                }
            },
        },
    )


# Member table


def test_member_table(template: Template) -> None:
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.has_resource(
        "AWS::DynamoDB::Table",
        {
            "Properties": {
                "TableName": "members",
                "BillingMode": "PAY_PER_REQUEST",
                "KeySchema": [{"AttributeName": "salesforce_member_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [
                    {"AttributeName": "salesforce_member_id", "AttributeType": "S"}
                ],
            },
            "DeletionPolicy": "Delete",
        },
    )


def test_member_sync_dynamodb_access_limited_to_table(template: Template) -> None:
    table = _logical_id(template, "AWS::DynamoDB::Table", {"TableName": "members"})
    statements = [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "dynamodb:" in json.dumps(statement["Action"])
    ]
    assert len(statements) == 1
    (statement,) = statements
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["dynamodb:GetItem", "dynamodb:PutItem"]
    assert statement["Resource"] == {"Fn::GetAtt": [table, "Arn"]}


def test_fixed_log_groups(template: Template) -> None:
    for name in ("/aws/lambda/member-sync", "/aws/lambda/relay-health-check"):
        template.has_resource(
            "AWS::Logs::LogGroup",
            {
                "Properties": {"LogGroupName": name, "RetentionInDays": 30},
                "DeletionPolicy": "Delete",
            },
        )


def test_member_sync_async_retries_and_on_failure(template: Template) -> None:
    sync_dlq = "MemberSyncDlq"
    configs = template.find_resources("AWS::Lambda::EventInvokeConfig")
    assert len(configs) == 1
    props = next(iter(configs.values()))["Properties"]
    assert props["MaximumRetryAttempts"] == 2
    on_failure = props["DestinationConfig"]["OnFailure"]["Destination"]
    assert on_failure["Fn::GetAtt"][0].startswith(sync_dlq)


def test_member_sync_put_events_limited_to_results_bus(template: Template) -> None:
    results_bus = _logical_id(template, "AWS::Events::EventBus", {"Name": "member-results"})
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    {
                        "Action": "events:PutEvents",
                        "Effect": "Allow",
                        "Resource": {"Fn::GetAtt": [results_bus, "Arn"]},
                    }
                ])
            }
        },
    )


# Outbound rule


def test_member_registered_rule(template: Template) -> None:
    _, rule = _single(template, "AWS::Events::Rule", {"Name": "member-registered-to-sync"})
    props = rule["Properties"]
    assert props["EventPattern"] == {
        "source": [{"prefix": "aws.partner/salesforce.com"}],
        "detail-type": ["Member_Registered__e"],
    }
    (target,) = props["Targets"]
    assert target["RetryPolicy"] == RETRY_POLICY
    assert target["DeadLetterConfig"]["Arn"]["Fn::GetAtt"][0].startswith("MemberRegisteredRuleDlq")


# Return path


def test_connection_uses_client_credentials_and_secret(template: Template) -> None:
    _, connection = _single(
        template, "AWS::Events::Connection", {"AuthorizationType": "OAUTH_CLIENT_CREDENTIALS"}
    )
    oauth = connection["Properties"]["AuthParameters"]["OAuthParameters"]
    assert oauth["AuthorizationEndpoint"] == f"https://{DOMAIN}/services/oauth2/token"
    assert oauth["HttpMethod"] == "POST"
    assert oauth["ClientParameters"]["ClientID"] == "3MVG9EXAMPLE"
    secret = json.dumps(oauth["ClientParameters"]["ClientSecret"])
    assert "resolve:secretsmanager:salesforce/member-relay:SecretString:client_secret" in secret
    assert oauth["OAuthHttpParameters"]["BodyParameters"] == [
        {"Key": "grant_type", "Value": "client_credentials", "IsValueSecret": False}
    ]


def test_api_destination(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Events::ApiDestination",
        {
            "InvocationEndpoint": f"https://{DOMAIN}/services/data/v68.0/sobjects/Member_Synced__e",
            "HttpMethod": "POST",
            "InvocationRateLimitPerSecond": 20,
        },
    )


def test_member_synced_rule_forwards_detail(template: Template) -> None:
    _, rule = _single(template, "AWS::Events::Rule", {"Name": "member-synced-to-salesforce"})
    props = rule["Properties"]
    assert props["EventPattern"] == {
        "source": ["com.example.member-sync"],
        "detail-type": ["MemberSynced"],
    }
    (target,) = props["Targets"]
    assert target["InputPath"] == "$.detail"
    assert target["RetryPolicy"] == RETRY_POLICY
    assert target["DeadLetterConfig"]["Arn"]["Fn::GetAtt"][0].startswith("ReturnDlq")
    assert "RoleArn" in target


def test_dlqs_retain_14_days_and_enforce_ssl(template: Template) -> None:
    queues = template.find_resources("AWS::SQS::Queue")
    assert len(queues) == 3
    for queue in queues.values():
        assert queue["Properties"]["MessageRetentionPeriod"] == 1209600
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


# Monitoring


def test_health_check_function(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "relay-health-check",
            "Handler": "relay_health_check.handler",
            "Timeout": 30,
            "Environment": {
                "Variables": {
                    "SF_MY_DOMAIN": DOMAIN,
                    "SF_API_VERSION": "v68.0",
                    "MONITOR_SECRET_NAME": "salesforce/relay-monitor",
                    "RELAY_ID": RELAY_ID,
                    "METRIC_NAMESPACE": "SalesforceEventRelay",
                }
            },
        },
    )


def test_health_check_put_metric_data_scoped_to_namespace(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    {
                        "Action": "cloudwatch:PutMetricData",
                        "Effect": "Allow",
                        "Resource": "*",
                        "Condition": {
                            "StringEquals": {"cloudwatch:namespace": "SalesforceEventRelay"}
                        },
                    }
                ])
            }
        },
    )


def test_health_check_schedule(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Scheduler::Schedule", {"ScheduleExpression": "rate(5 minutes)"}
    )


def test_alarms_treat_missing_data_as_breaching(template: Template) -> None:
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 2
    for alarm in alarms.values():
        assert alarm["Properties"]["TreatMissingData"] == "breaching"


def test_relay_unhealthy_alarm(template: Template) -> None:
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "SalesforceEventRelay",
            "MetricName": "RelayHealthy",
            "Dimensions": [{"Name": "RelayId", "Value": RELAY_ID}],
            "Statistic": "Minimum",
            "Period": 300,
            "EvaluationPeriods": 2,
            "Threshold": 1,
            "ComparisonOperator": "LessThanThreshold",
            "OKActions": Match.any_value(),
        },
    )


def test_freshness_alarm(template: Template) -> None:
    _, alarm = _single(
        template, "AWS::CloudWatch::Alarm", {"Namespace": "AWS/Events", "MetricName": "MatchedEvents"}
    )
    props = alarm["Properties"]
    assert props["Dimensions"] == [
        {"Name": "EventBusName", "Value": PARTNER},
        {"Name": "RuleName", "Value": "member-registered-to-sync"},
    ]
    assert props["Statistic"] == "Sum"
    assert props["Period"] == 3600
    assert props["EvaluationPeriods"] == 2
    assert props["ComparisonOperator"] == "LessThanThreshold"
    assert "business hours" in props["AlarmDescription"]


def test_no_email_subscription_by_default(template: Template) -> None:
    template.resource_count_is("AWS::SNS::Subscription", 0)


def test_email_subscription_when_alert_email_set() -> None:
    template = _synth(dataclasses.replace(SAMPLE, alert_email="ops@example.com"))
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"}
    )


def test_outputs(template: Template) -> None:
    outputs = template.find_outputs("*")
    assert set(outputs) >= {
        "PartnerBusName",
        "ResultsBusName",
        "MemberSyncDlqUrl",
        "MemberRegisteredRuleDlqUrl",
        "ReturnDlqUrl",
        "AlarmTopicArn",
        "MemberSyncLogGroupName",
        "MemberTableName",
    }


def test_health_check_disabled_without_relay_id(template_without_health_check: Template) -> None:
    template_without_health_check.resource_count_is("AWS::Lambda::Function", 1)
    template_without_health_check.resource_count_is("AWS::CloudWatch::Alarm", 1)
    template_without_health_check.resource_count_is("AWS::Scheduler::Schedule", 0)


def test_health_check_disabled_by_flag() -> None:
    template = _synth(dataclasses.replace(SAMPLE, enable_relay_health_check=False))
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::Scheduler::Schedule", 0)


# RelayConfig.from_context

REQUIRED_CONTEXT = {
    "partnerEventSourceName": PARTNER,
    "salesforceMyDomain": DOMAIN,
    "salesforceClientId": "3MVG9EXAMPLE",
}


def _from_context(**context: Any) -> RelayConfig:
    return RelayConfig.from_context(cdk.App(context=context).node)


def test_from_context_defaults() -> None:
    config = _from_context(**REQUIRED_CONTEXT)
    assert config == RelayConfig(
        partner_event_source_name=PARTNER,
        salesforce_my_domain=DOMAIN,
        salesforce_client_id="3MVG9EXAMPLE",
    )
    assert config.salesforce_api_version == "v68.0"
    assert config.health_check_enabled is False


def test_from_context_parses_strings() -> None:
    config = _from_context(
        **REQUIRED_CONTEXT,
        eventRelayConfigId=RELAY_ID,
        enableRelayHealthCheck="false",
        freshnessWindowHours="4",
    )
    assert config.enable_relay_health_check is False
    assert config.health_check_enabled is False
    assert config.freshness_window_hours == 4


def test_from_context_missing_partner_source() -> None:
    context = {k: v for k, v in REQUIRED_CONTEXT.items() if k != "partnerEventSourceName"}
    with pytest.raises(ValueError, match="Missing CDK context 'partnerEventSourceName'"):
        _from_context(**context)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("salesforceApiVersion", "v68"),
        ("partnerEventSourceName", "aws.partner/other.com/x"),
        ("salesforceMyDomain", "https://acme.my.salesforce.com"),
        ("eventRelayConfigId", "7k2'"),
        ("enableRelayHealthCheck", "yes"),
        ("alertEmail", "not-an-email"),
        ("freshnessWindowHours", "0"),
        ("freshnessWindowHours", "two"),
    ],
)
def test_from_context_rejects_invalid_values(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=f"Invalid CDK context '{key}'"):
        _from_context(**{**REQUIRED_CONTEXT, key: value})
