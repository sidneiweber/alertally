from datetime import UTC, datetime, timedelta

import pytest
from conftest import ADMIN, login

from routes.catalog import catalog
from utils.maintenance import windows

FORMAT = '%Y-%m-%dT%H:%M'


@pytest.fixture(autouse=True)
def clean(alerts):
    for collection in (catalog, windows):
        collection.delete_many({})
    yield
    for collection in (catalog, windows):
        collection.delete_many({})


def send(client, fingerprint, name='High latency', annotations=None, **labels):
    response = client.post('/alertmanager', json={'alerts': [{
        'fingerprint': fingerprint, 'startsAt': '2026-11-18T14:00:00Z', 'status': 'firing',
        'labels': {'alertname': name, **labels}, 'annotations': annotations or {},
    }]})
    assert response.status_code == 200


def service(client, name, rules, **fields):
    response = client.post('/catalog/new/service', data={'name': name, 'rules': rules, **fields})
    assert response.status_code == 302
    return response.headers['Location'].rsplit('/', 1)[1]


def window_form(minutes_from_now=-1, hours=1, **data):
    starts = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    return {'title': 'Deploy', 'starts_at': starts.strftime(FORMAT),
            'ends_at': (starts + timedelta(hours=hours)).strftime(FORMAT), **data}


def create_window(client, **data):
    response = client.post('/maintenance/new', data=window_form(**data))
    assert response.status_code == 302, response.data
    return response.headers['Location'].rsplit('/', 1)[1]


def row(page, fingerprint):
    """The <tr> opening tag of an alert in the alert list."""
    html = page.data.decode()
    start = html.rfind('<tr', 0, html.index(f'id="alert-am-{fingerprint}-'))
    return html[start:html.index('>', start)]


# Enrichment

def test_alert_list_shows_service_owner_and_links(client):
    login(client)
    service(client, 'Orders API', 'service=orders-api', owner='payments',
            runbook_url='https://wiki.example.com/orders', dashboard_url='https://grafana.example.com/orders')
    send(client, 'one', service='orders-api')
    send(client, 'two', name='Disk full', instance='vm-1')
    page = client.get('/alerts')
    assert b'Orders API' in page.data and b'payments' in page.data
    assert b'https://wiki.example.com/orders' in page.data
    assert b'https://grafana.example.com/orders' in page.data
    assert page.data.count(b'class="alert-context small"') == 1


def test_alert_runbook_takes_precedence(client, alerts):
    login(client)
    service(client, 'Orders API', 'service=orders-api', runbook_url='https://wiki.example.com/service')
    send(client, 'one', annotations={'runbook_url': 'https://wiki.example.com/alert'}, service='orders-api')
    send(client, 'two', annotations={'runbook_url': 'javascript:alert(1)'}, service='orders-api')
    assert alerts.find_one({'incident_id': {'$regex': '^am-two'}})['runbook_url'] is None
    page = client.get('/alerts')
    assert b'https://wiki.example.com/alert' in page.data
    assert page.data.count(b'https://wiki.example.com/service') == 1
    assert b'javascript:' not in page.data


def test_rule_created_later_enriches_open_alert(client, alerts):
    login(client)
    send(client, 'one', resource='orders-db')
    assert b'Orders database' not in client.get('/alerts').data
    service(client, 'Orders database', 'resource=orders-db', owner='storage')
    assert b'Orders database' in client.get('/alerts').data
    alert = alerts.find_one({})
    incident = client.post(f"/incidents/new/{alert['_id']}", data={'title': 'DB', 'severity': 'Warning'})
    assert b'storage' in client.get(incident.headers['Location']).data


# Maintenance windows

def test_only_admins_manage_windows(client):
    login(client)
    assert client.get('/maintenance').status_code == 200
    assert b'New window' not in client.get('/maintenance').data
    assert client.get('/maintenance/new').status_code == 403
    assert client.post('/maintenance/new', data=window_form(rules='service=x')).status_code == 403
    login(client, ADMIN)
    window_id = create_window(client, rules='service=x')
    login(client)
    assert client.post(f'/maintenance/{window_id}/end').status_code == 403
    assert b'End now' not in client.get(f'/maintenance/{window_id}').data


@pytest.mark.parametrize('data', [
    window_form(),
    window_form(rules='service=x', title=''),
    window_form(rules='service=x', hours=25),
    window_form(rules='service=x', hours=-1),
    window_form(rules='service=x', minutes_from_now=-180),
    window_form(rules='service=x', starts_at='tomorrow'),
    window_form(rules='$where=1'),
    window_form(entry_ids=['a' * 24]),
])
def test_window_validation(client, data):
    login(client, ADMIN)
    assert client.post('/maintenance/new', data=data).status_code == 400
    assert windows.count_documents({}) == 0


