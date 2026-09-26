"""Find open issues worth a look: from the tiered repos first, then anywhere."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from .config import Config
from .github import GitHub, RateLimited


def label_groups_for(label: str, cfg: Config) -> list[str]:
    return [g for g, pats in cfg.label_groups.items() if any(p.search(label) for p in pats)]


def interesting_labels(gh: GitHub, repo: str, cfg: Config) -> dict[str, list[str]]:
    """Map each of the repo's labels that matter to the label groups it belongs to."""
    out: dict[str, list[str]] = {}
    for lab in gh.paginate(f"repos/{repo}/labels", max_items=500, ttl=7 * 86400):
        name = lab["name"]
        groups = label_groups_for(name, cfg)
        if name in cfg.repo_labels.get(repo, []):
            groups = groups or ["confirmed_bug"]
        if groups:
            out[name] = groups
    return out


def _issue_record(issue: dict, repo: str, source: str, groups: set[str]) -> dict:
    return {
        "key": f"{repo}#{issue['number']}",
        "repo": repo,
        "number": issue["number"],
        "title": issue["title"],
        "url": issue["html_url"],
        "labels": [l["name"] for l in issue.get("labels", [])],
        "label_groups": sorted(groups),
        "created_at": issue["created_at"],
        "updated_at": issue["updated_at"],
        "comments": issue.get("comments", 0),
        "assignees": [a["login"] for a in issue.get("assignees") or []],
        "author": (issue.get("user") or {}).get("login"),
        "body": (issue.get("body") or "")[:4000],
        "source": source,
    }


def from_tiers(gh: GitHub, cfg: Config, per_label: int = 30) -> list[dict]:
    found: dict[str, dict] = {}
    for tier in cfg.tiers:
        for repo in tier.repos:
            labels = interesting_labels(gh, repo, cfg)
            for label, groups in labels.items():
                for issue in gh.paginate(f"repos/{repo}/issues",
                                         {"state": "open", "labels": label, "assignee": "none",
                                          "sort": "created", "direction": "desc"},
                                         max_items=per_label, ttl=3600):
                    if "pull_request" in issue:
                        continue
                    key = f"{repo}#{issue['number']}"
                    rec = found.get(key) or _issue_record(issue, repo, tier.name, set())
                    rec["label_groups"] = sorted(set(rec["label_groups"]) | set(groups))
                    found[key] = rec
    return list(found.values())


def open_discovery(gh: GitHub, cfg: Config, exclude: set[str]) -> list[dict]:
    d = cfg.discovery
    if not d.get("enabled"):
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=d.get("updated_within_days", 60))).date()
    found: dict[str, dict] = {}
    repos_seen: set[str] = set()
    labels = ",".join(f'"{l}"' for l in d.get("labels", []))  # comma = OR in GitHub search
    for lang in d.get("languages", []):
        q = (f'is:issue is:open no:assignee archived:false label:{labels} '
             f'language:"{lang}" updated:>={since}')
        try:
            results = gh.search_issues(q, max_items=d.get("max_issues_per_query", 30),
                                       sort="created", ttl=3600)
        except RateLimited as e:
            print(f"[scout] discovery for {lang} skipped: {e}", file=sys.stderr)
            continue
        for issue in results:
            repo = issue["repository_url"].split("/repos/", 1)[1]
            if repo in exclude:
                continue
            if repo not in repos_seen and len(repos_seen) >= d.get("max_repos", 20):
                continue
            repos_seen.add(repo)
            key = f"{repo}#{issue['number']}"
            groups = set()
            for l in issue.get("labels", []):
                groups |= set(label_groups_for(l["name"], cfg))
            found.setdefault(key, _issue_record(issue, repo, "discovery", groups))
    return list(found.values())
