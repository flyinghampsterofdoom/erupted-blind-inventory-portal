from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Mapping

from sqlalchemy.orm import Session


class CompletionState(str, Enum):
    COMPLETE = 'complete'
    INCOMPLETE = 'incomplete'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True)
class CompletionResolution:
    state: CompletionState
    href: str | None = None
    action_label: str | None = None


@dataclass(frozen=True)
class CompletionActivityDef:
    key: str
    label: str
    order: int
    permission: str


@dataclass(frozen=True)
class CompletionStatus:
    key: str
    label: str
    state: CompletionState
    href: str | None
    action_label: str | None

    @property
    def state_label(self) -> str:
        return {
            CompletionState.COMPLETE: 'Complete',
            CompletionState.INCOMPLETE: 'Not Complete',
            CompletionState.UNAVAILABLE: 'Coming Later',
        }[self.state]

    @property
    def state_icon(self) -> str:
        return {
            CompletionState.COMPLETE: '✓',
            CompletionState.INCOMPLETE: '✕',
            CompletionState.UNAVAILABLE: '—',
        }[self.state]


CompletionSource = Callable[[Session, int, date], CompletionResolution]


STORE_OPERATIONS_ACTIVITIES: tuple[CompletionActivityDef, ...] = (
    CompletionActivityDef(
        'daily_chore_list',
        'Daily Chore List',
        10,
        'nav.store_operations.daily_chores',
    ),
    CompletionActivityDef(
        'inventory_count',
        'Inventory Count',
        20,
        'nav.store_operations.inventory_counts',
    ),
    CompletionActivityDef(
        'non_sellable_stock_take',
        'Non-Sellable Stock Take',
        30,
        'nav.store_operations.non_sellable_counts',
    ),
    CompletionActivityDef(
        'change_box_count_am',
        'Change Box Count — AM',
        40,
        'nav.store_operations.change_box_count',
    ),
    CompletionActivityDef(
        'change_box_count_pm',
        'Change Box Count — PM',
        50,
        'nav.store_operations.change_box_count',
    ),
)


def completion_statuses(
    db: Session,
    *,
    store_id: int,
    business_date: date,
    permission_flags: Mapping[str, bool],
    sources: Mapping[str, CompletionSource] | None = None,
) -> list[CompletionStatus]:
    source_map = sources or {}
    broad_allowed = bool(permission_flags.get('nav.store_operations.all', False))
    statuses: list[CompletionStatus] = []
    for activity in sorted(STORE_OPERATIONS_ACTIVITIES, key=lambda row: row.order):
        if not (broad_allowed or permission_flags.get(activity.permission, False)):
            continue
        source = source_map.get(activity.key)
        resolution = (
            source(db, store_id, business_date)
            if source is not None
            else CompletionResolution(CompletionState.UNAVAILABLE)
        )
        statuses.append(
            CompletionStatus(
                key=activity.key,
                label=activity.label,
                state=resolution.state,
                href=resolution.href,
                action_label=resolution.action_label,
            )
        )
    return statuses
