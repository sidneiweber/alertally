from datetime import UTC, datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from pytz import timezone

from config import Config
from routes.auth import login_required
from routes.catalog import catalog, catalog_entries, get_document, read_rules, rule_text, selected_ids
from utils.enrichment import enrich_alerts
from utils.helpers import alerts, get_session_user
from utils.maintenance import window_for, window_rules, windows

maintenance_bp = Blueprint('maintenance', __name__, url_prefix='/maintenance')
TITLE_LIMIT = 120
RECENT_DAYS = 7
FORM_FORMAT = '%Y-%m-%dT%H:%M'


def can_manage():
    return get_session_user().get('email') in Config.ADMIN_USERS


@maintenance_bp.app_context_processor
def inject_permissions():
    return {'can_manage_maintenance': can_manage}


def _as_utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def status(window, now=None):
    now = now or datetime.now(UTC)
    if _as_utc(window['starts_at']) > now:
        return 'scheduled'
    return 'active' if _as_utc(window['ends_at']) > now else 'ended'


def _local_input(value):
    return _as_utc(value).astimezone(timezone(Config.TIMEZONE)).strftime(FORM_FORMAT)


def _read_time(field):
    """Read a datetime-local field, entered in TIMEZONE, as UTC."""
    try:
        naive = datetime.strptime(request.form.get(field, ''), FORM_FORMAT)
    except ValueError:
        raise ValueError(_('Enter valid start and end times.'))
    return timezone(Config.TIMEZONE).localize(naive).astimezone(UTC)


def _read_window():
    title = request.form.get('title', '').strip()
    if not title or len(title) > TITLE_LIMIT:
        raise ValueError(_('Enter a title of up to 120 characters.'))
    starts_at, ends_at = _read_time('starts_at'), _read_time('ends_at')
    if ends_at <= starts_at or ends_at <= datetime.now(UTC):
        raise ValueError(_('The window must end after it starts and in the future.'))
    if ends_at - starts_at > timedelta(hours=Config.MAINTENANCE_MAX_HOURS):
        raise ValueError(_('A maintenance window can last at most %(hours)d hours.',
                           hours=Config.MAINTENANCE_MAX_HOURS))
    entry_ids = selected_ids(catalog, 'entry_ids')
    rules = read_rules(request.form.get('rules', ''), '')
    if not entry_ids and not rules:
        raise ValueError(_('Choose at least one service or group, or enter a label rule.'))
    user = get_session_user()
    return {
        'title': title, 'starts_at': starts_at, 'ends_at': ends_at, 'entry_ids': entry_ids, 'rules': rules,
        'created_by': user['email'], 'created_by_name': user.get('name') or user['email'],
        'created_at': datetime.now(UTC),
    }


def _with_entries(rows):
    ids = {identifier for row in rows for identifier in row.get('entry_ids', [])}
    entries = {entry['_id']: entry for entry in catalog.find({'_id': {'$in': list(ids)}})} if ids else {}
    for row in rows:
        row['entries'] = [entries[identifier] for identifier in row.get('entry_ids', []) if identifier in entries]
        row['status'] = status(row)
    return rows


@maintenance_bp.get('')
@login_required
def index():
    now = datetime.now(UTC)
    active = list(windows.find({'starts_at': {'$lte': now}, 'ends_at': {'$gt': now}}).sort('ends_at', 1))
    scheduled = list(windows.find({'starts_at': {'$gt': now}}).sort('starts_at', 1))
    recent = list(windows.find({'ends_at': {'$lte': now, '$gte': now - timedelta(days=RECENT_DAYS)}})
                  .sort('ends_at', -1).limit(20))
    return render_template('maintenance/index.html', session=get_session_user(),
                           sections=[(_('In progress'), _with_entries(active)),
                                     (_('Scheduled'), _with_entries(scheduled)),
                                     (_('Ended in the last %(days)d days', days=RECENT_DAYS), _with_entries(recent))])


@maintenance_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    if not can_manage():
        abort(403)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    form = {'title': '', 'starts_at': _local_input(now), 'ends_at': _local_input(now + timedelta(hours=1)),
            'entry_ids': request.args.getlist('entry_id'), 'rules': ''}
    status_code = 200
    if request.method == 'POST':
        try:
            window = _read_window()
        except ValueError as error:
            form = {**request.form.to_dict(), 'entry_ids': request.form.getlist('entry_ids')}
            flash(str(error), 'danger')
            status_code = 400
        else:
            identifier = windows.insert_one(window).inserted_id
            return redirect(url_for('maintenance.detail', window_id=identifier))
    entries = [entry for entry in catalog_entries() if not entry.get('archived')]
    return render_template('maintenance/new.html', session=get_session_user(), form=form, entries=entries,
                           max_hours=Config.MAINTENANCE_MAX_HOURS, timezone_name=Config.TIMEZONE), status_code


@maintenance_bp.get('/<window_id>')
@login_required
def detail(window_id):
    window = _with_entries([get_document(windows, window_id)])[0]
    opened = enrich_alerts(list(alerts.find({'maintenance_id': window['_id']}).sort('opened_at', -1).limit(100)))
    held = []
    if window['status'] == 'active':
        compiled = window_rules([window])
        held = enrich_alerts([alert for alert in alerts.find({'state': 'open'}).sort('severity_number', -1)
                              if window_for(alert.get('labels'), compiled)][:100])
    return render_template('maintenance/detail.html', session=get_session_user(), window=window,
                           rules='\n\n'.join(rule_text(rule) for rule in window.get('rules', [])),
                           opened=opened, held=held)


@maintenance_bp.post('/<window_id>/end')
@login_required
def end(window_id):
    if not can_manage():
        abort(403)
    window = get_document(windows, window_id)
    now = datetime.now(UTC)
    current = status(window, now)
    if current == 'ended':
        flash(_('This maintenance window has already ended.'), 'warning')
    else:
        changes = {'ends_at': now, 'ended_by': get_session_user()['email'], 'ended_at': now}
        if current == 'scheduled':
            changes['starts_at'] = now
        windows.update_one({'_id': window['_id']}, {'$set': changes})
        flash(_('Maintenance window ended.') if current == 'active' else _('Maintenance window cancelled.'),
              'success')
    return redirect(url_for('maintenance.detail', window_id=window['_id']))
