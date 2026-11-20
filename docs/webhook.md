# Generic webhook

Any tool or script that can send JSON over HTTPS can open and resolve alerts in
Alertally through `POST /events`. Use it for sources without a dedicated endpoint:
uptime checkers, cron jobs, internal scripts, or monitoring tools that let you
write the webhook body yourself.

```
your tool  →  POST https://alertally.example.com/events
```

## 1. Configure Alertally

| Variable | Required | Purpose |
|---|---|---|
| `EVENTS_TOKEN` | Yes | The endpoint requires `Authorization: Bearer <token>`. Without the variable the endpoint answers `503`, unless `ALLOW_UNAUTHENTICATED_WEBHOOKS=true`. |

```bash
echo "EVENTS_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d web
```

## 2. Send events

```bash
curl -X POST https://alertally.example.com/events \
  -H "Authorization: Bearer $EVENTS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "disk-full/vm-1",
    "source": "backup-script",
    "name": "Disk full",
    "status": "firing",
    "severity": "critical",
    "summary": "Disk at 95% on vm-1",
    "labels": {"service": "orders-api", "host": "vm-1"},
    "url": "https://monitor.example.com/vm-1",
    "runbook_url": "https://wiki.example.com/runbooks/disk-full"
  }'
```

When the problem is gone, send the same `id` and `source` with `"status": "resolved"`:

```bash
curl -X POST https://alertally.example.com/events \
  -H "Authorization: Bearer $EVENTS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"id": "disk-full/vm-1", "source": "backup-script", "status": "resolved"}'
```

The body is one event or a list of up to 100 events. The whole list is checked
before anything is stored: one invalid event rejects the request with `400`, and
the response says which event (`index`, starting at 0) and why.

## Fields

| Field | Required | Description |
|---|---|---|
| `id` | Yes | Identifies the problem in your tool, for example `disk-full/vm-1`. Up to 500 characters. |
| `status` | Yes | `firing` or `resolved`. Also accepted: `triggered`, `problem`, `open`, `down`, `alert` for firing, and `ok`, `closed`, `recovered`, `up` for resolved. Case doesn't matter. |
| `name` | When firing | Alert title shown in the list. Up to 300 characters. |
| `source` | No | Short slug for the sending tool (`a-z`, `0-9`, `-`, `_`, up to 40 characters). Default `webhook`. `gcp`, `alertmanager` and `aws` are reserved. |
| `severity` | No | `critical`, `error` (or `high`), `warning` (or `warn`, `medium`), `info` (or `low`), `debug`. Anything else is stored as Unknown. |
| `summary` | No | One-line description. Up to 2,000 characters; longer text is cut. |
| `description` | No | Longer text. Up to 10,000 characters. |
| `labels` | No | Object with up to 50 labels. Values become strings. Characters other than letters, digits, `_` and `-` in label names become `_` (`k8s.namespace` → `k8s_namespace`). The `service` label feeds the [catalog](../README.md#features) like any other source. |
| `url` | No | Link to the alert in your tool. Only `http` and `https` links are kept. |
| `runbook_url` | No | Runbook for this alert. It wins over the catalog service's runbook. |
| `starts_at` | No | When the problem started: ISO 8601 with a time zone (`2026-11-01T10:00:00Z`) or epoch seconds. Without it, the opening time is when Alertally received the event. |

## How events become alerts

- `id` and `source` together identify the problem. The same `id` from two sources
  makes two separate alerts.
- A `firing` event for a problem that is already open updates that alert (summary,
  severity, labels, links). The opening time doesn't change.
- A `resolved` event closes the open alert for that problem. Sending it when nothing
  is open is not an error: the response shows `"closed": 0`.
- A `firing` event after the problem was resolved opens a **new** alert, so each
  occurrence keeps its own history and duration in the reports.
- Two first `firing` deliveries for the same problem that arrive at the same instant
  can create two alerts. Sending `starts_at` avoids this, because the alert is then
  identified by the start time too.

## Responses

```json
{"processed": 2, "results": [
  {"id": "backup-script/disk-full/vm-1", "state": "open", "incident": "ev-3f1c…"},
  {"id": "backup-script/cert-expiry", "state": "closed", "closed": 1}
]}
```

| Code | Cause |
|---|---|
| `200` | Processed |
| `400` | Body is not JSON, has more than 100 events, or an event is invalid (`error` and `index` say which) |
| `401` | The `Authorization` header is missing or wrong |
| `503` | `EVENTS_TOKEN` is not set (see `ALLOW_UNAUTHENTICATED_WEBHOOKS`) |

A `503` makes most senders retry, so no alert is lost while the configuration is fixed.
