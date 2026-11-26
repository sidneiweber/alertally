"""Métricas do relatório calculadas a partir do histórico de cada alerta.

Um documento de alerta pode abrir e se recuperar mais de uma vez (o histórico
guarda cada transição). Cada ciclo aberto → resolvido é uma "ocorrência", e é
sobre elas que medimos tempo de resposta, ruído e horário de disparo.
"""
import math
from datetime import datetime, timedelta
from statistics import median

from pytz import UTC, timezone

# Fecham uma ocorrência com um horário de resolução conhecido
RESOLVED = {'alert_recovered', 'alert_closed'}
# Recuperou sozinho, sem ACK, em até este tempo: alerta ruidoso (flapping)
FLAP_WINDOW = timedelta(minutes=5)
# Horário comercial usado no mapa de calor: seg–sex, das 9h às 18h
BUSINESS_DAYS = range(0, 5)
BUSINESS_HOURS = range(9, 18)

PROJECTION = {'name': 1, 'labels.service': 1, 'history': 1,
              'opened_at': 1, 'update_at': 1, 'ack_at': 1}


def _utc(value):
    """Datetime em UTC sem fuso (o Mongo devolve sem fuso, já em UTC)."""
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def occurrences(alert):
    """Ciclos aberto → resolvido do alerta, com horários de ACK e resolução."""
    history = [e for e in alert.get('history') or [] if _utc(e.get('at'))]
    history.sort(key=lambda e: _utc(e['at']))

    if not any(e.get('kind') == 'alert_opened' for e in history):
        # Alerta antigo, sem histórico de transições: só a abertura e o ACK
        opened = _utc(alert.get('opened_at')) or _utc(alert.get('update_at'))
        if opened:
            yield {'opened': opened, 'acked': _utc(alert.get('ack_at')),
                   'resolved': None, 'how': None}
        return

    current = None
    for event in history:
        kind, at = event.get('kind'), _utc(event['at'])
        if kind == 'alert_opened':
            if current:
                yield current
            current = {'opened': at, 'acked': None, 'resolved': None, 'how': None}
        elif current is None:
            continue
        elif kind == 'alert_acked':
            current['acked'] = current['acked'] or at
        elif kind in RESOLVED:
            yield {**current, 'resolved': at, 'how': kind}
            current = None
        elif kind == 'alert_superseded':
            # Fim real desconhecido: a ocorrência não entra no tempo de resolução
            yield {**current, 'how': kind}
            current = None
    if current:
        yield current


def _in_period(alerts, start, end):
    start, end = _utc(start), _utc(end)
    for alert in alerts:
        for occ in occurrences(alert):
            if start <= occ['opened'] < end:
                yield alert, occ


def _summary(durations):
    if not durations:
        return None
    ordered = sorted(durations)
    return {
        'count': len(ordered),
        'median': median(ordered),
        'p90': ordered[math.ceil(len(ordered) * .9) - 1],
    }


def response_times(alerts, start, end):
    """Mediana e p90 (em segundos) até o ACK e até a resolução."""
    occs = [occ for _, occ in _in_period(alerts, start, end)]
    to_ack = [(o['acked'] - o['opened']).total_seconds() for o in occs
              if o['acked'] and o['acked'] >= o['opened']]
    to_resolve = [(o['resolved'] - o['opened']).total_seconds() for o in occs
                  if o['resolved'] and o['resolved'] >= o['opened']]
    return {
        'occurrences': len(occs),
        'unacked': sum(1 for o in occs if not o['acked']),
        'ack': _summary(to_ack),
        'resolve': _summary(to_resolve),
    }


def change(current, previous):
    """Variação relativa entre duas medianas, ou None sem base de comparação."""
    if not current or not previous or not previous['median']:
        return None
    return (current['median'] - previous['median']) / previous['median']


def is_flap(occ):
    return (occ['how'] == 'alert_recovered' and not occ['acked']
            and occ['resolved'] - occ['opened'] <= FLAP_WINDOW)


def noisy_alerts(alerts, start, end, limit=5):
    """Alertas que mais se recuperam sozinhos, rápido e sem ninguém agir."""
    groups = {}
    for alert, occ in _in_period(alerts, start, end):
        key = (alert.get('name') or '', (alert.get('labels') or {}).get('service') or '')
        group = groups.setdefault(key, {'name': key[0], 'service': key[1],
                                        'total': 0, 'flaps': 0, 'durations': []})
        group['total'] += 1
        if is_flap(occ):
            group['flaps'] += 1
            group['durations'].append((occ['resolved'] - occ['opened']).total_seconds())

    noisy = [g for g in groups.values() if g['flaps']]
    for group in noisy:
        group['rate'] = group['flaps'] / group['total']
        group['median_duration'] = median(group.pop('durations'))
    noisy.sort(key=lambda g: (-g['flaps'], -g['rate'], g['name']))
    return {'items': noisy[:limit], 'count': len(noisy),
            'flaps': sum(g['flaps'] for g in noisy)}


def opening_heatmap(alerts, start, end, tz_name):
    """Ocorrências por dia da semana (seg = 0) e hora de abertura, no fuso local."""
    tz = timezone(tz_name)
    grid = [[0] * 24 for _ in range(7)]
    for _, occ in _in_period(alerts, start, end):
        local = UTC.localize(occ['opened']).astimezone(tz)
        grid[local.weekday()][local.hour] += 1

    total = sum(map(sum, grid))
    business = sum(grid[d][h] for d in BUSINESS_DAYS for h in BUSINESS_HOURS)
    peak_day, peak_hour = max(((d, h) for d in range(7) for h in range(24)),
                              key=lambda dh: grid[dh[0]][dh[1]])
    return {
        'grid': grid,
        'max': max(map(max, grid)),
        'total': total,
        'off_hours': total - business,
        'peak': {'day': peak_day, 'hour': peak_hour,
                 'count': grid[peak_day][peak_hour]} if total else None,
    }
