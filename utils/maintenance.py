import logging
from datetime import UTC, datetime

from pymongo.errors import PyMongoError

from utils.catalog_rules import rule_matches
from utils.helpers import alerts, db

windows = db.maintenance_windows


def active_windows(now=None):
    now = now or datetime.now(UTC)
    return list(windows.find({'starts_at': {'$lte': now}, 'ends_at': {'$gt': now}}).sort('ends_at', 1))


def window_rules(found):
    """Pair each window with the label rules it covers.

    A window covers its own rules plus the current rules of its catalog
    entries; a group stands for its member services.
    """
    ids = {identifier for window in found for identifier in window.get('entry_ids', [])}
    entries = {entry['_id']: entry for entry in db.catalog.find({'_id': {'$in': list(ids)}})} if ids else {}
    member_ids = {identifier for entry in entries.values() if entry['kind'] == 'group'
                  for identifier in entry.get('service_ids', [])} - set(entries)
    if member_ids:
        entries.update((entry['_id'], entry) for entry in db.catalog.find({'_id': {'$in': list(member_ids)}}))
    compiled = []
    for window in found:
        rules = list(window.get('rules', []))
        for identifier in window.get('entry_ids', []):
            entry = entries.get(identifier)
            if entry is None:
                continue
            members = ([entries.get(key) for key in entry.get('service_ids', [])]
                       if entry['kind'] == 'group' else [entry])
            rules.extend(rule for member in members if member for rule in member.get('rules', []))
        # An empty rule would match every alert.
        compiled.append((window, [rule for rule in rules if rule]))
    return compiled


def window_for(labels, compiled):
    """Return the first window whose rules match the labels, or None."""
    return next((window for window, rules in compiled
                 if any(rule_matches(labels, rule) for rule in rules)), None)


def mark_maintenance(alert_id, labels, at):
    """Record the window an alert opened in, so reports can leave it out."""
    try:
        found = active_windows(at)
        window = window_for(labels, window_rules(found)) if found else None
        if window:
            alerts.update_one({'incident_id': alert_id}, {'$set': {'maintenance_id': window['_id']}})
    except PyMongoError:
        logging.exception('Could not check maintenance windows for %s', alert_id)
