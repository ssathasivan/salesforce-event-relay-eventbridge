#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.customer_relay_stack import CustomerRelayStack, RelayConfig

app = cdk.App()
config = RelayConfig.from_context(app.node)

CustomerRelayStack(
    app,
    "sf-customer-relay",
    config=config,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description=(
        "Salesforce Event Relay to AWS: partner event bus, rule, SQS queue and DLQ, "
        "Python Lambda, DynamoDB"
    ),
)

app.synth()
