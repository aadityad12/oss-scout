"""How well does a repo treat outside contributors?

Measured from its own recent history: of the PRs opened by people who are not
members, how many got merged, how fast, and how quickly a maintainer replied.
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import policy
from .github import GitHub

OUTSIDE = {"NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"}
MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
RESPONSE_SAMPLE = 10
CLOSED_SAMPLE = 300  # busy company repos can have few outside PRs among the last 100


def parse_ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _days(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400


def _scale(value: float | None, good: float, bad: float) -> float:
    """Map value onto 1..0 on a log scale between `good` and `bad` (good < bad)."""
    if value is None:
        return 0.5
    v = max(value, good)
    if v >= bad:
        return 0.0
    return 1 - math.log(v / good) / math.log(bad / good)


FIRST_TIMERS = {"NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"}
REGULAR_THRESHOLD = 3  # this many PRs in the sample marks someone as staff or a regular


def from_fork(pr: dict, repo: str) -> bool:
    """Outsiders must open PRs from a fork; only people with write access can push
    a branch to the project itself. A deleted fork shows as head.repo = null."""
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    return head_repo is None or head_repo.lower() != repo.lower()


def is_outside(pr: dict, author_counts: dict[str, int] | None = None, repo: str | None = None) -> bool:
    """An occasional outside contributor.

    Staff whose org membership is private show up as CONTRIBUTOR, just like real
    outsiders. Two signals separate them: staff usually push branches to the
    project repo itself (outsiders can't), and staff author many PRs. The question
    this answers is the useful one: how does the project treat people who show up
    now and then?
    """
    user = pr.get("user") or {}
    if user.get("type") == "Bot":
        return False
    if repo and not from_fork(pr, repo):
        return False
    assoc = pr.get("author_association")
    if assoc in FIRST_TIMERS:
        return True
    if assoc == "CONTRIBUTOR":
        return (author_counts or {}).get(user.get("login"), 0) < REGULAR_THRESHOLD
    return False


def first_maintainer_response_hours(gh: GitHub, repo: str, pr: dict,
                                    insiders: set[str] | None = None) -> float | None:
    insiders = insiders or set()
    author = pr["user"]["login"]
    opened = parse_ts(pr["created_at"])
    times = []
    for kind in ("issues/{n}/comments", "pulls/{n}/reviews"):
        try:
            items = gh.get(f"repos/{repo}/" + kind.format(n=pr["number"]),
                           {"per_page": 30}, ttl=7 * 86400)
        except Exception:
            continue
        for c in items or []:
            who = (c.get("user") or {})
            if who.get("login") == author or who.get("type") == "Bot":
                continue
            is_review = "submitted_at" in c
            if (not is_review and c.get("author_association") not in MAINTAINER
                    and who.get("login") not in insiders):
                continue  # a comment from another passer-by isn't a project response
            ts = parse_ts(c.get("created_at") or c.get("submitted_at"))
            if ts:
                times.append(ts)
    if not times:
        return None
    return max(0.0, (min(times) - opened).total_seconds() / 3600)


def pr_sample(gh: GitHub, repo: str) -> tuple[list[dict], list[dict]]:
    """The last CLOSED_SAMPLE closed PRs (by update time) and the 100 newest open ones."""
    all_closed = list(gh.paginate(f"repos/{repo}/pulls",
                                  {"state": "closed", "sort": "updated", "direction": "desc"},
                                  max_items=CLOSED_SAMPLE, ttl=86400))
    all_open = list(gh.paginate(f"repos/{repo}/pulls",
                                {"state": "open", "sort": "created", "direction": "desc"},
                                max_items=100, ttl=86400))
    return all_closed, all_open


def author_counts(prs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in prs:
        login = (p.get("user") or {}).get("login")
        counts[login] = counts.get(login, 0) + 1
    return counts


def measure(gh: GitHub, repo: str, now: datetime | None = None, data_dir: Path | None = None) -> dict:
    """Measure `repo`. With `data_dir`, also merge its outside PRs into data_dir/pr_outcomes.jsonl
    (see prdata.py); that uses only what was fetched here, no extra API calls."""
    now = now or datetime.now(timezone.utc)
    meta = gh.repo(repo) or {}
    all_closed, all_open = pr_sample(gh, repo)
    counts = author_counts(all_closed + all_open)
    insiders = {login for login, n in counts.items() if n >= REGULAR_THRESHOLD}
    insiders |= {(p.get("user") or {}).get("login") for p in all_closed + all_open
                 if p.get("author_association") in MAINTAINER or not from_fork(p, repo)}
    closed = [p for p in all_closed if is_outside(p, counts, repo)]
    open_prs = [p for p in all_open if is_outside(p, counts, repo)]

    merged = [p for p in closed if p.get("merged_at")]
    merge_rate = len(merged) / len(closed) if closed else None
    # Some projects land outside PRs through an internal mirror and close them on
    # GitHub, so "merged" never shows. Don't mistake that for rejecting everyone.
    merges_elsewhere = merge_rate is not None and merge_rate < 0.15 and len(closed) >= 20
    merge_days = statistics.median(
        _days(parse_ts(p["created_at"]), parse_ts(p["merged_at"])) for p in merged
    ) if merged else None
    stale_open = sum(1 for p in open_prs if _days(parse_ts(p["created_at"]), now) > 30)

    # PRs opened in the last few days may simply not have been answered *yet*.
    settled = [p for p in closed + open_prs if _days(parse_ts(p["created_at"]), now) >= 3]
    sample = sorted(settled, key=lambda p: p["created_at"], reverse=True)[:RESPONSE_SAMPLE]
    responses = [h for p in sample
                 if (h := first_maintainer_response_hours(gh, repo, p, insiders)) is not None]
    response_hours = statistics.median(responses) if responses else None
    responded_share = len(responses) / len(sample) if sample else None

    pushed = parse_ts(meta.get("pushed_at"))
    active = 1.0 if pushed and _days(pushed, now) < 7 else 0.5 if pushed and _days(pushed, now) < 30 else 0.0

    confident = len(closed) >= 5
    friendliness = (
        0.45 * (0.5 if merges_elsewhere or merge_rate is None else merge_rate)
        + 0.25 * (0.5 if merges_elsewhere else _scale(merge_days, good=2, bad=60))
        + 0.20 * (_scale(response_hours, good=12, bad=24 * 21) * (responded_share or 0.5))
        + 0.10 * active
    ) if confident else 0.5

    pol = policy.fetch(gh, repo)
    result = {
        "repo": repo,
        "measured_at": time.time(),
        "stars": meta.get("stargazers_count"),
        "language": meta.get("language"),
        "owner_type": (meta.get("owner") or {}).get("type"),
        "homepage": meta.get("homepage"),
        "archived": meta.get("archived", False),
        "description": meta.get("description"),
        "topics": meta.get("topics") or [],  # lets the scanner guess a field for discovered repos
        "outside_prs_sampled": len(closed),
        "merge_rate": merge_rate,
        "merges_elsewhere": merges_elsewhere,
        "median_days_to_merge": merge_days,
        "median_hours_to_first_response": response_hours,
        "responded_share": responded_share,
        "stale_open_outside_prs": stale_open,
        "confident": confident,
        "friendliness": round(friendliness, 3),
        "ai_policy": pol["ai"],
        "ai_policy_evidence": pol["evidence"],
        "ai_mode": pol.get("mode", "draft"),
        "ai_posts_forbidden": pol.get("ai_posts_forbidden", False),
        "disclosure_required": pol.get("disclosure_required", False),
        "cla": pol["cla"],
        "policy_files": pol["files"],
    }
    if data_dir is not None:
        from . import prdata
        prdata.merge(Path(data_dir) / prdata.FILE,
                     prdata.rows(repo, all_closed, all_open, counts, now, repo_meta=result),
                     add_since=prdata.complete_since(all_closed, all_open, CLOSED_SAMPLE))
    return result


def provisional(gh: GitHub, repo: str) -> dict:
    """A stand-in for a repo that hasn't been measured yet: its basic facts (one cached call)
    and a neutral friendliness. Not stored, so a later night measures it properly."""
    meta = gh.repo(repo) or {}
    return {"repo": repo, "provisional": True, "friendliness": 0.5, "confident": False,
            "stars": meta.get("stargazers_count"), "language": meta.get("language"),
            "owner_type": (meta.get("owner") or {}).get("type"), "archived": meta.get("archived", False),
            "description": meta.get("description"), "homepage": meta.get("homepage"),
            "topics": meta.get("topics") or [], "ai_policy": "none", "policy_files": []}


def get(gh: GitHub, repo: str, cache: dict, ttl_days: float, budget: list[int] | None = None,
        new_budget: list[int] | None = None, data_dir: Path | None = None) -> dict:
    """Return a cached measurement if fresh, else re-measure and store in `cache`.

    `budget` is a one-element list counting how many stale repos may still be
    re-measured this run; past it, stale numbers are reused so the nightly run
    stays short and the weekly refresh spreads across several nights.
    `new_budget` does the same for repos never measured before (about 35 API calls
    each): past it they get a provisional entry. Adding many projects at once would
    otherwise blow the Actions token's 1,000 requests an hour in a single night.
    `data_dir` is passed to `measure` so a fresh measurement also records per-PR rows.
    """
    hit = cache.get(repo)
    if hit and time.time() - hit.get("measured_at", 0) < ttl_days * 86400:
        return hit
    if hit and budget is not None:
        if budget[0] <= 0:
            return hit
        budget[0] -= 1
    if not hit and new_budget is not None:
        if new_budget[0] <= 0:
            return provisional(gh, repo)
        new_budget[0] -= 1
    cache[repo] = measure(gh, repo, data_dir=data_dir)
    return cache[repo]
