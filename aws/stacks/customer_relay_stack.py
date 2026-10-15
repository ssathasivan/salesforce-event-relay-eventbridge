"""Salesforce Event Relay to AWS: partner event bus, EventBridge rule, SQS queue
with a DLQ, Python Lambda, and DynamoDB."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
)
from constructs import Construct, Node

LAMBDAS_DIR = Path(__file__).resolve().parent.parent / "lambdas"

FUNCTION_TIMEOUT = Duration.seconds(30)
# Lambda's docs recommend a visibility timeout of at least 6x the function timeout.
QUEUE_VISIBILITY_TIMEOUT = Duration.seconds(30 * 6)
# Receives before SQS moves a message to the DLQ. Lambda's docs recommend at least 5.
MAX_RECEIVE_COUNT = 5

_PARTNER_SOURCE = re.compile(r"^aws\.partner/salesforce\.com/.+$")


@dataclass(frozen=True)
class RelayConfig:
    """Deploy inputs, read from CDK context (-c key=value)."""

    partner_event_source_name: str
    alert_email: str = ""

    @classmethod
    def from_context(cls, node: Node) -> "RelayConfig":
        partner = str(node.try_get_context("partnerEventSourceName") or "").strip()
        if partner == "":
            raise ValueError(
                "Missing CDK context 'partnerEventSourceName'. Pass it with "
                "-c partnerEventSourceName=aws.partner/salesforce.com/<orgId>/<channelId>"
            )
        if not _PARTNER_SOURCE.match(partner):
            raise ValueError(
                f"Invalid CDK context 'partnerEventSourceName': '{partner}'. "
                "Expected aws.partner/salesforce.com/<orgId>/<channelId>."
            )

        alert_email = str(node.try_get_context("alertEmail") or "").strip()
        if alert_email != "" and "@" not in alert_email:
            raise ValueError(
                f"Invalid CDK context 'alertEmail': '{alert_email}'. Expected an email address."
            )

        return cls(partner_event_source_name=partner, alert_email=alert_email)


class CustomerRelayStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, config: RelayConfig, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. Partner event bus. Creating a bus with the partner event source name
        #    is what "Associate with event bus" does in the console.
        partner_bus = events.EventBus(
            self, "SalesforcePartnerBus",
            event_source_name=config.partner_event_source_name,  # bus name = source name
        )

        # 2. SQS queue with a DLQ for messages that can't be processed.
        dlq = sqs.Queue(
            self, "CustomerCreatedDlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        queue = sqs.Queue(
            self, "CustomerCreatedQueue",
            visibility_timeout=QUEUE_VISIBILITY_TIMEOUT,
            retention_period=Duration.days(4),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=MAX_RECEIVE_COUNT),
        )

        # 3. Rule: route Customer_Created__e from the partner bus to the queue.
        #    If EventBridge can't deliver to the queue, the event goes to the same DLQ.
        events.Rule(
            self, "CustomerCreatedRule",
            event_bus=partner_bus,
            rule_name="customer-created-to-queue",
            event_pattern=events.EventPattern(
                source=events.Match.prefix("aws.partner/salesforce.com"),
                detail_type=["Customer_Created__e"],
            ),
            targets=[targets.SqsQueue(
                queue,
                dead_letter_queue=dlq,
                retry_attempts=185,
                max_event_age=Duration.hours(24),
            )],
        )

        # 4. DynamoDB table the AWS applications read from.
        #    DESTROY suits a demo. Use RETAIN for real data.
        table = dynamodb.Table(
            self, "CustomerTable",
            table_name="customers",
            partition_key=dynamodb.Attribute(name="account_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # 5. Lambda that reads the queue and writes to DynamoDB.
        log_group = logs.LogGroup(
            self, "CustomerSyncLogGroup",
            log_group_name="/aws/lambda/customer-sync",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        customer_sync = lambda_.Function(
            self, "CustomerSyncFunction",
            function_name="customer-sync",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="customer_sync.handler",
            code=lambda_.Code.from_asset(
                str(LAMBDAS_DIR / "customer_sync"), exclude=["__pycache__", "*.pyc"]
            ),
            memory_size=256,
            timeout=FUNCTION_TIMEOUT,
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            log_group=log_group,
            environment={"CUSTOMER_TABLE_NAME": table.table_name},
        )
        # Only the one call the Lambda makes, on this table only.
        customer_sync.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[table.table_arn],
        ))
        customer_sync.add_event_source(event_sources.SqsEventSource(
            queue,
            batch_size=10,
            report_batch_item_failures=True,  # retry only the failed messages
            max_concurrency=5,                # cap the load on DynamoDB
        ))

        # 6. Alarm when anything lands in the DLQ.
        alarm_topic = sns.Topic(self, "AlarmTopic")
        if config.alert_email:
            alarm_topic.add_subscription(subscriptions.EmailSubscription(config.alert_email))
        dlq_alarm = cloudwatch.Alarm(
            self, "CustomerCreatedDlqAlarm",
            alarm_description="Customer_Created__e messages are in the DLQ and need attention.",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            evaluation_periods=1,
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))

        CfnOutput(self, "PartnerBusName", value=partner_bus.event_bus_name)
        CfnOutput(self, "QueueUrl", value=queue.queue_url)
        CfnOutput(self, "DlqUrl", value=dlq.queue_url)
        CfnOutput(self, "CustomerTableName", value=table.table_name)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn)
