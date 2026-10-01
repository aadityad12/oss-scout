"""Do what the owner tapped on the dashboard.

  python -m scout act --key owner/repo#123 --action submit [--title T] [--body-file F] [--dry-run]

Runs in the data repo's act workflow with the owner's token (GH_TOKEN). Every
action re-checks the item against the files on disk first and refuses on any
failure. Nothing here runs unless the owner pressed the button.

  submit    open a pull request from the ready item's draft.patch and pr.json
  post      post the ready item's post.md as a comment
  followup  push the small review fix and post the reply
  approve   ready -> approved
  later     snooze a ready item for three days
  skip      turn the item down for good
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import briefings, ghwrite, state as statemod, wip
from .config import Config
from .github import GitHub, NotFound
from .score import parse_ts

ACTIONS = ("submit", "post", "followup", "approve", "later", "skip")
KEY = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")
TARGET = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/([0-9]+)$")
REF = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_TITLE, MAX_BODY, SNOOZE_DAYS, KEEP_ACTIONS = 256, 60000, 3, 200
STUCK_MINUTES = 30  # a "submitting" item older than this is a job that died; retrying is safe
PENDING = ("ready", "approved")
FOLLOWUP_MESSAGE = "Address review feedback"


class ActError(Exception):
    pass


def fail(msg: str):
    raise ActError(msg)


def log(msg: str) -> None:
    print(f"[scout] {mask(msg)}", file=sys.stderr, flush=True)


def mask(text: str) -> str:
    token = os.environ.get("GH_TOKEN")
    return text.replace(token, "***") if token else text


def git(args: list[str], cwd: Path | None = None) -> str:
    log("git " + " ".join(args))
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if proc.returncode:
        raise ActError(mask(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[-300:]}"))
    return proc.stdout


def read(path: str):
    return GitHub().get(path)


@dataclass
class Job:
    cfg: Config
    data: Path
    st: dict
    key: str
    title: str | None = None
    body: str | None = None
    dry_run: bool = False
    now: datetime | None = None

    def __post_init__(self):
        self.now = self.now or datetime.now(timezone.utc)
        self.repo, n = KEY.match(self.key).groups()
        self.number = int(n)
        self.slug = briefings.slug(self.key)
        self.folder = self.data / "briefings" / self.slug

    @property
    def stamp(self) -> str:
        return self.now.isoformat(timespec="seconds")

    def stuck(self, s: dict | None) -> bool:
        since = parse_ts((s or {}).get("submitting_at"))
        return bool((s or {}).get("status") == "submitting" and since
                    and self.now - since > timedelta(minutes=STUCK_MINUTES))

    def suggestion(self, statuses: tuple[str, ...]) -> dict:
        s = self.st["suggestions"].get(self.key)
        if not s:
            fail(f"no suggestion for {self.key}")
        if self.stuck(s) and "approved" in statuses:
            statuses += ("submitting",)
        if s.get("status") not in statuses:
            fail(f"status is {s.get('status')}, this needs {' or '.join(statuses)}")
        if (str(s.get("repo")).lower(), s.get("number")) != (self.repo.lower(), self.number):
            fail("the suggestion's repo does not match the key")
        return s

    def move(self, s: dict, status: str) -> None:
        if s["status"] != status:
            s.setdefault("history", []).append({"at": self.stamp, "from": s["status"], "to": status})
            s["status"] = status

    def briefing(self, s: dict) -> dict:
        if s.get("briefing") != f"briefings/{self.slug}":
            fail("the suggestion does not point at this item's briefing folder")
        b = briefings.read_json(self.folder / "briefing.json")
        if not b:
            fail("briefing.json is missing or unreadable")
        if b.get("key") != self.key or str(b.get("repo")).lower() != self.repo.lower():
            fail("the briefing's repo does not match the suggestion")
        return b


# -- checks -------------------------------------------------------------------

def sane_ref(name: str) -> bool:
    return bool(REF.match(name)) and not (name[0] in "-/." or name[-1] in "/." or ".." in name
                                          or "//" in name or name.endswith(".lock"))


def check_text(title: str | None, body: str | None, disclosure: str | None = None) -> None:
    if title is not None:
        if not title.strip() or "\n" in title or len(title) > MAX_TITLE:
            fail(f"the title must be one line, 1-{MAX_TITLE} characters")
    if body is not None:
        if not body.strip() or len(body) > MAX_BODY:
            fail(f"the text must be 1-{MAX_BODY} characters")
        if disclosure and disclosure not in body:
            fail("the text must keep the AI-disclosure sentence this project requires")
    if briefings.has_ai_marker(title or "", body or ""):
        fail("the text contains an AI marker")


def ready_item(job: Job, kinds: tuple[str, ...]) -> tuple[dict, dict]:
    s = job.suggestion(PENDING)
    kind = s.get("kind", "pr")
    if kind not in kinds:
        fail(f"a {kind} item can't be handled this way")
    b = job.briefing(s)
    if b.get("kind", "pr") != kind:
        fail("the briefing's kind does not match the suggestion")
    if b.get("ready") is not True:
        fail("the briefing is not marked ready")
    if b.get("mode", "draft") != "draft":
        fail("guide mode: this project does not accept AI-written work")
    if b.get("ai_posts_forbidden"):
        fail("this project forbids AI-written posts")
    if problems := briefings.validate(b, job.folder):
        fail("the briefing no longer validates: " + "; ".join(problems))
    return s, b


def check_wip(job: Job) -> None:
    others = {k: v for k, v in job.st["suggestions"].items() if k != job.key}
    w = wip.compute({**job.st, "suggestions": others}, job.cfg.settings, job.now)
    open_urls = {p["url"] for p in job.st.get("contributions", {}).get("prs", []) if p.get("status") == "open"}
    fresh = [v for v in others.values() if v.get("status") in ("pr_open", "waiting_on_you")
             and v.get("submitted_at") and v.get("pr_url") not in open_urls]  # opened since the last scan
    total = w["open_prs"] + len(fresh)
    in_repo = w["open_prs_by_repo"].get(job.repo, 0) + sum(v.get("repo") == job.repo for v in fresh)
    if total >= job.cfg.settings.get("max_open_prs", 3):
        fail(f"{total} PRs are already open (max {job.cfg.settings.get('max_open_prs', 3)})")
    if in_repo >= job.cfg.settings.get("max_open_prs_per_repo", 1):
        fail(f"{in_repo} of your PRs are already open in {job.repo}")


# -- git and GitHub -----------------------------------------------------------

def identity(job: Job) -> tuple[str, str]:
    login = job.cfg.login
    uid = read(f"users/{login}")["id"]
    return job.cfg.name or login, f"{uid}+{login}@users.noreply.github.com"


def commit(work: Path, who: tuple[str, str], message: str, signoff: bool) -> None:
    git(["-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}", "-c", "commit.gpgsign=false",
         "commit", *(["-s"] if signoff else []), "-m", message], work)


def apply_patch(work: Path, patch: Path) -> None:
    try:
        git(["apply", "--index", str(patch)], work)
    except ActError:
        git(["apply", "--3way", str(patch)], work)


def clone(full_name: str, into: Path, branch: str | None = None) -> None:
    token = os.environ.get("GH_TOKEN", "")
    git(["clone", "--filter=blob:none", *(["-b", branch] if branch else []),
         f"https://x-access-token:{token}@github.com/{full_name}.git", str(into)])


def ensure_fork(job: Job) -> str:
    login = job.cfg.login
    full = ghwrite.fork(job.repo).get("full_name") or f"{login}/{job.repo.split('/')[1]}"
    if full.split("/")[0].lower() != login.lower():
        fail(f"the fork would be created under {full.split('/')[0]}, not {login}")
    for _ in range(20):
        try:
            read(f"repos/{full}")
            return full
        except NotFound:
            time.sleep(3)
    fail("the fork was not ready after 60 seconds; try again")


def pr_head(job: Job, pr_url: str) -> tuple[str, str]:
    """(fork full name, branch) of one of the owner's open PRs."""
    m = TARGET.match(pr_url)
    pr = read(f"repos/{m.group(1)}/pulls/{m.group(3)}")
    head = pr.get("head") or {}
    full = (head.get("repo") or {}).get("full_name") or ""
    if pr.get("state") != "open":
        fail("the pull request is no longer open")
    if full.split("/")[0].lower() != job.cfg.login.lower():
        fail("the pull request's branch is not on your fork")
    if not sane_ref(str(head.get("ref"))):
        fail("the pull request's branch name looks wrong")
    return full, head["ref"]


