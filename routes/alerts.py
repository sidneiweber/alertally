
import hmac
import json
import re
from datetime import datetime
from urllib.parse import quote, urlparse

import requests

from flask import Blueprint, current_app, flash, redirect, request, url_for, jsonify, render_template
from flask_babel import gettext as _
from pytz import timezone

from config import Config
from routes.auth import login_required
from utils.helpers import alerts, alerts_dict, get_session_user

alerts_bp = Blueprint('alerts', __name__)

@alerts_bp.route('/alerts', methods=['GET'])
@login_required
def alerts_list():
    results = alerts.find(
        {'state': 'open'},
        {
            'incident_id': 1,
            'name': 1,
            'source': 1,
            'state': 1,
            'severity': 1,
            'severity_number': 1,
            'summary': 1,
            'url': 1,
            'update_at': 1,
            'labels': 1,
            'ack_by': 1,
            'ack_by_name': 1,
            'ack_at': 1
        }
    ).sort([('severity_number', -1), ('update_at', 1)]).limit(100)
    return render_template('alerts.html', alerts=list(results),
                           session=get_session_user(),
                           admin_users=Config.ADMIN_USERS)


@alerts_bp.route('/close/<incident_id>', methods=['POST'])
@login_required
def close_alert(incident_id):
    user = get_session_user()
    if user.get('email') not in Config.ADMIN_USERS:
        current_app.logger.warning(f'Close of {incident_id} denied for {user.get("email")}')
        return jsonify({'error': _('Only administrators can close alerts')}), 403
    if request.method == 'POST':
        alerts.update_one(
            {'incident_id': incident_id},
            {'$set': {'state': 'closed'}}
        )
        flash(_("You have successfully closed the alert."))
        current_app.logger.info(f'Incident {incident_id} closed')
    return redirect(url_for('main.index'))

def _basic_auth_ok(user, password):
    """Basic auth opcional: sem usuário configurado, libera."""
    if not user:
        return True
    auth = request.authorization
    return bool(
        auth
        and hmac.compare_digest(auth.username or '', user)
        and hmac.compare_digest(auth.password or '', password or '')
    )


def _unauthorized():
    return 'Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="alertally"'}


@alerts_bp.route('/gcp', methods=['POST'])
def parse_request():
    if not _basic_auth_ok(Config.GCP_WEBHOOK_USER, Config.GCP_WEBHOOK_PASSWORD):
        return _unauthorized()

    payload = request.get_json(silent=True)
    incident = payload.get('incident') if isinstance(payload, dict) else None
    if not isinstance(incident, dict) or not incident.get('incident_id'):
        return jsonify({'error': 'Invalid GCP Monitoring payload'}), 400

    incident_id = incident['incident_id']
    severity = _normalize_severity(incident.get('severity'))
    documentation = incident.get('documentation') or {}
    summary = (documentation.get('content') if isinstance(documentation, dict) else None) or 'No documentation'
    metric = incident.get('metric') or {}

    update_data = {
        'source': 'gcp',
        'name': incident.get('condition_name') or incident.get('policy_name') or 'Unnamed alert',
        'state': 'closed' if str(incident.get('state', '')).lower() == 'closed' else 'open',
        'severity': severity,
        'severity_number': alerts_dict[severity],
        'summary': summary,
        'url': incident.get('url'),
        'threshold': incident.get('observed_value', 'Null'),
        'labels': metric.get('labels') or {},
        'update_at': datetime.now(tz=timezone(Config.TIMEZONE)),
    }

    alerts.update_one(
        {'incident_id': incident_id},
        {'$set': update_data},
        upsert=True
    )

    current_app.logger.info(f"GCP incident {incident_id} {update_data['state']}")
    return jsonify({'incident': incident_id, 'state': update_data['state']})


def _normalize_severity(raw, default='Unknown'):
    """Mapeia um texto de severidade (critical, warning...) para as chaves de alerts_dict."""
    raw = str(raw or '').strip().lower()
    mapping = {
        'critical': 'Critical',
        'page': 'Critical',
        'high': 'Error',
        'error': 'Error',
        'warning': 'Warning',
        'warn': 'Warning',
        'medium': 'Warning',
        'info': 'Info',
        'low': 'Info',
        'debug': 'Debug',
        'none': 'No severity',
        'no severity': 'No severity',
    }
    return mapping.get(raw, default)


def _alertmanager_severity(labels):
    """Mapeia o label `severity` do Prometheus para as chaves de alerts_dict."""
    return _normalize_severity(labels.get('severity'))


