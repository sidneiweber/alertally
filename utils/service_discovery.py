import logging

from pymongo.errors import DuplicateKeyError, PyMongoError

from config import Config
from utils.helpers import db

catalog = db.catalog
NAME_LIMIT = 120
VALUE_LIMIT = 300
# Labels that identify what an alert is about, in order of preference. Prometheus
# label names cannot contain dots, so Kubernetes labels arrive with underscores.
IDENTIFIER_LABELS = ('app_kubernetes_io_name', 'app', 'resource', 'service_name', 'DBInstanceIdentifier',
                     'InstanceId', 'FunctionName', 'LoadBalancer', 'ClusterName', 'job')
# Labels that keep resources with the same name apart.
QUALIFIER_LABELS = ('namespace', 'environment', 'env', 'cluster', 'project', 'project_id',
                    'account', 'region')


def _usable(value, limit=VALUE_LIMIT):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit \
        and value.splitlines() == [value]


def candidate_rule(labels):
    """Return the rule that would identify the alert's resource, or None."""
    if not isinstance(labels, dict):
        return None
    identifiers = (*Config.CATALOG_SERVICE_LABELS, *IDENTIFIER_LABELS)
    key = next((key for key in identifiers if _usable(labels.get(key))), None)
    if key is None:
        return None
    rule = {key: labels[key]}
    rule.update((qualifier, labels[qualifier]) for qualifier in QUALIFIER_LABELS
                if qualifier != key and _usable(labels.get(qualifier)))
    return rule


def service_label(labels):
    """Return the first (label, value) from CATALOG_SERVICE_LABELS that can name a catalog entry."""
    if not isinstance(labels, dict):
        return None
    return next(((key, labels[key]) for key in Config.CATALOG_SERVICE_LABELS
                 if _usable(labels.get(key), NAME_LIMIT)), None)


def ensure_service(labels, at):
    """Create the technical service named by the alert's service label.

    With several labels configured, the first one present names the service, so
    an alert never creates more than one entry.

    The entry is written only on insert, so later edits are never overwritten,
    and an archived entry is not created again.
    """
    if not Config.CATALOG_AUTO_CREATE:
        return
    found = service_label(labels)
    if found is None:
        return
    key, value = found
    try:
        covered = catalog.update_many(
            {'kind': 'service', f'rules.{key}': value},
            {'$max': {'last_alert_at': at}},
        )
        if covered.matched_count:
            return
        catalog.update_one(
            {'auto_key': f'{key}={value}'},
            {'$setOnInsert': {
                'kind': 'service', 'name': value, 'origin': 'auto', 'rules': [{key: value}],
                'description': '', 'owner': '', 'environment': '',
                'dashboard_url': '', 'runbook_url': '', 'repository_url': '',
            }, '$max': {'last_alert_at': at}},
            upsert=True,
        )
    except DuplicateKeyError:
        # A concurrent delivery created the same entry.
        pass
    except PyMongoError:
        logging.exception('Could not register service %r in the catalog', value)