def existing_pr(job: Job, branch: str) -> dict | None:
    """A PR from the owner's branch that is already on GitHub, so a retry never opens a second one."""
    head = urllib.parse.quote(f"{job.cfg.login}:{branch}", safe=":/")
    for pr in read(f"repos/{job.repo}/pulls?head={head}&state=all") or []:
        if (pr.get("head") or {}).get("ref") == branch:
            return pr
    return None


def owner_comment(job: Job, repo: str, number: int, text: str, since: datetime | None = None) -> dict | None:
    """The owner's latest comment with exactly this text (ignoring outer whitespace), if any."""
    found: list[dict] = []
    for page in range(1, 6):
        batch = read(f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}") or []
        found += batch
        if len(batch) < 100:
            break
    for c in reversed(found[-100:]):
        if ((c.get("user") or {}).get("login", "").lower() == job.cfg.login.lower()
                and (c.get("body") or "").strip() == text.strip()
                and not (since and (parse_ts(c.get("created_at")) or since) < since)):
            return c
    return None


# -- actions ------------------------------------------------------------------

def submit(job: Job) -> dict:
    s, b = ready_item(job, ("pr",))
    pr = briefings.read_json(job.folder / "pr.json")
    for field in ("branch", "base"):
        if not sane_ref(pr[field]):
            fail(f"pr.json {field} is not a safe name")
    check_wip(job)
    title = job.title or pr["title"]
    body = job.body if job.body is not None else pr["body"]
    check_text(title, body, pr.get("disclosure"))
    branch, base, login = pr["branch"], pr["base"], job.cfg.login
    message = pr.get("commit_message") or pr["title"]
    if found := existing_pr(job, branch):
        if job.dry_run:
            return {"existing_pr": found["html_url"], "calls": ["none: the PR already exists"]}
        job.move(s, "merged" if found.get("merged_at") else "pr_open" if found.get("state") == "open" else "closed")
        s.pop("last_error", None)
        s.pop("submitting_at", None)
        s["pr_url"] = found["html_url"]
        s.setdefault("submitted_at", job.stamp)
        return {"url": found["html_url"], "note": "already existed"}
    if job.dry_run:
        return {"commit_message": message, "signoff": pr["signoff"], "title": title, "body": body,
                "calls": [f"POST repos/{job.repo}/forks", f"git clone --filter=blob:none <your fork of {job.repo}>",
                          f"git fetch upstream {base}", f"git checkout -b {branch} upstream/{base}",
                          "git apply --index draft.patch", f"git commit{' -s' if pr['signoff'] else ''}",
                          f"git push origin {branch}",
                          f"POST repos/{job.repo}/pulls head={login}:{branch} base={base}"]}

    job.move(s, "submitting")
    s["submitting_at"] = job.stamp
    try:
        who = identity(job)
        fork = ensure_fork(job)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            clone(fork, work)
            git(["remote", "add", "upstream", f"https://github.com/{job.repo}.git"], work)
            git(["config", "remote.upstream.promisor", "true"], work)
            git(["config", "remote.upstream.partialclonefilter", "blob:none"], work)
            git(["fetch", "--filter=blob:none", "--no-tags", "upstream", base], work)
            existing = bool(git(["ls-remote", "--heads", "origin", branch], work).strip())
            git(["checkout", "--no-track", "-b", branch, f"upstream/{base}"], work)
            apply_patch(work, job.folder / "draft.patch")
            commit(work, who, message, pr["signoff"])
            git(["push", *(["--force-with-lease"] if existing else []), "origin", f"HEAD:refs/heads/{branch}"], work)
        made = ghwrite.create_pr(job.repo, title, body, f"{fork.split('/')[0]}:{branch}", base)
    except Exception as e:
        job.move(s, "approved")
        s.pop("submitting_at", None)
        s["last_error"] = mask(str(e))[:500]
        raise ActError(f"submit failed, it is back to approved: {mask(str(e))}") from e
    job.move(s, "pr_open")
    s.pop("last_error", None)
    s.pop("submitting_at", None)
    s["pr_url"], s["submitted_at"] = made["html_url"], job.stamp
    return {"url": made["html_url"]}


