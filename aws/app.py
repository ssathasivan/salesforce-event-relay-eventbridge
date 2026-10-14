#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.member_relay_stack import MemberRelayStack, RelayConfig

app = cdk.App()
config = RelayConfig.from_context(app.node)

MemberRelayStack(
    app,
    "sf-member-relay",
    config=config,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description=(
        "Salesforce Event Relay round trip (partner bus, Python Lambda, "
        "API destination back to Salesforce, relay monitoring)"
    ),
)

app.synth()
