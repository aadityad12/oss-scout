"""Turn raw issues + repo measurements into a ranked candidate list."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .score import _days, _scale, parse_ts

LABEL_FIT = {"bounty": 1.0, "confirmed_bug": 0.9, "beginner": 0.8, "help": 0.7}
POLICY_MULT = {"restrictive": 0.6, "disclosure": 1.0, "mentioned": 1.0, "none": 1.0}


def language_fit(lang: str | None, cfg: Config) -> float:
    if lang in cfg.strong_languages:
        return 1.0
    if lang in cfg.familiar_languages:
        return 0.8
    return 0.55


def home_multiplier(repo: str, state: dict, cfg: Config) -> float:
    per_repo = state.get("contributions", {}).get("per_repo", {}).get(repo, {})
    mult = 1.0
    if per_repo.get("merged", 0):
        mult = 1.3
    elif per_repo.get("open", 0):
        mult = 1.15
    homes = state.get("home_projects") or []
    if homes and not cfg.wide_phase():
        mult *= 1.25 if repo in homes else 0.7
    return mult


def score_issue(issue: dict, repo: dict, cfg: Config, state: dict,
                now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tier = cfg.tier_of(issue["repo"])
    tier_weight = tier.weight if tier else cfg.discovery.get("weight", 0.6)
    age = _days(parse_ts(issue["created_at"]), now)
    parts = {
        "friendliness": repo.get("friendliness", 0.5),
        "freshness": round(_scale(age, good=3, bad=cfg.settings.get("issue_max_age_days", 365)), 3),
        "label_fit": max((LABEL_FIT.get(g, 0.5) for g in issue["label_groups"]), default=0.5),
        "language_fit": language_fit(repo.get("language"), cfg),
    }
    base = (0.40 * parts["friendliness"] + 0.25 * parts["freshness"]
            + 0.20 * parts["label_fit"] + 0.15 * parts["language_fit"])
    mults = {
        "tier": tier_weight,
        "ai_policy": POLICY_MULT.get(repo.get("ai_policy", "none"), 1.0),
        "busy_thread": 0.7 if issue.get("comments", 0) > 15 else 1.0,
        "home": home_multiplier(issue["repo"], state, cfg),
    }
    total = base
    for m in mults.values():
        total *= m
    return {"score": round(100 * total, 1), "parts": parts, "multipliers": mults,
            "tier": tier.name if tier else "Discovered"}


def eligible(issue: dict, repo: dict, cfg: Config, state: dict,
             now: datetime | None = None) -> str | None:
    """Return a reason to drop this issue, or None if it's fine."""
    now = now or datetime.now(timezone.utc)
    if issue["key"] in state.get("suggestions", {}):
        return "already suggested"
    if passed := state.get("passed", {}).get(issue["key"]):
        cooldown = cfg.settings.get("pass_cooldown_days", 60)
        if _days(parse_ts(passed["at"]), now) < cooldown:
            return "turned down recently"
    if repo.get("archived"):
        return "repo archived"
    if issue.get("assignees"):
        return "assigned"
    if _days(parse_ts(issue["created_at"]), now) > cfg.settings.get("issue_max_age_days", 365):
        return "too old"
    if not cfg.tier_of(issue["repo"]) and (repo.get("stars") or 0) < cfg.discovery.get("min_stars", 0):
        return "too few stars"
    from .filters import blocking_label
    if lab := blocking_label(issue.get("labels", [])):
        return f"label: {lab}"
    return None
