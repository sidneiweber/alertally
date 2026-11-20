from pymongo.errors import DuplicateKeyError

from utils.helpers import alerts
from utils.service_discovery import ensure_service


def alert_event(kind, at, actor=None):
    return {'kind': kind, 'at': at, 'actor': actor}


def store_alert(alert_id, data, opened_at):
    """Store the alert and register the service named by its labels."""
    result = _write_alert(alert_id, data, opened_at)
    ensure_service(data.get('labels'), data['update_at'])
    return result


def _write_alert(alert_id, data, opened_at):
    """Record state transitions in the same write that changes the alert."""
    identity = {'incident_id': alert_id}
    state = data['state']
    fields = {'$set': data, '$min': {'opened_at': opened_at}}
    event = alert_event('alert_opened' if state == 'open' else 'alert_recovered',
                        data['update_at'])
    transition = {**fields, '$push': {'history': event}}

    while True:
        result = alerts.update_one({**identity, 'state': state}, fields)
        if result.matched_count:
            return result
        try:
            return alerts.update_one(
                {**identity, 'state': {'$ne': state}}, transition, upsert=True,
            )
        except DuplicateKeyError:
            # A concurrent delivery inserted or changed the same occurrence.
            continue
