"""Preserve the refresh's first failure; never replace it with a verifier symptom."""
import re


def require_current(result):
    if not result.started or result.state != 'current':
        raise RuntimeError(result.message or f'History refresh ended in {result.state}')


def failure_message(state, error):
    # Refresh already records its actionable source error. A later verifier must
    # not overwrite that first cause. Only a *successful* refresh can subsequently
    # fail source-parity or preservation verification.
    if state.last_result == 'FAILED' and state.last_error:
        return state.last_error
    return str(error)[:1000]


def safe_diagnostic(error):
    value=str(error)[:1000]
    value=re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+', '<redacted URL>', value)
    value=re.sub(r'(?i)(bearer\s+|(?:password|token|secret|access_key)\s*[=:]\s*)[^\s,;]+', r'\1<redacted>', value)
    return value
