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
    assert doc["labels"]["aws_namespace"] == "AWS/ApplicationELB" and "namespace" not in doc["labels"]

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


@pytest.mark.parametrize('starts_at', ['2026-11-01T11:00:00Z', '2026-11-01T08:00:00-03:00'])
def test_alertmanager_new_occurrence_closes_stale_same_fingerprint(client, alerts, caplog, starts_at):
    payload = am_payload(severity='warning')
    old_id = client.post('/alertmanager', json=payload).json['incidents'][0]
    # A critical alert has its own lifecycle, even for the same resource.
    critical = am_payload()
    critical['alerts'][0]['fingerprint'] = 'critical-fingerprint'
    client.post('/alertmanager', json=critical)
    payload['alerts'][0]['startsAt'] = starts_at
    new_id = client.post('/alertmanager', json=payload).json['incidents'][0]
    client.post('/alertmanager', json=payload)  # retry must not duplicate incidents
    old = alerts.find_one({'incident_id': old_id})
    assert old['state'] == 'closed'
    assert old['closure_reason'] == 'superseded' and old['superseded_by'] == new_id
    assert old['ends_at'] == '0001-01-01T00:00:00Z'  # no invented recovery time
    assert alerts.find_one({'incident_id': new_id})['state'] == 'open'
    assert alerts.find_one({'labels.severity': 'critical'})['state'] == 'open'
    assert alerts.count_documents({}) == 3
    assert 'previous occurrence superseded' in caplog.text


@pytest.mark.parametrize('starts_at', ['', 'invalid', '2026-11-01T11:00:00'])
def test_alertmanager_invalid_start_does_not_close_other_occurrences(client, alerts, starts_at):
    payload = am_payload()
    old_id = client.post('/alertmanager', json=payload).json['incidents'][0]
    payload['alerts'][0]['startsAt'] = starts_at
    client.post('/alertmanager', json=payload)
    assert alerts.find_one({'incident_id': old_id})['state'] == 'open'


def test_alertmanager_delayed_occurrence_does_not_close_newer(client, alerts):
    payload = am_payload()
    payload['alerts'][0]['startsAt'] = '2026-11-01T11:00:00Z'
    new_id = client.post('/alertmanager', json=payload).json['incidents'][0]
    client.post('/alertmanager', json=am_payload())
    client.post('/alertmanager', json=am_payload(status='resolved'))
    assert alerts.find_one({'incident_id': new_id})['state'] == 'open'


@pytest.mark.parametrize('source', ['alertmanager', 'gcp', 'aws'])
def test_repeated_notifications_preserve_opening_time(client, alerts, source):
    from datetime import datetime

    if source == 'alertmanager':
        path, body = '/alertmanager', {'json': am_payload()}
    elif source == 'gcp':
        path, body = '/gcp', {'json': gcp_payload(started_at=1793527200)}
    else:
        path, body = '/aws', sns(alarm('ALARM'))
    assert client.post(path, **body).status_code == 200
    first = alerts.find_one({'source': source})
    assert first['opened_at'] == datetime(2026, 11, 1, 10)
    alerts.update_one({'_id': first['_id']}, {'$set': {'update_at': datetime(2026, 11, 1, 12)}})
    assert client.post(path, **body).status_code == 200
    repeated = alerts.find_one({'_id': first['_id']})
    assert repeated['opened_at'] == first['opened_at']
    assert repeated['update_at'] > datetime(2026, 11, 1, 12)
    assert alerts.count_documents({'source': source}) == 1


def test_legacy_alert_without_source_start_preserves_previous_time(client, alerts):
    from datetime import datetime

    first_seen = datetime(2026, 10, 1)
    alerts.insert_one({'incident_id': '0.abc', 'source': 'gcp', 'state': 'open', 'update_at': first_seen})
    client.post('/gcp', json=gcp_payload())
    client.post('/gcp', json=gcp_payload())
    assert alerts.find_one({'incident_id': '0.abc'})['opened_at'] == first_seen