def post(job: Job) -> dict:
    s, b = ready_item(job, ("repro", "triage", "review"))
    m = TARGET.match(str(b.get("post_target")))
    if not m or m.group(1).lower() != job.repo.lower():
        fail("post_target must be an issue or pull request in this repo")
    if s.get("post_target") not in (None, b["post_target"]):
        fail("post_target differs between the suggestion and the briefing")
    text = job.body if job.body is not None else (job.folder / "post.md").read_text().strip()
    check_text(None, text)
    if found := owner_comment(job, m.group(1), int(m.group(3)), text):
        if job.dry_run:
            return {"existing_comment": found["html_url"], "calls": ["none: the comment already exists"]}
        job.move(s, "posted")
        s.pop("last_error", None)
        s.pop("submitting_at", None)
        s["comment_url"] = found["html_url"]
        return {"url": found["html_url"], "note": "already existed"}
    if job.dry_run:
        return {"body": text, "calls": [f"POST repos/{m.group(1)}/issues/{m.group(3)}/comments"]}
    job.move(s, "submitting")
    s["submitting_at"] = job.stamp
    try:
        made = ghwrite.comment(m.group(1), int(m.group(3)), text)
    except Exception as e:
        job.move(s, "approved")
        s.pop("submitting_at", None)
        s["last_error"] = mask(str(e))[:500]
        raise ActError(f"post failed, it is back to approved: {mask(str(e))}") from e
    job.move(s, "posted")
    s.pop("last_error", None)
    s.pop("submitting_at", None)
    s["comment_url"] = made["html_url"]
    return {"url": made["html_url"]}