@alerts_bp.route('/alertmanager', methods=['POST'])
def parse_alertmanager():
    # Token opcional: se ALERTMANAGER_TOKEN estiver definido, exige
    # "Authorization: Bearer <token>" (http_config.authorization no Alertmanager)
    if Config.ALERTMANAGER_TOKEN:
        if request.headers.get('Authorization') != f'Bearer {Config.ALERTMANAGER_TOKEN}':
            return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True)
    if not payload or 'alerts' not in payload:
        return jsonify({'error': 'Invalid Alertmanager payload'}), 400

    processed = []
    for am_alert in payload['alerts']:
        labels = am_alert.get('labels', {})
        annotations = am_alert.get('annotations', {})

        fingerprint = am_alert.get('fingerprint')
        if not fingerprint:
            current_app.logger.warning('Alertmanager alert without fingerprint ignored')
            continue

        # fingerprint identifica o conjunto de labels; startsAt diferencia cada
        # ocorrência, para que um alerta que dispara de novo vire um novo incidente
        starts_at = am_alert.get('startsAt', '')
        incident_id = f"am-{fingerprint}-{starts_at}".replace(':', '').replace('.', '')

        severity = _alertmanager_severity(labels)
        state = 'closed' if am_alert.get('status') == 'resolved' else 'open'

        update_data = {
            'source': 'alertmanager',
            'name': labels.get('alertname', 'Unnamed alert'),
            'state': state,
            'severity': severity,
            'severity_number': alerts_dict[severity],
            'summary': annotations.get('summary') or annotations.get('description') or 'No documentation',
            'description': annotations.get('description'),
            'url': am_alert.get('generatorURL') or payload.get('externalURL'),
            'threshold': annotations.get('value', 'Null'),
            'labels': labels,
            'starts_at': starts_at,
            'ends_at': am_alert.get('endsAt'),
            'update_at': datetime.now(tz=timezone(Config.TIMEZONE)),
        }

        alerts.update_one(
            {'incident_id': incident_id},
            {'$set': update_data},
            upsert=True
        )
        current_app.logger.info(f'Alertmanager incident {incident_id} {state}')
        processed.append(incident_id)

    return jsonify({'processed': len(processed), 'incidents': processed})


# ---------- AWS: CloudWatch Alarm -> SNS -> HTTPS ----------

# SubscribeURL legítima do SNS: https://sns.<região>.amazonaws.com[.cn]/...
_SNS_HOST = re.compile(r'^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$')
# Convenção na descrição do alarme: "severity=critical" (ou "severity: critical")
_SEVERITY_TAG = re.compile(r'\bseverity\s*[=:]\s*([a-zA-Z]+)', re.IGNORECASE)


def _aws_alarm_url(alarm_arn, alarm_name):
    # arn:aws:cloudwatch:<região>:<conta>:alarm:<nome>
    parts = alarm_arn.split(':')
    region = parts[3] if len(parts) > 3 else ''
    if not region:
        return None
    return (f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#alarmsV2:alarm/{quote(alarm_name, safe='')}")


def _aws_labels(alarm, region):
    trigger = alarm.get('Trigger') or {}
    labels = {
        'account': alarm.get('AWSAccountId'),
        'region': region,
        'namespace': trigger.get('Namespace'),
        'metric': trigger.get('MetricName'),
    }
    for dim in trigger.get('Dimensions') or []:
        name, value = dim.get('name') or dim.get('Name'), dim.get('value') or dim.get('Value')
        if name:
            labels[name] = value
    return {k: v for k, v in labels.items() if v}


@alerts_bp.route('/aws', methods=['POST'])
def parse_aws_sns():
    if not _basic_auth_ok(Config.AWS_SNS_USER, Config.AWS_SNS_PASSWORD):
        # O SNS só envia as credenciais depois de receber o desafio 401
        return _unauthorized()

    # O SNS envia JSON com Content-Type text/plain
    try:
        envelope = json.loads(request.get_data(as_text=True))
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict):
        return jsonify({'error': 'Invalid SNS payload'}), 400

    msg_type = request.headers.get('x-amz-sns-message-type') or envelope.get('Type')
    topic_arn = envelope.get('TopicArn', '')
    if Config.AWS_SNS_TOPIC_ARNS and topic_arn not in Config.AWS_SNS_TOPIC_ARNS:
        current_app.logger.warning(f'SNS topic not allowed: {topic_arn}')
        return jsonify({'error': 'Topic not allowed'}), 403

    if msg_type == 'SubscriptionConfirmation':
        subscribe_url = envelope.get('SubscribeURL', '')
        parsed = urlparse(subscribe_url)
        if parsed.scheme != 'https' or not _SNS_HOST.match(parsed.hostname or ''):
            return jsonify({'error': 'Invalid SubscribeURL'}), 400
        requests.get(subscribe_url, timeout=10).raise_for_status()
        current_app.logger.info(f'SNS subscription confirmed for {topic_arn}')
        return jsonify({'confirmed': topic_arn})

    if msg_type != 'Notification':
        # UnsubscribeConfirmation e afins: nada a fazer
        return jsonify({'ignored': msg_type})

    try:
        alarm = json.loads(envelope.get('Message', ''))
    except ValueError:
        alarm = None
    if not isinstance(alarm, dict) or 'AlarmName' not in alarm:
        # Não é um alarme do CloudWatch. Responde 200 para o SNS não reenviar.
        current_app.logger.warning(f'SNS message ignored (not a CloudWatch alarm) from {topic_arn}')
        return jsonify({'ignored': 'not a CloudWatch alarm'})

    name = alarm['AlarmName']
    alarm_arn = alarm.get('AlarmArn', name)
    new_state = alarm.get('NewStateValue')
    state_time = alarm.get('StateChangeTime', '')
    now = datetime.now(tz=timezone(Config.TIMEZONE))

    if new_state == 'OK':
        result = alerts.update_many(
            {'source': 'aws', 'aws_alarm_arn': alarm_arn, 'state': 'open'},
            {'$set': {'state': 'closed', 'ends_at': state_time, 'update_at': now}}
        )
        current_app.logger.info(f'AWS alarm {name} OK, {result.modified_count} incident(s) closed')
        return jsonify({'closed': result.modified_count})

    if new_state != 'ALARM':
        # INSUFFICIENT_DATA: falta de dados, não é disparo
        return jsonify({'ignored': new_state})

    description = alarm.get('AlarmDescription') or ''
    tag = _SEVERITY_TAG.search(description)
    severity = _normalize_severity(tag.group(1) if tag else None, default='Warning')
    summary = _SEVERITY_TAG.sub('', description).strip(' \n;,-') or alarm.get('NewStateReason') or 'No documentation'

    region = alarm_arn.split(':')[3] if alarm_arn.count(':') >= 3 else None
    # ARN + horário da mudança de estado: reentregas do SNS caem no mesmo incidente
    incident_id = 'aws-' + re.sub(r'[^A-Za-z0-9_-]', '', f"{alarm_arn.split(':')[-1]}-{state_time}")[:200]
    trigger = alarm.get('Trigger') or {}

    update_data = {
        'source': 'aws',
        'name': name,
        'state': 'open',
        'severity': severity,
        'severity_number': alerts_dict[severity],
        'summary': summary,
        'description': alarm.get('NewStateReason'),
        'url': _aws_alarm_url(alarm_arn, name),
        'threshold': trigger.get('Threshold', 'Null'),
        'labels': _aws_labels(alarm, region),
        'aws_alarm_arn': alarm_arn,
        'starts_at': state_time,
        'update_at': now,
    }
    alerts.update_one({'incident_id': incident_id}, {'$set': update_data}, upsert=True)
    current_app.logger.info(f'AWS incident {incident_id} opened')
    return jsonify({'incident': incident_id})


