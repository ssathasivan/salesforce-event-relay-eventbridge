# Salesforce Event Relay to Amazon EventBridge

A two-way integration between Salesforce and AWS with no middleware. When a `Member__c` record is created, [Salesforce Event Relay](https://help.salesforce.com/s/articleView?id=platform.ev_relay_intro.htm&type=5) streams a `Member_Registered__e` platform event to an EventBridge partner bus, a Python Lambda registers the member in our AWS application (a DynamoDB table) and assigns an external member id, and an [EventBridge API destination](https://help.salesforce.com/s/articleView?id=platform.ev_relay_send_event_to_salesforce.htm&language=en_US&type=5) publishes `Member_Synced__e` back into Salesforce. The AWS side is an AWS CDK (Python) app. The Salesforce side is an SFDX project plus bash scripts for the parts that only the Tooling API can do.

Full write-up: [dev.to article](https://dev.to/<your-devto-username>/<article-slug>) <!-- Replace with the published dev.to URL -->

```bash
git clone https://github.com/ssathasivan/salesforce-event-relay-eventbridge.git
cd salesforce-event-relay-eventbridge
```

## Architecture

```
 Salesforce org                                         AWS account (us-east-1)
┌───────────────────────────────────┐                  ┌──────────────────────────────────────────────┐
│ Member__c (after insert)          │                  │ Partner event bus                            │
│   │ Apex trigger                  │                  │ aws.partner/salesforce.com/<orgId>/<chanId>  │
│   ▼                               │   Event Relay    │   │ Rule: detail-type = Member_Registered__e │
│ Member_Registered__e ─► Channel ──┼─────────────────►│   ▼                                          │
│   (high-volume)  Member_Relay__chn│  (partner API,   │ Lambda: member-sync (Python)                 │
│                                   │   TLS)           │   ├─► DynamoDB: members (conditional put)    │
│                                   │                  │   │ PutEvents                                │
│                                   │                  │   ▼                                          │
│ Apex trigger ◄─ Member_Synced__e ◄┼──────────────────┼── Custom bus: member-results                 │
│   │                               │  REST POST       │     Rule ──► API destination (OAuth CC)      │
│   ▼                               │  /sobjects/      │              └─► DLQ (SQS) on failure        │
│ Member__c.External_Member_Id__c,  │  Member_Synced__e│                                              │
│   Sync_Status__c                  │                  │                                              │
└───────────────────────────────────┘                  └──────────────────────────────────────────────┘
```

- `Member_Synced__e` is not a member of the relay channel. If it were, every result would relay back to AWS in a loop.
- The Lambda writes to a separate custom bus instead of calling Salesforce. OAuth, rate limiting, retries, and the DLQ stay in EventBridge configuration instead of Python code.

Writing `External_Member_Id__c` and `Sync_Status__c` back doesn't start another round trip. The publisher fires on insert only, and the write-back is an update. Member updates are out of scope for this example.

## Repository layout

```
salesforce-event-relay-eventbridge/
├── README.md
├── LICENSE
├── .gitignore
├── .gitattributes
├── salesforce/                        SFDX project
│   ├── sfdx-project.json
│   ├── .forceignore
│   ├── force-app/main/default/
│   │   ├── objects/                   Member__c, Member_Registered__e, Member_Synced__e, Relay_Error_Log__c
│   │   ├── triggers/                  MemberRegisteredPublisher, MemberSyncedSubscriber
│   │   ├── classes/                   RelayErrorLog, MemberRelayTriggersTest
│   │   └── permissionsets/            Member_Relay_Integration, Relay_Monitor, Member_Relay_Admin
│   └── scripts/
│       ├── env.sh                     shared variables and curl helpers (source it)
│       ├── 01-create-channel.sh       channel + channel member (Tooling API)
│       ├── 02-create-relay.sh         relay in STOP state
│       ├── 03-get-partner-source.sh   partner event source name
│       ├── 04-start-relay.sh          set the relay to RUN (or STOP)
│       ├── 05-relay-status.sh         EventRelayFeedback status
│       ├── 06-create-test-member.sh   insert one Member__c
│       ├── 07-teardown-relay.sh       stop and delete relay, member, channel
│       └── test-client-credentials.sh test an External Client App token flow
└── aws/                               AWS CDK app (Python)
    ├── app.py                         stack sf-member-relay
    ├── cdk.json                       app command and context defaults
    ├── requirements.txt               aws-cdk-lib, constructs (pinned)
    ├── requirements-dev.txt           + pytest, boto3 (pinned)
    ├── pytest.ini
    ├── stacks/
    │   └── member_relay_stack.py      RelayConfig (context validation) + MemberRelayStack
    ├── lambdas/
    │   ├── member_sync/member_sync.py
    │   └── relay_health_check/relay_health_check.py
    └── tests/
        ├── conftest.py
        ├── fixtures/relayed_event.json
        ├── test_member_sync.py
        ├── test_relay_health_check.py
        └── test_stack.py              CDK assertion tests
```

Both Lambdas run on `python3.14` on arm64. Every Python runtime has a fixed [deprecation date](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html), so start on the newest one. The code uses only `boto3` and the standard library, so CDK zips each folder as is. No bundling and no Docker. Logs use the JSON log format, and the fields passed through `extra` become searchable keys ([Python logging in Lambda](https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html)).

## Prerequisites and constraints

Check these before you start, because several fail quietly:

| Constraint | Detail |
|---|---|
| Editions | Enterprise, Unlimited, Developer ([Event Relay Developer Guide](https://developer.salesforce.com/docs/platform/event-relay/guide/intro.html)) |
| Event types | Custom high-volume platform events, Real-Time Event Monitoring events, and Change Data Capture events. Standard platform events and standard-volume custom events are not supported ([Event Type, Size, and Features](https://help.salesforce.com/s/articleView?id=platform.ev_relay_feature_support.htm&language=en&type=5)) |
| Payload size | Salesforce's relay docs still say events over 256 KB aren't delivered ([same page](https://help.salesforce.com/s/articleView?id=platform.ev_relay_feature_support.htm&language=en&type=5)), even though AWS [raised the EventBridge event bus limit to 1 MB](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-eventbridge-increases-event-payload-size-256-kb-1-mb/) in January 2026. Design for 256 KB until Salesforce documents the higher limit for relays |
| Not supported | Salesforce Private Connect; Developer Edition orgs with a namespace |
| AWS regions | Default regions only. Opt-in regions aren't supported ([Considerations](https://help.salesforce.com/s/articleView?id=platform.ev_relay_considerations.htm&language=en_US&type=5)) |
| Max relays | 200 per org ([Allocations](https://help.salesforce.com/s/articleView?id=platform.ev_relay_allocations.htm&language=en_US&type=5)) |
| Permission | Customize Application |

Relayed deliveries count against the same 24-hour event delivery allocation as CometD, Pub/Sub API, and `empApi` subscribers ([Allocations](https://help.salesforce.com/s/articleView?id=platform.ev_relay_allocations.htm&language=en_US&type=5)). Check Company Information → usage before you go to production.

Tools:

- Salesforce CLI (`sf`), logged in to the target org
- `curl` and `jq`
- AWS CLI v2 with credentials for the target account
- Node.js LTS and the AWS CDK CLI 2.1133.0 (`npm install -g aws-cdk@2.1133.0`)
- Python 3.10 or later (tested on 3.14)
- bash. On Windows, use Git Bash or WSL.

The stack uses fixed names (`member-sync`, `member-results`, `members`, `/aws/lambda/member-sync`, and so on), so the commands below work without stack lookups. The tradeoff is one copy of the stack per account and region.

## Environment setup

Log in once, then source `env.sh` from the repo root in every new shell:

```bash
sf org login web --alias relay-dev --instance-url https://acme.my.salesforce.com
export SF_ORG=relay-dev
source salesforce/scripts/env.sh
```

| Variable | Set by | Meaning |
|---|---|---|
| `SF_ORG` | you (default `relay-dev`) | sf CLI org alias |
| `SF_API_VERSION` | `env.sh` (default `v68.0`) | One Salesforce API version for every call in the repo |
| `AWS_REGION` | `env.sh` (default `us-east-1`) | Target region. Must not be an opt-in region |
| `AWS_ACCOUNT` | `env.sh` | From `aws sts get-caller-identity` |
| `STACK_NAME` | `env.sh` (default `sf-member-relay`) | CloudFormation stack name. The CDK app fixes it in `aws/app.py`, so keep the default |
| `SF_TOKEN`, `SF_URL` | `env.sh` | Session token and instance URL from `sf org display` |
| `SF_MY_DOMAIN`, `CID` | you, in step 3 | My Domain host and return-path consumer key |
| `RELAY_ID` | you, from step 7 output | `EventRelayConfig` Id |
| `PARTNER_SOURCE` | you, from step 8 output | Partner event source name |

Keep the API version current. Check the org's latest and export it before sourcing `env.sh`:

```bash
curl -s "$SF_URL/services/data/" | jq -r '.[-1].version'   # e.g. 68.0
export SF_API_VERSION=v68.0
source salesforce/scripts/env.sh
```

The same value goes to CDK as `-c salesforceApiVersion="$SF_API_VERSION"` in step 9, so the scripts, the API destination, and the health check all use one version.

The session token expires. If calls start returning `401 INVALID_SESSION_ID`, source `env.sh` again.

## Deploy

The order matters. Both External Client Apps and both secrets must exist before the AWS stack is deployed in step 9.

### Step 1: Deploy the Salesforce metadata

```bash
(cd salesforce && sf project deploy start --source-dir force-app --target-org "$SF_ORG")
```

This deploys both platform events (`HighVolume`, `PublishAfterCommit`), the `Member__c` object with `Email__c`, `Membership_Type__c`, `External_Member_Id__c`, and `Sync_Status__c`, `Relay_Error_Log__c`, both triggers, the Apex classes, and three permission sets. [Event relays aren't copied to sandboxes](https://help.salesforce.com/s/articleView?id=platform.ev_relay_considerations.htm&language=en_US&type=5), so repeat steps 1 to 10 per environment.

### Step 2: Create the users and assign permission sets

Create two users in Setup:

- An API-only integration user for the return path. Assign `Member_Relay_Integration` (API Enabled and Create on `Member_Synced__e`, nothing more). If you use the Salesforce Integration user license, assign the Salesforce API Integration permission set license first.
- A monitoring user for the health check. Assign `Relay_Monitor` (API Enabled and View Setup).

Assign `Member_Relay_Admin` to yourself. It grants Create on `Member__c`, which the end-to-end test needs. Fields deployed from source get no field-level security, and without it the checks in the end-to-end test fail with "No such column".

```bash
sf org assign permset --name Member_Relay_Integration --on-behalf-of integration.user@example.com --target-org "$SF_ORG"
sf org assign permset --name Relay_Monitor --on-behalf-of relay.monitor@example.com --target-org "$SF_ORG"
sf org assign permset --name Member_Relay_Admin --target-org "$SF_ORG"
```

### Step 3: Return-path External Client App and secret

The Event Relay docs still say to [create a connected app](https://help.salesforce.com/s/articleView?id=platform.ev_relay_send_event_to_salesforce.htm&language=en_US&type=5). As of Spring '26, [new connected apps can't be created](https://help.salesforce.com/s/articleView?id=005228017&language=en_US&type=1) unless Salesforce Support enables it. Use an External Client App (ECA) instead ([Configure an ECA for Client Credentials](https://help.salesforce.com/s/articleView?id=xcloud.configure_client_credentials_flow_for_external_client_apps.htm&language=en_US&type=5)):

1. Setup → External Client App Manager → New. Set Distribution State to Local.
2. Enable OAuth with the `api` scope, then select Enable Client Credentials Flow.
3. In Policies, set Run As to the integration user from step 2.
4. Copy the consumer key and consumer secret.

```bash
export SF_MY_DOMAIN=acme.my.salesforce.com
export CID="<consumer-key>"

# Test the token flow before you involve EventBridge.
./salesforce/scripts/test-client-credentials.sh "$SF_MY_DOMAIN" "$CID"
```

The test script reads the consumer secret from a hidden prompt, so it never lands in your shell history or the process list. A token here splits "OAuth is wrong" from "EventBridge is wrong" later.

Store the secret in Secrets Manager as `{"client_secret": "..."}`. Write it to a local file instead of the command line, then delete the file. `secret.json` is in `.gitignore`.

```bash
read -rs -p "Consumer secret: " CSECRET; echo
jq -n --arg s "$CSECRET" '{client_secret: $s}' > secret.json
unset CSECRET
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name salesforce/member-relay --secret-string file://secret.json
rm secret.json
```

The client-credentials flow [doesn't issue refresh tokens](https://help.salesforce.com/s/articleView?language=en_US&id=xcloud.remoteaccess_oauth_client_credentials_flow.htm&type=5). That's fine, because the EventBridge connection requests a new token when it needs one.

### Step 4: Monitoring External Client App and secret

A client-credentials app has exactly one Run As user, so the health check can't share the publish-only integration user. Create a second ECA ("Relay Monitor") the same way as step 3, with Run As set to the monitoring user. Test it with the same script:

```bash
./salesforce/scripts/test-client-credentials.sh "$SF_MY_DOMAIN" "<monitor-consumer-key>"
```

This secret holds both values as `{"client_id": "...", "client_secret": "..."}`:

```bash
read -r -p "Monitor consumer key: " MON_CID
read -rs -p "Monitor consumer secret: " MON_CSECRET; echo
jq -n --arg id "$MON_CID" --arg s "$MON_CSECRET" '{client_id: $id, client_secret: $s}' > monitor-secret.json
unset MON_CID MON_CSECRET
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name salesforce/relay-monitor --secret-string file://monitor-secret.json
rm monitor-secret.json
```

The health check reads this secret at runtime. If it's missing, the deploy still succeeds, the check reports `RelayHealthy = 0`, and the alarm fires.

### Step 5: Create the channel and channel member

```bash
./salesforce/scripts/01-create-channel.sh
```

Expect two JSON responses with `"success": true`. A relay points at a channel, and the channel's members decide which event types flow through it. Setup has no UI for this ([Create a Channel for a Custom Platform Event](https://help.salesforce.com/s/articleView?id=platform.ev_relay_create_channel_pe.htm&language=en_US&type=5)).

Watch the naming. The channel is `Member_Relay__chn` (double underscore), but the member's `FullName` uses single underscores (`Member_Relay_chn_Member_Registered_e`), as the [PlatformEventChannelMember reference](https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_platformeventchannelmember.htm) requires. For Change Data Capture, use `"channelType": "data"` ([CDC channel member](https://help.salesforce.com/s/articleView?id=platform.ev_relay_create_channel_member_cdc.htm&language=en_US&type=5)) and consider [filter expressions and enriched fields](https://developer.salesforce.com/docs/platform/change-data-capture/guide/cdc-filter-metadata-api-add-filter.html) per member.

### Step 6: Create the named credential

The named credential holds only your AWS account ID and region. Salesforce uses the EventBridge partner APIs, so [no AWS authentication is involved](https://help.salesforce.com/s/articleView?id=platform.ev_relay_security.htm&language=en_US&type=5). You don't need an IAM role that trusts Salesforce.

In Setup → Named Credentials → New ▾ → New Legacy ([Create a Named Credential](https://help.salesforce.com/s/articleView?id=platform.ev_relay_create_named_credential.htm&language=en_US&type=5)):

| Field | Value |
|---|---|
| Label / Name | `AWS_US_EAST_1` |
| URL | `arn:aws:US-EAST-1:123456789012` (your account) |
| Identity Type, Auth Protocol | Leave defaults |
| Generate Authorization Header | Leave checked |

The region must be **UPPERCASE**. You also can't change the credential once a relay uses it. The fix is to delete the relay, edit the credential, and recreate the relay. Name credentials after the account and region, not the use case. If you use another name, pass it to `02-create-relay.sh`.

### Step 7: Create the relay

```bash
./salesforce/scripts/02-create-relay.sh            # or: 02-create-relay.sh AWS_PROD_US_EAST_1
export RELAY_ID=7k2XXXXXXXXXXXXXXX                 # the line the script printed
```

From the [EventRelayConfig reference](https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_eventrelayconfig.htm) and its [Metadata API twin](https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_eventrelayconfig.htm):

- `STOP` is the only valid `state` on create.
- After creation you can update only `state` and `relayOption`. A wrong channel or credential means delete and recreate.
- `relayOption` is a JSON string inside JSON, so the escaping in the script is intentional.

The script sets `ReplayRecovery` to `EARLIEST`, so after an unrecoverable error the relay re-sends what is still in the event bus instead of skipping it ([error recovery option](https://help.salesforce.com/s/articleView?id=platform.ev_relay_error_recovery.htm&language=en_US&type=5)). Expect duplicates during recovery.

### Step 8: Get the partner event source

Creating the relay makes Salesforce register a partner event source in your AWS account in `PENDING` state ([Create an Event Relay](https://help.salesforce.com/s/articleView?id=platform.ev_relay_create_ui.htm&language=en_US&type=5)). The script reads its name from `EventRelayFeedback` with the data API ([Monitoring Event Relays](https://help.salesforce.com/s/articleView?id=platform.ev_relay_soql_queries.htm&language=en_US&type=5)):

```bash
./salesforce/scripts/03-get-partner-source.sh
```

```
export PARTNER_SOURCE=aws.partner/salesforce.com/00D5g000000XXXXEAA/0YL5g000000XXXXGAA
Run the line above, then pass it to CDK as -c partnerEventSourceName="$PARTNER_SOURCE".
```

The field fills in after a short delay. If it's empty, wait a minute and re-run.

Run the printed `export` line. The script then confirms the source from the AWS side with `aws events list-event-sources`. It stays `PENDING` until step 9:

```
--------------------------------------------------------------------------------
|                               ListEventSources                               |
+---------------------------------------------------------------------+--------+
|                                Name                                 | State  |
+---------------------------------------------------------------------+--------+
|  aws.partner/salesforce.com/00D5g000000XXXXEAA/0YL5g000000XXXXGAA   | PENDING|
+---------------------------------------------------------------------+--------+
```

### Step 9: Deploy the AWS stack with CDK

Set up the CDK app once:

```bash
cd aws
python3 -m venv .venv
source .venv/bin/activate          # Git Bash on Windows: source .venv/Scripts/activate
pip install -r requirements.txt
```

Org-specific values are CDK context values. Define them once in a `CTX` array and pass the same `CTX` to every cdk command, including `cdk destroy`. In a new shell, define it again.

```bash
CTX=(
  -c partnerEventSourceName="$PARTNER_SOURCE"
  -c salesforceMyDomain="$SF_MY_DOMAIN"
  -c salesforceClientId="$CID"
  -c salesforceApiVersion="$SF_API_VERSION"
  -c eventRelayConfigId="$RELAY_ID"
)
# Optional: email alarm notifications (confirm the SNS subscription email)
# CTX+=(-c alertEmail=you@example.com)
```

Bootstrap once per account and region. Every cdk command run inside `aws/` synthesizes the app first, so bootstrap needs the context too:

```bash
cdk bootstrap "aws://$AWS_ACCOUNT/$AWS_REGION" "${CTX[@]}"
```

Synthesize to check the inputs, then deploy:

```bash
cdk synth "${CTX[@]}" > /dev/null
cdk deploy "${CTX[@]}"
```

`cdk deploy` lists the IAM changes and asks you to confirm. The stack is named `sf-member-relay`.

| Context key | Required | Default (`cdk.json`) | Meaning |
|---|---|---|---|
| `partnerEventSourceName` | yes | none | From step 8. `aws.partner/salesforce.com/<orgId>/<channelId>` |
| `salesforceMyDomain` | yes | none | Host only, no `https://`. e.g. `acme.my.salesforce.com` |
| `salesforceClientId` | yes | none | Return-path consumer key from step 3 |
| `salesforceApiVersion` | no | `v68.0` | Used in the API destination URL and the health check |
| `returnSecretName` | no | `salesforce/member-relay` | Secret from step 3 |
| `monitorSecretName` | no | `salesforce/relay-monitor` | Secret from step 4 |
| `eventRelayConfigId` | no | empty | From step 7. Empty means no health check |
| `enableRelayHealthCheck` | no | `true` | `false` skips the health check Lambda and its alarm |
| `alertEmail` | no | empty | Email subscription on the alarm topic |
| `freshnessWindowHours` | no | `2` | Hours without a matched `Member_Registered__e` before the freshness alarm fires |

Values are validated at synth time. A missing required key fails with `Missing CDK context '<key>'`, and a malformed one fails with `Invalid CDK context '<key>'`, before anything reaches CloudFormation.

Don't put org values in `cdk.json`. It's committed, and it only holds defaults. The consumer key is an identifier, not a secret, but it ends up in the synthesized template.

Why the secret must already exist: the connection reads `salesforce/member-relay` through a CloudFormation dynamic reference, resolved at deploy time. If the secret is missing, the deploy fails.

The partner bus is an `AWS::Events::EventBus` whose name [must exactly match the partner event source name](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-events-eventbus.html). Creating it is what associates the source, so after the deploy the source shows `ACTIVE`:

```bash
aws events list-event-sources --region "$AWS_REGION" --name-prefix aws.partner/salesforce.com \
  --query 'EventSources[].{Name:Name,State:State}' --output table
cd ..
```

Both rule targets get a DLQ and a retry policy (185 attempts, 24 hours). See the [CDK events targets docs](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_events_targets/README.html) for the target options.

### Step 10: Start the relay

```bash
./salesforce/scripts/04-start-relay.sh             # expect HTTP 204
watch -n 15 ./salesforce/scripts/05-relay-status.sh
```

`state` and `relayOption` are updated through the `Metadata` field ([EventRelayConfig](https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_eventrelayconfig.htm)). State changes [can take several minutes](https://help.salesforce.com/s/articleView?id=platform.ev_relay_status.htm&language=en_US&type=5) and show a transitional status in the meantime. Wait for `RUNNING`.

## End-to-end test

The test script inserts a real `Member__c` record with anonymous Apex, so the real publisher trigger runs. Don't hand-build a `Member_Registered__e` with a made-up Id. It gets through the outbound leg, but the return leg fails: the subscriber can't update a record that doesn't exist, and the failure lands in `Relay_Error_Log__c`.

```bash
./salesforce/scripts/06-create-test-member.sh member+test@example.com Standard
```

The script prints the new record's Id with `Sync_Status__c` = `Pending`. In a second shell, follow the Lambda logs:

```bash
aws logs tail /aws/lambda/member-sync --follow --region "$AWS_REGION"
```

Optionally, check the item in the AWS application's table:

```bash
aws dynamodb get-item --region "$AWS_REGION" --table-name members \
  --key '{"salesforce_member_id":{"S":"<MemberId>"}}'
```

Check the record in Salesforce. `Sync_Status__c` should be `Synced`, and `External_Member_Id__c` should hold a UUID:

```bash
sf data query --target-org "$SF_ORG" -q "SELECT Id, Sync_Status__c, External_Member_Id__c FROM Member__c WHERE Id = '<MemberId>'"
sf data query --target-org "$SF_ORG" -q "SELECT Source__c, Record_Id__c, Message__c FROM Relay_Error_Log__c ORDER BY CreatedDate DESC LIMIT 10"
```

Members created any other way (API, Flow, or the UI once you add a tab for `Member__c`) take the same path.

## Debugging in layers

Each layer has its own signal:

| Layer | Check |
|---|---|
| Salesforce → relay | `EventRelayFeedback.Status` = `RUNNING`, and `LastRelayedEventTime` advances (`05-relay-status.sh`) |
| Relay → partner bus | Rule metric `MatchedEvents` > 0 in CloudWatch (namespace `AWS/Events`, rule `member-registered-to-sync`) |
| Rule → Lambda | `FailedInvocations`, `MemberRegisteredRuleDlq` depth, Lambda logs |
| Lambda → DynamoDB and results bus | DynamoDB and `PutEvents` errors in logs, `member-synced-to-salesforce` `MatchedEvents` |
| API destination → Salesforce | `FailedInvocations`, `ReturnDlq` depth (the DLQ message attributes include the HTTP error) |
| Salesforce subscriber | `Relay_Error_Log__c` rows, trigger status in Setup → Platform Events → Member Synced → Subscriptions |

If you're stuck at layer 2, temporarily add a catch-all rule (`source` prefix `aws.partner/salesforce.com`) on the partner bus that targets a CloudWatch log group. Salesforce's [verification guide](https://help.salesforce.com/s/articleView?id=platform.ev_relay_verify_connection.htm&language=en_US&type=5) uses this approach, and it shows whether the problem is delivery or your event pattern.

Know the API destination's failure semantics. EventBridge [retries only 401, 407, 409, 429, and 5xx responses](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-api-destinations.html), and the endpoint must respond within 5 seconds. A `400` from Salesforce, such as a misspelled field API name in `detail`, is not retried and goes straight to `ReturnDlq`. A DLQ that fills up right after a deploy usually means a payload or schema mismatch, not an outage.

There are three DLQs. `MemberRegisteredRuleDlq` holds events EventBridge couldn't deliver to the Lambda. `MemberSyncDlq` holds events the Lambda received but threw on, after two async retries. `ReturnDlq` holds events the API destination couldn't deliver. The stack outputs list their URLs:

```bash
aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' --output table
aws sqs receive-message --region "$AWS_REGION" --queue-url "<ReturnDlqUrl>" \
  --attribute-names All --message-attribute-names All
```

## Monitoring

When a relay goes into `ERROR`, Salesforce [retries periodically](https://help.salesforce.com/s/articleView?id=platform.ev_relay_status.htm&language=en_US&type=5), but nothing pages you. The stack adds two signals. Read them together.

Relay health check. The `relay-health-check` Lambda runs every 5 minutes on EventBridge Scheduler. It authenticates with the monitoring ECA from step 4, queries `EventRelayFeedback` for `RELAY_ID`, and publishes `RelayHealthy` (1 or 0) to the `SalesforceEventRelay` namespace with a `RelayId` dimension. Any failure (missing secret, bad credentials, query error) reports 0. `RelayUnhealthyAlarm` fires when the value stays below 1 for two checks in a row, and also when the metric is missing. It is only deployed when `eventRelayConfigId` is set and `enableRelayHealthCheck` is `true`.

```bash
aws logs tail /aws/lambda/relay-health-check --follow --region "$AWS_REGION"
```

Salesforce doesn't document the exact permission needed to query `EventRelayFeedback`. Querying `EventRelayConfig` [requires View Setup and Configuration](https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_eventrelayconfig.htm), which `Relay_Monitor` grants. Verify the query works as your monitoring user.

Freshness alarm. `MemberRegisteredFreshnessAlarm` watches `MatchedEvents` on `member-registered-to-sync` and fires when no event matched for `freshnessWindowHours` hours (default 2). Missing data counts as breaching. On its own it's noisy, because a quiet Sunday looks like an outage. Combined with `RelayHealthy`, it tells you whether "no events" means "relay broken" or "nobody registered."

CloudWatch alarms can't be limited to business hours on their own. Use metric math on the alarm, or enable and disable the alarm on a schedule.

Both alarms notify the `AlarmTopic` SNS topic. Set `alertEmail` to subscribe an address, or subscribe your own endpoint to the `AlarmTopicArn` output.

Plan for Salesforce maintenance. Org migrations and instance refreshes [reset the stored event stream](https://help.salesforce.com/s/articleView?id=platform.ev_relay_considerations.htm&language=en_US&type=5). Events published during the migration aren't forwarded, and even `EARLIEST` can't recover them. For flows where every event matters, keep a reconciliation job that republishes the stragglers. For example, members created more than 30 minutes ago that are still `Pending` or have no external id:

```bash
CUTOFF="$(date -u -d '-30 minutes' +%Y-%m-%dT%H:%M:%SZ)"
sf data query --target-org "$SF_ORG" -q "SELECT Id, CreatedDate FROM Member__c WHERE (Sync_Status__c = 'Pending' OR External_Member_Id__c = null) AND CreatedDate < $CUTOFF"
```

Republishing `Member_Registered__e` for these records is safe. The Lambda's conditional write returns the existing external id for a member it has already registered.

## Claims to verify in your org

Some behavior here comes from docs that are thin or out of date. Check these before you rely on them:

- `publishBehavior` default. The [CustomObject reference](https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/customobject.htm) documents it as `PublishImmediately`. This repo sets `PublishAfterCommit` explicitly, so it doesn't matter here, but it matters for events you define elsewhere.
- AppFlow large-payload offload. AppFlow offloads large payloads to S3 with a pointer. Check the [AWS docs](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-saas-salesforce.html) for the current threshold.
- `detail.id` as a dedup key. Salesforce doesn't document it for relays. In Pub/Sub API the event `id` [is the event UUID](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/considerations.html). Compare `detail.id` with `EventUuid` in an Apex subscriber ([Verify Receiving Events](https://help.salesforce.com/s/articleView?id=platform.ev_relay_test_receiving_events.htm&language=en_US&type=5)).
- `EventRelayFeedback` permission. Test the health check query as the monitoring user (see Monitoring).
- Teardown filters. `07-teardown-relay.sh` finds the channel and member by `DeveloperName`. Check the filters against the Tooling API reference for your API version.

## Teardown

Run these in order. Use the same `CTX` from step 9 (define it again in a new shell).

1. Delete the relay, channel member, and channel. Deleting the relay also removes the partner event source in AWS.

   ```bash
   ./salesforce/scripts/07-teardown-relay.sh
   ```

2. Drain or inspect the three DLQs first if you need their messages. Destroying the stack deletes the queues and any messages in them. It also deletes both log groups and the `members` table, which uses a `DESTROY` removal policy for the demo.

   ```bash
   cd aws && cdk destroy "${CTX[@]}" && cd ..
   ```

3. Delete both secrets. The default recovery window lets you restore them for a while. `--force-delete-without-recovery` deletes immediately and is irreversible.

   ```bash
   aws secretsmanager delete-secret --region "$AWS_REGION" --secret-id salesforce/member-relay
   aws secretsmanager delete-secret --region "$AWS_REGION" --secret-id salesforce/relay-monitor
   ```

4. In Setup, delete both External Client Apps and the named credential, and deactivate the integration and monitoring users.
5. Optionally remove the metadata. Unassign the three permission sets first.

   ```bash
   (cd salesforce && sf project delete source --source-dir force-app --target-org "$SF_ORG")
   ```

The CDK bootstrap stack (`CDKToolkit`) stays. Other CDK apps in the account may use it.

## Gotcha checklist

- [ ] Platform event is HighVolume (`eventType` in metadata).
- [ ] Named credential URL region is UPPERCASE: `arn:aws:US-EAST-1:<account>`.
- [ ] Target AWS region is not an opt-in region.
- [ ] Channel member `FullName` uses single underscores.
- [ ] Relay created in `STOP`, partner source associated, then set to `RUN`.
- [ ] `relayOption` set deliberately (`EARLIEST` for business data).
- [ ] The consumer is idempotent per member (DynamoDB conditional write).
- [ ] Payloads stay well under 256 KB.
- [ ] Inbound event types are not in the relay channel (no loops).
- [ ] Both Lambda DLQ paths (rule target `dead_letter_queue` and function `on_failure`) and the API destination DLQ are configured.
- [ ] ECA (not a connected app), with a least-privilege integration user.
- [ ] Relay is recreated per sandbox, since refreshes don't copy it.
- [ ] Relay health is monitored from outside Salesforce.
- [ ] A reconciliation job covers maintenance-window gaps.
- [ ] ECA and Secrets Manager secret exist before `cdk deploy`.
- [ ] Monitoring uses its own ECA and run-as user.
- [ ] Test with a real `Member__c` record (`06-create-test-member.sh`), not a hand-built event.
- [ ] Scripts and the API destination use the same, current API version.

## Tests

AWS side: Lambda unit tests (AWS calls stubbed with `botocore.stub.Stubber`) and CDK assertion tests on the synthesized template. No AWS credentials needed.

```bash
cd aws
pip install -r requirements-dev.txt
python -m pytest
```

Salesforce side: `MemberRelayTriggersTest` covers both triggers. Production deploys need 75% coverage.

```bash
sf apex run test --class-names MemberRelayTriggersTest --target-org "$SF_ORG" --result-format human
```

## License

MIT. See [LICENSE](LICENSE).
