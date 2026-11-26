from urllib.parse import urlparse

from flask import Blueprint, make_response, redirect, render_template, request
from flask_babel import gettext as _
from datetime import datetime, timedelta
from utils.helpers import alerts, get_session_user
from routes.auth import login_required
from config import Config
from utils.i18n import LANG_COOKIE, LANGUAGES, current_language
from utils import report_metrics
from babel.dates import get_day_names

main_bp = Blueprint('main', __name__)


@main_bp.route('/', methods=['GET'])
@login_required
def index():
    return render_template('index.html',
                           session=get_session_user(),
                           admin_users=Config.ADMIN_USERS)


@main_bp.route('/lang/<code>', methods=['GET'])
def set_language(code):
    """Troca o idioma (cookie) e volta para a página de origem."""
    target = '/'
    if request.referrer:
        ref = urlparse(request.referrer)
        if ref.netloc == request.host and ref.path.startswith('/'):
            target = ref.path + (f'?{ref.query}' if ref.query else '')
    resp = make_response(redirect(target))
    if code in LANGUAGES:
        resp.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, samesite='Lax')
    return resp


def _daily_counts(start, end, scope):
    """Contagem de alertas por dia entre start e end, com zero nos dias vazios."""
    rows = alerts.aggregate([
        {"$match": {**scope, "update_at": {"$gte": start, "$lt": end}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$update_at"}},
            "count": {"$sum": 1}
        }}
    ])
    counts = {row["_id"]: row["count"] for row in rows}

    days = []
    day = start
    while day <= end:
        days.append({
            "_id": {"year": day.year, "month": day.month, "day": day.day},
            "count": counts.get(day.strftime("%Y-%m-%d"), 0)
        })
        day += timedelta(days=1)
    return days


@main_bp.route('/reports', methods=['GET'])
@login_required
def reports():
    days_str = request.args.get('days', '7')
    error_message = None

    try:
        days = int(days_str)
        if days not in Config.ALLOWED_DAYS:
            raise ValueError()
    except ValueError:
        days = 7
        error_message = _("Invalid 'days' parameter. Using %(days)d as default.", days=days)

    # Alertas que abriram numa janela de manutenção ficam de fora, salvo pedido
    include_maintenance = request.args.get('maintenance') == '1'
    scope = {} if include_maintenance else {"maintenance_id": {"$exists": False}}

    now = datetime.now()
    limit_date = now - timedelta(days=days)
    previous_limit_date = limit_date - timedelta(days=days)

    # Separate resources/services; keep the latest summary for each group.
    reports = list(alerts.aggregate([
        {
            "$match": {
                **scope,
                "update_at": {"$gte": limit_date}
            }
        },
        {"$sort": {"update_at": 1}},
        {
            "$group": {
                "_id": {
                    "name": "$name",
                    "source": {"$ifNull": ["$source", ""]},
                    "resource": {"$ifNull": ["$labels.resource", ""]},
                    "service": {"$ifNull": ["$labels.service", ""]},
                    "severity": "$severity",
                    "severity_number": "$severity_number"
                },
                "count": {"$sum": 1},
                "source": {"$first": "$source"},
                "summary": {"$last": "$summary"}
            }
        },
        {
            "$sort": {"count": -1}
        }
    ]))

    # Agregação por severidade (novo)
    severity_reports = list(alerts.aggregate([
        {
            "$match": {
                **scope,
                "update_at": {"$gte": limit_date}
            }
        },
        {
            "$group": {
                "_id": {
                    "severity": "$severity",
                    "severity_number": "$severity_number"
                },
                "count": {"$sum": 1}
            }
        },
        {
            "$sort": {"_id.severity_number": -1}
        }
    ]))

    # Agregação para comparação de períodos
    current_period_count = alerts.count_documents({**scope, "update_at": {"$gte": limit_date}})
    previous_period_count = alerts.count_documents(
        {**scope, "update_at": {"$gte": previous_limit_date, "$lt": limit_date}})
    maintenance_count = alerts.count_documents(
        {"maintenance_id": {"$exists": True}, "update_at": {"$gte": limit_date}})
    
    # Calcular variação percentual
    if previous_period_count > 0:
        change_percent = ((current_period_count - previous_period_count) / previous_period_count) * 100
        change_amount = current_period_count - previous_period_count
    else:
        change_percent = 100 if current_period_count > 0 else 0
        change_amount = current_period_count

    # Tendência diária do período atual e do anterior (dados reais dos dois),
    # incluindo dias sem alertas
    current_daily = _daily_counts(limit_date, now, scope)
    previous_daily = _daily_counts(previous_limit_date, limit_date, scope)
    previous_daily = (previous_daily + [{"count": 0}] * len(current_daily))[:len(current_daily)]

    # Tempo de resposta, alertas ruidosos e horário de abertura (pelo histórico)
    history_docs = list(alerts.find({**scope, "update_at": {"$gte": previous_limit_date}},
                                    report_metrics.PROJECTION))
    response = report_metrics.response_times(history_docs, limit_date, now)
    previous_response = report_metrics.response_times(history_docs, previous_limit_date, limit_date)
    response['ack_change'] = report_metrics.change(response['ack'], previous_response['ack'])
    response['resolve_change'] = report_metrics.change(response['resolve'], previous_response['resolve'])
    noisy = report_metrics.noisy_alerts(history_docs, limit_date, now)
    heatmap = report_metrics.opening_heatmap(history_docs, limit_date, now, Config.TIMEZONE)
    day_names = get_day_names('abbreviated', locale=current_language())
    heatmap['days'] = [day_names[d].rstrip('.') for d in range(7)]

    period_comparison = {
        "current_count": current_period_count,
        "previous_count": previous_period_count,
        "change_amount": change_amount,
        "change_percent": round(change_percent, 1),
        "is_increase": change_amount > 0,
        "daily_alerts": current_daily,
        "previous_daily_alerts": [d["count"] for d in previous_daily]
    }

    return render_template(
        'reports.html',
        reports=reports,
        severity_reports=severity_reports,
        period_comparison=period_comparison,
        days=days,
        error=error_message,
        session=get_session_user(),
        options=Config.ALLOWED_DAYS,
        include_maintenance=include_maintenance,
        maintenance_count=maintenance_count,
        response=response,
        noisy=noisy,
        flap_minutes=int(report_metrics.FLAP_WINDOW.total_seconds() // 60),
        heatmap=heatmap,
        timezone_name=Config.TIMEZONE,
    )