# ---------- Credenciais obrigatórias ----------

@pytest.mark.parametrize("path, body", [
    ("/gcp", gcp_payload()),
    ("/alertmanager", {"alerts": []}),
    ("/events", {"id": "x", "name": "x", "status": "firing"}),
])
def test_webhook_without_credentials_is_refused(client, alerts, monkeypatch, path, body):
    monkeypatch.setattr(Config, "ALLOW_UNAUTHENTICATED_WEBHOOKS", False)
    resp = client.post(path, json=body)
    assert resp.status_code == 503
    assert alerts.count_documents({}) == 0


def test_aws_without_credentials_is_refused(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_UNAUTHENTICATED_WEBHOOKS", False)
    assert client.post("/aws", **sns(alarm("ALARM"))).status_code == 503


def test_webhook_with_credentials_needs_no_opt_in(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_UNAUTHENTICATED_WEBHOOKS", False)
    monkeypatch.setattr(Config, "ALERTMANAGER_TOKEN", "tok")
    resp = client.post("/alertmanager", json={"alerts": []}, headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200


@pytest.mark.parametrize("header", ["Bearer wrong", "Bearer tök", "tok", ""])
def test_alertmanager_rejects_wrong_token(client, alerts, monkeypatch, header):
    monkeypatch.setattr(Config, "ALERTMANAGER_TOKEN", "tok")
    resp = client.post("/alertmanager", json={"alerts": []}, headers={"Authorization": header})
    assert resp.status_code == 401


# ---------- Assinatura do SNS ----------

CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"


@pytest.fixture
def sns_signer(monkeypatch):
    """Sign envelopes with a throwaway certificate served as if it came from SNS."""
    from datetime import datetime, timedelta, timezone as tz

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.x509.oid import NameOID

    from utils import sns as sns_module

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.now(tz.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    fetched = []
    monkeypatch.setattr(sns_module, "_certificate", lambda url: fetched.append(url) or cert)
    monkeypatch.setattr(Config, "AWS_SNS_VERIFY_SIGNATURE", True)

    def sign(kwargs, version="2", **changes):
        envelope = json.loads(kwargs["data"])
        envelope.update(MessageId="m-1", Timestamp="2026-11-01T10:00:00.000Z",
                        SignatureVersion=version, SigningCertURL=CERT_URL)
        text = sns_module._string_to_sign(envelope)
        algorithm = hashes.SHA1() if version == "1" else hashes.SHA256()
        envelope["Signature"] = base64.b64encode(
            key.sign(text.encode(), padding.PKCS1v15(), algorithm)).decode()
        envelope.update(changes)
        return {**kwargs, "data": json.dumps(envelope)}

    sign.fetched = fetched
    return sign


@pytest.mark.parametrize("version", ["1", "2"])
def test_aws_accepts_signed_message(client, alerts, sns_signer, version):
    resp = client.post("/aws", **sns_signer(sns(alarm("ALARM")), version=version))
    assert resp.status_code == 200
    assert alerts.count_documents({"source": "aws"}) == 1
    assert sns_signer.fetched == [CERT_URL]


@pytest.mark.parametrize("changes", [
    {"Message": json.dumps(alarm("OK"))},                      # mensagem trocada
    {"Signature": "bm90IGEgc2lnbmF0dXJl"},                     # assinatura inválida
    {"Signature": "%%%"},                                      # não é base64
    {"SigningCertURL": "https://evil.example.com/cert.pem"},  # certificado de fora da AWS
    {"SigningCertURL": "http://sns.us-east-1.amazonaws.com/cert.pem"},
    {"SignatureVersion": "3"},
])
def test_aws_rejects_bad_signature(client, alerts, sns_signer, changes):
    resp = client.post("/aws", **sns_signer(sns(alarm("ALARM")), **changes))
    assert resp.status_code == 403
    assert alerts.count_documents({}) == 0


def test_aws_rejects_unsigned_message(client, alerts, sns_signer):
    assert client.post("/aws", **sns(alarm("ALARM"))).status_code == 403
