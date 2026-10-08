"""How much is already in flight? Decides whether tonight may prepare a ready item.

Read from state: your open PRs (from the tracker) and ready items that haven't
been submitted yet. A failed send leaves its item unsent, so one stuck item must not
block every later night: only `max_unsent` of them together do. The result goes into
candidates.json for the Claude step.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .score import parse_ts
from .track import PENDING


def snoozed(s: dict, now: datetime | None = None) -> bool:
    until = parse_ts(s.get("snoozed_until"))
    return bool(until and until > (now or datetime.now(timezone.utc)))


def requested(state: dict) -> list[str]:
    """Suggestions you asked to have prepared, oldest request first."""
    asked = [(s["prepare_requested_at"], k) for k, s in state.get("suggestions", {}).items()
             if s.get("prepare_requested_at") and s.get("status") == "suggested"]
    return [k for _, k in sorted(asked)]


def compute(state: dict, settings: dict, now: datetime | None = None) -> dict:
    max_ready = settings.get("max_ready", 1)
    max_open = settings.get("max_open_prs", 3)
    max_per_repo = settings.get("max_open_prs_per_repo", 1)
    max_unsent = settings.get("max_unsent", 2)

    open_prs = [p for p in state.get("contributions", {}).get("prs", []) if p.get("status") == "open"]
    by_repo: dict[str, int] = {}
    for p in open_prs:
        by_repo[p["repo"]] = by_repo.get(p["repo"], 0) + 1
    waiting = [{"repo": p["repo"], "number": p["number"], "url": p.get("url")}
               for p in open_prs if p.get("waiting_on_you")]
    unsent = sorted(k for k, s in state.get("suggestions", {}).items() if s.get("status") in PENDING and not snoozed(s, now))

    reasons = []
    if max_ready < 1:
        reasons.append("max_ready is 0")
    if waiting:
        reasons.append("a maintainer is waiting on you: " + ", ".join(f"{w['repo']}#{w['number']}" for w in waiting))
    if len(open_prs) >= max_open:
        reasons.append(f"{len(open_prs)} open PRs (max {max_open})")
    if len(unsent) >= max_unsent:
        reasons.append(f"{len(unsent)} ready but not submitted yet (max {max_unsent}): " + ", ".join(unsent))

    return {
        "ready_allowed": not reasons,
        "reason": "; ".join(reasons) or "ok",
        "open_prs": len(open_prs),
        "open_prs_by_repo": by_repo,
        "waiting_on_you": waiting,
        "blocked_repos": sorted(r for r, n in by_repo.items() if n >= max_per_repo),
    }
