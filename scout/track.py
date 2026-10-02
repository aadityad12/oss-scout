"""What have you actually done? Read from GitHub, never self-reported.

Finds your PRs, reviews and issues on other people's repos, links them to past
suggestions, and moves each suggestion along:

  suggested -> claimed -> pr_open -> waiting_on_you -> merged | closed
  suggested -> skipped (untouched too long) | taken (someone else got it)
  ready -> approved -> submitting -> pr_open -> ...   (a fully prepared pick; the
                                                     submit step moves it to approved
                                                     and onwards)
  ready | approved | submitting -> posted   (comment-only items, once posted)
  ready -> skipped | taken                   (stale or gone; approved items are never skipped)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import briefings, state as statemod
from .github import GitHub
from .score import MAINTAINER, _days, parse_ts

MAX_EXTRA_DRAFTS = 3  # Claude runs started by a review alert, per UTC day
PENDING = {"ready", "approved", "submitting"}  # prepared, not yet a PR or a posted comment
ACTIVE = {"suggested", "claimed", "pr_open", "waiting_on_you"} | PENDING
MAX_REVIEW_COMMENTS = 10


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


def review_state(gh: GitHub, repo: str, number: int, login: str) -> tuple[bool, list[dict]]:
    """(waiting on you?, the latest comments from others you haven't answered yet)."""
    mine: list[datetime] = []
    theirs: list[tuple[datetime, dict]] = []
    sources = [
        (f"repos/{repo}/issues/{number}/comments", "created_at", "comment"),
        (f"repos/{repo}/pulls/{number}/reviews", "submitted_at", "review"),
        (f"repos/{repo}/pulls/{number}/comments", "created_at", "inline"),
    ]
    for path, field, kind in sources:
        for c in gh.paginate(path, max_items=100, ttl=1800):
            who = c.get("user") or {}
            ts = parse_ts(c.get(field))
            if not ts or who.get("type") == "Bot":
                continue
            if who.get("login") == login:
                mine.append(ts)
                continue
            entry = {"author": who.get("login"), "kind": kind, "at": c.get(field),
                     "body": (c.get("body") or "")[:2000], "url": c.get("html_url")}
            if kind == "inline":
                entry["path"] = c.get("path")
                entry["line"] = c.get("line") or c.get("original_line")
            if kind == "review":
                entry["state"] = c.get("state")
            theirs.append((ts, entry))
    for c in gh.paginate(f"repos/{repo}/pulls/{number}/commits", max_items=250, ttl=1800):
        ts = parse_ts(((c.get("commit") or {}).get("committer") or {}).get("date"))
        if ts:
            mine.append(ts)
    last_mine = max(mine, default=None)
    unanswered = sorted((t for t in theirs if last_mine is None or t[0] > last_mine), key=lambda t: t[0])
    # an empty approval or comment-only review shell asks nothing of you
    needs_reply = [e for _, e in unanswered if e["body"].strip() or e.get("state") == "CHANGES_REQUESTED"]
    return bool(needs_reply), [e for e in needs_reply if e["body"].strip()][-MAX_REVIEW_COMMENTS:]


def waiting_on_you(gh: GitHub, repo: str, number: int, login: str) -> bool:
    """True when the latest human word on the PR is someone else's."""
    return review_state(gh, repo, number, login)[0]


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
            pr["waiting_on_you"], comments = review_state(gh, repo, it["number"], login)
            if comments:
                pr["review_comments"] = comments
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
        old = s.get("status")
        if old not in ACTIVE:
            continue
        repo, number = s["repo"], s["number"]
        pr_kind = s.get("kind", "pr") == "pr"
        linked = [p for p in contributions["prs"] if p["repo"] == repo and _mentions(p, repo, number)]
        if linked and pr_kind:
            pr = max(linked, key=lambda p: p["created_at"])
            s["pr_url"] = pr["url"]
            s["status"] = {"merged": "merged", "closed": "closed"}.get(
                pr["status"], "waiting_on_you" if pr.get("waiting_on_you") else "pr_open")
        elif old in PENDING and not pr_kind and key in commented:
            s["status"] = "posted"
        elif old in ("approved", "submitting") or (old in ("pr_open", "waiting_on_you") and s.get("submitted_at")):
            pass  # in flight (or just opened, not indexed yet): only a PR or a posted comment moves it
        elif key in commented and old != "ready":
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


def alert(st: dict, data: Path, now: datetime | None = None) -> list[dict]:
    """Find reviews that are new since the last check; write them to alerts.json."""
    now = now or datetime.now(timezone.utc)
    by_pr = {s["pr_url"]: k for k, s in st.get("suggestions", {}).items() if s.get("pr_url")}
    before, alerted, new = st.get("alerted", {}), {}, []
    for p in st.get("contributions", {}).get("prs", []):
        if p.get("status") != "open" or not p.get("waiting_on_you"):
            continue
        last = max((c for c in p.get("review_comments", []) if parse_ts(c.get("at"))),
                   key=lambda c: parse_ts(c["at"]), default={})
        at = last.get("at") or p.get("updated_at") or p.get("created_at") or ""
        alerted[p["url"]] = at
        seen, current = parse_ts(before.get(p["url"])), parse_ts(at)
        if p["url"] in before and not (current and (not seen or current > seen)):
            continue
        key = by_pr.get(p["url"], f"{p['repo']}#{p['number']}")
        new.append({"at": at, "key": key, "slug": briefings.slug(key), "pr_url": p["url"],
                    "author": last.get("author") or "a reviewer",
                    "excerpt": " ".join((last.get("body") or "").split())[:140]})
    st["alerted"] = alerted
    statemod.write_json(data / "alerts.json", {"generated_at": now.isoformat(timespec="seconds"), "alerts": new})
    return new


def extra_draft(st: dict, new: list[dict], now: datetime | None = None) -> bool:
    """True when these alerts should start a Claude run now; counts it against today's cap."""
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    used = st.get("extra_drafts") or {}
    if used.get("date") != today:
        used = {"date": today, "n": 0}
    fire = bool(new) and used["n"] < MAX_EXTRA_DRAFTS
    used["n"] += fire
    st["extra_drafts"] = used
    return fire
