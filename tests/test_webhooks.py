"""Entrada de alertas: GCP Monitoring, Alertmanager e AWS (CloudWatch via SNS)."""
import base64
import json

import pytest

from config import Config


def _basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


# ---------- GCP ----------

def gcp_payload(**incident):
    base = {
        "incident_id": "0.abc",
        "condition_name": "VM CPU high",
        "state": "open",
        "severity": "Critical",
        "url": "https://console.cloud.google.com/monitoring/alerting/incidents/0.abc",
        "observed_value": "0.97",
        "documentation": {"content": "Check the VM"},
        "metric": {"labels": {"instance": "vm-1"}},
    }
    base.update(incident)
    return {"version": "1.2", "incident": base}


def test_gcp_opens_and_closes(client, alerts):
    assert client.post("/gcp", json=gcp_payload()).status_code == 200
    doc = alerts.find_one({"incident_id": "0.abc"})
    assert doc["state"] == "open"
    assert doc["severity"] == "Critical" and doc["severity_number"] == 5
    assert doc["summary"] == "Check the VM"
    assert doc["labels"] == {"instance": "vm-1"}

    client.post("/gcp", json=gcp_payload(state="closed"))
    assert alerts.find_one({"incident_id": "0.abc"})["state"] == "closed"


@pytest.mark.parametrize("raw, expected", [
    ("CRITICAL", "Critical"),
    ("warning", "Warning"),
    ("Error", "Error"),
    ("No severity", "No severity"),
    ("something-new", "Unknown"),   # antes derrubava o webhook com erro 500
    (None, "Unknown"),
])
def test_gcp_severity_is_normalized(client, alerts, raw, expected):
    payload = gcp_payload(severity=raw)
    assert client.post("/gcp", json=payload).status_code == 200
    assert alerts.find_one({"incident_id": "0.abc"})["severity"] == expected


def test_gcp_tolerates_missing_optional_fields(client, alerts):
    payload = {"incident": {"incident_id": "0.min", "state": "open"}}
    assert client.post("/gcp", json=payload).status_code == 200
    doc = alerts.find_one({"incident_id": "0.min"})
    assert doc["name"] == "Unnamed alert" and doc["labels"] == {}


@pytest.mark.parametrize("body", [
    {"json": {}},
    {"json": {"incident": "x"}},
    {"json": {"incident": {"state": "open"}}},
    {"data": "not json", "content_type": "text/plain"},
])
def test_gcp_rejects_invalid_payload(client, alerts, body):
    assert client.post("/gcp", **body).status_code == 400


def test_gcp_basic_auth(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "GCP_WEBHOOK_USER", "gcp")
    monkeypatch.setattr(Config, "GCP_WEBHOOK_PASSWORD", "s3cret")
    assert client.post("/gcp", json=gcp_payload()).status_code == 401
    assert client.post("/gcp", json=gcp_payload(), headers=_basic("gcp", "wrong")).status_code == 401
    assert client.post("/gcp", json=gcp_payload(), headers=_basic("gcp", "s3cret")).status_code == 200


# ---------- Alertmanager ----------

def am_payload(status="firing", severity="critical"):
    return {
        "status": status,
        "externalURL": "http://alertmanager:9093",
        "alerts": [{
            "status": status,
            "fingerprint": "f00d",
            "startsAt": "2026-11-01T10:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "labels": {"alertname": "PodCrashLooping", "severity": severity, "pod": "api-1"},
            "annotations": {"summary": "Pod is crash looping"},
        }],
    }


def test_alertmanager_firing_then_resolved(client, alerts):
    resp = client.post("/alertmanager", json=am_payload())
    assert resp.status_code == 200 and resp.json["processed"] == 1
    doc = alerts.find_one({"source": "alertmanager"})
    assert doc["state"] == "open" and doc["severity"] == "Critical"

    client.post("/alertmanager", json=am_payload(status="resolved"))
    assert alerts.count_documents({"source": "alertmanager"}) == 1
    assert alerts.find_one({"source": "alertmanager"})["state"] == "closed"


