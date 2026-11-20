import pytest
from conftest import OTHER_USER, login

from routes.catalog import catalog, dismissed, incidents
from utils.service_discovery import candidate_rule


@pytest.fixture(autouse=True)
def operations_data(alerts):
    for collection in (catalog, incidents, dismissed):
        collection.delete_many({})
    yield
    for collection in (catalog, incidents, dismissed):
        collection.delete_many({})


def send_alert(client, fingerprint, name, environment='production', status='firing'):
    return client.post('/alertmanager', json={'alerts': [{
        'fingerprint': fingerprint, 'startsAt': '2026-11-18T14:00:00Z',
        'status': status,
        'labels': {'alertname': name, 'resource': 'orders-db', 'environment': environment},
    }]})


def create_service(client, name, **data):
    response = client.post('/catalog/new/service', data={'name': name, **data})
    assert response.status_code == 302
    return response.headers['Location']


def test_catalog_matches_existing_and_new_alerts_and_groups(client):
    login(client)
    assert send_alert(client, 'one', 'Connections exhausted').status_code == 200
    assert send_alert(client, 'two', 'Staging connections', 'staging').status_code == 200
    form = client.get('/catalog/new/service')
    assert b'resource=orders-db' in form.data
    service_url = create_service(client, 'Orders database',
                                 observed='resource=orders-db\nenvironment=production')
    duplicate_match_url = create_service(client, 'Orders storage',
                                         rules='resource=other\n\nresource=orders-db\nenvironment=production')
    service_ids = [url.rsplit('/', 1)[1] for url in (service_url, duplicate_match_url)]
    for name in ('Checkout', 'Orders'):
        group = client.post('/catalog/new/group', data={'name': name, 'service_ids': service_ids})
        assert group.status_code == 302
        page = client.get(group.headers['Location'])
        assert page.status_code == 200
        assert page.data.count(b'Connections exhausted') == 1
        assert b'Staging connections' not in page.data
        assert page.data.count(b'Orders storage') == 2

    overview = client.get('/catalog')
    assert overview.data.count(b'1 active alert') == 4
    assert overview.data.count(b"ops-card-alert") == 4

    send_alert(client, 'three', 'Pool near limit')
    assert client.get('/catalog').data.count(b'2 active alerts') == 4
    send_alert(client, 'one', 'Connections exhausted', status='resolved')
    page = client.get(service_url)
    assert b'Pool near limit' in page.data
    assert b'Connections exhausted' in page.data
    assert b'Recent closed alerts' in page.data
    assert b'Staging connections' not in page.data
    assert b'Confirmed impact' not in client.get('/catalog').data
    assert b'Checkout' in page.data and b'Orders' in page.data


def test_catalog_validates_associations(client):
    login(client)
    assert client.post('/catalog/new/service', data={
        'name': 'Orders database', 'rules': 'resource=orders-db\nresource=other',
    }).status_code == 400
    assert client.post('/catalog/new/service', data={
        'name': 'Orders database', 'rules': '$where=anything',
    }).status_code == 400
    assert client.post('/catalog/new/group', data={
        'name': 'Checkout', 'service_ids': ['a' * 24],
    }).status_code == 400
    assert catalog.count_documents({}) == 0


@pytest.mark.parametrize('rule', ['resource=orders-db', 'resource="orders-db"'])
def test_catalog_accepts_a_single_resource_label(client, rule):
    login(client)
    assert send_alert(client, 'one', 'Connections exhausted').status_code == 200
    service_url = create_service(client, 'Orders database', rules=rule)
    assert b'Connections exhausted' in client.get(service_url).data


