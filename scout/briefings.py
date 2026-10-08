"""Briefings are what the Claude step writes for each pick.

Layout, inside the data dir:

  briefings/<owner>__<repo>__<number>/briefing.json   (schema: REQUIRED below)
  briefings/<owner>__<repo>__<number>/draft.patch     (draft mode only)
  picks/<YYYY-MM-DD>.json                             what was considered and why

A briefing marked `"ready": true` is fully prepared for one-tap submit:
  kind "pr"                      draft.patch + pr.json (title, body, base, branch, ...)
  kind "repro" | "triage" | "review"   post.md (the exact comment) + post_target (URL to comment on)
Review follow-ups live in briefings/<slug>/followups/<YYYY-MM-DD>/followup.json
(+ the patch it names).

`ingest` turns new briefings into tracked suggestions, and `record_passes`
remembers the issues each night turned down so the scanner stops offering them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = {
    "key": str, "repo": str, "number": int, "title": str, "url": str,
    "summary": str,            # the issue in plain English
    "why": str,                # why this one, for you, now
    "difficulty": str,         # easy | medium | hard
    "time_estimate": str,      # e.g. "45-90 min"
    "walkthrough": str,        # markdown: how the relevant code works
    "change_explained": str,   # markdown: what the draft changes and why, piece by piece
    "alternatives": list,      # approaches considered and rejected, with reasons
    "maintainer_qa": list,     # [{q, a}] questions a reviewer is likely to ask
    "tests": dict,             # {ran, command, result, not_verified}
    "claim_comment": str,      # draft comment for YOU to post on the issue
    "submit_steps": list,      # exact steps from fork to PR
    "ai_disclosure": str,      # what this repo's policy asks for
}


def slug(key: str) -> str:
    repo, number = key.split("#")
    return repo.replace("/", "__") + f"__{number}"


MODES = {"draft", "guide"}
KINDS = {"pr", "repro", "triage", "review"}
FOLLOWUP_KINDS = {"small", "discuss"}
PR_FIELDS = ("title", "body", "branch", "base")
AI_MARKERS = ("co-authored-by", "generated with", "🤖")


def has_ai_marker(*texts) -> bool:
    return any(m in str(t).lower() for t in texts for m in AI_MARKERS)


def validate(b: dict, folder: Path | None = None) -> list[str]:
    """Problems with a briefing. With `folder`, also checks the files a ready item needs."""
    problems = [f"missing {k}" for k in REQUIRED if k not in b]
    if b.get("mode", "draft") not in MODES:
        problems.append(f"mode should be one of {sorted(MODES)}")
    problems += [f"{k} should be {t.__name__}" for k, t in REQUIRED.items()
                 if k in b and not isinstance(b[k], t)]
    kind = b.get("kind", "pr")
    if kind not in KINDS:
        problems.append(f"kind should be one of {sorted(KINDS)}")
    for k in ("ready", "ai_posts_forbidden"):
        if k in b and not isinstance(b[k], bool):
            problems.append(f"{k} should be bool")
    models = b.get("models_used", [])
    if not isinstance(models, list) or not all(isinstance(m, dict) and m.get("model") for m in models):
        problems.append("models_used should be a list of {model, did}")
    if b.get("ready") is True:
        problems += _ready_problems(b, kind, folder)
    return problems


def _ready_problems(b: dict, kind: str, folder: Path | None) -> list[str]:
    problems = []
    if b.get("mode", "draft") != "draft":
        problems.append("a ready item needs mode draft")
    if b.get("ai_posts_forbidden"):
        problems.append("a ready item can't be AI-written when the project forbids AI-written posts")
    if kind == "pr":
        pr = read_json(folder / "pr.json") if folder else {}
        if folder and not pr:
            problems.append("ready pr needs pr.json")
        problems += [f"pr.json needs {k}" for k in PR_FIELDS
                     if folder and pr and not (isinstance(pr.get(k), str) and pr[k].strip())]
        if pr:
            if not isinstance(pr.get("fixes"), int):
                problems.append("pr.json fixes should be the issue number")
            if not isinstance(pr.get("signoff"), bool):
                problems.append("pr.json signoff should be bool")
            disclosure = pr.get("disclosure")
            if disclosure is not None and (not isinstance(disclosure, str) or disclosure not in str(pr.get("body"))):
                problems.append("pr.json disclosure should be null or a sentence that appears in body")
            if has_ai_marker(pr.get("title"), pr.get("body"), pr.get("commit_message")):
                problems.append("pr.json contains an AI marker")
        if folder and not (folder / "draft.patch").exists():
            problems.append("ready pr needs draft.patch")
    elif kind in KINDS:
        target = b.get("post_target")
        if not (isinstance(target, str) and target.startswith("https://github.com/")):
            problems.append("ready item needs post_target (a github.com URL)")
        if folder:
            post = folder / "post.md"
            if not post.exists() or not post.read_text().strip():
                problems.append("ready item needs post.md")
            elif has_ai_marker(post.read_text()):
                problems.append("post.md contains an AI marker")
    return problems


def validate_followup(fu: dict, folder: Path | None = None, briefing: dict | None = None) -> list[str]:
    problems = []
    if not (isinstance(fu.get("pr_url"), str) and fu["pr_url"]):
        problems.append("missing pr_url")
    if not isinstance(fu.get("comments_addressed"), list):
        problems.append("comments_addressed should be list")
    kind = fu.get("kind")
    patch = fu.get("patch")
    if kind not in FOLLOWUP_KINDS:
        problems.append(f"kind should be one of {sorted(FOLLOWUP_KINDS)}")
    elif kind == "small":
        if not (isinstance(fu.get("reply"), str) and fu["reply"].strip()):
            problems.append("small follow-up needs reply")
        elif has_ai_marker(fu["reply"]):
            problems.append("reply contains an AI marker")
        if briefing and briefing.get("ai_posts_forbidden"):
            problems.append("the project forbids AI-written posts: use discuss with talking_points")
    else:
        if not (isinstance(fu.get("talking_points"), list) and fu["talking_points"]):
            problems.append("discuss follow-up needs talking_points")
        if patch:
            problems.append("discuss follow-up carries no patch")
    if patch is not None and not isinstance(patch, str):
        problems.append("patch should be a file name or null")
    elif patch and briefing and briefing.get("mode", "draft") != "draft":
        problems.append("guide mode: no patch")
    elif patch and folder and not (folder / patch).exists():
        problems.append(f"{patch} is missing")
    return problems


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_followup(folder: Path, briefing: dict) -> dict | None:
    """The newest follow-up dated folder, as {dir, kind, problems}, or None."""
    dirs = sorted(d for d in (folder / "followups").glob("*") if d.is_dir())
    if not dirs:
        return None
    fu = read_json(dirs[-1] / "followup.json")
    problems = validate_followup(fu, dirs[-1], briefing) if fu else ["missing followup.json"]
    return {"dir": str(dirs[-1].relative_to(folder.parent.parent)), "kind": fu.get("kind"),
            "problems": problems}


def load_all(data: Path) -> list[dict]:
    out = []
    for f in sorted((data / "briefings").glob("*/briefing.json")):
        b = json.loads(f.read_text())
        patch = f.parent / "draft.patch"
        b["_dir"] = str(f.parent.relative_to(data))
        b.setdefault("mode", "draft")
        # guide mode means the project doesn't accept AI-written code: never show a patch
        b["_patch"] = patch.read_text() if patch.exists() and b["mode"] == "draft" else ""
        b["_problems"] = validate(b, f.parent)
        b["_pr"] = read_json(f.parent / "pr.json") if b["mode"] == "draft" else {}
        post = f.parent / "post.md"
        b["_post"] = post.read_text() if post.exists() else ""
        b["_followup"] = latest_followup(f.parent, b)
        out.append(b)
    return out


DEMOTED_REFRESH = "The updated draft didn't work out, so this is a briefing for your laptop now."


def ingest(data: Path, state: dict) -> int:
    added = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for b in load_all(data):
        # a briefing that claims "ready" but isn't fully prepared still counts, as a plain suggestion
        if b["_problems"] and validate({**b, "ready": False}, data / b["_dir"]):
            continue
        status = "ready" if b.get("ready") is True and not b["_problems"] else "suggested"
        if b["key"] in state["suggestions"]:
            s = state["suggestions"][b["key"]]
            fu = b["_followup"]
            if fu and not fu["problems"]:
                s["followup"], s["followup_kind"] = fu["dir"], fu["kind"]
            if b.get("ready") is False and s["status"] in ("ready", "approved"):
                # the routine gave up on a refresh and made this a plain briefing
                s.setdefault("history", []).append({"at": now, "from": s["status"], "to": "suggested"})
                s.update(status="suggested", demoted_reason=DEMOTED_REFRESH)
                s.pop("send_after_refresh", None)
            # a demoted item stays a briefing until the owner asks for it to be prepared again
            elif status == "ready" and s["status"] == "suggested" and (
                    s.get("prepare_requested_at") or not s.get("demoted_reason")):
                s.update(status="ready", kind=b.get("kind", "pr"))
                s.setdefault("history", []).append({"at": now, "from": "suggested", "to": "ready"})
                for k in ("prepare_requested_at", "demoted_reason", "failure", "last_error", "send_after_refresh",
                          "refresh_requested_at", "refresh_result", "refresh_attempts"):
                    s.pop(k, None)
                if b.get("post_target"):
                    s["post_target"] = b["post_target"]
            continue
        s = state["suggestions"][b["key"]] = {
            "repo": b["repo"], "number": b["number"], "title": b["title"], "url": b["url"],
            "status": status, "suggested_at": b.get("picked_at", now),
            "briefing": b["_dir"], "difficulty": b["difficulty"], "mode": b["mode"],
            "kind": b.get("kind", "pr"),
            "history": [{"at": now, "from": None, "to": status}],
        }
        if b.get("post_target"):
            s["post_target"] = b["post_target"]
        added += 1
    return added


def record_passes(data: Path, state: dict) -> int:
    """Remember every issue a night considered and didn't pick, with its reason.

    Reads all picks files, oldest first, so a later pass of the same issue
    restarts its cooldown. Safe to run repeatedly.
    """
    passed = state.setdefault("passed", {})
    added = 0
    for f in sorted((data / "picks").glob("*.json")):
        picks = json.loads(f.read_text())
        at = f'{picks.get("date") or f.stem}T00:00:00+00:00'
        for c in picks.get("considered", []):
            key = c.get("key")
            # "deferred" means worth another look soon, so only "skipped" is remembered
            if not key or c.get("decision") != "skipped" or key in state["suggestions"]:
                continue
            if key not in passed:
                added += 1
            elif passed[key]["at"] >= at:
                continue
            passed[key] = {"at": at, "reason": c.get("reason", "")}
    return added
