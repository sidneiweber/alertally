import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from bson import ObjectId
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from config import Config
from routes.auth import login_required
from utils.helpers import alerts, db, get_session_user
from utils.service_discovery import candidate_rule

catalog_bp = Blueprint('catalog', __name__, url_prefix='/catalog')
catalog = db.catalog
incidents = db.operational_incidents
LABEL_KEY = re.compile(r'[a-zA-Z_][a-zA-Z0-9_-]{0,127}')
dismissed = db.catalog_dismissed
RULE_LIMIT = 30
SUGGESTION_DAYS = 30
SUGGESTION_SAMPLE = 5000
SUGGESTION_LIMIT = 50
COVERAGE_DAYS = 7
MATCH_LIMIT = 100


def catalog_entries(kind=None):
    query = {'kind': kind} if kind else {}
    return list(catalog.find(query).sort('name', 1))


def get_document(collection, identifier, **filters):
    if not ObjectId.is_valid(identifier):
        abort(404)
    document = collection.find_one({'_id': ObjectId(identifier), **filters})
    if document is None:
        abort(404)
    return document


def selected_ids(collection, field, **filters):
    values = list(dict.fromkeys(request.form.getlist(field)))
    if any(not ObjectId.is_valid(value) for value in values):
        raise ValueError(_('Invalid selection.'))
    ids = [ObjectId(value) for value in values]
    if collection.count_documents({'_id': {'$in': ids}, **filters}) != len(ids):
        raise ValueError(_('Invalid selection.'))
    return ids


def alert_query(services):
    rules = [
        {f'labels.{key}': value for key, value in rule.items()}
        for service in services for rule in service.get('rules', [])
    ]
    return {'$or': rules} if rules else {'_id': {'$in': []}}


def _matching_services(alert, services):
    labels = alert.get('labels')
    if not isinstance(labels, dict):
        return []
    return [service for service in services if any(
        all(labels.get(key) == value for key, value in rule.items())
        for rule in service['rules']
    )]


def _label_value_text(value):
    if value.startswith('"') or value != value.strip() or value.splitlines() != [value]:
        return json.dumps(value, ensure_ascii=True)
    return value


def _rule_text(rule, separator='\n'):
    return separator.join(f'{key}={_label_value_text(value)}' for key, value in rule.items())


def _observed_rules():
    samples = alerts.find({}, {'labels': 1}).sort('update_at', -1).limit(250)
    options = {}
    for alert in samples:
        rule = candidate_rule(alert.get('labels'))
        if rule:
            options[_rule_text(rule)] = _rule_text(rule, ' · ')
    return sorted(options.items(), key=lambda option: option[1])[:50]


def _read_rules(raw, observed):
    rules = []
    current = {}
    for line in [*raw.splitlines(), '', *observed.splitlines(), '']:
        if not line.strip():
            if current and current not in rules:
                rules.append(current)
            current = {}
            continue
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
                value.encode('utf-8')
            except ValueError:
                value = ''
        if not separator or not LABEL_KEY.fullmatch(key) or not value or len(value) > 300 or key in current:
            raise ValueError(_('Use one valid key=value per line, with unique keys in each rule.'))
        current[key] = value
    if len(rules) > RULE_LIMIT or any(len(rule) > 15 for rule in rules):
        raise ValueError(_('Use at most 30 rules with up to 15 labels each.'))
    return rules


def _read_entry(kind):
    name = request.form.get('name', '').strip()
    if not name or len(name) > 120:
        raise ValueError(_('Enter a name of up to 120 characters.'))
    entry = {'kind': kind, 'name': name, 'description': request.form.get('description', '').strip()}
    if len(entry['description']) > 2000:
        raise ValueError(_('Use up to 2,000 characters for the description.'))
    if kind == 'group':
        entry['service_ids'] = selected_ids(catalog, 'service_ids', kind='service')
        return entry

    entry['rules'] = _read_rules(request.form.get('rules', ''), request.form.get('observed', ''))
    for field in ('owner', 'environment'):
        entry[field] = request.form.get(field, '').strip()
        if len(entry[field]) > 120:
            raise ValueError(_('Use up to 120 characters for the owner and environment.'))
    for field in ('dashboard_url', 'runbook_url', 'repository_url'):
        value = request.form.get(field, '').strip()
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname
        except ValueError:
            valid = False
        if value and (len(value) > 2000 or not valid):
            raise ValueError(_('Enter a valid HTTP or HTTPS link.'))
        entry[field] = value
    return entry