def test_incident_lifecycle_and_timeline(client, alerts):
    login(client)
    send_alert(client, 'one', 'Connections exhausted')
    send_alert(client, 'two', 'Response time increasing')
    first = alerts.find_one({'name': 'Connections exhausted'})
    second = alerts.find_one({'name': 'Response time increasing'})
    start = f"/incidents/new/{first['_id']}"
    assert client.get('/alerts').data.count(start.encode()) == 1
    assert client.get(start).status_code == 200
    form = {'title': 'Checkout latency', 'severity': 'Critical'}
    response = client.post(start, data=form)
    assert response.status_code == 302
    incident_url = response.headers['Location']
    assert client.post(start, data=form).headers['Location'] == incident_url
    assert incidents.count_documents({}) == 1
    assert b'Checkout latency' in client.get('/incidents').data
    service_url = create_service(client, 'Orders database', rules='resource=orders-db')
    service_id = service_url.rsplit('/', 1)[1]
    group = client.post('/catalog/new/group', data={'name': 'Checkout', 'service_ids': [service_id]})
    group_url = group.headers['Location']

    assert b'Response time increasing' in client.get(f'{incident_url}?q=Response').data
    for _ in range(2):
        client.post(incident_url, data={'action': 'link', 'alert_ids': str(second['_id'])})
    client.post(incident_url, data={'action': 'impact', 'impact': 'Orders are taking longer.',
                                    'impact_ids': [service_id, group_url.rsplit('/', 1)[1]]})
    client.post(incident_url, data={'action': 'note', 'note': 'Investigating connection limits.'})
    assert b'Confirmed impact' in client.get(service_url).data
    assert b'Checkout latency' in client.get(group_url).data
    send_alert(client, 'one', 'Connections exhausted')
    for _ in range(2):
        send_alert(client, 'one', 'Connections exhausted', status='resolved')

    detail = client.get(incident_url)
    assert detail.status_code == 200
    assert detail.data.count(b'Source reported recovery') == 1
    assert detail.data.count(b'Alert opened') == 2
    assert detail.data.count(b'Linked an alert') == 2
    assert b'Investigating connection limits.' in detail.data
    assert incidents.find_one()['status'] == 'investigating'

    login(client, OTHER_USER)
    assert client.post(incident_url, data={'action': 'status', 'status': 'resolved'}).status_code == 403
    client.post(incident_url, data={'action': 'assign'})
    client.post(incident_url, data={'action': 'status', 'status': 'mitigated'})
    client.post(incident_url, data={'action': 'status', 'status': 'resolved'})
    assert incidents.find_one()['status'] == 'resolved'
    assert b'Checkout latency' not in client.get('/incidents').data
    assert b'Checkout latency' in client.get('/incidents?status=resolved').data
    assert b'Checkout latency' not in client.get(group_url).data
    assert b'Took ownership' in client.get(incident_url).data
    assert b'Assumiu o incidente' in client.get(incident_url, headers={'Accept-Language': 'pt-BR'}).data


def test_operations_require_login_and_csrf(app, client):
    assert client.get('/catalog').status_code == 302
    assert client.get('/incidents').status_code == 302
    assert client.post('/catalog/new/group', data={'name': 'Checkout'}).status_code == 302
    login(client)
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/catalog/new/group', data={'name': 'Checkout'}).status_code == 400


def send_labeled_alert(client, fingerprint, **labels):
    return client.post('/alertmanager', json={'alerts': [{
        'fingerprint': fingerprint, 'startsAt': '2026-11-18T14:00:00Z', 'status': 'firing',
        'labels': {'alertname': 'High latency', **labels},
    }]})


def test_service_label_creates_catalog_entry_once(client):
    login(client)
    assert send_labeled_alert(client, 'one', service='orders-api').status_code == 200
    assert send_labeled_alert(client, 'two', service='orders-api', environment='staging').status_code == 200
    entry = catalog.find_one({'name': 'orders-api'})
    assert catalog.count_documents({}) == 1
    assert entry['origin'] == 'auto' and entry['kind'] == 'service'
    assert entry['rules'] == [{'service': 'orders-api'}]
    assert entry['last_alert_at'] is not None
    overview = client.get('/catalog')
    assert b'Automatic' in overview.data and b'2 active alerts' in overview.data


def test_service_label_keeps_manual_entries_and_edits(client):
    login(client)
    create_service(client, 'Orders', rules='service=orders-api\nenvironment=production')
    send_labeled_alert(client, 'one', service='orders-api', environment='production')
    assert catalog.count_documents({}) == 1
    assert catalog.find_one({'name': 'Orders'})['last_alert_at'] is not None

    send_labeled_alert(client, 'two', service='billing')
    entry = catalog.find_one({'auto_key': 'service=billing'})
    response = client.post(f"/catalog/{entry['_id']}/edit", data={
        'name': 'Billing API', 'owner': 'payments', 'rules': 'service=billing'})
    assert response.status_code == 302
    send_labeled_alert(client, 'three', service='billing')
    entry = catalog.find_one({'_id': entry['_id']})
    assert (entry['name'], entry['owner'], entry['origin']) == ('Billing API', 'payments', 'auto')
    assert catalog.count_documents({}) == 2


