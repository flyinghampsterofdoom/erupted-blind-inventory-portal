from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ResultKind(str, Enum):
    SUCCESS = 'success'
    VALIDATION_ERROR = 'validation_error'
    AUTHORIZATION_FAILURE = 'authorization_failure'
    CONFLICT = 'conflict'
    EXTERNAL_FAILURE = 'external_failure'
    PARTIAL_EXTERNAL_SUCCESS = 'partial_external_success'
    SERVER_FAILURE = 'server_failure'


class SaveOutcome(str, Enum):
    NOTHING_SAVED = 'nothing_saved'
    LOCAL_SAVED = 'local_saved'
    LOCAL_AND_EXTERNAL_SAVED = 'local_and_external_saved'
    PARTIAL_EXTERNAL_SUCCESS = 'partial_external_success'


@dataclass(frozen=True)
class ActionResult:
    kind: ResultKind
    message: str
    save_outcome: SaveOutcome
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    field_errors: dict[str, str] = field(default_factory=dict)
    safe_retry: bool = False
    manual_resolution_required: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        result = asdict(self)
        result['kind'] = self.kind.value
        result['save_outcome'] = self.save_outcome.value
        return result
