"""The public portfolio page.

Shows finished, public facts only: contributions to other people's projects, the
per-project research, and briefings for work that is already done. Anything in
progress (today's picks, open suggestions, draft comments, candidates) stays on
the private dashboard, and so does all AI prep for projects that don't accept
AI-assisted work.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import briefings
from .config import ROOT, Config

FINISHED = {"merged", "closed"}
BRIEFING_FIELDS = ("summary", "why", "difficulty", "walkthrough", "change_explained",
                   "alternatives", "maintainer_qa", "tests")
PR_FIELDS = ("repo", "number", "title", "url", "status", "created_at", "updated_at", "merged_at")
REPO_FIELDS = ("repo", "tier", "language", "stars", "friendliness", "confident", "merge_rate",
               "merges_elsewhere", "median_days_to_merge", "median_hours_to_first_response",
               "outside_prs_sampled", "ai_policy", "cla")


def publishable(suggestion: dict, briefing: dict, repo: dict) -> bool:
    return (suggestion.get("status") in FINISHED
            and bool(suggestion.get("pr_url"))
            and briefing.get("mode", "draft") == "draft"
            and not briefing.get("_problems")
            and repo.get("ai_policy") != "restrictive")


def build_payload(cfg: Config, data: Path, st: dict, domain: str | None = None) -> dict:
    contributions = st.get("contributions", {})
    repos = st.get("repos", {})
    all_b = {b["key"]: b for b in briefings.load_all(data)}

    finished = []
    for key, s in st.get("suggestions", {}).items():
        b = all_b.get(key)
        if b and publishable(s, b, repos.get(s["repo"], {})):
            finished.append({"key": key, "repo": s["repo"], "title": s["title"], "url": s["url"],
                             "pr_url": s["pr_url"], "status": s["status"],
                             "briefing": {k: b[k] for k in BRIEFING_FIELDS if k in b}})

    research = []
    for name, r in repos.items():
        if "friendliness" not in r:
            continue
        tier = cfg.tier_of(name)
        research.append({**{k: r.get(k) for k in REPO_FIELDS}, "repo": name,
                         "tier": tier.name if tier else "Discovered"})
    research.sort(key=lambda r: r["friendliness"], reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "login": cfg.login,
        "domain": domain,
        # external contributions only: team_prs and private repos never reach this page
        "prs": [{k: p.get(k) for k in PR_FIELDS} for p in contributions.get("prs", [])],
        "reviews": [{k: r.get(k) for k in ("repo", "number", "title", "url", "updated_at")}
                    for r in contributions.get("reviews", [])],
        "issues": [{k: i.get(k) for k in ("repo", "number", "title", "url", "state", "created_at")}
                   for i in contributions.get("issues", [])],
        "per_repo": contributions.get("per_repo", {}),
        "research": research,
        "finished": finished,
    }


def render(cfg: Config, data: Path, st: dict, out: Path, domain: str | None = None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(build_payload(cfg, data, st, domain), default=str).replace("</", "<\\/")
    html = (ROOT / "dashboard" / "public.html").read_text()
    page = out / "index.html"
    page.write_text(html.replace("/*__SCOUT_PUBLIC__*/null", payload))
    if domain:
        (out / "CNAME").write_text(domain + "\n")
    return page