def test_archived_service_is_not_recreated(client):
    login(client)
    send_labeled_alert(client, 'one', service='orders-api')
    entry = catalog.find_one({'auto_key': 'service=orders-api'})
    # Even without the original rule, the entry keeps its place for this service value.
    client.post(f"/catalog/{entry['_id']}/edit", data={'name': 'Orders', 'rules': 'resource=orders'})
    response = client.post(f"/catalog/{entry['_id']}/archive", data={'archived': '1'})
    assert response.status_code == 302
    send_labeled_alert(client, 'two', service='orders-api')
    assert catalog.count_documents({}) == 1
    overview = client.get('/catalog')
    assert b'>Orders<' not in overview.data and b'Show 1 archived entry' in overview.data
    archived = client.get('/catalog?archived=1')
    assert b'>Orders<' in archived.data and b'Archived' in archived.data
    client.post(f"/catalog/{entry['_id']}/archive", data={'archived': '0'})
    assert b'>Orders<' in client.get('/catalog').data


@pytest.mark.parametrize('value', ['', '   ', 'orders\napi', 'x' * 121])
def test_invalid_service_label_does_not_create_entry(client, alerts, value):
    assert send_labeled_alert(client, 'one', service=value).status_code == 200
    assert alerts.count_documents({}) == 1
    assert catalog.count_documents({}) == 0


def test_service_discovery_settings(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'CATALOG_AUTO_CREATE', False)
    send_labeled_alert(client, 'one', service='orders-api')
    assert catalog.count_documents({}) == 0
    monkeypatch.setattr(Config, 'CATALOG_AUTO_CREATE', True)
    monkeypatch.setattr(Config, 'CATALOG_SERVICE_LABELS', ['app'])
    send_labeled_alert(client, 'two', service='orders-api', app='checkout')
    assert [entry['rules'] for entry in catalog.find()] == [[{'app': 'checkout'}]]


def test_service_labels_follow_priority(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, 'CATALOG_SERVICE_LABELS', ['service', 'resource'])
    send_labeled_alert(client, 'one', service='orders-api', resource='orders-db')
    send_labeled_alert(client, 'two', resource='orders-db', environment='production')
    send_labeled_alert(client, 'three', service='  ', resource='payments-db')
    send_labeled_alert(client, 'four', resource='orders-db', environment='staging')
    entries = {entry['name']: entry['rules'] for entry in catalog.find()}
    assert entries == {
        'orders-api': [{'service': 'orders-api'}],
        'orders-db': [{'resource': 'orders-db'}],
        'payments-db': [{'resource': 'payments-db'}],
    }


def test_service_labels_setting_is_validated(monkeypatch):
    from config import _env_labels
    monkeypatch.setenv('CATALOG_SERVICE_LABELS', 'service, resource ,app.kubernetes.io/name')
    assert _env_labels('CATALOG_SERVICE_LABELS', ['service']) == ['service', 'resource']
    monkeypatch.setenv('CATALOG_SERVICE_LABELS', ' , ')
    assert _env_labels('CATALOG_SERVICE_LABELS', ['service']) == ['service']


def test_gcp_policy_label_creates_service(client, alerts):
    payload = {'version': '1.2', 'incident': {
        'incident_id': '0.svc', 'condition_name': 'Latency', 'state': 'open',
        'resource': {'type': 'k8s_container', 'labels': {'namespace_name': 'shop', 'service': 'resource-side'}},
        'metric': {'labels': {'response_code': '500'}},
        'policy_user_labels': {'service': 'checkout'},
    }}
    assert client.post('/gcp', json=payload).status_code == 200
    labels = alerts.find_one({'incident_id': '0.svc'})['labels']
    assert labels == {'namespace_name': 'shop', 'service': 'checkout', 'response_code': '500'}
    assert catalog.find_one({'auto_key': 'service=checkout'}) is not None


def test_catalog_failure_does_not_block_alert(client, alerts, monkeypatch):
    from pymongo.errors import PyMongoError

    from utils import service_discovery

    def fail(*args, **kwargs):
        raise PyMongoError('catalog unavailable')

    monkeypatch.setattr(service_discovery.catalog, 'update_many', fail)
    assert send_labeled_alert(client, 'one', service='orders-api').status_code == 200
    assert alerts.count_documents({}) == 1


def test_stale_service_is_flagged(client):
    from datetime import UTC, datetime, timedelta
    login(client)
    send_labeled_alert(client, 'one', service='orders-api')
    assert b'No recent activity' not in client.get('/catalog').data
    catalog.update_one({}, {'$set': {'last_alert_at': datetime.now(UTC) - timedelta(days=31)}})
    assert b'No recent activity' in client.get('/catalog').data


