from datetime import datetime, timedelta, timezone

from tests.conftest import login
from utils import report_metrics

T0 = datetime(2026, 11, 23, 14, 0)  # segunda-feira, 14h UTC
START, END = T0 - timedelta(days=1), T0 + timedelta(days=1)


def ev(kind, minutes, actor=None):
    return {'kind': kind, 'at': T0 + timedelta(minutes=minutes), 'actor': actor}


def alert(name, *events, service='api', **extra):
    return {'name': name, 'labels': {'service': service}, 'history': list(events),
            'update_at': T0, **extra}


def test_occurrences_split_reopened_alert_into_cycles():
    doc = alert('Disk', ev('alert_opened', 0), ev('alert_acked', 3, 'ana@example.com'),
                ev('alert_recovered', 20), ev('alert_opened', 60), ev('alert_closed', 90))
    occs = list(report_metrics.occurrences(doc))
    assert [(o['how'], o['acked'] is not None) for o in occs] == [
        ('alert_recovered', True), ('alert_closed', False)]
    assert occs[1]['opened'] == T0 + timedelta(minutes=60)


def test_occurrences_accept_timezone_aware_events_and_legacy_alerts():
    aware = alert('Cpu', {'kind': 'alert_opened', 'at': T0.replace(tzinfo=timezone.utc)},
                  ev('alert_recovered', 4))
    assert list(report_metrics.occurrences(aware))[0]['resolved'] == T0 + timedelta(minutes=4)

    legacy = {'name': 'Old', 'opened_at': T0, 'ack_at': T0 + timedelta(minutes=2)}
    [occ] = report_metrics.occurrences(legacy)
    assert occ['acked'] - occ['opened'] == timedelta(minutes=2) and occ['resolved'] is None


def test_response_times_use_median_and_p90_and_skip_superseded():
    docs = [alert(f'A{i}', ev('alert_opened', 0), ev('alert_acked', i), ev('alert_recovered', i * 10))
            for i in range(1, 11)]
    docs.append(alert('Gone', ev('alert_opened', 0), ev('alert_superseded', 500)))
    result = report_metrics.response_times(docs, START, END)
    assert result['occurrences'] == 11
    assert result['unacked'] == 1
    assert result['ack'] == {'count': 10, 'median': 330.0, 'p90': 540.0}
    assert result['resolve']['count'] == 10  # a substituída não tem fim conhecido
    assert result['resolve']['p90'] == 5400.0


def test_response_times_only_count_occurrences_opened_in_period():
    doc = alert('Disk', ev('alert_opened', -3 * 24 * 60), ev('alert_recovered', -3 * 24 * 60 + 5))
    assert report_metrics.response_times([doc], START, END)['occurrences'] == 0


def test_change_compares_medians():
    assert report_metrics.change({'median': 150}, {'median': 100}) == .5
    assert report_metrics.change({'median': 150}, None) is None


def test_noisy_alerts_count_quick_unacked_self_recoveries():
    docs = [
        # 2 de 3 se recuperaram sozinhos em menos de 5 min
        alert('Flappy', ev('alert_opened', 0), ev('alert_recovered', 2),
              ev('alert_opened', 10), ev('alert_recovered', 13),
              ev('alert_opened', 30), ev('alert_recovered', 50)),
        # rápido, mas alguém deu ACK
        alert('Watched', ev('alert_opened', 0), ev('alert_acked', 1), ev('alert_recovered', 2)),
        # rápido, mas fechado à mão
        alert('Closed', ev('alert_opened', 0), ev('alert_closed', 1)),
    ]
    result = report_metrics.noisy_alerts(docs, START, END)
    assert result['count'] == 1 and result['flaps'] == 2
    [item] = result['items']
    assert (item['name'], item['service'], item['flaps'], item['total']) == ('Flappy', 'api', 2, 3)
    assert item['median_duration'] == 150.0


def test_opening_heatmap_uses_local_time_and_business_hours():
    docs = [
        alert('Day', ev('alert_opened', 0)),            # seg 14h UTC → 11h em São Paulo
        alert('Night', ev('alert_opened', 10 * 60)),    # seg 24h UTC → seg 21h em São Paulo
        alert('Night2', ev('alert_opened', 10 * 60 + 5)),
    ]
    result = report_metrics.opening_heatmap(docs, START, END, 'America/Sao_Paulo')
    assert result['grid'][0][11] == 1 and result['grid'][0][21] == 2
    assert result['total'] == 3 and result['off_hours'] == 2 and result['max'] == 2
    assert result['peak'] == {'day': 0, 'hour': 21, 'count': 2}


def test_reports_page_shows_response_noisy_and_heatmap(client, alerts):
    login(client)
    now = datetime.now(timezone.utc)
    for i in range(3):
        opened = now - timedelta(hours=i + 1)
        alerts.insert_one({
            'incident_id': f'r{i}', 'name': 'FlappyAlert', 'severity': 'Warning', 'severity_number': 3,
            'labels': {'service': 'checkout'}, 'state': 'closed', 'update_at': opened,
            'history': [{'kind': 'alert_opened', 'at': opened},
                        {'kind': 'alert_recovered', 'at': opened + timedelta(minutes=2)}],
        })
    page = client.get('/reports?days=7').get_data(as_text=True)
    assert 'FlappyAlert' in page and 'service=checkout' in page
    assert '2min' in page  # tempo até resolver
    assert 'heatmap-cell' in page


def test_reports_page_renders_without_data(client, alerts):
    login(client)
    response = client.get('/reports?days=7')
    assert response.status_code == 200
    assert 'heatmap-cell' not in response.get_data(as_text=True)
