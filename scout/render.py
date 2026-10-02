"""Build the single-file private dashboard: template + all data embedded as JSON.

The page opens to an inbox sorted by urgency. Every row has a slug (`owner__repo__n`,
the same as `briefings.slug`) so an email link can open it. The payload is:

  inbox        groups in order: waiting, ready, pairing, briefings, snoozed. Each item carries
               everything its detail view needs (briefing, patch, pr.json, post text, follow-up)
  to_do        how many items need you today (everything in the inbox except snoozed)
  note         last night's note about why there is (or isn't) a ready item
  in_flight    open PRs, plus claimed, submitting and approved items
  portfolio    exactly `public.build_payload`, the same data the public page shows
  limits       what the portfolio buttons enforce (featured PRs, summary length)
  projects     {home, repos}: the repo research
  log          {actions, runs}: your taps, newest first, and recent nightly scans
  digest, login, generated_at

Nothing secret goes in: only what state.json, the briefings and digest.json already hold.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import briefings, digest as digestmod, public, wip
from .config import ROOT, Config

UNSENT = ("ready", "approved", "submitting")
READY = ("ready", "approved")  # what the inbox can send; "submitting" is already on its way
PAIRABLE = ("suggested", "claimed", "ready", "approved")
SNOOZABLE = ("suggested", "ready", "approved")
GROUPS = (("waiting", "Waiting on you"), ("ready", "Ready"), ("pairing", "Pairing queue"),
          ("briefings", "New briefings"), ("snoozed", "Snoozed"))
LOG_ACTIONS, LOG_RUNS = 30, 10


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


def followup_of(data: Path, s: dict | None) -> dict | None:
    """The drafted reply to a review, for the dashboard: {dir, kind, reply, talking_points, patch, ...}."""
    rel = (s or {}).get("followup")
    if not isinstance(rel, str) or not rel.startswith("briefings/") or ".." in rel:
        return None
    fdir = data / rel
    fu = briefings.read_json(fdir / "followup.json")
    if not fu:
        return None
    patch = fu.get("patch")
    text = (fdir / patch).read_text() if isinstance(patch, str) and "/" not in patch and (fdir / patch).exists() else ""
    return {"dir": rel, "kind": fu.get("kind"), "reply": fu.get("reply", ""),
            "talking_points": fu.get("talking_points", []), "comments_addressed": fu.get("comments_addressed", []),
            "patch": text, "problems": briefings.validate_followup(fu, fdir),
            "done": s.get("followup_done") == rel}


def waiting_items(data: Path, st: dict, now: datetime) -> list[dict]:
    """Open PRs waiting on you, longest-waiting first (overdue ones are the longest)."""
    prs = {p["url"]: p for p in st.get("contributions", {}).get("prs", [])}
    out = []
    for w in digestmod.waiting_on_you(st, now):
        pr, s = prs.get(w["pr_url"], {}), st["suggestions"].get(w["key"])
        out.append({**w, "title": pr.get("title") or (s or {}).get("title", ""), "repo": pr.get("repo"),
                    "pr_number": pr.get("number"), "comments": pr.get("review_comments", []),
                    "followup": followup_of(data, s)})
    out.sort(key=lambda w: (not w["overdue"], w.get("since") or ""))
    return out


def open_prs_of(st: dict) -> list[dict]:
    """Your open PRs as in-flight rows, keyed like the digest: the suggestion's key, else owner/repo#n."""
    sugg = st.get("suggestions", {})
    by_pr = {s["pr_url"]: k for k, s in sugg.items() if s.get("pr_url")}
    out = []
    for p in st.get("contributions", {}).get("prs", []):
        if p.get("status") != "open":
            continue
        key = by_pr.get(p["url"], f"{p['repo']}#{p['number']}")
        approved = any(c.get("state") == "APPROVED" for c in p.get("review_comments", []))
        out.append({"key": key, "slug": briefings.slug(key), "kind": "open_pr", "repo": p["repo"],
                    "number": p["number"], "title": p["title"], "url": p["url"],
                    "status": "waiting_on_you" if p.get("waiting_on_you") else "approved_by_reviewer" if approved else "in_review",
                    "created_at": p.get("created_at"), "updated_at": p.get("updated_at"),
                    "comments": p.get("review_comments", [])})
    return out


