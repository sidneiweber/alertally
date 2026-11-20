"""Generic webhook: any tool that can send JSON creates and resolves alerts here.

Format and examples: docs/webhook.md
"""
import hashlib
import re
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request
from pytz import timezone

from config import Config
from routes.alerts import _check_bearer, _http_url, _incident_opened_at, _normalize_severity
from utils.alert_history import alert_event, store_alert
from utils.helpers import alerts, alerts_dict

events_bp = Blueprint('events', __name__)

MAX_EVENTS = 100
MAX_LABELS = 50
LABEL_KEY = re.compile(r'[a-zA-Z_][a-zA-Z0-9_-]{0,127}')
SOURCE = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}')
# Sources with their own endpoint and handling.
RESERVED_SOURCES = {'gcp', 'alertmanager', 'aws'}
OPEN_STATUSES = {'firing', 'triggered', 'problem', 'open', 'down', 'alert'}
CLOSED_STATUSES = {'resolved', 'ok', 'closed', 'recovered', 'up'}


class InvalidEvent(ValueError):
    pass


def _text(event, field, limit, required=False):
    value = event.get(field)
    if value is None or value == '':
        if required:
            raise InvalidEvent(f'"{field}" is required')
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise InvalidEvent(f'"{field}" must be a string')
    return str(value).strip()[:limit]


def _labels(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidEvent('"labels" must be an object')
    if len(raw) > MAX_LABELS:
        raise InvalidEvent(f'"labels" accepts at most {MAX_LABELS} entries')
    labels = {}
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            raise InvalidEvent(f'label "{key}" must be a string')
        # Same convention as Prometheus: characters other than letters, digits, _ and - become _.
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', str(key))[:128]
        if not LABEL_KEY.fullmatch(name):
            name = f'_{name}'[:128]
        value = str(value).lower() if isinstance(value, bool) else str(value)
        labels[name] = value[:1000]
    return labels


def _start_time(raw):
    if raw is None or raw == '':
        return None
    try:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return datetime.fromtimestamp(raw, tz=timezone('UTC'))
        started = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except (ValueError, OverflowError, OSError):
        started = None
    if started is None or started.tzinfo is None:
        raise InvalidEvent('"starts_at" must be ISO 8601 with a time zone, or epoch seconds')
    return started


def parse_event(event):
    """Validate one event and return it normalized, or raise InvalidEvent."""
    if not isinstance(event, dict):
        raise InvalidEvent('each event must be an object')
    event_id = _text(event, 'id', 500, required=True)
    status = str(event.get('status') or '').strip().lower()
    if status in OPEN_STATUSES:
        state = 'open'
    elif status in CLOSED_STATUSES:
        state = 'closed'
    else:
        raise InvalidEvent('"status" must be "firing" or "resolved"')

    source = _text(event, 'source', 40) or 'webhook'
    source = source.lower()
    if not SOURCE.fullmatch(source) or source in RESERVED_SOURCES:
        raise InvalidEvent('"source" must be a short slug (a-z, 0-9, - and _), '
                           'other than gcp, alertmanager and aws')

    return {
        'key': f'{source}/{event_id}',
        'state': state,
        'source': source,
        'name': _text(event, 'name', 300, required=state == 'open'),
        'severity': _normalize_severity(event.get('severity')),
        'summary': _text(event, 'summary', 2000),
        'description': _text(event, 'description', 10000),
        'url': _http_url(event.get('url')),
        'runbook_url': _http_url(event.get('runbook_url')),
        'labels': _labels(event.get('labels')),
        'starts_at': _start_time(event.get('starts_at')),
    }


def _occurrence_id(key, started):
    """One alert per occurrence: the same key firing again after it resolved opens a new one."""
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return f"ev-{digest}-{started.astimezone(timezone('UTC')).strftime('%Y%m%dT%H%M%S%f')}"


def _open(event, now):
    current = alerts.find_one({'event_key': event['key'], 'state': 'open'}, {'incident_id': 1})
    if current:
        incident_id = current['incident_id']
    else:
        incident_id = _occurrence_id(event['key'], event['starts_at'] or now)
    severity = event['severity']
    data = {
        'source': event['source'],
        'event_key': event['key'],
        'name': event['name'],
        'state': 'open',
        'severity': severity,
        'severity_number': alerts_dict[severity],
        'summary': event['summary'] or 'No documentation',
        'description': event['description'],
        'url': event['url'],
        'runbook_url': event['runbook_url'],
        'threshold': 'Null',
        'labels': event['labels'],
        'update_at': now,
    }
    if event['starts_at']:
        data['starts_at'] = event['starts_at'].isoformat()
    store_alert(incident_id, data, _incident_opened_at(
        incident_id, data.get('starts_at'), now,
    ))
    return {'id': event['key'], 'state': 'open', 'incident': incident_id}


def _close(event, now):
    result = alerts.update_many(
        {'event_key': event['key'], 'state': 'open'},
        {'$set': {'state': 'closed', 'ends_at': now.isoformat(), 'update_at': now},
         '$push': {'history': alert_event('alert_recovered', now)}},
    )
    if not result.modified_count:
        current_app.logger.info(f'Event {event["key"]} resolved without an open alert')
    return {'id': event['key'], 'state': 'closed', 'closed': result.modified_count}


@events_bp.route('/events', methods=['POST'])
def receive_events():
    refused = _check_bearer(Config.EVENTS_TOKEN, 'EVENTS_TOKEN')
    if refused:
        return refused

    payload = request.get_json(silent=True)
    batch = payload if isinstance(payload, list) else [payload]
    if not batch or payload is None:
        return jsonify({'error': 'Send one event object or a list of events'}), 400
    if len(batch) > MAX_EVENTS:
        return jsonify({'error': f'Send at most {MAX_EVENTS} events per request'}), 400

    # Validate the whole batch first, so a bad event never leaves it half stored.
    events = []
    for index, raw in enumerate(batch):
        try:
            events.append(parse_event(raw))
        except InvalidEvent as error:
            return jsonify({'error': str(error), 'index': index}), 400

    now = datetime.now(tz=timezone(Config.TIMEZONE))
    results = [(_open if event['state'] == 'open' else _close)(event, now) for event in events]
    current_app.logger.info(f'Generic webhook: {len(results)} event(s)')
    return jsonify({'processed': len(results), 'results': results})
