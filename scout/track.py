"""What have you actually done? Read from GitHub, never self-reported.

Finds your PRs, reviews and issues on other people's repos, links them to past
suggestions, and moves each suggestion along:

  suggested -> claimed -> pr_open -> waiting_on_you -> merged | closed
  suggested -> skipped (untouched too long) | taken (someone else got it)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .github import GitHub
from .score import MAINTAINER, _days, parse_ts

ACTIVE = {"suggested", "claimed", "pr_open", "waiting_on_you"}


def ladder(merged: int, maintainer: bool) -> str:
    if maintainer:
        return "Maintainer"
    if merged >= 8:
        return "Trusted contributor"
    if merged >= 3:
        return "Regular contributor"
    if merged >= 1:
        return "Contributor"
    return "Getting started"


def _repo_of(item: dict) -> str:
    return item["repository_url"].split("/repos/", 1)[1]


def waiting_on_you(gh: GitHub, repo: str, number: int, login: str) -> bool:
    """True when the latest human word on the PR is someone else's."""
    mine, theirs = [], []
    sources = [
        (f"repos/{repo}/issues/{number}/comments", "created_at"),
        (f"repos/{repo}/pulls/{number}/reviews", "submitted_at"),
        (f"repos/{repo}/pulls/{number}/comments", "created_at"),
    ]
    for path, field in sources:
        for c in gh.paginate(path, max_items=100, ttl=1800):
            who = c.get("user") or {}
            ts = parse_ts(c.get(field))
            if not ts or who.get("type") == "Bot":
                continue
            (mine if who.get("login") == login else theirs).append(ts)
    for c in gh.paginate(f"repos/{repo}/pulls/{number}/commits", max_items=250, ttl=1800):
        ts = parse_ts(((c.get("commit") or {}).get("committer") or {}).get("date"))
        if ts:
            mine.append(ts)
    return bool(theirs) and (not mine or max(theirs) > max(mine))


def collect(gh: GitHub, login: str) -> dict:
    prs = []
    for it in gh.search_issues(f"type:pr author:{login} -user:{login}", max_items=200):
        repo = _repo_of(it)
        merged_at = (it.get("pull_request") or {}).get("merged_at")
        status = "merged" if merged_at else ("closed" if it["state"] == "closed" else "open")
        pr = {
            "repo": repo, "number": it["number"], "title": it["title"], "url": it["html_url"],
            "status": status, "created_at": it["created_at"], "updated_at": it["updated_at"],
            "merged_at": merged_at, "author_association": it.get("author_association"),
            "body": (it.get("body") or "")[:2000],
        }
        if status == "open":
            pr["waiting_on_you"] = waiting_on_you(gh, repo, it["number"], login)
        prs.append(pr)

    reviews = [{"repo": _repo_of(it), "number": it["number"], "title": it["title"],
                "url": it["html_url"], "updated_at": it["updated_at"]}
               for it in gh.search_issues(f"type:pr reviewed-by:{login} -author:{login} -user:{login}",
                                          max_items=100)]
    issues = [{"repo": _repo_of(it), "number": it["number"], "title": it["title"],
               "url": it["html_url"], "state": it["state"], "created_at": it["created_at"]}
              for it in gh.search_issues(f"type:issue author:{login} -user:{login}", max_items=100)]
    commented = {f"{_repo_of(it)}#{it['number']}"
                 for it in gh.search_issues(f"type:issue commenter:{login} -user:{login}",
                                            sort="updated", max_items=100)}

    # PRs to repos where you're already a collaborator (team and hackathon projects)
    # aren't open source contributions; keep them out of the ladder and stats.
    team = [p for p in prs if p.get("author_association") in MAINTAINER]
    prs = [p for p in prs if p.get("author_association") not in MAINTAINER]

    per_repo: dict[str, dict] = {}
    for pr in prs:
        r = per_repo.setdefault(pr["repo"], {"merged": 0, "open": 0, "closed": 0, "maintainer": False})
        r[{"merged": "merged", "open": "open", "closed": "closed"}[pr["status"]]] += 1
        if pr.get("author_association") in MAINTAINER:
            r["maintainer"] = True
    for r in per_repo.values():
        r["ladder"] = ladder(r["merged"], r["maintainer"])

    reviews = [r for r in reviews if r["repo"] not in {p["repo"] for p in team}]
    return {"login": login, "prs": prs, "team_prs": team, "reviews": reviews, "issues": issues,
            "commented_issues": sorted(commented), "per_repo": per_repo}


def _mentions(pr: dict, repo: str, number: int) -> bool:
    text = f"{pr['title']}\n{pr.get('body', '')}"
    return bool(re.search(rf"(?<![\w/])#{number}\b", text)
                or f"github.com/{repo}/issues/{number}" in text)


def update_suggestions(gh: GitHub, state: dict, contributions: dict, skip_after_days: int,
                       now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    commented = set(contributions.get("commented_issues", []))
    for key, s in state.get("suggestions", {}).items():
        if s.get("status") not in ACTIVE:
            continue
        repo, number = s["repo"], s["number"]
        linked = [p for p in contributions["prs"] if p["repo"] == repo and _mentions(p, repo, number)]
        old = s.get("status")
        if linked:
            pr = max(linked, key=lambda p: p["created_at"])
            s["pr_url"] = pr["url"]
            s["status"] = {"merged": "merged", "closed": "closed"}.get(
                pr["status"], "waiting_on_you" if pr.get("waiting_on_you") else "pr_open")
        elif key in commented:
            s["status"] = "claimed"
        else:
            issue = gh.get(f"repos/{repo}/issues/{number}", ttl=3 * 3600)
            if issue.get("state") == "closed" or issue.get("assignees"):
                s["status"] = "taken"
            elif _days(parse_ts(s["suggested_at"]), now) > skip_after_days:
                s["status"] = "skipped"
        if s["status"] != old:
            s.setdefault("history", []).append({"at": now.isoformat(timespec="seconds"),
                                                "from": old, "to": s["status"]})