def test_alertmanager_token(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "ALERTMANAGER_TOKEN", "tok")
    assert client.post("/alertmanager", json=am_payload()).status_code == 401
    resp = client.post("/alertmanager", json=am_payload(), headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200


def test_alertmanager_resolves_only_one_resource_in_mixed_batch(client, alerts):
    payload = am_payload()
    first = payload['alerts'][0]
    first['labels'].update(alertname='NodeMemoryUsage', resource='api-geral')
    second = dict(first, fingerprint='other', labels=dict(first['labels'], resource='api-validation'))
    payload['alerts'].append(second)
    client.post('/alertmanager', json=payload)
    # The group is still firing while one individual alert is resolved.
    first['status'] = 'resolved'
    client.post('/alertmanager', json=payload)
    client.post('/alertmanager', json=payload)
    assert alerts.count_documents({}) == 2
    assert alerts.find_one({'labels.resource': 'api-geral'})['state'] == 'closed'
    assert alerts.find_one({'labels.resource': 'api-validation'})['state'] == 'open'


def test_alertmanager_logs_unmatched_resolution(client, alerts, caplog):
    client.post('/alertmanager', json=am_payload(status='resolved'))
    assert 'resolved without matching incident' in caplog.text


def test_alertmanager_invalid_payload(client, alerts):
    assert client.post("/alertmanager", json={"foo": 1}).status_code == 400


def test_alertmanager_debug_logs_correlate_batch_alert_and_database(client, alerts, caplog, monkeypatch):
    monkeypatch.setattr(Config, 'ALERTMANAGER_TOKEN', 'private-token')
    caplog.set_level('DEBUG', logger='alertally.alertmanager')
    payload = am_payload()
    payload.update(receiver='alertally', groupKey='memory-group',
                   groupLabels={'alertname': 'PodCrashLooping'})
    headers = {'Authorization': 'Bearer private-token'}
    client.post('/alertmanager', json=payload, headers=headers)
    client.post('/alertmanager', json=am_payload(status='resolved'), headers=headers)
    records = [r.getMessage() for r in caplog.records if r.name == 'alertally.alertmanager']
    batch = json.loads(next(r.removeprefix('Alertmanager batch ') for r in records
                            if r.startswith('Alertmanager batch ')))
    detail = json.loads(next(r.removeprefix('Alertmanager alert ') for r in records
                             if r.startswith('Alertmanager alert ')))
    result = json.loads(next(r.removeprefix('Alertmanager database result ') for r in records
                             if r.startswith('Alertmanager database result ')))
    assert batch['request_id'] == detail['request_id'] == result['request_id']
    assert batch['groupKey'] == 'memory-group'
    assert detail['labels']['pod'] == 'api-1'
    assert result['inserted'] is True
    assert any('"matched_count": 1' in r and '"state": "closed"' in r for r in records)
    assert 'private-token' not in caplog.text


def test_alertmanager_truncation_warning_without_debug(client, alerts, caplog):
    caplog.set_level('INFO', logger='alertally.alertmanager')
    payload = am_payload()
    payload['truncatedAlerts'] = 3
    assert client.post('/alertmanager', json=payload).status_code == 200
    assert 'truncatedAlerts=3' in caplog.text
    assert 'Alertmanager database result' not in caplog.text


# ---------- AWS (SNS) ----------

ALARM_ARN = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:HighLatency"


def sns(message, msg_type="Notification", topic="arn:aws:sns:us-east-1:123456789012:alerts"):
    envelope = {"Type": msg_type, "TopicArn": topic,
                "Message": json.dumps(message) if isinstance(message, dict) else message}
    return {"data": json.dumps(envelope), "content_type": "text/plain",
            "headers": {"x-amz-sns-message-type": msg_type}}


def alarm(state, when="2026-11-01T10:00:00.000+0000"):
    return {
        "AlarmName": "HighLatency", "AlarmArn": ALARM_ARN, "NewStateValue": state,
        "StateChangeTime": when, "AlarmDescription": "severity=critical; p99 above 2s",
        "AWSAccountId": "123456789012",
        "Trigger": {"Namespace": "AWS/ApplicationELB", "MetricName": "TargetResponseTime",
                    "Dimensions": [{"name": "LoadBalancer", "value": "app/web/1"}]},
    }


def test_aws_alarm_then_ok(client, alerts):
    assert client.post("/aws", **sns(alarm("ALARM"))).status_code == 200
    doc = alerts.find_one({"source": "aws"})
    assert doc["state"] == "open" and doc["severity"] == "Critical"
    assert doc["summary"] == "p99 above 2s"
    assert doc["labels"]["LoadBalancer"] == "app/web/1"

    # reentrega do SNS não duplica
    client.post("/aws", **sns(alarm("ALARM")))
    assert alerts.count_documents({"source": "aws"}) == 1

    resp = client.post("/aws", **sns(alarm("OK", when="2026-11-01T10:05:00.000+0000")))
    assert resp.json["closed"] == 1
    assert alerts.find_one({"source": "aws"})["state"] == "closed"


def test_aws_topic_allow_list(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "AWS_SNS_TOPIC_ARNS", ["arn:aws:sns:us-east-1:123456789012:other"])
    assert client.post("/aws", **sns(alarm("ALARM"))).status_code == 403


def test_aws_rejects_fake_subscribe_url(client, alerts):
    envelope = sns({}, msg_type="SubscriptionConfirmation")
    body = json.loads(envelope["data"])
    body["SubscribeURL"] = "https://evil.example.com/confirm"
    envelope["data"] = json.dumps(body)
    assert client.post("/aws", **envelope).status_code == 400


@pytest.mark.parametrize("data", ["not json", "[1, 2]"])
def test_aws_invalid_envelope(client, alerts, data):
    assert client.post("/aws", data=data, content_type="text/plain").status_code == 400


def test_aws_basic_auth(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "AWS_SNS_USER", "sns")
    monkeypatch.setattr(Config, "AWS_SNS_PASSWORD", "pw")
    assert client.post("/aws", **sns(alarm("ALARM"))).status_code == 401
    ok = sns(alarm("ALARM"))
    ok["headers"].update(_basic("sns", "pw"))
    assert client.post("/aws", **ok).status_code == 200
