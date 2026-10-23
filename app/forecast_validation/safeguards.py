"""Manual, fail-closed qualification supervisor. Never installs a scheduler.

The operator supplies verified custody and fresh infrastructure observations.
Infrastructure credentials stay in that observer, outside the forecast child.
Nothing here writes business/evidence databases, enables roles or sends messages.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import math
import os
import signal
import stat
import subprocess
import sys
import time

from .contracts import EvidenceError, stamp


class Yield(EvidenceError):
    """Stop this explicit attempt; never automatically retry collection."""


@dataclass(frozen=True)
class Limits:
    web_bytes: int = 1280 * 1024**2
    worker_bytes: int = 900 * 1024**2
    worker_address_bytes: int = 1024 * 1024**2
    worker_cpu_seconds: int = 120
    database_bytes: int = 750 * 1024**2
    web_cores: float = .80
    database_cores: float = .35
    worker_cores: float = .35
    database_connections: int = 30
    active_business_connections: int = 8
    business_latency_ms: int = 1500
    observation_seconds: int = 30
    provider_seconds: int = 90
    deadline_seconds: int = 900
    poll_seconds: float = .25
    termination_seconds: float = 3

    def __post_init__(self):
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid qualification limit: ' + name)
        if not self.worker_bytes < self.worker_address_bytes < self.web_bytes or self.worker_cores >= self.web_cores:
            raise ValueError('Worker must reserve capacity for the web application')


def private_json(path, *, maximum=16384):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > maximum):
            raise Yield('Private operator receipt unavailable')
        return json.load(file)


def _age(value, now, maximum):
    try:
        age = (stamp(now) - stamp(value)).total_seconds()
    except (ValueError, TypeError, KeyError, EvidenceError):
        raise Yield('Missing observation timestamp') from None
    if not 0 <= age <= maximum:
        raise Yield('Stale or future observation')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise Yield('Invalid resource measurement')
    return value


def require_backup(receipt, *, now=None):
    """Check an independently verified receipt, never manufacture one.

    The commissioning adapter must actually download/decrypt/restore the remote
    archive, verify custody and read the retention policy before issuing this
    receipt. A declared destination alone does not satisfy this gate.
    """
    current = now or datetime.now(timezone.utc)
    for name in ('remote_copy_verified', 'authenticated_decryption_verified',
                 'all_table_restore_verified', 'independent_key_recovery_verified',
                 'retention_verified', 'approved_retention_policy', 'isolated_destination_verified'):
        if receipt.get(name) is not True:
            raise Yield('Backup prerequisite unverified: ' + name)
    for name in ('encrypted_sha256', 'key_sha256', 'retention_policy_sha256'):
        value = receipt.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise Yield('Invalid backup verification fingerprint')
    if not receipt.get('custodian') or not receipt.get('archive_identity'):
        raise Yield('Missing independent custody/archive identity')
    if _number(receipt.get('retention_days')) < 1:
        raise Yield('Explicit positive backup retention required')
    _age(receipt.get('verified_at'), current, 86400)
    return True


def require_capacity(sample, limits, *, now=None):
    current = now or datetime.now(timezone.utc)
    _age(sample.get('observed_at'), current, limits.observation_seconds)
    for name in ('web_memory_at', 'web_cpu_at', 'database_memory_at', 'database_cpu_at'):
        _age(sample.get(name), current, limits.provider_seconds)
    checks = [('web_memory_bytes', limits.web_bytes), ('worker_rss_bytes', limits.worker_bytes),
              ('database_memory_bytes', limits.database_bytes), ('web_cpu_cores', limits.web_cores),
              ('database_cpu_cores', limits.database_cores), ('database_connections', limits.database_connections),
              ('active_business_connections', limits.active_business_connections),
              ('business_latency_ms', limits.business_latency_ms)]
    for name, ceiling in checks:
        if _number(sample.get(name)) >= ceiling:
            raise Yield('Resource ceiling: ' + name)
    if _number(sample.get('web_memory_bytes')) == 0 or _number(sample.get('database_memory_bytes')) == 0:
        raise Yield('Memory utilization unknown')
    blocked = sample.get('blocked_business_sessions')
    if isinstance(blocked, bool) or blocked != 0:
        raise Yield('Business lock pressure')
    for name in ('business_backup_active', 'scheduling_active', 'source_sync_active'):
        if sample.get(name) is not False:
            raise Yield('Business work has priority: ' + name)
    return True


def linux_group_usage(group):
    """Current RSS and CPU for exactly the new worker process group."""
    ticks = os.sysconf('SC_CLK_TCK');page = os.sysconf('SC_PAGE_SIZE')
    rss = cpu = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm may contain spaces or parentheses; fields after its last ')'.
            values = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(values[2]) != group:
                continue
            cpu += (int(values[11]) + int(values[12])) / ticks
            rss += max(0, int(values[21])) * page
        except (FileNotFoundError, ProcessLookupError):
            continue
    return {'rss_bytes': rss, 'cpu_seconds': cpu}


def _stop_group(process, grace):
    def send(sig):
        try:os.killpg(process.pid, sig)
        except ProcessLookupError:pass
    send(signal.SIGTERM)
    send(signal.SIGCONT)  # A CPU-paused worker must receive pending termination.
    end = time.monotonic() + grace
    while process.poll() is None and time.monotonic() < end:
        time.sleep(.02)
    send(signal.SIGKILL)  # Includes descendants if the main process exited first.
    process.wait(timeout=max(1, grace))


def supervise(argv, *, environment, backup_receipt, observe, journal, mutex,
              limits=None, usage=linux_group_usage, now=None):
    """One manual child, no retries, no recurring activation or shell execution.

    Failures kill only the newly created process group. The caller must use the
    existing durable period/source publication contracts, never a new origin on
    retry. This local mutex complements, not replaces, PostgreSQL advisory locks.
    """
    if sys.platform != 'linux':
        raise Yield('Qualification supervisor requires Linux process accounting')
    limits = limits or Limits()
    clock = now or (lambda: datetime.now(timezone.utc))
    require_backup(backup_receipt, now=clock())
    require_capacity(observe(), limits, now=clock())
    if environment.get('FORECAST_VALIDATION_SCHEDULE_ENABLED', '').lower() not in ('', 'false', '0'):
        raise Yield('Recurring execution must remain disabled')
    # Never pass provider-admin/session/storage-administration credentials.
    if any(k.startswith(('RENDER_API_', 'CLOUDFLARE_API_', 'OWNER_SESSION')) for k in environment):
        raise Yield('Operator credentials cannot enter the forecast child')
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    lockfd = os.open(mutex, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    process = None;result = {'status': 'YIELDED', 'reason': 'NOT_STARTED'}
    try:
        info = os.fstat(lockfd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Yield('Unsafe qualification mutex')
        try:fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:raise Yield('Qualification already running') from None
        # Recheck after acquiring the serialization boundary, before any child.
        require_backup(backup_receipt, now=clock())
        require_capacity(observe(), limits, now=clock())
        logfd = os.open(journal, flags, 0o600)
        info = os.fstat(logfd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            os.close(logfd);raise Yield('Unsafe qualification journal')
        with os.fdopen(logfd, 'a', buffering=1) as log:
            def record(value):
                log.write(json.dumps({'at': clock().isoformat(), **value})+'\n');log.flush();os.fsync(log.fileno())
            record({'status':'STARTING', 'limits':vars(limits)})
            began = time.monotonic()
            paused = False;cpu_start = None;peak = 0
            try:
                command = [sys.executable, '-m', 'app.forecast_validation.guarded_exec',
                           '--parent', str(os.getpid()), '--address-bytes', str(limits.worker_address_bytes),
                           '--cpu-seconds', str(limits.worker_cpu_seconds), '--', *argv]
                child_environment = {**environment, 'OPENBLAS_NUM_THREADS':'1',
                                     'OMP_NUM_THREADS':'1', 'NUMEXPR_NUM_THREADS':'1'}
                process = subprocess.Popen(command, env=child_environment, start_new_session=True,
                                           stdin=subprocess.DEVNULL, close_fds=True)
                while process.poll() is None:
                    elapsed = time.monotonic() - began
                    if elapsed >= limits.deadline_seconds:
                        raise Yield('Qualification execution deadline')
                    consumed = usage(process.pid);peak = max(peak, consumed['rss_bytes'])
                    sample = dict(observe());sample['worker_rss_bytes'] = consumed['rss_bytes']
                    require_capacity(sample, limits, now=clock())
                    if cpu_start is None:cpu_start = consumed['cpu_seconds']
                    # Cumulative CPU budget plus one polling interval of burst.
                    # SIGSTOP throttles only this private worker group; elapsed
                    # wall-clock budget continues while paused.
                    should_pause = consumed['cpu_seconds'] - cpu_start > limits.worker_cores * (elapsed + limits.poll_seconds)
                    if should_pause != paused:
                        os.killpg(process.pid, signal.SIGSTOP if should_pause else signal.SIGCONT);paused = should_pause
                    record({'status':'PAUSED_CPU' if paused else 'RUNNING','elapsed_seconds':elapsed,'worker_rss_bytes':consumed['rss_bytes']})
                    time.sleep(limits.poll_seconds)
                if process.returncode != 0:
                    raise Yield('Qualification child failed')
                result = {'status':'COMPLETE','exit_code':0,'elapsed_seconds':time.monotonic()-began,'peak_worker_rss_bytes':peak}
            except BaseException as exc:
                # Error messages are bounded fixed codes, never child SQL/URLs.
                result = {'status':'YIELDED','reason':str(exc) if isinstance(exc,Yield) else type(exc).__name__}
                raise
            finally:
                if process is not None:_stop_group(process, limits.termination_seconds)
                record(result)
        return result
    finally:
        if process is not None and process.poll() is None:_stop_group(process, limits.termination_seconds)
        os.close(lockfd)
