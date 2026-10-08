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
  prepare   ask the next nightly run to prepare this suggestion
  pair      mark an item to work on together in a /contribute session (unpair undoes it)
  feature   show a merged PR first on the public page (unfeature undoes it)
  summary   set the one-line summary of a PR on the public page (empty text removes it)
  refresh   mark an approved item to be rebuilt on the project's current code, then sent
            (the act workflow's check and send jobs do the rebuild; see refresh.py)

The last five don't touch GitHub. A failed submit, post or follow-up stores a
plain-language `failure` on the item (see failures.py).
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

from . import briefings, failures, ghwrite, refresh as refreshmod, state as statemod, track, wip
from .config import Config
from .github import GitHub, NotFound
from .score import parse_ts

ACTIONS = ("submit", "post", "followup", "approve", "later", "skip",
           "prepare", "pair", "unpair", "feature", "unfeature", "summary", "refresh")
KEY = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")
TARGET = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/([0-9]+)$")
MAX_TITLE, MAX_BODY, SNOOZE_DAYS, KEEP_ACTIONS = 256, 60000, 3, 200
MAX_FEATURED, MAX_SUMMARY = 3, 200
STUCK_MINUTES = 30  # a "submitting" item older than this is a job that died; retrying is safe
PENDING = ("ready", "approved")
FOLLOWUP_MESSAGE = "Address review feedback"


class ActError(Exception):
    pass


class Failure(ActError):
    """A refusal the owner should be told about in plain words (see failures.py)."""

    def __init__(self, code: str, plain: str, why: str = "", message: str | None = None):
        super().__init__(message or why or plain)
        self.code, self.plain, self.why = code, plain, why


class PatchError(ActError):
    """git could not apply a patch to the current code."""


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

    def pr(self, *statuses: str) -> dict:
        """One of the owner's PRs from the last scan, matched by repo and number."""
        for p in self.st.get("contributions", {}).get("prs", []):
            if str(p.get("repo")).lower() == self.repo.lower() and p.get("number") == self.number:
                if statuses and p.get("status") not in statuses:
                    fail(f"the pull request is {p.get('status')}, this needs {' or '.join(statuses)}")
                return p
        fail(f"no pull request of yours for {self.key}")

    def public(self) -> dict:
        pub = self.st.setdefault("public", {})
        pub.setdefault("featured", [])
        pub.setdefault("summaries", {})
        return pub

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

    # -- failure records --

    def draft_patch(self) -> str:
        try:
            return (self.folder / "draft.patch").read_text()
        except OSError:
            return ""

    def base_branch(self) -> str | None:
        base = briefings.read_json(self.folder / "pr.json").get("base")
        return base if isinstance(base, str) and failures.sane_ref(base) else None

    def failure(self, exc: Exception, patch: str = "", followup: bool = False) -> dict:
        """The plain-language record for `exc`: what happened, why, what to do, and when the draft goes stale."""
        code, plain, why, fix = self.explain(exc, patch, followup)
        # followups push to the PR's own branch, so "how fast does upstream move" doesn't apply
        commits = None if followup else failures.recent_commit_count(
            read, self.repo, self.base_branch(), failures.patch_files(patch), self.now)
        rec = failures.record(code, plain, mask(why)[:400], self.now.date(), commits, self.stamp, fix)
        if followup:
            rec["refresh_by"] = None
        return rec

    def explain(self, exc: Exception, patch: str, followup: bool) -> tuple[str, str, str, str | None]:
        if isinstance(exc, Failure):
            return exc.code, exc.plain, exc.why or str(exc), None
        msg = mask(str(exc))
        low = msg.lower()
        if isinstance(exc, PatchError):
            files = failures.patch_files(patch)
            files = [f for f in files if f in msg] or files
            if followup:
                return ("upstream_moved", "Couldn't send: the pull request's branch changed after this fix was written.",
                        "The saved fix no longer fits the branch. Ask for a new draft or push it yourself.", "none")
            touch = failures.latest_touch(read, self.repo, self.base_branch(), files) if files else None
            name = self.repo.split("/")[1]
            if touch:
                when = failures.day(touch[1])
                return ("upstream_moved",
                        f"Couldn't send: {name} changed {failures.names(files)} on {when}, after this draft was written.",
                        f"The project edited {failures.names(files)} on {when}, so the saved change no longer fits. "
                        "Refresh rebuilds it on their latest code.", None)
            return ("upstream_moved", f"Couldn't send: {name} changed after this draft was written.",
                    "The saved change no longer fits the project's latest code. Refresh rebuilds it.", None)
        if "401" in msg or "bad credentials" in low or "authentication failed" in low or "invalid username" in low:
            return ("token_expired", "Your GitHub key has expired or was turned down.",
                    "GitHub rejected the key. Make a new one and replace SUBMIT_TOKEN in the data repo's secrets.", None)
        if "workflow" in low and ("scope" in low or "refusing to allow" in low):
            return ("token_scope", "Your GitHub key is missing the 'workflow' permission this change needs.",
                    "The change edits a file under .github/workflows, which needs the key's 'workflow' permission.", None)
        return "github_error", "Couldn't send: GitHub returned an error.", msg[:300], None

    def problem(self, s: dict, exc: Exception, patch: str = "", followup: bool = False) -> None:
        """Store a runtime failure on the item: the masked error and the plain record."""
        s["last_error"] = mask(str(exc))[:500]
        s["failure"] = self.failure(exc, patch, followup)

    @staticmethod
    def clear_problem(s: dict) -> None:
        for k in ("last_error", "submitting_at", "failure", "send_after_refresh"):
            s.pop(k, None)


# -- checks -------------------------------------------------------------------

sane_ref = failures.sane_ref


def check_text(title: str | None, body: str | None, disclosure: str | None = None) -> None:
    def rejected(why: str):
        raise Failure("text_rejected", "Couldn't send: the edited text was turned down.", why[0].upper() + why[1:])
    if title is not None:
        if not title.strip() or "\n" in title or len(title) > MAX_TITLE:
            rejected(f"the title must be one line, 1-{MAX_TITLE} characters")
    if body is not None:
        if not body.strip() or len(body) > MAX_BODY:
            rejected(f"the text must be 1-{MAX_BODY} characters")
        if disclosure and disclosure not in body:
            rejected("the text must keep the AI-disclosure sentence this project requires")
    if briefings.has_ai_marker(title or "", body or ""):
        rejected("the text contains an AI marker")


def check_token(patch: str) -> None:
    """Before cloning anything: is the key alive, and does it have the permission this patch needs?"""
    status, scopes = ghwrite.token_scopes()
    if status == 401:
        raise Failure("token_expired", "Your GitHub key has expired or was turned down.",
                      "GitHub rejected the key. Make a new one and replace SUBMIT_TOKEN in the data repo's secrets.")
    if scopes is not None and failures.touches_workflows(patch) and "workflow" not in scopes:
        raise Failure("token_scope", "Your GitHub key is missing the 'workflow' permission this change needs.",
                      "The change edits a file under .github/workflows, which needs the key's 'workflow' permission.")


def check_taken(job: Job) -> None:
    """Refuse when the issue was closed or someone else already has a pull request open for it."""
    try:
        issue = read(f"repos/{job.repo}/issues/{job.number}")
        other = None if issue.get("state") == "closed" else track.competing_pr(read, job.repo, job.number, job.cfg.login)
    except Exception:
        return  # a read that failed must not block a send the owner approved
    if issue.get("state") == "closed":
        raise Failure("taken", "Couldn't send: this issue was closed.", "The project closed the issue, so there is nothing left to fix.")
    if other:
        raise Failure("taken", "Couldn't send: someone else already opened a pull request for this issue.", f"See {other}.")


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
    if (mode := briefings.mode_of(b)) != "draft":  # a legacy `guide` counts as pair
        fail(f"{mode} mode: this is done on the laptop, not sent from here")
    if b.get("ai_posts_forbidden"):
        fail("this project forbids AI-written posts")
    if problems := briefings.validate(b, job.folder):
        text = "the briefing no longer validates: " + "; ".join(problems)
        if any(re.search(r"needs (pr\.json|draft\.patch|post\.md)", p) for p in problems):
            raise Failure("missing_files", "Couldn't send: a file the draft needs is missing.", text[0].upper() + text[1:])
        fail(text)
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
        raise Failure("wip_limit", f"Couldn't send: you already have {total} pull requests open.",
                      f"{total} PRs are already open (max {job.cfg.settings.get('max_open_prs', 3)}). "
                      "It can go once one of them is merged or closed.")
    if in_repo >= job.cfg.settings.get("max_open_prs_per_repo", 1):
        raise Failure("wip_limit", f"Couldn't send: you already have a pull request open in {job.repo}.",
                      f"{in_repo} of your PRs are already open in {job.repo}. It can go once that one is merged or closed.")


# -- git and GitHub -----------------------------------------------------------

def identity(job: Job) -> tuple[str, str]:
    login = job.cfg.login
    uid = read(f"users/{login}")["id"]
    return job.cfg.name or login, f"{uid}+{login}@users.noreply.github.com"


def commit(work: Path, who: tuple[str, str], message: str, signoff: bool) -> None:
    git(["-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}", "-c", "commit.gpgsign=false",
         "commit", *(["-s"] if signoff else []), "-m", message], work)


def apply_patch(work: Path, patch: Path) -> None:
    patch = patch.resolve()  # git runs inside work, so a relative path would miss
    try:
        git(["apply", "--index", str(patch)], work)
    except ActError:
        try:
            git(["apply", "--3way", str(patch)], work)
        except ActError as e:
            raise PatchError(str(e)) from e


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
        job.clear_problem(s)
        s["pr_url"] = found["html_url"]
        s.setdefault("submitted_at", job.stamp)
        return {"url": found["html_url"], "note": "already existed"}
    if job.dry_run:
        return {"commit_message": message, "signoff": pr["signoff"], "title": title, "body": body,
                "calls": ["GET user (check the key's permissions)", "GET the issue and its cross-references (is it still free?)",
                          f"POST repos/{job.repo}/forks", f"git clone --filter=blob:none <your fork of {job.repo}>",
                          f"git fetch upstream {base}", f"git checkout -b {branch} upstream/{base}",
                          "git apply --index draft.patch", f"git commit{' -s' if pr['signoff'] else ''}",
                          f"git push origin {branch}",
                          f"POST repos/{job.repo}/pulls head={login}:{branch} base={base}"]}

    check_taken(job)
    patch = job.draft_patch()
    job.move(s, "submitting")
    s["submitting_at"] = job.stamp
    try:
        check_token(patch)  # before the fork and the clone, so a bad key costs nothing
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
        job.problem(s, e, patch)
        raise ActError(f"submit failed, it is back to approved: {mask(str(e))}") from e
    job.move(s, "pr_open")
    job.clear_problem(s)
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
        job.clear_problem(s)
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
        job.problem(s, e)
        raise ActError(f"post failed, it is back to approved: {mask(str(e))}") from e
    job.move(s, "posted")
    job.clear_problem(s)
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
        job.clear_problem(s)  # the push comes before the reply, so both already happened
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

    patch_text = patch.read_text() if patch and not pushed and patch.exists() else ""
    try:
        if patch and not pushed:
            check_token(patch_text)
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
        job.problem(s, e, patch_text, followup=True)
        raise ActError(f"follow-up failed: {mask(str(e))}") from e
    job.clear_problem(s)
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


def prepare(job: Job) -> dict:
    job.suggestion(("suggested",))["prepare_requested_at"] = job.stamp
    return {}


def pair(job: Job) -> dict:
    s = job.suggestion(("suggested",) + PENDING)
    s["pairing"], s["pairing_at"] = True, job.stamp
    return {}


def unpair(job: Job) -> dict:
    s = job.st["suggestions"].get(job.key) or fail(f"no suggestion for {job.key}")
    s.pop("pairing", None)
    s.pop("pairing_at", None)
    return {}


def feature(job: Job) -> dict:
    url = job.pr("merged")["url"]
    featured = job.public()["featured"]
    if url not in featured:
        if len(featured) >= MAX_FEATURED:
            fail(f"{MAX_FEATURED} pull requests are already featured: unfeature one first")
        featured.append(url)
    return {}


def unfeature(job: Job) -> dict:
    url = job.pr()["url"]
    featured = job.public()["featured"]
    if url in featured:
        featured.remove(url)
    return {}


def summary(job: Job) -> dict:
    text = (job.body or "").strip()  # no text at all clears it, the same as empty text
    summaries = job.public()["summaries"]
    if not text:
        summaries.pop(job.pr()["url"], None)
        return {}
    pr = job.pr("merged", "open")
    if len(text) > MAX_SUMMARY or len(text.splitlines()) > 1:
        fail(f"the summary must be one line, 1-{MAX_SUMMARY} characters")
    if briefings.has_ai_marker(text):
        fail("the text contains an AI marker")
    summaries[pr["url"]] = text
    return {}


def refresh(job: Job) -> dict:
    """Mark a prepared PR to be rebuilt on the project's current code and then sent.

    The rebuild is the act workflow's check and send jobs (refresh.py); this only records
    that the owner asked, so a rebuilt draft goes out without another tap. A dry run
    does the apply check (without the project's tests) and says which way it would go.
    """
    s, b = ready_item(job, ("pr",))
    if job.dry_run:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                verdict = refreshmod.check(job.data, job.key, Path(tmp), run_tests=False)
            except refreshmod.RefreshError as e:
                fail(str(e))
        cmd = verdict["tests"]["command"]
        same = verdict["structural_ok"]
        return {"path": "send" if same else "routine", "reason": verdict["reason"] or None,
                "calls": ["git clone --filter=blob:none <the project, no key>", "git apply --3way draft.patch",
                          f"run the tests: {cmd}" if cmd else "no test command",
                          "submit the refreshed patch" if same else "start the routine to rebuild this one item"]}
    s["send_after_refresh"] = True
    return {}


HANDLERS = {"submit": submit, "post": post, "followup": followup, "approve": approve, "later": later, "skip": skip,
            "prepare": prepare, "pair": pair, "unpair": unpair, "feature": feature, "unfeature": unfeature,
            "summary": summary, "refresh": refresh}


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
        s = st["suggestions"].get(key)
        if isinstance(e, Failure) and not dry_run and action in ("submit", "post", "followup") and s and s.get("status") in (
                *PENDING, "submitting", "waiting_on_you", "pr_open"):
            s["failure"] = job.failure(e, job.draft_patch(), followup=action == "followup")  # refused before anything ran
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