def followup(job: Job) -> dict:
    s = job.suggestion(("waiting_on_you", "pr_open"))
    b = job.briefing(s)
    rel = s.get("followup")
    if not (isinstance(rel, str) and re.fullmatch(rf"briefings/{re.escape(job.slug)}/followups/\d{{4}}-\d{{2}}-\d{{2}}", rel)):
        fail("there is no follow-up for this item")
    fdir = job.data / rel
    fu = briefings.read_json(fdir / "followup.json")
    if problems := briefings.validate_followup(fu, fdir, b):
        fail("the follow-up no longer validates: " + "; ".join(problems))
    if fu["kind"] != "small":
        fail("this one needs a conversation: take it to a /contribute session")
    if s.get("followup_done") == rel:
        fail("this follow-up was already sent")
    m = TARGET.match(fu["pr_url"])
    if not m or m.group(2) != "pull" or m.group(1).lower() != job.repo.lower():
        fail("the follow-up's pr_url is not a pull request in this repo")
    if s.get("pr_url") not in (None, fu["pr_url"]):
        fail("the follow-up is for a different pull request")
    reply = job.body if job.body is not None else fu["reply"]
    message = job.title or FOLLOWUP_MESSAGE
    check_text(message, reply)
    patch = fu.get("patch") and fdir / fu["patch"]
    pushed = s.get("followup_pushed") == rel
    since = datetime.fromisoformat(rel.rsplit("/", 1)[1]).replace(tzinfo=timezone.utc)
    if found := owner_comment(job, m.group(1), int(m.group(3)), reply, since):
        if job.dry_run:
            return {"existing_comment": found["html_url"], "calls": ["none: the reply already exists"]}
        s.pop("last_error", None)  # the push comes before the reply, so both already happened
        s["followup_pushed"] = s["followup_done"] = rel
        s["followup_done_at"] = job.stamp
        job.move(s, "pr_open")
        return {"url": found["html_url"], "note": "already existed"}
    if job.dry_run:
        calls = [f"POST repos/{m.group(1)}/issues/{m.group(3)}/comments"]
        if patch and not pushed:
            calls = ["git clone --filter=blob:none <the PR's branch on your fork>", "git apply --index followup.patch",
                     "git commit", "git push origin <the PR's branch>"] + calls
        return {"commit_message": message if patch else None, "body": reply, "calls": calls}

    try:
        if patch and not pushed:
            who = identity(job)
            fork, branch = pr_head(job, fu["pr_url"])
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp) / "repo"
                clone(fork, work, branch)
                apply_patch(work, patch)
                commit(work, who, message, False)
                git(["push", "origin", f"HEAD:refs/heads/{branch}"], work)
            s["followup_pushed"] = rel
        made = ghwrite.comment(m.group(1), int(m.group(3)), reply)
    except Exception as e:
        s["last_error"] = mask(str(e))[:500]
        raise ActError(f"follow-up failed: {mask(str(e))}") from e
    s.pop("last_error", None)
    s["followup_done"], s["followup_done_at"] = rel, job.stamp
    job.move(s, "pr_open")
    return {"url": made["html_url"]}