@catalog_bp.get('')
@login_required
def index():
    show_archived = request.args.get('archived') == '1'
    entries = catalog_entries()
    services = [entry for entry in entries if entry['kind'] == 'service']
    groups = [entry for entry in entries if entry['kind'] == 'group']
    for service in services:
        service['active_count'] = alerts.count_documents({**alert_query([service]), 'state': 'open'})
    active_services = {service['_id']: service for service in services if service['active_count']}
    for group in groups:
        members = [active_services[key] for key in group['service_ids'] if key in active_services]
        group['active_count'] = alerts.count_documents({**alert_query(members), 'state': 'open'}) if members else 0
    impacted_ids = {identifier for incident in incidents.find(
        {'status': {'$ne': 'resolved'}}, {'impact_ids': 1},
    ) for identifier in incident.get('impact_ids', [])}

    for entry in entries:
        entry['impacted'] = entry['_id'] in impacted_ids
        entry['stale'] = is_stale(entry)
    services, groups = ([entry for entry in rows if bool(entry.get('archived')) == show_archived]
                        for rows in (services, groups))
    return render_template('catalog/index.html', session=get_session_user(), services=services, groups=groups,
                           show_archived=show_archived, stale_days=Config.CATALOG_STALE_DAYS,
                           suggestion_count=len(service_suggestions()[0]),
                           archived_count=catalog.count_documents({'archived': True}))


def is_stale(entry):
    """An entry tracked by the service label without alerts for CATALOG_STALE_DAYS."""
    last = entry.get('last_alert_at')
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return datetime.now(UTC) - last > timedelta(days=Config.CATALOG_STALE_DAYS)


@catalog_bp.get('/<entry_id>')
@login_required
def detail(entry_id):
    entry = get_document(catalog, entry_id)
    if entry['kind'] == 'group':
        related = list(catalog.find({'_id': {'$in': entry['service_ids']}}).sort('name', 1))
        services = related
    else:
        related = list(catalog.find({'kind': 'group', 'service_ids': entry['_id']}).sort('name', 1))
        services = [entry]
    query = alert_query(services)
    active_query = {**query, 'state': 'open'}
    active = list(alerts.find(active_query).sort([('severity_number', -1), ('opened_at', 1)]).limit(MATCH_LIMIT))
    past = list(alerts.find({**query, 'state': 'closed'}).sort('update_at', -1).limit(MATCH_LIMIT))
    if entry['kind'] == 'group':
        for alert in active + past:
            alert['matching_services'] = _matching_services(alert, services)
    impacting = list(incidents.find({'impact_ids': entry['_id'], 'status': {'$ne': 'resolved'}})
                     .sort('created_at', -1))
    return render_template('catalog/detail.html', session=get_session_user(), entry=entry,
                           related=related, active=active, active_count=alerts.count_documents(active_query),
                           past=past, impacting=impacting, stale=is_stale(entry),
                           stale_days=Config.CATALOG_STALE_DAYS)


@catalog_bp.post('/<entry_id>/archive')
@login_required
def archive(entry_id):
    entry = get_document(catalog, entry_id)
    archived = request.form.get('archived') == '1'
    catalog.update_one({'_id': entry['_id']}, {'$set': {'archived': archived}})
    flash(_('Entry archived.') if archived else _('Entry restored.'), 'success')
    return redirect(url_for('catalog.detail', entry_id=entry['_id']))


