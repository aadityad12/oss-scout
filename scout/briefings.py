"""Briefings are what the Claude step writes for each pick.

Layout, inside the data dir:

  briefings/<owner>__<repo>__<number>/briefing.json   (schema: REQUIRED below)
  briefings/<owner>__<repo>__<number>/draft.patch     (draft mode only)
  picks/<YYYY-MM-DD>.json                             what was considered and why

`ingest` turns new briefings into tracked suggestions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = {
    "key": str, "repo": str, "number": int, "title": str, "url": str,
    "summary": str,            # the issue in plain English
    "why": str,                # why this one, for you, now
    "difficulty": str,         # easy | medium | hard
    "time_estimate": str,      # e.g. "45-90 min"
    "walkthrough": str,        # markdown: how the relevant code works
    "change_explained": str,   # markdown: what the draft changes and why, piece by piece
    "alternatives": list,      # approaches considered and rejected, with reasons
    "maintainer_qa": list,     # [{q, a}] questions a reviewer is likely to ask
    "tests": dict,             # {ran, command, result, not_verified}
    "claim_comment": str,      # draft comment for YOU to post on the issue
    "submit_steps": list,      # exact steps from fork to PR
    "ai_disclosure": str,      # what this repo's policy asks for
}


def slug(key: str) -> str:
    repo, number = key.split("#")
    return repo.replace("/", "__") + f"__{number}"


MODES = {"draft", "guide"}


def validate(b: dict) -> list[str]:
    problems = [f"missing {k}" for k in REQUIRED if k not in b]
    if b.get("mode", "draft") not in MODES:
        problems.append(f"mode should be one of {sorted(MODES)}")
    problems += [f"{k} should be {t.__name__}" for k, t in REQUIRED.items()
                 if k in b and not isinstance(b[k], t)]
    return problems


def load_all(data: Path) -> list[dict]:
    out = []
    for f in sorted((data / "briefings").glob("*/briefing.json")):
        b = json.loads(f.read_text())
        patch = f.parent / "draft.patch"
        b["_dir"] = str(f.parent.relative_to(data))
        b.setdefault("mode", "draft")
        # guide mode means the project doesn't accept AI-written code: never show a patch
        b["_patch"] = patch.read_text() if patch.exists() and b["mode"] == "draft" else ""
        b["_problems"] = validate(b)
        out.append(b)
    return out


def ingest(data: Path, state: dict) -> int:
    added = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for b in load_all(data):
        if b["_problems"] or b["key"] in state["suggestions"]:
            continue
        state["suggestions"][b["key"]] = {
            "repo": b["repo"], "number": b["number"], "title": b["title"], "url": b["url"],
            "status": "suggested", "suggested_at": b.get("picked_at", now),
            "briefing": b["_dir"], "difficulty": b["difficulty"], "mode": b["mode"],
            "history": [{"at": now, "from": None, "to": "suggested"}],
        }
        added += 1
    return added
