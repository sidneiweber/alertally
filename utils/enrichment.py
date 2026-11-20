from utils.catalog_rules import matching_services
from utils.helpers import db


def catalog_services():
    """All technical services, archived ones included: they still match their alerts."""
    return list(db.catalog.find({'kind': 'service'}).sort('name', 1))


def enrich_alerts(rows, services=None):
    """Add the matching services, owner, runbook and dashboard to each alert."""
    services = catalog_services() if services is None else services
    for alert in rows:
        matched = matching_services(alert, services)
        alert['services'] = matched
        alert['owner'] = next((s['owner'] for s in matched if s.get('owner')), '')
        # A runbook sent with the alert is specific to its rule, so it wins.
        alert['runbook'] = alert.get('runbook_url') or next(
            (s['runbook_url'] for s in matched if s.get('runbook_url')), '')
        alert['dashboard'] = next((s['dashboard_url'] for s in matched if s.get('dashboard_url')), '')
    return rows
