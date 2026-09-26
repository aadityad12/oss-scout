from __future__ import annotations

import json
from pathlib import Path

EMPTY = {"version": 1, "repos": {}, "suggestions": {}, "contributions": {},
         "home_projects": [], "runs": []}


def load(data: Path) -> dict:
    p = data / "state.json"
    if not p.exists():
        return json.loads(json.dumps(EMPTY))
    state = json.loads(p.read_text())
    for k, v in EMPTY.items():
        state.setdefault(k, json.loads(json.dumps(v)))
    return state


def save(data: Path, state: dict) -> None:
    state["runs"] = state["runs"][-60:]
    write_json(data / "state.json", state)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")
    tmp.replace(path)
