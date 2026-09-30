"""What needs the owner's attention today? Written to digest.json; a later step emails it."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .score import parse_ts
from .wip import snoozed

OVERDUE_HOURS = 48
TOKEN_WARN_DAYS = 80


def token_age_days(cfg: Config, today: date) -> int | None:
    try:
        return max(0, (today - date.fromisoformat(cfg.submit["token_rotated"])).days)
    except (KeyError, ValueError, TypeError):
        return None


def waiting_on_you(st: dict, now: datetime) -> list[dict]:
    sugg = st.get("suggestions", {})
    by_pr = {s["pr_url"]: k for k, s in sugg.items() if s.get("pr_url")}
    out = []
    for p in st.get("contributions", {}).get("prs", []):
        if p.get("status") != "open" or not p.get("waiting_on_you"):
            continue
        since = min((c["at"] for c in p.get("review_comments", []) if c.get("at")),
                    default=p.get("updated_at"))
        done = parse_ts(sugg.get(by_pr.get(p["url"], ""), {}).get("followup_done_at"))
        if done and since and done > parse_ts(since):
            continue  # already answered from the dashboard; the next scan will confirm
        hours = (now - parse_ts(since)).total_seconds() / 3600 if since else 0
        out.append({"key": by_pr.get(p["url"], f"{p['repo']}#{p['number']}"), "pr_url": p["url"],
                    "since": since, "overdue": hours > OVERDUE_HOURS})
    return out


def build(cfg: Config, data: Path, st: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    sugg = st.get("suggestions", {})

    def item(key: str) -> dict:
        return {"key": key, "title": sugg[key].get("title", ""), "kind": sugg[key].get("kind", "pr")}

    picks = data / "picks" / f"{today}.json"
    picked = set(json.loads(picks.read_text()).get("picked", [])) if picks.exists() else set()
    fresh = sorted(k for k, s in sugg.items() if s.get("status") == "suggested"
                   and (k in picked or str(s.get("suggested_at", "")).startswith(today)))
    ready = sorted(k for k, s in sugg.items() if s.get("status") == "ready" and not snoozed(s, now))
    waiting = waiting_on_you(st, now)
    age = token_age_days(cfg, now.date())
    warn = age is not None and age > TOKEN_WARN_DAYS
    return {
        "date": today,
        "ready": [item(k) for k in ready],
        "waiting_on_you": waiting,
        "new_briefings": [item(k) for k in fresh],
        "token_age_days": age,
        "token_warning": warn,
        "send": bool(ready or waiting or fresh or warn),  # an expiring token breaks submits
    }
