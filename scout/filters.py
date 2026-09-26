"""Is anyone already on this issue?"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .github import GitHub
from .score import MAINTAINER, parse_ts

CLAIM = re.compile(
    r"\b(?:i'?d like to|i would like to|i want to|can i|could i|may i|let me|i'?ll|i will|"
    r"i am going to|i'?m going to)\s+(?:\w+\s+){0,2}(?:work|take|pick|handle|tackle|try|fix|look)"
    r"|assign (?:it |this )?(?:issue )?to me|please assign|working on (?:this|it)|"
    r"i'?ve (?:opened|submitted|raised) a pr|pr (?:is )?(?:up|open)\b",
    re.I,
)
DISCUSSION_KEEP = 5

BLOCKING_LABELS = re.compile(
    r"wontfix|won't fix|duplicate|invalid|blocked|on.?hold|needs.?design|needs.?decision|"
    r"discussion|question|stale|pr submitted|in progress|assigned",
    re.I,
)


def blocking_label(labels: list[str]) -> str | None:
    return next((l for l in labels if BLOCKING_LABELS.search(l)), None)


def activity(gh: GitHub, repo: str, number: int, claim_window_days: int,
             now: datetime | None = None) -> dict:
    """Read the issue timeline once and report linked PRs and claim comments."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=claim_window_days)
    linked_open_prs, claims, maintainer_comments, discussion = [], [], 0, []
    for ev in gh.paginate(f"repos/{repo}/issues/{number}/timeline", max_items=300, ttl=3 * 3600):
        kind = ev.get("event")
        if kind == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if "pull_request" in src and src.get("state") == "open":
                linked_open_prs.append(src.get("html_url"))
        elif kind == "connected":
            linked_open_prs.append("connected-pr")
        elif kind == "commented":
            if ev.get("author_association") in MAINTAINER:
                maintainer_comments += 1
            discussion.append({"by": (ev.get("user") or ev.get("actor") or {}).get("login"),
                               "role": ev.get("author_association"), "at": ev.get("created_at"),
                               "body": (ev.get("body") or "")[:1500]})
            ts = parse_ts(ev.get("created_at"))
            if ts and ts >= cutoff and CLAIM.search(ev.get("body") or ""):
                claims.append({"by": (ev.get("user") or ev.get("actor") or {}).get("login"),
                               "at": ev.get("created_at")})
    # The nightly Claude step can't call the GitHub API, so it reads the discussion from here.
    return {"linked_open_prs": linked_open_prs, "recent_claims": claims,
            "maintainer_comments": maintainer_comments, "discussion": discussion[-DISCUSSION_KEEP:]}