@pytest.mark.parametrize('labels, rule', [
    ({'service': 'orders-api', 'app': 'web', 'environment': 'production'},
     {'service': 'orders-api', 'environment': 'production'}),
    ({'job': 'node-exporter', 'app': 'web', 'namespace': 'shop'}, {'app': 'web', 'namespace': 'shop'}),
    ({'resource': 'orders-db', 'cluster': 'prod-1', 'env': 'prod'},
     {'resource': 'orders-db', 'env': 'prod', 'cluster': 'prod-1'}),
    ({'DBInstanceIdentifier': 'orders', 'account': '123', 'region': 'us-east-1', 'metric': 'CPU',
      'aws_namespace': 'AWS/RDS'},
     {'DBInstanceIdentifier': 'orders', 'account': '123', 'region': 'us-east-1'}),
    ({'service_name': 'notifications', 'project_id': 'demo', 'location': 'us-east1'},
     {'service_name': 'notifications', 'project_id': 'demo'}),
    ({'service': '  ', 'instance': 'vm-1'}, None),
    ({'environment': 'production'}, None),
    (None, None),
])
def test_candidate_rule(labels, rule):
    assert candidate_rule(labels) == rule


def seed_suggestions(client):
    send_labeled_alert(client, 'db-1', resource='orders-db', environment='production')
    send_labeled_alert(client, 'db-2', alertname='Replication lag', resource='orders-db', environment='production')
    send_labeled_alert(client, 'web', app='web', job='kubernetes-pods', namespace='shop')
    send_labeled_alert(client, 'svc', service='orders-api')
    send_labeled_alert(client, 'none', instance='vm-1')


def test_suggestions_group_uncovered_alerts(client):
    login(client)
    seed_suggestions(client)
    page = client.get('/catalog/suggestions')
    assert page.status_code == 200
    assert b'resource=orders-db \xc2\xb7 environment=production' in page.data
    assert b'2 alerts' in page.data and b'Replication lag' in page.data
    assert b'app=web \xc2\xb7 namespace=shop' in page.data
    assert b'service=orders-api' not in page.data
    assert b'1 of 5 alerts' in page.data and b'(20%)' in page.data
    assert b'2 suggestions' in client.get('/catalog').data


def test_suggestion_can_be_ignored_and_restored(client):
    login(client)
    seed_suggestions(client)
    rule = 'app=web\nnamespace=shop'
    assert client.post('/catalog/suggestions/dismiss', data={'rule': rule}).status_code == 302
    assert client.post('/catalog/suggestions/dismiss', data={'rule': rule}).status_code == 302
    assert dismissed.count_documents({}) == 1
    assert b'app=web' not in client.get('/catalog/suggestions').data
    ignored = client.get('/catalog/suggestions?dismissed=1')
    assert b'app=web' in ignored.data and b'resource=orders-db' not in ignored.data
    client.post('/catalog/suggestions/dismiss', data={'rule': rule, 'restore': '1'})
    assert b'app=web' in client.get('/catalog/suggestions').data


def test_service_created_from_suggestion(client):
    login(client)
    seed_suggestions(client)
    form = client.get('/catalog/new/service?name=orders-db&rules=resource%3Dorders-db%0Aenvironment%3Dproduction')
    assert b'value="orders-db"' in form.data
    assert b'resource=orders-db\nenvironment=production</textarea>' in form.data
    create_service(client, 'Orders database', rules='resource=orders-db\nenvironment=production')
    page = client.get('/catalog/suggestions')
    assert b'resource=orders-db' not in page.data and b'3 of 5 alerts' in page.data


def test_suggestion_rule_added_to_existing_service(client):
    login(client)
    seed_suggestions(client)
    service = catalog.find_one({'auto_key': 'service=orders-api'})
    data = {'rule': 'resource=orders-db\nenvironment=production', 'service_id': str(service['_id'])}
    assert client.post('/catalog/suggestions/add', data=data).status_code == 302
    assert client.post('/catalog/suggestions/add', data=data).status_code == 302
    assert catalog.find_one({'_id': service['_id']})['rules'] == [
        {'service': 'orders-api'}, {'resource': 'orders-db', 'environment': 'production'}]
    assert b'resource=orders-db' not in client.get('/catalog/suggestions').data
    assert client.post('/catalog/suggestions/add', data={**data, 'rule': '$where=x'}).status_code == 400
    assert client.post('/catalog/suggestions/add', data={**data, 'rule': 'a=1\n\nb=2'}).status_code == 400
    response = client.post('/catalog/suggestions/add', data={**data, 'service_id': 'a' * 24})
    assert response.status_code == 302 and catalog.count_documents({}) == 1


def test_suggestions_require_login_and_csrf(app, client):
    assert client.get('/catalog/suggestions').status_code == 302
    assert client.post('/catalog/suggestions/dismiss', data={'rule': 'app=web'}).status_code == 302
    login(client)
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/catalog/suggestions/dismiss', data={'rule': 'app=web'}).status_code == 400
    assert client.post('/catalog/suggestions/add', data={'rule': 'app=web'}).status_code == 400
