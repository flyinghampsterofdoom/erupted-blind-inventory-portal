from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status

from app.auth import Principal, get_current_principal
from app.config import settings


def _keys(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in str(value or '').split(',') if item.strip())


def _principal_entries(value: str) -> frozenset[tuple[int, str]]:
    entries: set[tuple[int, str]] = set()
    for raw in str(value or '').split(','):
        principal_raw, separator, feature = raw.strip().partition(':')
        if not separator or not principal_raw.isdigit() or not feature.strip():
            continue
        entries.add((int(principal_raw), feature.strip()))
    return frozenset(entries)


@dataclass(frozen=True)
class FeatureExposure:
    global_features: frozenset[str]
    principal_features: frozenset[tuple[int, str]]

    @classmethod
    def from_settings(cls) -> 'FeatureExposure':
        return cls(_keys(settings.v2_enabled_features), _principal_entries(settings.v2_principal_features))

    def enabled(self, feature: str, *, principal_id: int | None = None) -> bool:
        return feature in self.global_features or (
            principal_id is not None and (principal_id, feature) in self.principal_features
        )


def require_v2_feature(feature: str):
    def _dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not FeatureExposure.from_settings().enabled(feature, principal_id=principal.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return principal

    return _dependency
