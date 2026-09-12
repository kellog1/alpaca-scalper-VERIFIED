"""Tiny JSON-backed store for open swing positions across daily runs.

This is local bookkeeping only, not a source of truth - it should always be
reconciled against what actually filled in Webull (see agent.record_fill).
Committed to git (not gitignored) rather than left as an ephemeral file,
so position tracking survives a container restart between scheduled runs.
"""
import json
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"positions": {}}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))
