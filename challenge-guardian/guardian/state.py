"""Persist TrackerState to a JSON file so restarts keep the peak equity,
daily anchor, and breach/pass flags. Losing the peak on a trailing-drawdown
account would silently loosen the floor, so persistence is not optional."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .rules import TrackerState


def load_state(path: Path) -> TrackerState:
    if not path.exists():
        return TrackerState()
    data = json.loads(path.read_text())
    return TrackerState(**data)


def save_state(path: Path, state: TrackerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    tmp.replace(path)
