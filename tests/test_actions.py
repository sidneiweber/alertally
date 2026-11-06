"""ACK, remoção de ACK e fechamento de alertas pela interface."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ADMIN, OTHER_USER, USER, login


@pytest.fixture
def open_alert(alerts):
    alerts.insert_one({
        "incident_id": "inc-1", "name": "HighCPU", "source": "alertmanager", "state": "open",
        "severity": "Critical", "severity_number": 5, "summary": "cpu", "url": "http://x",
        "update_at": datetime.now(timezone.utc), "labels": {},
    })
    return "inc-1"


def _post_json(client, url, **kw):
    return client.post(url, headers={"Content-Type": "application/json"}, **kw)


def test_alerts_sorted_by_severity_then_oldest_update(client, alerts):
    login(client)
    now = datetime.now(timezone.utc)
    for incident_id, severity, age in (
        ("warning-new", "Warning", 0),
        ("critical-new", "Critical", 1),
        ("warning-old", "Warning", 4),
        ("critical-old", "Critical", 2),
    ):
        alerts.insert_one({
            "incident_id": incident_id, "name": incident_id, "state": "open",
            "severity": severity, "severity_number": 5 if severity == "Critical" else 3,
            "summary": "test", "url": "http://x", "labels": {},
            "update_at": now - timedelta(hours=age),
        })

    response = client.get("/alerts")

    assert response.status_code == 200
    assert re.findall(r'<tr class="alert-row [^"]+" id="alert-([^"]+)"',
                      response.get_data(as_text=True)) == [
        "critical-old", "critical-new", "warning-old", "warning-new",
    ]


def test_ack_and_unack(client, alerts, open_alert):
    login(client)
    resp = _post_json(client, f"/ack/{open_alert}")
    assert resp.status_code == 200 and resp.json["success"]
    assert alerts.find_one({"incident_id": open_alert})["ack_by"] == USER["email"]

    resp = _post_json(client, f"/ack/{open_alert}")
    assert resp.status_code == 400

    resp = _post_json(client, f"/unack/{open_alert}")
    assert resp.status_code == 200
    assert "ack_by" not in alerts.find_one({"incident_id": open_alert})


def test_reports_separate_resource_service_and_source(client, alerts, monkeypatch):
    from routes import main

    login(client)
    for i, (source, labels) in enumerate([
        ('alertmanager', {'resource': 'api-geral', 'service': 'api'}),
        ('alertmanager', {'resource': 'api-geral', 'service': 'api'}),
        ('alertmanager', {'resource': 'api-validation', 'service': 'api'}),
        ('alertmanager', {'resource': 'api-geral', 'service': 'worker'}),
        ('gcp', {'resource': 'api-geral', 'service': 'api'}),
        ('alertmanager', {}),
    ]):
        alerts.insert_one({
            'incident_id': str(i), 'name': 'NodeMemoryUsage', 'source': source,
            'severity': 'Warning', 'severity_number': 3, 'labels': labels,
            'summary': 'Memory usage', 'update_at': datetime.now(timezone.utc),
        })
    context = {}
    render_template = main.render_template

    def capture(template, **kwargs):
        context.update(kwargs)
        return render_template(template, **kwargs)

    monkeypatch.setattr(main, 'render_template', capture)
    response = client.get('/reports')
    assert response.status_code == 200
    assert sorted(r['count'] for r in context['reports']) == [1, 1, 1, 1, 2]
    assert 'resource=api-validation' in response.get_data(as_text=True)
    assert 'service=worker' in response.get_data(as_text=True)


def test_cannot_remove_someone_elses_ack(client, alerts, open_alert):
    login(client)
    _post_json(client, f"/ack/{open_alert}")
    login(client, OTHER_USER)
    assert _post_json(client, f"/unack/{open_alert}").status_code == 403


def test_admin_can_remove_any_ack(client, alerts, open_alert):
    login(client)
    _post_json(client, f"/ack/{open_alert}")
    login(client, ADMIN)
    assert _post_json(client, f"/unack/{open_alert}").status_code == 200


def test_ack_unknown_alert(client, alerts):
    login(client)
    assert _post_json(client, "/ack/missing").status_code == 404


def test_only_admin_can_close(client, alerts, open_alert):
    login(client)
    assert client.post(f"/close/{open_alert}").status_code == 403
    assert alerts.find_one({"incident_id": open_alert})["state"] == "open"

    login(client, ADMIN)
    assert client.post(f"/close/{open_alert}").status_code == 302
    assert alerts.find_one({"incident_id": open_alert})["state"] == "closed"


def test_ack_message_is_translated(client, alerts, open_alert):
    login(client)
    resp = client.post(f"/ack/{open_alert}",
                       headers={"Content-Type": "application/json", "Accept-Language": "es"})
    assert resp.json["message"] == "Alerta reconocida por Ana"


class TestCsrf:
    @pytest.fixture(autouse=True)
    def csrf_on(self, app):
        app.config["WTF_CSRF_ENABLED"] = True

    def test_post_without_token_is_rejected(self, client, alerts, open_alert):
        login(client)
        assert _post_json(client, f"/ack/{open_alert}").status_code == 400
        assert "ack_by" not in alerts.find_one({"incident_id": open_alert})

    def test_post_with_token_from_page_works(self, client, alerts, open_alert):
        login(client)
        html = client.get("/").get_data(as_text=True)
        token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        resp = client.post(f"/ack/{open_alert}",
                           headers={"Content-Type": "application/json", "X-CSRFToken": token})
        assert resp.status_code == 200

    def test_close_form_carries_token(self, client, alerts, open_alert):
        login(client, ADMIN)
        html = client.get("/alerts").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        assert client.post(f"/close/{open_alert}", data={"csrf_token": token}).status_code == 302

    def test_webhooks_are_exempt(self, client, alerts):
        resp = client.post("/alertmanager", json={"alerts": []})
        assert resp.status_code == 200
