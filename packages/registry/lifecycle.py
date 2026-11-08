from __future__ import annotations

from enum import StrEnum


class IngestionState(StrEnum):
    STAGED_PRIVATE = "staged_private"
    SCAN_PENDING = "scan_pending"
    SCANNING = "scanning"
    SCAN_CLEAN = "scan_clean"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    INSPECTING = "inspecting"
    READY = "ready"
    FAILED = "failed"


TRANSITIONS: dict[IngestionState, frozenset[IngestionState]] = {
    IngestionState.STAGED_PRIVATE: frozenset({IngestionState.SCAN_PENDING, IngestionState.REJECTED}),
    IngestionState.SCAN_PENDING: frozenset({IngestionState.SCANNING, IngestionState.FAILED}),
    IngestionState.SCANNING: frozenset({IngestionState.SCAN_CLEAN, IngestionState.QUARANTINED, IngestionState.FAILED}),
    IngestionState.SCAN_CLEAN: frozenset({IngestionState.INSPECTING, IngestionState.FAILED}),
    IngestionState.INSPECTING: frozenset({IngestionState.READY, IngestionState.FAILED}),
    IngestionState.QUARANTINED: frozenset(),
    IngestionState.REJECTED: frozenset(),
    IngestionState.READY: frozenset(),
    IngestionState.FAILED: frozenset(),
}


def transition(current: IngestionState, target: IngestionState) -> IngestionState:
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid_ingestion_transition:{current.value}:{target.value}")
    return target


def content_available_to_normal_viewers(state: IngestionState) -> bool:
    return state is IngestionState.READY
