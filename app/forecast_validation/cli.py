"""Explicit isolated operator commands; no scheduler installation or automatic activation."""
import argparse
import json
import signal
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from app.config import settings
from .storage import Store
from .runner import forecast
from .evaluation import evaluate
from .contracts import EvidenceError, TZ
from .transport import BoundedRead
from .codec import materialize, CHUNK_BYTES, MAX_BYTES


def read_file(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise EvidenceError('Evidence input file exceeds256MiB')
    with path.open('rb') as file:
        return materialize(iter(lambda: file.read(CHUNK_BYTES), b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'forecast', 'evaluate', 'capture', 'capture-outcome',
                                            'status', 'commission-legacy', 'weekly', 'reconcile'])
    parser.add_argument('--horizon', type=int, choices=[28, 56])
    parser.add_argument('--revision')
    parser.add_argument('--file')
    parser.add_argument('--run-id')
    parser.add_argument('--record-kind', choices=['run', 'evaluation'], default='run')
    parser.add_argument('--period-start')
    parser.add_argument('--activation-start')
    parser.add_argument('--pressure-signal')
    parser.add_argument('--backup-directory')
    parser.add_argument('--backup-key-file')
    parser.add_argument('--backup-public-recipient')
    parser.add_argument('--age-binary')
    parser.add_argument('--key-custody-receipt')
    args = parser.parse_args()
    # Both gates are checked before a Store or business/source engine is opened.
    if not settings.forecast_validation_worker_enabled:
        raise EvidenceError('Shadow worker is disabled')
    if args.command in ('weekly', 'reconcile') and not settings.forecast_validation_schedule_enabled:
        raise EvidenceError('Recurring forecasting execution is disabled')
    store = None
    source_engine = None
    def expired(*_):
        raise TimeoutError('Forecast operation wall-clock budget exceeded')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(1200 if args.command == 'weekly' else 960)
    try:
        store = Store(settings.forecast_validation_database_url, business_url=settings.database_url,
                      production=settings.environment == 'production')
        if args.command == 'init':
            store.initialize()
            print('Dedicated append-only evidence schema initialized')
            return
        if args.command == 'status':
            print(json.dumps({'runs': store.list_runs(), 'failures': store.failures()}))
            return
        if args.command == 'commission-legacy':
            print(store.commission_legacy(args.record_kind, args.run_id))
            return
        if args.command == 'forecast':
            print(forecast(store, read_file(args.file)))
            return
        if args.command == 'evaluate':
            print(evaluate(store, args.run_id, read_file(args.file)))
            return
        if args.command == 'reconcile':
            if not args.activation_start:
                raise EvidenceError('Explicit approved activation week required')
            from .operations import missed_periods
            result = missed_periods(store, activation_start=args.activation_start)
            print(json.dumps(result))
            if result:
                raise EvidenceError('Missing/incomplete forecast periods; see durable alerts')
            return
        if not settings.forecast_validation_source_url:
            raise EvidenceError('Explicit SELECT-only source connection required')
        source_engine = create_engine(settings.forecast_validation_source_url, isolation_level='REPEATABLE READ',
                        pool_size=1, max_overflow=0, pool_timeout=5, hide_parameters=True,
                        connect_args={'options': '-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000'})
        from .collection import collect
        from .exposure import EvidenceGateway
        from .shared import FrozenSource
        from .weekly import source_preflight, run_period
        from app.services.square_ordering_data_service import _square_post
        def collect_source(period=None, remaining=400):
            source_preflight(source_engine)
            seconds = period.remaining_source_seconds() if period else 900
            if seconds <= 0:
                raise EvidenceError('Durable source deadline exhausted; no recollection')
            transport = BoundedRead(_square_post, max_calls=remaining, budget=seconds,
                                    max_retry_attempts=20-period.retry_count() if period else 20,
                                    attempt_hook=period.reserve_request if period else None)
            snapshot = collect(source_engine, EvidenceGateway(post=transport), budget_seconds=seconds)
            snapshot['source_budget']['square'] = transport.metrics()
            return snapshot
        if args.command == 'weekly':
            if (not all((args.period_start, args.pressure_signal, args.backup_directory))
                    or bool(args.backup_key_file) == bool(args.backup_public_recipient)):
                raise EvidenceError('Explicit period, fresh priority signal, backup destination and exactly one encryption mode required')
            # This CLI has no remote backup or custody defaults. Those must be
            # commissioned explicitly before a production period can complete.
            from .priority import read_signal
            from .backup import export_encrypted, read_key, verify_key_custody, archive_to_s3
            from .public_backup import export_public, verify_public_custody
            if args.backup_public_recipient:
                if not args.age_binary or not args.key_custody_receipt:
                    raise EvidenceError('Public-key encryption requires an explicit age executable and Owner custody receipt')
                public_custody = verify_public_custody(args.key_custody_receipt, args.backup_public_recipient)
            else:
                read_key(args.backup_key_file)
            client = custody = None
            if store.production:
                if not all((args.key_custody_receipt, settings.forecast_validation_backup_endpoint_url,
                            settings.forecast_validation_backup_bucket, settings.forecast_validation_backup_access_key_id,
                            settings.forecast_validation_backup_secret_access_key)):
                    raise EvidenceError('Off-device archive and independent key custody must be commissioned before collection')
                custody = (public_custody if args.backup_public_recipient else
                           verify_key_custody(args.key_custody_receipt, args.backup_key_file))
                import boto3
                from botocore.config import Config
                client = boto3.client('s3', endpoint_url=settings.forecast_validation_backup_endpoint_url,
                                      aws_access_key_id=settings.forecast_validation_backup_access_key_id,
                                      aws_secret_access_key=settings.forecast_validation_backup_secret_access_key,
                                      region_name='auto', config=Config(connect_timeout=5, read_timeout=15,
                                                                       retries={'max_attempts': 2}))
            def priority():
                read_signal(args.pressure_signal)
                return True
            def backup(period, publication):
                suffix = '.age' if args.backup_public_recipient else '.aesgcm'
                destination = Path(args.backup_directory) / (period.id + '-' + period.attempt_id + suffix)
                receipt = (export_public(store, destination, args.backup_public_recipient, age=args.age_binary)
                           if args.backup_public_recipient else export_encrypted(store, destination, args.backup_key_file))
                if client:
                    receipt = archive_to_s3(client, settings.forecast_validation_backup_bucket, destination,
                                            receipt, period_id=period.id, custody=custody)
                return receipt
            print(json.dumps(run_period(store, args.period_start, collect_source=collect_source,
                                        pressure=priority, backup=backup)))
            return
        if args.command == 'capture-outcome':
            if not args.run_id or not args.horizon or not args.revision:
                raise EvidenceError('Outcome requires an origin, horizon and explicit revision')
            original = store.get_run_summary(args.run_id)
            if not original:
                raise EvidenceError('Forecast not found/indexed')
            end = datetime.fromisoformat(original['snapshot']['evaluation_start']).date() + timedelta(days=args.horizon)
            if datetime.now(timezone.utc) < datetime.combine(end, datetime.min.time(), TZ):
                raise EvidenceError('Outcome not mature; no source collection started')
            snapshot = collect_source()
            artifact = store.stage_artifact(snapshot)
            previous = store.list_evaluation_meta(args.run_id, horizon=args.horizon, limit=1)
            outcome = FrozenSource(snapshot, artifact=artifact, verified=True).outcome(store, args.run_id, args.horizon,
                        revision=args.revision, supersedes_id=previous[0]['id'] if previous else None)
            print(evaluate(store, args.run_id, outcome))
        else:
            print(forecast(store, collect_source()))
    except Exception as exc:
        if store is not None and args.command not in ('init', 'status', 'commission-legacy'):
            try:
                store.attempt('operator', 'FAILED', type(exc).__name__ + ': ' + str(exc)[:500])
            except Exception as cleanup:
                exc.collection_attempt_error = type(cleanup).__name__
        raise
    finally:
        signal.alarm(0)
        primary = sys.exc_info()[1]
        for engine in (source_engine, store.engine if store else None):
            if engine is not None:
                try:
                    engine.dispose()
                except Exception as cleanup:
                    if primary is None:
                        raise
                    primary.collection_dispose_error = type(cleanup).__name__


if __name__ == '__main__':
    main()
