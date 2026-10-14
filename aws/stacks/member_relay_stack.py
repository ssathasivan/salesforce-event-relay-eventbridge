"""Salesforce Event Relay round trip: partner bus, Python Lambda, API destination
back to Salesforce, and relay monitoring."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    SecretValue,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_destinations as destinations,
    aws_logs as logs,
    aws_scheduler as scheduler,
    aws_scheduler_targets as scheduler_targets,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
)
from constructs import Construct, Node

LAMBDAS_DIR = Path(__file__).resolve().parent.parent / "lambdas"

MEMBER_REGISTERED_RULE_NAME = "member-registered-to-sync"
METRIC_NAMESPACE = "SalesforceEventRelay"

_PARTNER_SOURCE = re.compile(r"^aws\.partner/salesforce\.com/.+$")
_MY_DOMAIN = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_API_VERSION = re.compile(r"^v[0-9]+\.0$")
_SALESFORCE_ID = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


@dataclass(frozen=True)
class RelayConfig:
    """Deploy inputs. Read from CDK context (defaults in cdk.json, org values via -c)."""

    partner_event_source_name: str
    salesforce_my_domain: str
    salesforce_client_id: str
    salesforce_api_version: str = "v68.0"
    return_secret_name: str = "salesforce/member-relay"
    monitor_secret_name: str = "salesforce/relay-monitor"
    event_relay_config_id: str = ""
    enable_relay_health_check: bool = True
    alert_email: str = ""
    freshness_window_hours: int = 2

    @property
    def health_check_enabled(self) -> bool:
        return self.enable_relay_health_check and self.event_relay_config_id != ""

    @classmethod
    def from_context(cls, node: Node) -> "RelayConfig":
        def required(key: str) -> str:
            value = node.try_get_context(key)
            if value is None or str(value).strip() == "":
                raise ValueError(
                    f"Missing CDK context '{key}'. Pass it with -c {key}=... "
                    "(see README, Deploy step 9)."
                )
            return str(value).strip()

        def optional(key: str, default: Any) -> str:
            value = node.try_get_context(key)
            return str(default if value is None else value).strip()

        def invalid(key: str, value: str, expected: str) -> ValueError:
            return ValueError(f"Invalid CDK context '{key}': '{value}'. Expected {expected}.")

        partner = required("partnerEventSourceName")
        if not _PARTNER_SOURCE.match(partner):
            raise invalid("partnerEventSourceName", partner, "aws.partner/salesforce.com/<orgId>/<channelId>")

        my_domain = required("salesforceMyDomain")
        if not _MY_DOMAIN.match(my_domain):
            raise invalid("salesforceMyDomain", my_domain, "a host name like acme.my.salesforce.com, without https://")

        client_id = required("salesforceClientId")

        api_version = optional("salesforceApiVersion", cls.salesforce_api_version)
        if not _API_VERSION.match(api_version):
            raise invalid("salesforceApiVersion", api_version, "a version like v68.0")

        return_secret = optional("returnSecretName", cls.return_secret_name)
        if return_secret == "":
            raise invalid("returnSecretName", return_secret, "a Secrets Manager secret name")

        monitor_secret = optional("monitorSecretName", cls.monitor_secret_name)
        if monitor_secret == "":
            raise invalid("monitorSecretName", monitor_secret, "a Secrets Manager secret name")

        relay_id = optional("eventRelayConfigId", cls.event_relay_config_id)
        if relay_id != "" and not _SALESFORCE_ID.match(relay_id):
            raise invalid("eventRelayConfigId", relay_id, "empty or a 15 or 18 character Salesforce Id")

        enable_raw = optional("enableRelayHealthCheck", "true").lower()
        if enable_raw not in ("true", "false"):
            raise invalid("enableRelayHealthCheck", enable_raw, "true or false")

        alert_email = optional("alertEmail", cls.alert_email)
        if alert_email != "" and "@" not in alert_email:
            raise invalid("alertEmail", alert_email, "empty or an email address")

        hours_raw = optional("freshnessWindowHours", cls.freshness_window_hours)
        try:
            hours = int(hours_raw)
        except ValueError:
            raise invalid("freshnessWindowHours", hours_raw, "a whole number of hours, 1 or more") from None
        if hours < 1:
            raise invalid("freshnessWindowHours", hours_raw, "a whole number of hours, 1 or more")

        return cls(
            partner_event_source_name=partner,
            salesforce_my_domain=my_domain,
            salesforce_client_id=client_id,
            salesforce_api_version=api_version,
            return_secret_name=return_secret,
            monitor_secret_name=monitor_secret,
            event_relay_config_id=relay_id,
            enable_relay_health_check=enable_raw == "true",
            alert_email=alert_email,
            freshness_window_hours=hours,
        )


class MemberRelayStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, config: RelayConfig, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        cfg = config

        # 1. Associating the partner source == creating a bus with the same name.
        # CDK sets the bus Name to the event source name (passing both is an error).
        partner_bus = events.EventBus(
            self, "SalesforcePartnerBus",
            event_source_name=cfg.partner_event_source_name,  # bus name = source name
        )

        # 2. Custom bus for results going back to Salesforce
        results_bus = events.EventBus(self, "ResultsBus", event_bus_name="member-results")

        sync_dlq = self._dlq("MemberSyncDlq")
        rule_dlq = self._dlq("MemberRegisteredRuleDlq")
        return_dlq = self._dlq("ReturnDlq")

        # Our AWS application's member store. DESTROY is for the demo; keep data in real use.
        member_table = dynamodb.Table(
            self, "MemberTable",
            table_name="members",
            partition_key=dynamodb.Attribute(
                name="salesforce_member_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # 3. Lambda subscribed to Member_Registered__e only
        sync_log_group = self._log_group("MemberSyncLogGroup", "member-sync")
        member_sync = lambda_.Function(
            self, "MemberSyncFunction",
            function_name="member-sync",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="member_sync.handler",
            code=lambda_.Code.from_asset(
                str(LAMBDAS_DIR / "member_sync"), exclude=["__pycache__", "*.pyc"]
            ),
            memory_size=256,
            timeout=Duration.seconds(15),
            logging_format=lambda_.LoggingFormat.JSON,  # extra= fields become searchable keys
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            log_group=sync_log_group,
            environment={
                "RESULT_BUS_NAME": results_bus.event_bus_name,
                "MEMBER_TABLE_NAME": member_table.table_name,
            },
            retry_attempts=2,
            on_failure=destinations.SqsDestination(sync_dlq),  # code threw
        )
        results_bus.grant_put_events_to(member_sync)
        # Only the two calls the Lambda makes, on this table only.
        member_sync.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem"],
            resources=[member_table.table_arn],
        ))

        # Fixed rule name (MEMBER_REGISTERED_RULE_NAME), used by the freshness alarm.
        events.Rule(
            self, "MemberRegisteredRule",
            event_bus=partner_bus,
            rule_name=MEMBER_REGISTERED_RULE_NAME,
            event_pattern=events.EventPattern(
                source=events.Match.prefix("aws.partner/salesforce.com"),
                detail_type=["Member_Registered__e"],
            ),
            targets=[targets.LambdaFunction(
                member_sync,
                dead_letter_queue=rule_dlq,
                retry_attempts=185,
                max_event_age=Duration.hours(24),
            )],
        )

        # 4. Return path: OAuth client-credentials connection to Salesforce.
        # The secret is a CloudFormation dynamic reference, resolved at deploy
        # time, so it must exist before cdk deploy.
        connection = events.Connection(
            self, "SalesforceConnection",
            authorization=events.Authorization.oauth(
                authorization_endpoint=f"https://{cfg.salesforce_my_domain}/services/oauth2/token",
                http_method=events.HttpMethod.POST,
                client_id=cfg.salesforce_client_id,
                client_secret=SecretValue.secrets_manager(
                    cfg.return_secret_name, json_field="client_secret"
                ),
                body_parameters={
                    "grant_type": events.HttpParameter.from_string("client_credentials"),
                },
            ),
        )

        member_synced_destination = events.ApiDestination(
            self, "MemberSyncedDestination",
            connection=connection,
            endpoint=(
                f"https://{cfg.salesforce_my_domain}/services/data/"
                f"{cfg.salesforce_api_version}/sobjects/Member_Synced__e"
            ),
            http_method=events.HttpMethod.POST,
            rate_limit_per_second=20,
        )

        events.Rule(
            self, "MemberSyncedRule",
            event_bus=results_bus,
            rule_name="member-synced-to-salesforce",
            event_pattern=events.EventPattern(
                source=["com.example.member-sync"],
                detail_type=["MemberSynced"],
            ),
            targets=[targets.ApiDestination(
                member_synced_destination,
                event=events.RuleTargetInput.from_event_path("$.detail"),  # POST body = just the event fields
                dead_letter_queue=return_dlq,
                retry_attempts=185,
                max_event_age=Duration.hours(24),
            )],
        )

        # 5. Monitoring: alarm topic, relay health check, freshness alarm
        alarm_topic = sns.Topic(self, "AlarmTopic")
        if cfg.alert_email:
            alarm_topic.add_subscription(subscriptions.EmailSubscription(cfg.alert_email))
        alarm_action = cw_actions.SnsAction(alarm_topic)

        if cfg.health_check_enabled:
            self._relay_health_check(cfg, alarm_action)

        freshness_alarm = cloudwatch.Alarm(
            self, "MemberRegisteredFreshnessAlarm",
            alarm_description=(
                "No Member_Registered__e event matched the partner-bus rule within the freshness window. "
                "Noisy on its own. Read it together with RelayHealthy. CloudWatch alarms can't be "
                "restricted to business hours natively. Use metric math on the alarm, or enable and "
                "disable it on a schedule."
            ),
            metric=cloudwatch.Metric(
                namespace="AWS/Events",
                metric_name="MatchedEvents",
                dimensions_map={
                    "EventBusName": cfg.partner_event_source_name,
                    "RuleName": MEMBER_REGISTERED_RULE_NAME,
                },
                statistic="Sum",
                period=Duration.hours(1),
            ),
            evaluation_periods=cfg.freshness_window_hours,
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        freshness_alarm.add_alarm_action(alarm_action)

        CfnOutput(self, "PartnerBusName", value=partner_bus.event_bus_name)
        CfnOutput(self, "ResultsBusName", value=results_bus.event_bus_name)
        CfnOutput(
            self, "MemberSyncDlqUrl", value=sync_dlq.queue_url,
            description="Events Lambda received but the code threw on, after async retries",
        )
        CfnOutput(
            self, "MemberRegisteredRuleDlqUrl", value=rule_dlq.queue_url,
            description="Events EventBridge could not deliver to the Lambda",
        )
        CfnOutput(
            self, "ReturnDlqUrl", value=return_dlq.queue_url,
            description="Events the API destination could not deliver to Salesforce",
        )
        CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn)
        CfnOutput(self, "MemberSyncLogGroupName", value=sync_log_group.log_group_name)
        CfnOutput(self, "MemberTableName", value=member_table.table_name)

    def _dlq(self, construct_id: str) -> sqs.Queue:
        return sqs.Queue(
            self, construct_id,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

    def _log_group(self, construct_id: str, function_name: str) -> logs.LogGroup:
        # Explicit log group so the name is fixed and cdk destroy removes it.
        return logs.LogGroup(
            self, construct_id,
            log_group_name=f"/aws/lambda/{function_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _relay_health_check(self, cfg: RelayConfig, alarm_action: cw_actions.SnsAction) -> None:
        # Uses its OWN External Client App and run-as user (a client-credentials app
        # has a single Run As user). The secret is read at runtime, so a missing
        # secret doesn't fail the deploy. It reports RelayHealthy = 0 instead.
        health_check = lambda_.Function(
            self, "RelayHealthCheckFunction",
            function_name="relay-health-check",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="relay_health_check.handler",
            code=lambda_.Code.from_asset(
                str(LAMBDAS_DIR / "relay_health_check"), exclude=["__pycache__", "*.pyc"]
            ),
            memory_size=256,
            timeout=Duration.seconds(30),
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            log_group=self._log_group("RelayHealthCheckLogGroup", "relay-health-check"),
            environment={
                "SF_MY_DOMAIN": cfg.salesforce_my_domain,
                "SF_API_VERSION": cfg.salesforce_api_version,
                "MONITOR_SECRET_NAME": cfg.monitor_secret_name,
                "RELAY_ID": cfg.event_relay_config_id,
                "METRIC_NAMESPACE": METRIC_NAMESPACE,
            },
        )

        secretsmanager.Secret.from_secret_name_v2(
            self, "MonitorSecret", cfg.monitor_secret_name
        ).grant_read(health_check)

        # PutMetricData has no resource-level ARNs. Limit it to our namespace.
        health_check.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},
        ))

        scheduler.Schedule(
            self, "RelayHealthCheckSchedule",
            schedule=scheduler.ScheduleExpression.rate(Duration.minutes(5)),
            target=scheduler_targets.LambdaInvoke(health_check, retry_attempts=0),
        )

        unhealthy_alarm = cloudwatch.Alarm(
            self, "RelayUnhealthyAlarm",
            alarm_description=(
                "Event Relay is not RUNNING (or the health check could not confirm it) "
                "for two consecutive checks."
            ),
            metric=cloudwatch.Metric(
                namespace=METRIC_NAMESPACE,
                metric_name="RelayHealthy",
                dimensions_map={"RelayId": cfg.event_relay_config_id},
                statistic="Minimum",
                period=Duration.minutes(5),
            ),
            evaluation_periods=2,
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        unhealthy_alarm.add_alarm_action(alarm_action)
        unhealthy_alarm.add_ok_action(alarm_action)
