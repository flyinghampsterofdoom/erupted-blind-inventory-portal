"""Fail-closed handoff from an approved operations monitor; no monitor installed.

The forecasting worker never receives infrastructure-admin credentials. A fresh
Owner-controlled signal must account for the database and business workload.
Missing/stale signals yield rather than guessing that store operations are idle.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import stat

from .contracts import EvidenceError, stamp


def read_signal(path, *, now=None):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise EvidenceError('Operational priority signal must be a small private Owner-controlled file')
        value = json.load(source)
    current = stamp(now or datetime.now(timezone.utc))
    if not timedelta(0) <= current - stamp(value['observed_at']) <= timedelta(minutes=5):
        raise EvidenceError('Operational priority signal is stale or from the future')
    required = ('database_memory_bytes', 'blocked_business_sessions', 'business_backup_active',
                'sunday_automation_active', 'business_latency_acceptable')
    if any(key not in value for key in required):
        raise EvidenceError('Incomplete operational priority signal')
    if (not isinstance(value['database_memory_bytes'], int) or isinstance(value['database_memory_bytes'], bool)
            or not 0 < value['database_memory_bytes'] < 750 * 1024 ** 2
            or value['blocked_business_sessions'] != 0 or value['business_backup_active'] is not False
            or value['sunday_automation_active'] is not False or value['business_latency_acceptable'] is not True):
        raise EvidenceError('Forecasting yields to business pressure or backup/automation work')
    return value
