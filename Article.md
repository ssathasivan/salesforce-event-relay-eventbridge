---
title: "Replacing Boomi: Streaming Salesforce Events to AWS with Event Relay and EventBridge"
published: false
description: "An architecture for pushing Salesforce events into AWS with Salesforce Event Relay, the EventBridge partner integration, SQS, Lambda, and DynamoDB. No middleware."
tags: aws, salesforce, eventbridge, serverless
series: The Developer Journal
---

## Background

In our organization, member registration and payments happen in Salesforce, and the relevant information was synced to the applications hosted in AWS through Boomi. Over time, this turned out to be an expensive solution because of the licensing cost for Boomi.

This article describes how we used [Salesforce Event Relay](https://help.salesforce.com/s/articleView?id=platform.ev_relay_intro.htm&type=5) and the [Amazon EventBridge partner integration for Salesforce](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-saas-salesforce.html) to push events from Salesforce to EventBridge.

## Architecture

![Architecture](https://raw.githubusercontent.com/ssathasivan/salesforce-event-relay-eventbridge/main/Architecture.png)

The flow runs left to right. Salesforce and Event Relay are configuration on the Salesforce side. Everything inside the AWS account is deployed with CDK.

### Salesforce: Member Created

When a member registers, Salesforce publishes a **platform event**. A platform event is Salesforce's version of a message on an event bus. It has a name and a schema, like `Member_Registered__e` with fields `Member_Id__c`, `Email__c`, and `Membership_Type__c`. The `__e` and `__c` suffixes are Salesforce naming conventions for custom events and custom fields.

An Apex trigger (Salesforce's equivalent of a database trigger) publishes the event when a member record is inserted. A Flow, Salesforce's low-code automation, can do the same.

The event is set to publish after commit. If the registration fails and rolls back, nothing is sent to AWS.

### Salesforce Event Relay

Event Relay is a Salesforce feature that forwards platform events to EventBridge. There is no code to write and nothing to host. Salesforce subscribes to its own event bus and delivers to AWS.

Three Salesforce pieces make up a relay:

- A **channel**. This is a group of event types, similar to a topic. You add `Member_Registered__e` to it as a channel member.
- A **named credential**. This is Salesforce's configuration for an external endpoint. For Event Relay it holds only the AWS account ID and region. No AWS access keys or IAM roles are stored in Salesforce.
- The **relay** itself, which links the channel to the named credential.

### EventBridge partner integration and event bus

Salesforce is a registered EventBridge partner. When you create a relay, a **partner event source** appears in your AWS account in a pending state. You associate it with a **partner event bus**, and events start to arrive.

One relay maps to one channel, one partner event source, and one partner bus.

### EventBridge rule: filter and route by subsystem

Each event arrives with `detail-type` set to the Salesforce event name. Rules match on it, so they route without reading the payload.

We use one rule per subsystem. Member events go to the membership queue. Payment events, when we add them, get their own rule and queue. Each subsystem can then be deployed, scaled, and paused on its own.

### Amazon SQS queue and DLQ

The rule doesn't invoke the Lambda directly. It sends events to an SQS queue, and the Lambda reads from the queue. This is a pattern we follow for every event-driven consumer:

- **Buffering.** A burst of registrations waits in the queue instead of driving Lambda concurrency up. The event source mapping's maximum concurrency caps how hard the Lambda hits downstream systems.
- **Retries.** If the Lambda fails, the message becomes visible again after the visibility timeout and is retried.
- **A dead-letter queue.** After a set number of failed receives, SQS moves the message to the DLQ. Nothing is lost, and the message can be redriven to the main queue after a fix.
- **Partial batch failures.** The Lambda reports which messages in a batch failed, so only those are retried.

The DLQ is the main operational signal. An alarm on its depth tells us something is failing.

### Lambda and DynamoDB

The Lambda reads the member details from the event and writes them to DynamoDB. In our case, that is where the AWS applications read member data from.

## What arrives in AWS

This is a relayed event as EventBridge delivers it:

```json
{
  "version": "0",
  "id": "6f33d717-8e35-4488-5690-89e681b5737c",
  "detail-type": "Member_Registered__e",
  "source": "aws.partner/salesforce.com/00D.../0YL...",
  "account": "123456789012",
  "time": "2026-10-13T19:23:23Z",
  "region": "us-east-1",
  "resources": [],
  "detail": {
    "payload": {
      "CreatedById": "005...",
      "CreatedDate": "2026-10-13T19:23:22.686Z",
      "Member_Id__c": "a005g00000AbCdEAAV",
      "Email__c": "member@example.com",
      "Membership_Type__c": "Standard"
    },
    "schemaId": "mVBhpA7tU9MCtBEbqjycBQ",
    "id": "118dab75-7b45-498e-8478-52d0324c1060"
  }
}
```

- `source` contains the Salesforce org ID and channel ID.
- `detail-type` is the Salesforce event name, which is what the rule matches on.
- The fields are under `detail.payload`, with their Salesforce API names.
- The top-level `id` comes from EventBridge. `detail.id` comes from Salesforce.

## Design decisions

### Recovery after an outage

If the relay hits an error it can't resume from, a recovery setting decides what happens ([Error Recovery](https://help.salesforce.com/s/articleView?id=platform.ev_relay_error_recovery.htm&language=en_US&type=5)):

- `LATEST` (the default) skips events published during the outage.
- `EARLIEST` re-sends everything still retained on the Salesforce event bus, up to 72 hours.

We use `EARLIEST`, because a missed registration means a member who can't use our applications. The cost is duplicates.

### Duplicates

Between `EARLIEST` recovery, EventBridge retries, and SQS retries, the same event can be delivered more than once. The Lambda writes to DynamoDB with a conditional put keyed on the Salesforce member ID. A second delivery of the same member doesn't create a second record.

### Small payloads

Salesforce documents a 256 KB limit for relayed events, and larger events are dropped without an error. We send IDs and the few fields the AWS side needs. If a consumer needs more, it can query Salesforce by ID.

### Monitoring the relay

When a relay goes into an error state, Salesforce retries, but it doesn't alert anyone ([Relay status](https://help.salesforce.com/s/articleView?id=platform.ev_relay_status.htm&language=en_US&type=5)). From AWS, a broken relay looks the same as a quiet day.

We watch two things:

- The relay status, which Salesforce exposes through a queryable object called `EventRelayFeedback`. A scheduled Lambda checks it and publishes a CloudWatch metric.
- The DLQ depth on the AWS side.

## Limits to know

- Editions: Enterprise, Unlimited, or Developer ([Developer Guide](https://developer.salesforce.com/docs/platform/event-relay/guide/intro.html)).
- Only high-volume custom platform events, Change Data Capture events, and Real-Time Event Monitoring events can be relayed ([Event Type, Size, and Features](https://help.salesforce.com/s/articleView?id=platform.ev_relay_feature_support.htm&language=en&type=5)).
- AWS opt-in regions aren't supported ([Considerations](https://help.salesforce.com/s/articleView?id=platform.ev_relay_considerations.htm&language=en_US&type=5)).
- Sandbox refreshes don't copy relays, so each environment needs its own.
- Relayed events count toward the org's daily event delivery allocation, which is shared with other subscribers ([Allocations](https://help.salesforce.com/s/articleView?id=platform.ev_relay_allocations.htm&language=en_US&type=5)).

## Implementation

The [README](https://github.com/ssathasivan/salesforce-event-relay-eventbridge#readme) walks through the setup in order: the Salesforce metadata, the channel, the named credential, the relay, and the CDK stack for the AWS side.
