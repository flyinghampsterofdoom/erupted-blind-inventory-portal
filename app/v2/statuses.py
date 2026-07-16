from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PresentationStatus:
    key: str
    label: str
    meaning: str
    icon: str
    tone: str
    category: str = 'business'


def _status(key: str, label: str, meaning: str, icon: str, tone: str, category: str = 'business'):
    return PresentationStatus(key, label, meaning, icon, tone, category)


STATUS_REGISTRY = {
    row.key: row
    for row in (
        _status('draft', 'Draft', 'Editable work that has not been submitted.', 'edit', 'neutral'),
        _status('submitted', 'Submitted', 'Finalized locally for review or processing.', 'send', 'info'),
        _status('pending', 'Pending', 'A queued operation has not reached a terminal outcome.', 'clock', 'warning', 'sync'),
        _status('needs_review', 'Needs Review', 'A documented human review is required.', 'review', 'warning'),
        _status('in_progress', 'In Progress', 'Work has begun and is not complete.', 'progress', 'info'),
        _status('in_transit', 'In Transit', 'Goods left the source and are not yet received.', 'truck', 'info'),
        _status('partially_received', 'Partially Received', 'Some expected quantity was received.', 'package-open', 'warning'),
        _status('partially_completed', 'Partially Completed', 'Only some command targets completed.', 'split', 'warning'),
        _status('completed', 'Completed', 'The local business workflow reached terminal success.', 'check', 'success'),
        _status('cancelled', 'Cancelled', 'Work ended intentionally without completion.', 'cancel', 'neutral'),
        _status('succeeded', 'Succeeded', 'A technical operation completed successfully.', 'check-circle', 'success', 'sync'),
        _status('failed', 'Failed', 'An operation failed and needs recovery or review.', 'error', 'danger', 'sync'),
        _status('submitted_to_square', 'Submitted to Square', 'Recorded evidence shows submission to Square.', 'external', 'info', 'sync'),
        _status('inactive', 'Inactive', 'Configuration is retained but unavailable for new use.', 'archive', 'neutral'),
        _status('verified', 'Verified', 'An authenticated reviewer confirmed the fact.', 'verified', 'success'),
        _status('resolved', 'Resolved', 'A defined issue closed with resolution evidence.', 'resolved', 'success'),
        _status('needs_attention', 'Needs Attention', 'A derived flag links to the underlying reason.', 'alert', 'warning'),
    )
}

STATUS_ALIASES = {
    'success': 'succeeded',
    'pushed': 'submitted_to_square',
}


def presentation_status(value: object) -> PresentationStatus:
    raw = str(getattr(value, 'value', value) or '').strip()
    key = raw.lower().replace(' ', '_')
    key = STATUS_ALIASES.get(key, key)
    return STATUS_REGISTRY.get(
        key,
        PresentationStatus(
            key='unknown',
            label=f'Unknown ({raw})' if raw else 'Unknown',
            meaning='The stored value is not mapped and requires investigation.',
            icon='help',
            tone='neutral',
        ),
    )


def status_context(value: object) -> dict:
    return asdict(presentation_status(value))