@alerts_bp.route('/ack/<incident_id>', methods=['POST'])
@login_required
def ack_alert(incident_id):
    user = get_session_user()
    
    if request.method == 'POST':
        alert = alerts.find_one({'incident_id': incident_id})
        
        if not alert:
            return jsonify({'error': _('Alert not found')}), 404
            
        if alert.get('ack_by'):
            return jsonify({'error': _('Alert already acknowledged by %(name)s',
                                       name=alert.get('ack_by_name') or _('someone'))}), 400
        
        alerts.update_one(
            {'incident_id': incident_id},
            {'$set': {
                'ack_by': user.get('email'),
                'ack_by_name': user.get('name'),
                'ack_at': datetime.now(tz=timezone(Config.TIMEZONE))
            }}
        )
        
        current_app.logger.info(f'Incident {incident_id} acknowledged by {user.get("email")}')
        
        if request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': True, 
                'message': _('Alert acknowledged by %(name)s', name=user.get('name')),
                'ack_by': user.get('email'),
                'ack_by_name': user.get('name')
            })
        else:
            flash(_("You have successfully acknowledged the alert."))
            return redirect(url_for('main.index'))

@alerts_bp.route('/unack/<incident_id>', methods=['POST'])
@login_required
def unack_alert(incident_id):
    user = get_session_user()
    
    if request.method == 'POST':
        alert = alerts.find_one({'incident_id': incident_id})
        
        if not alert:
            return jsonify({'error': _('Alert not found')}), 404
            
        if alert.get('ack_by') != user.get('email') and user.get('email') not in Config.ADMIN_USERS:
            return jsonify({'error': _('You can only remove your own acknowledgment')}), 403
        
        alerts.update_one(
            {'incident_id': incident_id},
            {'$unset': {
                'ack_by': '',
                'ack_by_name': '',
                'ack_at': ''
            }}
        )
        
        current_app.logger.info(f'Incident {incident_id} acknowledgment removed by {user.get("email")}')
        
        if request.headers.get('Content-Type') == 'application/json':
            return jsonify({'success': True, 'message': _('Acknowledgment removed')})
        else:
            flash(_("You have successfully removed the acknowledgment."))
            return redirect(url_for('main.index'))

@alerts_bp.route('/count', methods=['GET'])
def count_alerts():
    count = alerts.count_documents({'state': 'open'})
    return str(count)