def approve(job: Job) -> dict:
    job.move(job.suggestion(("ready",)), "approved")
    return {}


def later(job: Job) -> dict:
    s = job.suggestion(("suggested",) + PENDING)
    s["snoozed_until"] = (job.now + timedelta(days=SNOOZE_DAYS)).isoformat(timespec="seconds")
    return {}


def skip(job: Job) -> dict:
    s = job.suggestion(("suggested",) + PENDING)
    job.move(s, "skipped")
    job.st.setdefault("passed", {})[job.key] = {"at": job.stamp, "reason": "turned down on dashboard"}
    return {}


HANDLERS = {"submit": submit, "post": post, "followup": followup, "approve": approve, "later": later, "skip": skip}


def run(cfg: Config, data: Path, key: str, action: str, title: str | None = None,
        body: str | None = None, dry_run: bool = False) -> int:
    if not KEY.match(key) or action not in HANDLERS:
        print("error: need --key owner/repo#123 and a known --action", file=sys.stderr)
        return 2
    st = statemod.load(data)
    job = Job(cfg, data, st, key, title, body, dry_run)
    out: dict = {}
    try:
        out = HANDLERS[action](job)
        result = "ok"
    except Exception as e:
        result = mask(str(e))
        result = result if result.startswith(("submit failed", "post failed", "follow-up failed")) else f"refused: {result}"
    if not dry_run:
        acts = st.setdefault("actions", [])
        acts.append({"at": job.stamp, "key": key, "action": action, "result": result,
                     **({"url": out["url"]} if out.get("url") else {}),
                     **({"note": out["note"]} if out.get("note") else {})})
        del acts[:-KEEP_ACTIONS]
        statemod.save(data, st)
    if result != "ok":
        print(f"error: {result}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "key": key, "action": action, "dry_run": dry_run, **out}, indent=2))
    return 0
