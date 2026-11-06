# Alertmanager integration

Alertally receives alerts from Prometheus Alertmanager at:

```
POST https://alertally.example.com/alertmanager
```

Alertmanager sends its standard webhook (payload `version: "4"`), and each alert in
the payload becomes an incident in Alertally. The same Alertmanager can send the alert
to Alertally **and** to Slack at the same time (see [Alertally + Slack](#alertally--slack)).

## 1. Configure Alertally

| Variable | Required | Purpose |
|---|---|---|
| `ALERTMANAGER_TOKEN` | No, but recommended | When set, the endpoint requires `Authorization: Bearer <token>` and returns `401` without it. Without it, anyone who can reach the URL can create alerts. |

Generate a token and add it to the server's `.env`:

```bash
echo "ALERTMANAGER_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d web
```

> The `/alertmanager` endpoint does not go through the Google login, so the token is
> what protects it.

## 2. Configure Alertmanager

### Alertally + Slack

A receiver can have several destinations. With `webhook_configs` and `slack_configs`
in the same receiver, each notification goes to both:

```yaml
# alertmanager.yml
route:
  receiver: alertally-and-slack
  group_by: ['alertname', 'namespace']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: alertally-and-slack
    webhook_configs:
      - url: https://alertally.example.com/alertmanager
        send_resolved: true          # required to close the alert in Alertally
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/alertally-token
    slack_configs:
      - api_url_file: /etc/alertmanager/secrets/slack-webhook-url
        channel: '#infra-alerts'
        send_resolved: true
        title: '[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}'
        text: >-
          {{ range .Alerts }}• *{{ .Labels.severity }}* {{ .Annotations.summary }}
          {{ end }}
```

The files in `/etc/alertmanager/secrets/` hold the Alertally token and the Slack
webhook URL. Mount them from a Secret instead of putting the values in the YAML.

### Destinations with different filters

If Slack should receive only some alerts (for example, only critical ones) and
Alertally should receive all of them, use separate receivers and `continue: true`.
Without `continue`, Alertmanager stops at the first matching route.

```yaml
route:
  receiver: alertally            # default: everything goes to Alertally
  routes:
    - receiver: alertally
      continue: true             # keep evaluating the next routes
    - receiver: slack-critical
      matchers:
        - severity = "critical"

receivers:
  - name: alertally
    webhook_configs:
      - url: https://alertally.example.com/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/alertally-token
  - name: slack-critical
    slack_configs:
      - api_url_file: /etc/alertmanager/secrets/slack-webhook-url
        channel: '#critical-alerts'
        send_resolved: true
```

### kube-prometheus-stack (Helm)

The configuration above goes in `alertmanager.config` in `values.yaml`, and the
secrets are mounted with `alertmanager.alertmanagerSpec.secrets`:

```yaml
alertmanager:
  alertmanagerSpec:
    secrets:
      - alertally-secrets   # Secret with the keys alertally-token and slack-webhook-url
  config:
    route: { ... }          # same as the example above
    receivers: [ ... ]
```

With `alertmanagerSpec.secrets`, each Secret is mounted at
`/etc/alertmanager/secrets/<secret-name>/`. Adjust the `credentials_file` and
`api_url_file` paths to include the Secret name, for example
`/etc/alertmanager/secrets/alertally-secrets/alertally-token`.

Alertmanager must be able to reach `alertally.example.com` over the network. Check
this from a pod in the cluster before enabling the route.

## 3. How Alertally reads the alert

| Field in Alertally | Comes from |
|---|---|
| Name | `alertname` label |
| Severity | `severity` label (table below) |
| Summary | `summary` annotation, otherwise `description` |
| Value / threshold | `value` annotation |
| Link | the alert's `generatorURL`, otherwise Alertmanager's `externalURL` |
| Labels | all of the alert's labels |
| State | `open` when `firing`, `closed` when `resolved` |

| `severity` label | Severity in Alertally |
|---|---|
| `critical`, `page` | Critical |
| `high`, `error` | Error |
| `warning`, `warn`, `medium` | Warning |
| `info`, `low` | Info |
| `debug` | Debug |
| `none` | No severity |
| missing or any other value | Unknown |

### Counting in reports

- Each incident is identified by `fingerprint` + `startsAt`.
- Re-sends of the same firing (`repeat_interval`, `group_interval`) update the same
  incident, so they **don't inflate** the report counts.
- When an alert resolves and later fires again, `startsAt` changes and it counts as a
  new firing. This is what the "most triggered alerts" ranking measures.
- Without `send_resolved: true`, the alert stays open in Alertally until someone
  closes it manually.

## 4. Testing

Send an alert straight to Alertally, bypassing Alertmanager:

```bash
curl -X POST https://alertally.example.com/alertmanager \
  -H "Authorization: Bearer $ALERTMANAGER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "4", "status": "firing",
    "alerts": [{
      "status": "firing",
      "fingerprint": "test123",
      "startsAt": "2026-10-24T12:00:00Z",
      "labels": {"alertname": "IntegrationTest", "severity": "info"},
      "annotations": {"summary": "Integration test alert"}
    }]
  }'
# Response: {"incidents": ["am-test123-..."], "processed": 1}
```

To close it, send the same JSON with `"status": "resolved"` inside the alert.

To test the full path (Alertmanager → Alertally and Slack), fire an alert through
Alertmanager itself:

```bash
amtool alert add IntegrationTest severity=info \
  --annotation=summary="Integration test alert" \
  --alertmanager.url=http://localhost:9093
```

Endpoint error responses:

| Code | Cause |
|---|---|
| `401` | `ALERTMANAGER_TOKEN` is set and the header is missing or wrong |
| `400` | body is not JSON or has no `alerts` key |
