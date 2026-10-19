"""Strict, timezone-aware RFC3339 comparisons without fractional truncation.

Returns exact rational UTC seconds. Python 3.10's datetime parser accepts only
some fractional widths; Render emits nine. Never compare provider times as floats.
"""
import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction

_PATTERN = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})\Z')


def timestamp(value):
    if not isinstance(value, str) or len(value)>4096:
        raise ValueError('Expected bounded RFC3339 timestamp')
    match = _PATTERN.fullmatch(value)
    if not match:
        raise ValueError('Malformed RFC3339 timestamp')
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    fraction, zone = match.groups()[6:]
    # RFC3339 -00:00 means UTC is known but the local offset is unknown.
    # That distinction does not prevent comparison of the UTC instants.
    offset = timedelta()
    if zone.upper() != 'Z':
        zh, zm = int(zone[1:3]), int(zone[4:6])
        if zh>23 or zm>59:
            raise ValueError('Invalid timezone offset')
        offset = timedelta(hours=zh, minutes=zm) * (1 if zone[0]=='+' else -1)
    base = datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    delta = base - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return Fraction(delta.days*86400+delta.seconds) + (Fraction(int(fraction),10**len(fraction)) if fraction else 0)


def provider_time(value):
    from scripts.release.verification_errors import ProviderEvidenceError
    try:
        return timestamp(value)
    except (TypeError, ValueError, OverflowError):
        raise ProviderEvidenceError('Malformed or non-comparable provider timestamp') from None