@catalog_bp.route('/new/<any(service,group):kind>', methods=['GET', 'POST'])
@catalog_bp.route('/<entry_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(kind=None, entry_id=None):
    entry = get_document(catalog, entry_id) if entry_id else {}
    kind = entry.get('kind', kind)
    form = entry
    rules = '\n\n'.join(_rule_text(rule) for rule in entry.get('rules', []))
    if not entry and kind == 'service':
        # Prefilled from a suggestion; the person still reviews and saves it.
        form = {'name': request.args.get('name', '')[:120]}
        rules = request.args.get('rules', '')
    status = 200
    if request.method == 'POST':
        try:
            values = _read_entry(kind)
        except ValueError as error:
            form = {**request.form.to_dict(), 'service_ids': request.form.getlist('service_ids')}
            rules = request.form.get('rules', '')
            flash(str(error), 'danger')
            status = 400
        else:
            if entry:
                catalog.update_one({'_id': entry['_id']}, {'$set': values})
                identifier = entry['_id']
            else:
                identifier = catalog.insert_one(values).inserted_id
            return redirect(url_for('catalog.detail', entry_id=identifier))
    return render_template('catalog/edit.html', session=get_session_user(), entry=entry, kind=kind,
                           form=form, rules=rules, options=_observed_rules() if kind == 'service' else [],
                           services=catalog_entries('service') if kind == 'group' else []), status


def _as_utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def service_suggestions():
    """Group recent alerts not matched by the catalog into candidate services.

    Returns the open suggestions, the dismissed ones and the catalog coverage of
    the last COVERAGE_DAYS days as (matched, total).
    """
    now = datetime.now(UTC)
    rows = alerts.find({'update_at': {'$gte': now - timedelta(days=SUGGESTION_DAYS)}},
                       {'labels': 1, 'name': 1, 'update_at': 1}).sort('update_at', -1).limit(SUGGESTION_SAMPLE)
    services = catalog_entries('service')
    ignored = {row['rule'] for row in dismissed.find({}, {'rule': 1})}
    coverage_since = now - timedelta(days=COVERAGE_DAYS)
    matched = total = 0
    groups = {}
    for alert in rows:
        covered = bool(_matching_services(alert, services))
        updated = _as_utc(alert['update_at'])
        if updated >= coverage_since:
            total += 1
            matched += covered
        rule = None if covered else candidate_rule(alert.get('labels'))
        if rule is None:
            continue
        key = _rule_text(rule)
        group = groups.setdefault(key, {
            'key': key, 'label': _rule_text(rule, ' · '), 'name': next(iter(rule.values())),
            'count': 0, 'last_at': updated, 'examples': [],
        })
        group['count'] += 1
        if alert.get('name') and alert['name'] not in group['examples'] and len(group['examples']) < 3:
            group['examples'].append(alert['name'])
    ranked = sorted(groups.values(), key=lambda group: (-group['count'], group['label']))
    return ([group for group in ranked if group['key'] not in ignored][:SUGGESTION_LIMIT],
            [group for group in ranked if group['key'] in ignored][:SUGGESTION_LIMIT],
            (matched, total))


@catalog_bp.get('/suggestions')
@login_required
def suggestions():
    show_dismissed = request.args.get('dismissed') == '1'
    open_rows, dismissed_rows, (matched, total) = service_suggestions()
    return render_template('catalog/suggestions.html', session=get_session_user(),
                           suggestions=dismissed_rows if show_dismissed else open_rows,
                           show_dismissed=show_dismissed, dismissed_count=len(dismissed_rows),
                           services=[entry for entry in catalog_entries('service') if not entry.get('archived')],
                           matched=matched, total=total, coverage_days=COVERAGE_DAYS,
                           suggestion_days=SUGGESTION_DAYS)


def _suggested_rule():
    try:
        rules = _read_rules(request.form.get('rule', ''), '')
    except ValueError:
        abort(400)
    if len(rules) != 1:
        abort(400)
    return rules[0]


@catalog_bp.post('/suggestions/add')
@login_required
def add_suggestion():
    rule = _suggested_rule()
    try:
        [service_id] = selected_ids(catalog, 'service_id', kind='service')
    except ValueError:
        flash(_('Choose a technical service.'), 'danger')
        return redirect(url_for('catalog.suggestions'))
    service = catalog.find_one({'_id': service_id})
    if rule not in service['rules']:
        if len(service['rules']) >= RULE_LIMIT:
            flash(_('Use at most 30 rules with up to 15 labels each.'), 'danger')
            return redirect(url_for('catalog.suggestions'))
        catalog.update_one({'_id': service_id}, {'$push': {'rules': rule}})
    flash(_('Rule added to %(name)s.', name=service['name']), 'success')
    return redirect(url_for('catalog.suggestions'))


@catalog_bp.post('/suggestions/dismiss')
@login_required
def dismiss_suggestion():
    key = _rule_text(_suggested_rule())
    if request.form.get('restore') == '1':
        dismissed.delete_one({'rule': key})
    else:
        dismissed.update_one({'rule': key}, {'$setOnInsert': {
            'rule': key, 'actor': get_session_user()['email'], 'at': datetime.now(UTC),
        }}, upsert=True)
    return redirect(url_for('catalog.suggestions', dismissed='1' if request.form.get('restore') == '1' else None))
