import re
from datetime import UTC, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_babel import lazy_gettext
from pymongo.errors import DuplicateKeyError

from routes.auth import login_required
from routes.catalog import (
    MATCH_LIMIT,
    catalog,
    catalog_entries,
    get_document,
    incidents,
    selected_ids,
)
from utils.enrichment import enrich_alerts
from utils.helpers import alerts, alerts_dict, get_session_user

incidents_bp = Blueprint('incidents', __name__, url_prefix='/incidents')
STATUSES = {
    'investigating': lazy_gettext('Investigating'),
    'mitigated': lazy_gettext('Mitigated'),
    'resolved': lazy_gettext('Resolved'),
}
TRANSITIONS = {
    'investigating': ('mitigated', 'resolved'),
    'mitigated': ('investigating', 'resolved'),
    'resolved': (),
}
EVENTS = {
    'created': lazy_gettext('Incident created'),
    'assigned': lazy_gettext('Took ownership'),
    'status': lazy_gettext('Changed incident status'),
    'note': lazy_gettext('Added a note'),
    'impact': lazy_gettext('Updated impact'),
    'alert_linked': lazy_gettext('Linked an alert'),
    'alert_opened': lazy_gettext('Alert opened'),
    'alert_recovered': lazy_gettext('Source reported recovery'),
    'alert_closed': lazy_gettext('Alert closed manually'),
    'alert_acked': lazy_gettext('Acknowledged the alert'),
    'alert_unacked': lazy_gettext('Acknowledgment removed'),
    'alert_superseded': lazy_gettext('Alert replaced by a newer occurrence'),
}


def _event(kind, **details):
    return {'kind': kind, 'at': datetime.now(UTC), 'actor': get_session_user()['email'], **details}


@incidents_bp.get('')
@login_required
def index():
    status = request.args.get('status', 'active')
    if status not in ('active', 'resolved'):
        abort(400)
    query = {'$ne': 'resolved'} if status == 'active' else 'resolved'
    rows = incidents.find({'status': query}).sort('created_at', -1)
    return render_template('incidents/index.html', session=get_session_user(),
                           incidents=rows, statuses=STATUSES, status=status)


@incidents_bp.route('/new/<alert_id>', methods=['GET', 'POST'])
@login_required
def create(alert_id):
    alert = get_document(alerts, alert_id)
    # Repeated submissions from the same occurrence lead to the same incident.
    identifier = alert['_id']
    if incidents.find_one({'_id': identifier}, {'_id': 1}):
        return redirect(url_for('incidents.detail', incident_id=identifier))

    title = alert.get('name', '')
    severity = alert.get('severity', 'Unknown')
    status = 200
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        severity = request.form.get('severity', '')
        if not title or len(title) > 160 or severity not in alerts_dict:
            flash(_('Enter a title of up to 160 characters and choose a severity.'), 'danger')
            status = 400
        else:
            created = _event('created')
            try:
                incidents.insert_one({
                    '_id': identifier, 'title': title, 'severity': severity,
                    'created_at': created['at'], 'status': 'investigating',
                    'owner': created['actor'], 'alert_ids': [identifier], 'impact_ids': [],
                    'impact': '', 'timeline': [created, _event('alert_linked', text=alert.get('name'))],
                })
            except DuplicateKeyError:
                pass
            return redirect(url_for('incidents.detail', incident_id=identifier))
    return render_template('incidents/new.html', session=get_session_user(), alert=alert,
                           title=title, severity=severity, severities=alerts_dict), status


def _timeline(incident, linked):
    events = list(incident['timeline'])
    for alert in linked:
        history = alert.get('history', [])
        opened = alert.get('opened_at')
        if opened and not history and alert.get('state') == 'open':
            events.append({'kind': 'alert_opened', 'at': opened, 'text': alert.get('name')})
        events.extend({**event, 'text': alert.get('name')} for event in history)
    return sorted(events, key=lambda event: event['at'])


@incidents_bp.get('/<incident_id>')
@login_required
def detail(incident_id):
    incident = get_document(incidents, incident_id)
    linked = enrich_alerts(list(alerts.find({'_id': {'$in': incident['alert_ids']}}).sort('opened_at', 1)))
    search = request.args.get('q', '').strip()[:120]
    query = {'_id': {'$nin': incident['alert_ids']}}
    if search:
        query['name'] = {'$regex': re.escape(search), '$options': 'i'}
    available = alerts.find(query).sort('update_at', -1).limit(MATCH_LIMIT)
    return render_template('incidents/detail.html', session=get_session_user(), incident=incident,
                           statuses=STATUSES, events=EVENTS, timeline=_timeline(incident, linked),
                           transitions=TRANSITIONS[incident['status']], linked=linked,
                           available=list(available), entries=catalog_entries(), search=search)


def _change(incident, action):
    query = {'_id': incident['_id']}
    if action == 'note':
        note = request.form.get('note', '').strip()
        if not note or len(note) > 3000:
            raise ValueError(_('Enter a note of up to 3,000 characters.'))
        update = {'$push': {'timeline': _event('note', text=note)}}
    else:
        query['status'] = {'$ne': 'resolved'}
        update = _active_change(incident, action, query)
    return incidents.update_one(query, update).matched_count


def _active_change(incident, action, query):
    if action == 'assign':
        email = get_session_user()['email']
        query['owner'] = {'$ne': email}
        return {'$set': {'owner': email}, '$push': {'timeline': _event('assigned')}}

    if action == 'status':
        email = get_session_user()['email']
        if incident['owner'] != email:
            abort(403)
        target = request.form.get('status')
        if target not in TRANSITIONS[incident['status']]:
            raise ValueError(_('Invalid incident status change.'))
        query.update(owner=email, status=incident['status'])
        event = _event('status', status=target)
        fields = {'status': target}
        if target == 'resolved':
            fields['resolved_at'] = event['at']
        return {'$set': fields, '$push': {'timeline': event}}

    if action == 'impact':
        ids = selected_ids(catalog, 'impact_ids')
        impact = request.form.get('impact', '').strip()
        if len(impact) > 3000 or (ids and not impact):
            raise ValueError(_('Describe the confirmed impact in up to 3,000 characters.'))
        query['$or'] = [{'impact': {'$ne': impact}}, {'impact_ids': {'$ne': ids}}]
        names = [entry['name'] for entry in catalog.find({'_id': {'$in': ids}}).sort('name', 1)]
        return {'$set': {'impact': impact, 'impact_ids': ids},
                '$push': {'timeline': _event('impact', text=impact, names=names)}}

    if action == 'link':
        ids = selected_ids(alerts, 'alert_ids')
        if len(ids) != 1:
            raise ValueError(_('Choose an alert to link.'))
        alert = alerts.find_one({'_id': ids[0]})
        query['alert_ids'] = {'$ne': alert['_id']}
        return {'$addToSet': {'alert_ids': alert['_id']},
                '$push': {'timeline': _event('alert_linked', text=alert.get('name'))}}

    abort(400)


@incidents_bp.post('/<incident_id>')
@login_required
def update(incident_id):
    incident = get_document(incidents, incident_id)
    try:
        if not _change(incident, request.form.get('action')):
            flash(_('No changes saved. Review the current incident state.'), 'warning')
    except ValueError as error:
        flash(str(error), 'danger')
    return redirect(url_for('incidents.detail', incident_id=incident_id))
