# AWS integration (CloudWatch)

Alertally receives CloudWatch alarms through an SNS topic with an HTTPS subscription:

```
CloudWatch alarm  →  SNS topic  →  POST https://alertally.example.com/aws
```

Every time an alarm enters `ALARM`, an incident is opened in Alertally. When the
alarm returns to `OK`, the incident is closed.

## 1. Configure Alertally

| Variable | Required | Purpose |
|---|---|---|
| `AWS_SNS_USER` / `AWS_SNS_PASSWORD` | No, but recommended | Username and password (basic auth) sent by SNS. Without them, anyone who can reach the URL can create alerts. |
| `AWS_SNS_TOPIC_ARNS` | No, but recommended | Comma-separated ARNs of the accepted topics. Messages from other topics get `403`. |

```bash
cat >> .env <<EOF
AWS_SNS_USER=sns
AWS_SNS_PASSWORD=$(openssl rand -hex 24)
AWS_SNS_TOPIC_ARNS=arn:aws:sns:us-east-1:123456789012:alertally
EOF
docker compose up -d web
```

> SNS doesn't send custom headers, so unlike Alertmanager there is no Bearer token
> here. The credentials go in the subscription URL itself (step 2). SNS first calls
> without credentials and, after receiving `401` with `WWW-Authenticate: Basic`,
> retries with them. The endpoint already responds that way.

## 2. Create the SNS topic and subscription

```bash
REGION=us-east-1

TOPIC_ARN=$(aws sns create-topic --name alertally --region $REGION \
  --query TopicArn --output text)

aws sns subscribe --region $REGION \
  --topic-arn "$TOPIC_ARN" \
  --protocol https \
  --notification-endpoint "https://sns:<AWS_SNS_PASSWORD>@alertally.example.com/aws"
```

- Always use `https`, because the password is in the URL.
- If the password has special characters (`@`, `:`, `/`…), URL-encode them. A
  password generated with `openssl rand -hex` avoids this.
- **Confirmation is automatic:** SNS sends a `SubscriptionConfirmation` and Alertally
  visits the confirmation link. Only `https://sns.<region>.amazonaws.com` links are
  accepted. Check with `aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN"`:
  the subscription should leave `PendingConfirmation`.
- Alarms in another region need a topic in that region. Add each topic's ARN to
  `AWS_SNS_TOPIC_ARNS`.

## 3. Point the alarms at the topic

Add the topic to **both the `ALARM` and `OK` actions**. Without the `OK` action, the
incident stays open until someone closes it manually.

```bash
aws cloudwatch put-metric-alarm --region $REGION \
  --alarm-name "RDS FreeStorageLow prod-db" \
  --alarm-description "severity=critical; RDS prod-db free storage below 10 GB" \
  --namespace AWS/RDS --metric-name FreeStorageSpace \
  --dimensions Name=DBInstanceIdentifier,Value=prod-db \
  --statistic Average --period 300 --evaluation-periods 1 \
  --threshold 10000000000 --comparison-operator LessThanThreshold \
  --alarm-actions "$TOPIC_ARN" \
  --ok-actions "$TOPIC_ARN"
```

In the console: **CloudWatch → Alarms → edit → Actions**, add one notification for
"In alarm" and another for "OK", both with the `alertally` topic.

In Terraform, `alarm_actions` and `ok_actions` on `aws_cloudwatch_metric_alarm` take
the topic ARN.

### Severity

CloudWatch has no severity field. By convention, Alertally reads `severity=<value>`
(or `severity: <value>`) from the **alarm description**:

| In the description | Severity in Alertally |
|---|---|
| `severity=critical` | Critical |
| `severity=error` or `high` | Error |
| `severity=warning` | Warning |
| `severity=info` or `low` | Info |
| no convention | **Warning** (default) |

The `severity=...` part is removed from the description before it becomes the alert
summary.

## 4. How Alertally reads the alarm

| Field in Alertally | Comes from |
|---|---|
| Name | `AlarmName` |
| Summary | `AlarmDescription` without `severity=...`, otherwise `NewStateReason` |
| Description | `NewStateReason` (e.g. "Threshold Crossed: 1 datapoint…") |
| Value / threshold | `Trigger.Threshold` |
| Link | CloudWatch console in the alarm's region |
| Labels | account, region, namespace, metric and the alarm's dimensions |

| New alarm state | What happens |
|---|---|
| `ALARM` | Opens an incident (counts as one firing in reports) |
| `OK` | Closes that alarm's open incidents |
| `INSUFFICIENT_DATA` | Ignored: missing data is not a firing |

- The incident is identified by the alarm + the time of the state change. If SNS
  redelivers the same message, it lands on the same incident and isn't counted twice.
- Messages on the topic that aren't CloudWatch alarms are ignored with `200`, so SNS
  doesn't keep retrying.
- Composite alarms work. Since they have no metric, they have no metric labels.

## 5. Testing

Forcing the alarm state through CloudWatch tests the full path. The alarm returns to
its real state on the next evaluation.

```bash
aws cloudwatch set-alarm-state --region $REGION \
  --alarm-name "RDS FreeStorageLow prod-db" \
  --state-value ALARM --state-reason "Alertally integration test"

aws cloudwatch set-alarm-state --region $REGION \
  --alarm-name "RDS FreeStorageLow prod-db" \
  --state-value OK --state-reason "End of test"
```

Endpoint responses:

| Code | Cause |
|---|---|
| `200` | Processed, or ignored (the body says why) |
| `400` | Body is not JSON, or the confirmation link is outside AWS |
| `401` | Username/password missing or wrong |
| `403` | Topic not in `AWS_SNS_TOPIC_ARNS` |

## Limitations

- Alertally doesn't verify the cryptographic signature of SNS messages. Protection
  comes from basic auth plus the topic allowlist. If you need stronger guarantees,
  signature verification can be added later.
- Only CloudWatch alarms become incidents. Other sources published to the same topic
  (EventBridge, Budgets…) are ignored.
