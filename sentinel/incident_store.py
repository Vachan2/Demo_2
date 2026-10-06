"""Thread-safe in-memory incident store."""
from __future__ import annotations

import threading
from typing import Iterator

from models import Incident, IncidentStatus

_store: dict[str, Incident] = {}
_lock = threading.Lock()
_seq: int = 0


def save(incident: Incident) -> Incident:
    with _lock:
        _store[incident.id] = incident
    return incident


def get(incident_id: str) -> Incident | None:
    with _lock:
        return _store.get(incident_id)


def all_incidents() -> list[Incident]:
    with _lock:
        return list(_store.values())


def has_open_incident(service: str) -> bool:
    """Return True if there is already an open (non-terminal) incident for this service."""
    terminal = {IncidentStatus.RESOLVED, IncidentStatus.FAILED}
    with _lock:
        return any(
            inc.service == service and inc.status not in terminal
            for inc in _store.values()
        )


def next_incident_id() -> str:
    """Generate a sequential, human-readable incident ID."""
    global _seq
    with _lock:
        _seq += 1
        return f"INC-{_seq:03d}"


def clear() -> None:
    """Remove all incidents (test helper)."""
    with _lock:
        _store.clear()
