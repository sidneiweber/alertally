"""Webhook genérico (/events)."""
from datetime import datetime, timezone

import pytest

from config import Config
from utils.helpers import db


def event(**fields):
    base = {"id": "disk/vm-1", "name": "Disk full", "status": "firing", "severity": "critical",
            "summary": "Disk at 95%", "labels": {"host": "vm-1"}}
    base.update(fields)
    return base


@pytest.fixture(autouse=True)
def clean_catalog():
    db.catalog.delete_many({})
    yield
    db.catalog.delete_many({})


def test_firing_creates_alert(client, alerts):
    resp = client.post("/events", json=event(
        url="https://monitor.example.com/disk", runbook_url="https://wiki.example.com/disk",
        description="Root volume", labels={"host": "vm-1", "k8s.namespace": "prod", "retries": 3,
                                           "paged": True, "empty": None},
    ))
    assert resp.status_code == 200
    assert resp.json["results"][0]["state"] == "open"
    doc = alerts.find_one({})
    assert doc["incident_id"] == resp.json["results"][0]["incident"]
    assert doc["source"] == "webhook" and doc["event_key"] == "webhook/disk/vm-1"
    assert doc["state"] == "open" and doc["name"] == "Disk full"
    assert doc["severity"] == "Critical" and doc["severity_number"] == 5
    assert doc["summary"] == "Disk at 95%" and doc["description"] == "Root volume"
    assert doc["url"] == "https://monitor.example.com/disk"
    assert doc["runbook_url"] == "https://wiki.example.com/disk"
    assert doc["labels"] == {"host": "vm-1", "k8s_namespace": "prod", "retries": "3", "paged": "true"}
    assert [e["kind"] for e in doc["history"]] == ["alert_opened"]


def test_repeated_firing_updates_the_same_alert(client, alerts):
    client.post("/events", json=event())
    client.post("/events", json=event(summary="Disk at 97%"))
    assert alerts.count_documents({}) == 1
    assert alerts.find_one({})["summary"] == "Disk at 97%"


def test_resolved_closes_and_new_firing_opens_new_occurrence(client, alerts):
    client.post("/events", json=event())
    resp = client.post("/events", json={"id": "disk/vm-1", "status": "resolved"})
    assert resp.json["results"][0] == {"id": "webhook/disk/vm-1", "state": "closed", "closed": 1}
    first = alerts.find_one({})
    assert first["state"] == "closed"
    assert [e["kind"] for e in first["history"]] == ["alert_opened", "alert_recovered"]

    client.post("/events", json=event())
    assert alerts.count_documents({}) == 2
    assert alerts.count_documents({"state": "open"}) == 1


def test_resolved_without_open_alert(client, alerts):
    resp = client.post("/events", json={"id": "nothing", "status": "resolved"})
    assert resp.status_code == 200 and resp.json["results"][0]["closed"] == 0
    assert alerts.count_documents({}) == 0


@pytest.mark.parametrize("opened, closed", [("down", "up"), ("problem", "ok"), ("TRIGGERED", "Recovered")])
def test_status_aliases(client, alerts, opened, closed):
    client.post("/events", json=event(status=opened))
    assert alerts.find_one({})["state"] == "open"
    client.post("/events", json=event(status=closed))
    assert alerts.find_one({})["state"] == "closed"


def test_same_id_from_different_sources_are_separate(client, alerts):
    client.post("/events", json=event(source="uptime-kuma"))
    client.post("/events", json=event(source="zabbix"))
    assert sorted(alerts.distinct("source")) == ["uptime-kuma", "zabbix"]
    client.post("/events", json={"id": "disk/vm-1", "source": "zabbix", "status": "resolved"})
    assert alerts.find_one({"source": "uptime-kuma"})["state"] == "open"


@pytest.mark.parametrize("starts_at", ["2026-11-01T10:00:00Z", "2026-11-01T07:00:00-03:00", 1793527200])
def test_starts_at_sets_opening_time(client, alerts, starts_at):
    client.post("/events", json=event(starts_at=starts_at))
    opened = alerts.find_one({})["opened_at"].replace(tzinfo=timezone.utc)
    assert opened == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def test_batch_is_stored(client, alerts):
    resp = client.post("/events", json=[event(id="a"), event(id="b"), {"id": "a", "status": "resolved"}])
    assert resp.json["processed"] == 3
    assert alerts.find_one({"event_key": "webhook/a"})["state"] == "closed"
    assert alerts.find_one({"event_key": "webhook/b"})["state"] == "open"


@pytest.mark.parametrize("bad, message", [
    ({"id": None}, '"id" is required'),
    ({"name": ""}, '"name" is required'),
    ({"status": "maybe"}, '"status" must be'),
    ({"status": None}, '"status" must be'),
    ({"labels": ["a"]}, '"labels" must be an object'),
    ({"labels": {"a": {"b": 1}}}, 'label "a"'),
    ({"labels": {f"k{i}": "v" for i in range(51)}}, "at most 50"),
    ({"starts_at": "yesterday"}, '"starts_at"'),
    ({"starts_at": "2026-11-01T10:00:00"}, '"starts_at"'),
    ({"source": "aws"}, '"source"'),
    ({"source": "Has Spaces"}, '"source"'),
    ({"name": ["x"]}, '"name" must be a string'),
])
def test_invalid_event_rejects_whole_batch(client, alerts, bad, message):
    resp = client.post("/events", json=[event(id="ok"), event(**bad)])
    assert resp.status_code == 400
    assert message in resp.json["error"] and resp.json["index"] == 1
    assert alerts.count_documents({}) == 0


@pytest.mark.parametrize("body", [None, [], "text", 42])
def test_invalid_body(client, alerts, body):
    resp = (client.post("/events", data="not json", content_type="application/json") if body is None
            else client.post("/events", json=body))
    assert resp.status_code == 400


def test_batch_limit(client, alerts):
    resp = client.post("/events", json=[event(id=str(i)) for i in range(101)])
    assert resp.status_code == 400 and alerts.count_documents({}) == 0


def test_unknown_severity_and_long_fields(client, alerts):
    client.post("/events", json=event(severity="weird", summary="x" * 5000, url="javascript:alert(1)"))
    doc = alerts.find_one({})
    assert doc["severity"] == "Unknown"
    assert len(doc["summary"]) == 2000
    assert doc["url"] is None


def test_token(client, alerts, monkeypatch):
    monkeypatch.setattr(Config, "EVENTS_TOKEN", "tok")
    assert client.post("/events", json=event()).status_code == 401
    assert client.post("/events", json=event(), headers={"Authorization": "Bearer nope"}).status_code == 401
    resp = client.post("/events", json=event(), headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200 and alerts.count_documents({}) == 1


def test_service_label_feeds_the_catalog(client, alerts):
    client.post("/events", json=event(labels={"service": "orders-api"}))
    assert db.catalog.find_one({"auto_key": "service=orders-api"})


def test_alert_shows_in_list(client, alerts):
    from conftest import login
    client.post("/events", json=event(source="uptime-kuma"))
    login(client)
    page = client.get("/alerts")
    assert b"Disk full" in page.data and b"uptime-kuma" in page.data