def build_payload(cfg: Config, data: Path, st: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    all_b = {b["key"]: b for b in briefings.load_all(data)}
    picks = latest_picks(data)
    sugg = st.get("suggestions", {})

    def item(key: str, s: dict, group: str) -> dict:
        b = all_b.get(key, {})
        it = {**s, "key": key, "slug": briefings.slug(key), "group": group,
              "briefing": {k: v for k, v in b.items() if not k.startswith("_")}}
        if s.get("status") in UNSENT:  # only what the one-tap flow edits and sends
            it.update(patch=b.get("_patch", ""), pr=b.get("_pr", {}), post=b.get("_post", ""),
                      problems=b.get("_problems", []))
        return it

    snoozed = {k for k, s in sugg.items() if s.get("status") in SNOOZABLE and wip.snoozed(s, now)}
    live = {k: s for k, s in sugg.items() if k not in snoozed}
    ready_keys = [k for k, s in live.items() if s.get("status") in READY]
    pairing_keys = [k for k, s in live.items() if s.get("pairing") and s.get("status") in PAIRABLE
                    and k not in ready_keys]  # a prepared item stays in Ready, with an Unpair button
    briefing_keys = [k for k, s in live.items() if s.get("status") == "suggested" and not s.get("pairing")]

    def ordered(keys, field, newest_first=False):
        return sorted(keys, key=lambda k: sugg[k].get(field) or "", reverse=newest_first)

    waiting = waiting_items(data, st, now)
    groups = {
        "waiting": [{**w, "group": "waiting"} for w in waiting],
        "ready": [item(k, sugg[k], "ready") for k in ordered(ready_keys, "suggested_at")],
        "pairing": [item(k, sugg[k], "pairing") for k in ordered(pairing_keys, "pairing_at", True)],
        "briefings": [item(k, sugg[k], "briefings") for k in ordered(briefing_keys, "suggested_at", True)],
        "snoozed": [item(k, sugg[k], "snoozed") for k in ordered(snoozed, "snoozed_until")],
    }
    inbox = [{"id": gid, "label": label, "items": groups[gid]} for gid, label in GROUPS]
    to_do = sum(len(groups[g]) for g in ("waiting", "ready", "pairing", "briefings"))
    inbox_slugs = {it["slug"] for g in groups.values() for it in g}

    # in flight: open PRs, then what is claimed, being sent, or stuck after a failed send
    flight = open_prs_of(st)
    seen = {r["url"] for r in flight}
    for k, s in sugg.items():
        status = s.get("status")
        if status in ("submitting", "approved", "claimed") or (
                status in ("pr_open", "waiting_on_you") and s.get("pr_url") and s["pr_url"] not in seen):
            row = {"key": k, "slug": briefings.slug(k), "kind": "suggestion", "repo": s.get("repo"),
                   "number": s.get("number"), "title": s.get("title", ""), "status": status,
                   "url": s.get("pr_url") or s.get("url"), "created_at": s.get("submitted_at") or s.get("suggested_at"),
                   "last_error": s.get("last_error")}
            flight.append(row)
    for r in flight:
        r["in_inbox"] = r["slug"] in inbox_slugs  # then the inbox row owns the id and the deep link
    order = {"waiting_on_you": 0, "submitting": 1, "approved": 2}
    flight.sort(key=lambda r: (order.get(r["status"], 3), r.get("created_at") or ""))

    repos = []
    for name, r in st.get("repos", {}).items():
        if "friendliness" not in r:
            continue
        tier = cfg.tier_of(name)
        repos.append({**r, "tier": tier.name if tier else "Discovered"})
    repos.sort(key=lambda r: r["friendliness"], reverse=True)

    digest_file = data / "digest.json"
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "login": cfg.login,
        "digest": briefings.read_json(digest_file) if digest_file.exists() else {},
        "inbox": inbox,
        "to_do": to_do,
        "note": picks.get("note", ""),
        "in_flight": flight,
        "portfolio": public.build_payload(cfg, st, now=now),
        "limits": {"featured": public.MAX_FEATURED, "summary": 200},
        "projects": {"home": home_recommendation(cfg, st), "repos": repos},
        "log": {"actions": st.get("actions", [])[-LOG_ACTIONS:][::-1],
                "runs": st.get("runs", [])[-LOG_RUNS:][::-1]},
    }
    return payload


def render(cfg: Config, data: Path, st: dict) -> Path:
    payload = build_payload(cfg, data, st)
    text = json.dumps(payload, default=str)
    text = text.replace("</", "<\\/")  # keep </script> inside strings from closing the tag
    html = (ROOT / "dashboard" / "template.html").read_text()
    out = data / "dashboard.html"
    out.write_text(html.replace("/*__SCOUT_DATA__*/null", text))
    return out
