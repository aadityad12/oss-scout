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


ANONYMOUS_LINK = "connected-pr"  # a `connected` event that doesn't say which PR it was


def _link_id(ev: dict) -> str | None:
    """Which PR a connected/disconnected event is about, when the event says."""
    src = (ev.get("source") or {}).get("issue") or {}
    sub = ev.get("subject") or {}
    return src.get("html_url") or sub.get("html_url") or sub.get("url")


def _is_stale(updated_at: str | None, now: datetime, stale_pr_days: int) -> bool:
    """A PR nobody has touched for this long is most likely abandoned."""
    ts = parse_ts(updated_at)
    return bool(ts) and ts < now - timedelta(days=stale_pr_days)


def _unlink(linked: list[str], ev: dict) -> None:
    """A disconnect cancels the matching connect, or any one unnamed connect if the event names no PR."""
    ident = _link_id(ev)
    if ident in linked:
        linked.remove(ident)
    elif ANONYMOUS_LINK in linked:
        linked.remove(ANONYMOUS_LINK)


def activity(gh: GitHub, repo: str, number: int, claim_window_days: int,
             now: datetime | None = None, stale_pr_days: int = 90) -> dict:
    """Read the issue timeline once and report linked PRs and claim comments.

    A linked PR whose own last update is older than `stale_pr_days` is reported
    separately (`stale_linked_prs`) and does not block the issue: the author has
    most likely walked away, and the routine can mention it.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=claim_window_days)
    linked_open_prs, stale_linked_prs, claims, maintainer_comments, discussion = [], [], [], 0, []
    for ev in gh.paginate(f"repos/{repo}/issues/{number}/timeline", max_items=300, ttl=3 * 3600):
        kind = ev.get("event")
        if kind == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if "pull_request" in src and src.get("state") == "open":
                if _is_stale(src.get("updated_at"), now, stale_pr_days):
                    if src.get("html_url") not in [p["url"] for p in stale_linked_prs]:
                        stale_linked_prs.append({"url": src.get("html_url"), "updated_at": src.get("updated_at")})
                else:
                    linked_open_prs.append(src.get("html_url"))
        elif kind == "connected":
            ident = _link_id(ev) or ANONYMOUS_LINK
            if ident == ANONYMOUS_LINK or ident not in linked_open_prs:
                linked_open_prs.append(ident)
        elif kind == "disconnected":
            _unlink(linked_open_prs, ev)
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
    return {"linked_open_prs": linked_open_prs, "stale_linked_prs": stale_linked_prs,
            "recent_claims": claims, "maintainer_comments": maintainer_comments,
            "discussion": discussion[-DISCUSSION_KEEP:]}
