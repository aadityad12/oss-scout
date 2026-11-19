"""What needs the owner's attention today? Written to digest.json; a later step emails it."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import briefings, fields, plain, refresh
from .config import Config
from .score import parse_ts
from .wip import snoozed

OVERDUE_HOURS = 48
WEEKLY_DAYS = 7  # the Saturday email looks back this far for briefings worth doing on the laptop
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
        key = by_pr.get(p["url"], f"{p['repo']}#{p['number']}")
        out.append({"key": key, "slug": briefings.slug(key), "pr_url": p["url"],
                    "since": since, "overdue": hours > OVERDUE_HOURS})
    return out


STUCK_STATUSES = ("ready", "approved", "waiting_on_you", "pr_open")


def lines_of(s: dict, b: dict | None, now: datetime) -> dict:
    """The three plain lines (problem, sending, your_part) and the `headline` for a suggestion and its briefing."""
    return {"headline": plain.headline(s, b or {}),
            **plain.lines({**s, "refresh_pending": refresh.pending(s, now)}, b or {})}


MAX_PLAN_STEPS = 5


def stars_text(n) -> str:
    """43210 as '43k', 4300 as '4.3k', 1500000 as '1.5M'; '' when it isn't a positive number."""
    if isinstance(n, bool) or not isinstance(n, (int, float)) or n <= 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".removesuffix(".0") + "M"
    if n >= 10_000:
        return f"{round(n / 1000)}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}".removesuffix(".0") + "k"
    return str(round(n))


def intro_fallback(repo: str, info: dict | None) -> str:
    """What the project is, from state alone: its GitHub description and its star count ('' if unknown)."""
    info = info or {}
    desc = re.sub(r":[a-z0-9_+-]+:", "", str(info.get("description") or ""))
    desc = re.sub(r"\s+", " ", desc).strip()
    if desc and desc[-1] not in ".!?":
        desc += "."
    name = repo.split("/")[-1]
    named = r"[-_ .]?".join(re.escape(w) for w in re.split(r"[-_.]", name) if w)
    if desc and named and not re.search(rf"(?<![A-Za-z0-9]){named}(?![A-Za-z0-9])", desc, re.I):
        desc = f"{name}: {desc}"  # a description that doesn't say whose it is gets the project's name in front
    stars = stars_text(info.get("stars"))
    return " ".join(x for x in (desc, f"About {stars} stars on GitHub." if stars else "") if x)


def extras_of(cfg: Config, st: dict, key: str, b: dict | None) -> dict:
    """What every item shows beyond the three lines: its field tag, an intro to the project, and the plan.

    The briefing's own `field`, `project_intro` and `plan_steps` win; without them the field is the
    scanner's hint (per-repo override, tier, topics), the intro is built from state, the plan is empty.
    """
    b = b or {}
    repo = key.split("#")[0]
    info = st.get("repos", {}).get(repo, {})
    tier = cfg.tier_of(repo)
    fallback = fields.hint(repo, tier.field if tier else None, cfg.repo_fields, info.get("topics"), info.get("description"))
    intro = b.get("project_intro")
    steps = b.get("plan_steps")
    return {"field": fields.resolve(b.get("field"), fallback),
            "project_intro": intro.strip() if isinstance(intro, str) and intro.strip() else intro_fallback(repo, info),
            "plan_steps": [x.strip() for x in steps if isinstance(x, str) and x.strip()][:MAX_PLAN_STEPS]
            if isinstance(steps, list) else []}


def stuck(st: dict, now: datetime, bs: dict | None = None) -> list[dict]:
    """Items whose last send failed, in plain words. A snoozed item is left out."""
    bs = bs or {}
    out = []
    for key, s in sorted(st.get("suggestions", {}).items()):
        f = s.get("failure")
        if not isinstance(f, dict) or s.get("status") not in STUCK_STATUSES or snoozed(s, now):
            continue
        out.append({"key": key, "slug": briefings.slug(key), "title": s.get("title", ""), "plain": f.get("plain", ""),
                    "why": f.get("why", ""), "fix_action": f.get("fix_action", "none"),
                    "refresh_by": f.get("refresh_by"), "refreshing": refresh.pending(s, now),
                    **lines_of(s, bs.get(key), now)})
    return out


def build(cfg: Config, data: Path, st: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    sugg = st.get("suggestions", {})

    bs = {b["key"]: b for b in briefings.load_all(data)}

    def item(key: str) -> dict:
        return {"key": key, "title": sugg[key].get("title", ""), "kind": sugg[key].get("kind", "pr"),
                "slug": briefings.slug(key)}

    def with_lines(key: str) -> dict:
        return {**item(key), **lines_of(sugg[key], bs.get(key), now), **extras_of(cfg, st, key, bs.get(key))}

    picks = data / "picks" / f"{today}.json"
    picked = set(json.loads(picks.read_text()).get("picked", [])) if picks.exists() else set()
    fresh = sorted(k for k, s in sugg.items() if s.get("status") == "suggested"
                   and (k in picked or str(s.get("suggested_at", "")).startswith(today)))
    stuck_items = [{**i, **extras_of(cfg, st, i["key"], bs.get(i["key"]))} for i in stuck(st, now, bs)]
    stuck_keys = {i["key"] for i in stuck_items}  # shown under "couldn't send", not again as ready
    ready = sorted(k for k, s in sugg.items() if s.get("status") == "ready" and not snoozed(s, now)
                   and k not in stuck_keys)
    waiting = waiting_on_you(st, now)
    prs = {p["url"]: p for p in st.get("contributions", {}).get("prs", [])}
    for w in waiting:
        title = prs.get(w["pr_url"], {}).get("title") or sugg.get(w["key"], {}).get("title", "")
        w.update(title=title, **plain.reply_lines(title, sugg.get(w["key"], {}).get("followup_kind")))
    cutoff = now - timedelta(days=WEEKLY_DAYS)
    weekly = sorted((k for k, s in sugg.items() if s.get("status") == "suggested" and not snoozed(s, now)
                     and (parse_ts(s.get("suggested_at")) or now - timedelta(days=99)) >= cutoff),
                    key=lambda k: sugg[k].get("suggested_at") or "", reverse=True)
    age = token_age_days(cfg, now.date())
    warn = age is not None and age > TOKEN_WARN_DAYS
    return {
        "date": today,
        "stuck": stuck_items,
        "ready": [with_lines(k) for k in ready],
        "waiting_on_you": waiting,
        "new_briefings": [item(k) for k in fresh],  # kept as it was; new_today carries the same keys in full
        "new_today": [with_lines(k) for k in fresh],
        "pairing": [{"key": k, "title": sugg[k].get("title", ""), "slug": briefings.slug(k)}
                    for k in sorted(sugg) if sugg[k].get("pairing")
                    and sugg[k].get("status") in ("suggested", "claimed", "ready", "approved")],
        "weekly": [with_lines(k) for k in weekly],  # briefings for the Saturday laptop email
        "token_age_days": age,
        "token_warning": warn,
        # today's new briefings go in the daily email; an expiring token breaks submits
        "send": bool(stuck_items or ready or waiting or fresh or warn),
    }
