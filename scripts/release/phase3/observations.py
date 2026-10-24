"""Phase 3.5 independent evidence evaluation. Offline; no mutation interface.

Inputs must come from the fixed signed HTTPS probe and read-only provider GETs.
The release coordinator must not infer release success from an HTTP 200 or a
configured cron. This report does not grant deployment/maintenance authority.
"""
from datetime import datetime, timedelta, timezone
import re

STATUSES = {'PASS', 'FAIL', 'INCONCLUSIVE', 'NOT YET OBSERVABLE'}


def result(status, code):
    return {'status': status, 'code': code}


def aggregate(values):
    statuses = [x.get('status') for x in values]
    if any(x not in STATUSES for x in statuses):
        return result('INCONCLUSIVE', 'invalid-evidence-status')
    status = ('FAIL' if 'FAIL' in statuses else 'INCONCLUSIVE' if 'INCONCLUSIVE' in statuses
              else 'NOT YET OBSERVABLE' if 'NOT YET OBSERVABLE' in statuses else 'PASS')
    return result(status, 'all-required-independent-observations')


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed.astimezone(timezone.utc)


def _valid_finding(value):
    return (isinstance(value, dict) and value.get('status') in STATUSES
            and isinstance(value.get('code'), str))


def probe(fetch, *, expected, nonce, now=None):
    """fetch() must not follow redirects/cookies, and must bound time/body size.

    It returns {status: int, body: dict}. This adapter never handles credentials.
    """
    now = now or datetime.now(timezone.utc)
    unavailable = result('NOT YET OBSERVABLE', 'observation-not-activated')
    def unavailable_report(value):
        return {name: dict(value) for name in ('application', 'database', 'authorization')}
    try:
        response = fetch()
        if response['status'] == 404:
            return unavailable_report(unavailable)
        if response['status'] in (401, 403):
            return unavailable_report(result('INCONCLUSIVE', 'verification-identity-rejected'))
        if response['status'] == 429:
            return unavailable_report(result('INCONCLUSIVE', 'observation-busy'))
        body = response['body']
        if (response['status'] != 200 or body.get('contract') != 'erupted-observation-v1'
                or body.get('nonce') != nonce
                or abs((now - timestamp(body['observed_at'])).total_seconds()) > 120):
            raise ValueError()
        names = ('application', 'database', 'authorization')
        if not all(_valid_finding(body.get(name)) for name in names):
            raise ValueError()
        # A PASS requires exact source, a fresh compatible database, and all
        # three actual role samples. Unknown/missing fields cannot become PASS.
        report = {name: body[name] for name in names}
        if body.get('web') != {k: expected[k] for k in ('revision', 'source_tree_sha256')} | {'status': 'VERIFIED'}:
            report['application'] = result('FAIL', 'unexpected-web-source')
        if report['database']['status'] == 'PASS' and (
            report['database'].get('schema') != expected_schema(expected) or
            report['database'].get('read_only') is not True):
            report['database'] = result('FAIL', 'schema-or-readonly-mismatch')
        auth = report['authorization']
        if auth['status'] == 'PASS' and (set(auth.get('roles', {})) != {'owner', 'lead', 'employee'}
                or not all(_valid_finding(v) and v['status'] == 'PASS' for v in auth['roles'].values())):
            report['authorization'] = result('INCONCLUSIVE', 'incomplete-authorization-evidence')
        return report
    except (TimeoutError, ConnectionError, OSError):
        return unavailable_report(result('INCONCLUSIVE', 'verification-network-interruption'))
    except Exception:
        return unavailable_report(result('INCONCLUSIVE', 'malformed-observation-evidence'))


def expected_schema(expected):
    # Callers provide the manifest's deployed-source schema, never assume main.
    return expected.get('schema')


def latest_due(schedule, now):
    """Reviewed Render UTC schedules only; unsupported schedules fail unknown.

    Existing production is weekly Sunday 10:00 UTC, not the old hourly default.
    """
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError()
    minute, hour, day, month, weekday = fields
    if day != '*' or month != '*' or not minute.isdigit() or not 0 <= int(minute) <= 59:
        raise ValueError()
    if hour == '*' and weekday == '*':
        candidate = now.replace(minute=int(minute), second=0, microsecond=0)
        return candidate if candidate <= now else candidate - timedelta(hours=1)
    if not hour.isdigit() or not 0 <= int(hour) <= 23 or not weekday.isdigit() or not 0 <= int(weekday) <= 6:
        raise ValueError()
    candidate = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    candidate -= timedelta(days=((now.weekday()+1) % 7 - int(weekday)) % 7)
    return candidate if candidate <= now else candidate - timedelta(days=7)


