"""Build the single-file dashboard: template + all data embedded as JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import briefings
from .config import ROOT, Config

ACTIVE = ("ready", "approved", "submitting", "claimed", "pr_open", "waiting_on_you")


def latest_picks(data: Path) -> dict:
    files = sorted((data / "picks").glob("*.json"))
    return json.loads(files[-1].read_text()) if files else {}


def home_recommendation(cfg: Config, st: dict) -> dict:
    per_repo = st.get("contributions", {}).get("per_repo", {})
    repos = st.get("repos", {})
    acted: dict[str, int] = {}
    for s in st.get("suggestions", {}).values():
        if s["status"] not in ("suggested", "skipped", "taken"):
            acted[s["repo"]] = acted.get(s["repo"], 0) + 1
    scores = []
    for repo in set(per_repo) | set(acted):
        c = per_repo.get(repo, {})
        fr = repos.get(repo, {}).get("friendliness", 0.5)
        pts = 3 * c.get("merged", 0) + c.get("open", 0) + 0.5 * acted.get(repo, 0) + 2 * fr
        scores.append({"repo": repo, "points": round(pts, 2), "merged": c.get("merged", 0),
                       "open": c.get("open", 0), "friendliness": fr})
    scores.sort(key=lambda r: r["points"], reverse=True)
    return {"provisional": cfg.wide_phase(), "until": cfg.settings.get("wide_phase_until"),
            "top": scores[:3], "chosen": st.get("home_projects", [])}


def build_payload(cfg: Config, data: Path, st: dict) -> dict:
    all_b = {b["key"]: b for b in briefings.load_all(data)}
    picks = latest_picks(data)

    def with_briefing(key: str, s: dict) -> dict:
        b = all_b.get(key, {})
        return {**s, "key": key, "briefing": {k: v for k, v in b.items() if not k.startswith("_")},
                "patch": b.get("_patch", "")}

    sugg = st.get("suggestions", {})
    today_keys = picks.get("picked", [])
    repos = []
    for name, r in st.get("repos", {}).items():
        if "friendliness" not in r:
            continue
        tier = cfg.tier_of(name)
        repos.append({**r, "tier": tier.name if tier else "Discovered"})
    repos.sort(key=lambda r: r["friendliness"], reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "login": cfg.login,
        "today": {
            "date": picks.get("date"), "note": picks.get("note", ""),
            "considered": picks.get("considered", []),
            "picks": [with_briefing(k, sugg.get(k, {"status": "suggested"})) for k in today_keys],
        },
        "open_suggestions": [with_briefing(k, s) for k, s in sugg.items()
                             if s["status"] == "suggested" and k not in today_keys],
        "in_progress": [with_briefing(k, s) for k, s in sugg.items() if s["status"] in ACTIVE],
        "finished_suggestions": [with_briefing(k, s) for k, s in sugg.items()
                                 if s["status"] not in ACTIVE + ("suggested",)],
        "contributions": st.get("contributions", {}),
        "repos": repos,
        "home": home_recommendation(cfg, st),
        "last_run": (st.get("runs") or [{}])[-1],
    }


def render(cfg: Config, data: Path, st: dict) -> Path:
    payload = json.dumps(build_payload(cfg, data, st), default=str)
    payload = payload.replace("</", "<\\/")  # keep </script> inside strings from closing the tag
    html = (ROOT / "dashboard" / "template.html").read_text()
    out = data / "dashboard.html"
    out.write_text(html.replace("/*__SCOUT_DATA__*/null", payload))
    return out