def test_active_window_holds_alerts_until_it_ends(client, alerts):
    login(client, ADMIN)
    service_id = service(client, 'Orders API', 'service=orders-api')
    send(client, 'before', service='orders-api')
    send(client, 'other', name='Disk full', instance='vm-1')
    window_id = create_window(client, title='Deploy orders', entry_ids=[service_id])
    send(client, 'during', service='orders-api')

    page = client.get('/alerts')
    assert 'maintenance-row' in row(page, 'before') and 'maintenance-row' in row(page, 'during')
    assert 'maintenance-row' not in row(page, 'other')
    assert b'2 alerts in maintenance' in page.data
    assert b'Maintenance in progress: Deploy orders' in page.data
    assert client.get('/count').data == b'1'
    # Only the occurrence that opened during the window is recorded in it.
    assert alerts.find_one({'incident_id': {'$regex': '^am-during'}})['maintenance_id'] is not None
    assert 'maintenance_id' not in alerts.find_one({'incident_id': {'$regex': '^am-before'}})

    detail = client.get(f'/maintenance/{window_id}')
    assert b'Alerts in maintenance now' in detail.data and b'End now' in detail.data

    assert client.post(f'/maintenance/{window_id}/end').status_code == 302
    page = client.get('/alerts')
    assert 'maintenance-row' not in row(page, 'before')
    assert b'in maintenance' not in page.data
    assert client.get('/count').data == b'3'
    assert windows.find_one({})['ended_by'] == ADMIN['email']


def test_scheduled_window_waits_and_can_be_cancelled(client, alerts):
    login(client, ADMIN)
    window_id = create_window(client, minutes_from_now=60, rules='service=orders-api')
    send(client, 'one', service='orders-api')
    assert 'maintenance-row' not in row(client.get('/alerts'), 'one')
    assert b'Scheduled' in client.get('/maintenance').data
    assert b'Cancel window' in client.get(f'/maintenance/{window_id}').data
    client.post(f'/maintenance/{window_id}/end')
    window = windows.find_one({})
    assert window['starts_at'] == window['ends_at']
    assert b'Cancel window' not in client.get(f'/maintenance/{window_id}').data


def test_group_and_rule_windows(client):
    login(client, ADMIN)
    member = service(client, 'Orders API', 'service=orders-api')
    service(client, 'Billing', 'service=billing')
    group = client.post('/catalog/new/group', data={'name': 'Checkout', 'service_ids': [member]})
    group_id = group.headers['Location'].rsplit('/', 1)[1]
    create_window(client, entry_ids=[group_id], rules='instance=vm-1')
    send(client, 'member', service='orders-api')
    send(client, 'outside', service='billing')
    send(client, 'rule', name='Disk full', instance='vm-1')
    page = client.get('/alerts')
    assert 'maintenance-row' in row(page, 'member')
    assert 'maintenance-row' in row(page, 'rule')
    assert 'maintenance-row' not in row(page, 'outside')


def test_reports_leave_out_maintenance_alerts(client):
    login(client, ADMIN)
    create_window(client, rules='service=orders-api')
    send(client, 'held', name='Deploy noise', service='orders-api')
    send(client, 'real', name='Disk full', instance='vm-1')
    report = client.get('/reports?days=7')
    assert b'Disk full' in report.data and b'Deploy noise' not in report.data
    assert b'1 alert opened during maintenance windows is left out.' in report.data
    included = client.get('/reports?days=7&maintenance=1')
    assert b'Deploy noise' in included.data and b'days=30&amp;maintenance=1' in included.data


def test_maintenance_requires_login_and_csrf(app, client):
    assert client.get('/maintenance').status_code == 302
    login(client, ADMIN)
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/maintenance/new', data=window_form(rules='service=x')).status_code == 400
    assert windows.count_documents({}) == 0


def test_catalog_offers_maintenance_to_admins(client):
    login(client)
    service_id = service(client, 'Orders API', 'service=orders-api')
    assert b'Schedule maintenance' not in client.get(f'/catalog/{service_id}').data
    login(client, ADMIN)
    assert b'Schedule maintenance' in client.get(f'/catalog/{service_id}').data
    form = client.get(f'/maintenance/new?entry_id={service_id}')
    assert f'value="{service_id}" checked'.encode() in form.data