def cron(config, *, expected, schema, receipts, now=None):
    now = now or datetime.now(timezone.utc)
    unknown = result('INCONCLUSIVE', 'execution-evidence-missing')
    report = {'configuration': result('PASS' if config.get('enabled') is True and config.get('schedule') else 'FAIL', 'configured-enabled'),
        'source_schema': result('INCONCLUSIVE', 'runtime-schema-not-observed'),
        'recent_execution': dict(unknown), 'expected_work': dict(unknown)}
    if config.get('revision') != expected['revision']:
        report['source_schema'] = result('FAIL', 'cron-deployed-revision-mismatch')
    try:
        deployed = timestamp(config['deployed_at'])
        # Ten-minute scheduler grace is explicit; no fixed hourly freshness rule.
        due = latest_due(config['schedule'], now - timedelta(minutes=10))
        if due <= deployed:
            pending = result('NOT YET OBSERVABLE', 'next-scheduled-run-not-reached')
            report['recent_execution'] = dict(pending)
            report['expected_work'] = dict(pending)
            if report['source_schema']['status'] != 'FAIL':
                report['source_schema'] = result('NOT YET OBSERVABLE', 'awaiting-first-runtime-schema-receipt')
        # Only terminal receipts for this exact service/deployment may qualify.
        relevant = [r for r in receipts if r.get('event') == 'ERUPTED_CRON_EXECUTION_V1'
            and r.get('service_id') == config['service_id'] and r.get('phase') in ('committed', 'failed', 'disabled')
            and deployed <= timestamp(r['observed_at']) <= now]
        if relevant:
            last = max(relevant, key=lambda r: timestamp(r['observed_at']))
            runtime_ok = (last.get('revision') == expected['revision']
                and last.get('source_tree_sha256') == expected['source_tree_sha256']
                and last.get('identity_status') == 'VERIFIED')
            if not runtime_ok:
                report['source_schema'] = result('FAIL', 'cron-runtime-revision-mismatch')
            elif last.get('phase') == 'committed' and report['source_schema']['status'] != 'FAIL':
                report['source_schema'] = result('PASS' if last.get('schema') == schema else 'FAIL', 'cron-runtime-schema')
            fresh = timestamp(last['observed_at']) >= due and bool(re.fullmatch('[a-f0-9]{32}', last.get('run_id', '')))
            if fresh:
                if last['phase'] != 'committed':
                    report['recent_execution'] = result('FAIL', 'cron-execution-'+last['phase'])
                    report['expected_work'] = result('FAIL', 'expected-work-not-completed')
                elif (not config.get('last_success') or
                      abs((timestamp(config['last_success']) - timestamp(last['observed_at'])).total_seconds()) > 300):
                    report['recent_execution'] = result('INCONCLUSIVE', 'provider-success-not-correlated')
                else:
                    report['recent_execution'] = result('PASS', 'committed-and-provider-success-correlated')
                    counts = last.get('counts', {})
                    valid_counts = (set(counts) == {'generated_period_ids', 'published_period_ids', 'blocked_period_ids'}
                        and all(type(v) is int and v >= 0 for v in counts.values()))
                    work_ok = (valid_counts and counts['blocked_period_ids'] == 0
                        and counts['published_period_ids'] == 0
                        and ((last.get('work') == 'no-work-due' and counts['generated_period_ids'] == 0)
                             or (last.get('work') == 'generated' and counts['generated_period_ids'] > 0)))
                    report['expected_work'] = result('PASS' if work_ok else 'FAIL', 'expected-work-'+str(last.get('work', 'unknown')))
    except Exception:
        report['recent_execution'] = result('INCONCLUSIVE', 'unsupported-or-incomplete-cron-evidence')
        report['expected_work'] = dict(unknown)
    report['overall'] = aggregate(report.values())
    return report


def qualification(observation, cron_report):
    compatibility = aggregate([observation['database'], observation['application'], cron_report['source_schema']])
    report = {'application_readiness': observation['application'], 'database_readiness': observation['database'],
        'authorization_readiness': observation['authorization'], 'web_cron_compatibility': compatibility,
        'cron_execution_health': cron_report['overall'], 'cron_details': cron_report}
    report['overall'] = aggregate([report[name] for name in report if name != 'cron_details'])
    return report


def main():
    """Evaluate saved, sanitized observation evidence; never contact production."""
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    now = timestamp(evidence['observed_at'])
    expected = evidence['expected']
    observed = probe(lambda: evidence['http'], expected=expected, nonce=evidence['nonce'], now=now)
    cron_report = cron(evidence['cron'], expected=expected, schema=expected['schema'],
                       receipts=evidence['receipts'], now=now)
    from scripts.release.phase3.verification import production_readiness
    report = production_readiness(observed, cron_report)
    # Append-only evidence; never overwrite an earlier qualification.
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps(report))
    raise SystemExit(0 if report['overall']['status'] == 'PASS' else 2)


if __name__ == '__main__':
    main()
