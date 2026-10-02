"""The public page: the owner's own open source contributions, and nothing else.

Everything here comes from the GitHub-derived `contributions` and the stars and
language of the projects in `repos`, plus the owner's own choices in `state["public"]`
(featured PRs and plain-language summaries). Briefings, suggestions, candidates and any
per-project research never reach the output.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT, Config
from .score import parse_ts

DEFAULT_DOMAIN = "oss.aadityad.dev"
WEEKS = 26
MAX_FEATURED = 3
PR_FIELDS = ("repo", "number", "title", "summary", "url", "status", "created_at", "merged_at",
             "updated_at")


def _week_start(ts: datetime) -> datetime:
    day = ts.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def activity(prs: list[dict], issues: list[dict], now: datetime) -> dict:
    """Weekly counts of PRs opened or merged and issues filed."""
    counts: dict[datetime, int] = {}
    for stamp in ([p.get("created_at") for p in prs] + [p.get("merged_at") for p in prs]
                  + [i.get("created_at") for i in issues]):
        if ts := parse_ts(stamp):
            counts[_week_start(ts)] = counts.get(_week_start(ts), 0) + 1
    this_week = _week_start(now)
    weeks = [this_week - timedelta(weeks=n) for n in range(WEEKS - 1, -1, -1)]
    # the current week may still be quiet: a streak ending last week is still alive
    cursor = this_week if this_week in counts else this_week - timedelta(weeks=1)
    streak = 0
    while cursor in counts:
        streak += 1
        cursor -= timedelta(weeks=1)
    return {"weeks": [{"week": w.date().isoformat(), "n": counts.get(w, 0)} for w in weeks],
            "weeks_active": len(counts), "streak": streak}


def projects_of(prs: list[dict], per_repo: dict, repos: dict) -> list[dict]:
    """Projects with a merged or open PR, most merged first."""
    found: dict[str, dict] = {}
    for p in prs:
        r = found.setdefault(p["repo"], {"repo": p["repo"], "merged": 0, "open": 0})
        r[p["status"]] += 1
    out = []
    for name, r in found.items():
        info = repos.get(name, {})
        out.append({**r, "stars": info.get("stars"), "language": info.get("language"),
                    "ladder": per_repo.get(name, {}).get("ladder")})
    return sorted(out, key=lambda r: (-r["merged"], -r["open"], r["repo"]))


def _clean(text) -> str | None:
    return text.strip() or None if isinstance(text, str) else None


def apply_public_choices(prs: list[dict], choices) -> tuple[list[dict], list[dict]]:
    """Apply the owner's `state["public"]` to the PR list: (featured, the rest).

    A plain-language summary there overrides the PR's own `summary`. Featured PRs are
    merged ones only, in the owner's order, at most MAX_FEATURED; unknown, open and
    repeated urls are skipped. Featured PRs leave the main list so nothing shows twice.
    """
    choices = choices if isinstance(choices, dict) else {}
    summaries = choices.get("summaries")
    summaries = summaries if isinstance(summaries, dict) else {}
    for p in prs:
        p["summary"] = _clean(summaries.get(p["url"])) or _clean(p.get("summary"))
    wanted = choices.get("featured")
    by_url = {p["url"]: p for p in prs if p["status"] == "merged"}
    featured: list[dict] = []
    for url in wanted if isinstance(wanted, list) else []:
        pr = by_url.get(url) if isinstance(url, str) else None
        if pr and pr not in featured:
            featured.append(pr)
    featured = featured[:MAX_FEATURED]
    return featured, [p for p in prs if not any(p is f for f in featured)]


def build_payload(cfg: Config, st: dict, domain: str | None = None,
                  now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    c = st.get("contributions", {})
    # external contributions only: team_prs are never read
    # only merged and open PRs: the page shows what landed and what is in review
    prs = [{k: p.get(k) for k in PR_FIELDS} for p in c.get("prs", [])
           if p.get("status") in ("merged", "open")]
    featured, listed = apply_public_choices(prs, st.get("public"))
    issues = [{k: i.get(k) for k in ("repo", "number", "title", "url", "state", "created_at")}
              for i in c.get("issues", [])]
    reviews = [{k: r.get(k) for k in ("repo", "number", "title", "url")}
               for r in c.get("reviews", [])]
    projects = projects_of(prs, c.get("per_repo", {}), st.get("repos", {}))
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "login": cfg.login,
        "url": f"https://{domain or DEFAULT_DOMAIN}/",
        "merged_prs": sum(p["status"] == "merged" for p in prs),
        "open_prs": sum(p["status"] == "open" for p in prs),
        "projects": projects,
        "featured": featured,
        "prs": listed,
        "issues": issues,
        "reviews": reviews,
        "activity": activity(prs, issues, now),
    }


def build_stats(payload: dict) -> dict:
    """The small JSON that other sites (the portfolio) can fetch."""
    projects = [{k: p[k] for k in ("repo", "merged", "open")} for p in payload["projects"]]
    return {"generated_at": payload["generated_at"], "merged_prs": payload["merged_prs"],
            "open_prs": payload["open_prs"], "projects": projects,
            "top_projects": [p["repo"] for p in projects[:3]], "url": payload["url"]}


def description(payload: dict) -> str:
    merged = payload["merged_prs"]
    if not merged:
        return "Open source contributions by Aaditya Desai, tracked from GitHub."
    projects = sum(p["merged"] > 0 for p in payload["projects"])
    return (f"Aaditya Desai has {merged} merged pull request{'s' * (merged != 1)} across "
            f"{projects} open source project{'s' * (projects != 1)}.")


def render(cfg: Config, st: dict, out: Path, domain: str | None = None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    payload = build_payload(cfg, st, domain)
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    page_html = (ROOT / "dashboard" / "public.html").read_text()
    page_html = (page_html.replace("__PAGE_URL__", html.escape(payload["url"], quote=True))
                 .replace("__DESCRIPTION__", html.escape(description(payload), quote=True))
                 .replace("/*__SCOUT_PUBLIC__*/null", data))
    page = out / "index.html"
    page.write_text(page_html)
    (out / "stats.json").write_text(json.dumps(build_stats(payload), indent=2) + "\n")
    if domain:
        (out / "CNAME").write_text(domain + "\n")
    return page
