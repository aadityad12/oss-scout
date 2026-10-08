"""Refresh & send: put an approved draft back on top of the project's current code.

The owner taps Refresh & send on an item whose draft stopped applying because the
project moved on. The data repo's act workflow then does, in order:

  check    `refresh-check`: clone upstream (public, no token), `git apply --3way`
           the saved draft.patch, and run the briefing's test command. The patch is
           written first, then the tests run, so code from the project can never
           change what the next job receives. This job holds no secrets.
  send     only when it is the same fix: `refresh-apply` swaps in the new patch and
           records `refresh_result`, then the normal submit runs.
  request  otherwise: `refresh-request` records that a rebuild is wanted
           (`refresh_requested_at`) and the workflow starts the Claude routine for
           that one item.

It is the same fix only when the patch applied with no conflicts, touches the same
files, adds+removes about as many lines (within 10%, or 5 lines), and the tests
passed or there is no command.

State fields on a suggestion:
  refresh_requested_at  when a rebuild was asked for (ISO)
  refresh_result        {"at", "same_fix", "what_changed", "tests", ...}; the check's
                        "no" has no `at` (it is not an answer yet), the routine's has one
  refresh_attempts      failed rebuilds so far (the routine counts them)
  send_after_refresh    true once the owner tapped Refresh & send
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import briefings, state as statemod
from .failures import changed_lines, patch_files, sane_ref
from .score import parse_ts

TEST_TIMEOUT = 900          # seconds the project's tests may run
SIZE_SLACK_LINES, SIZE_SLACK_PCT = 5, 0.10
PENDING_HOURS = 48          # a requested rebuild counts as in flight this long
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL", re.I)
KEY = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")


class RefreshError(Exception):
    pass


def upstream_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def pending(s: dict, now: datetime | None = None) -> bool:
    """True while a requested rebuild has no answer yet (and was asked for recently)."""
    now = now or datetime.now(timezone.utc)
    asked = parse_ts(s.get("refresh_requested_at"))
    if not asked or now - asked > timedelta(hours=PENDING_HOURS):
        return False
    answered = parse_ts((s.get("refresh_result") or {}).get("at"))
    return not (answered and answered >= asked)


def sendable_after_refresh(s: dict) -> bool:
    """An approved item whose refresh answered "same fix" after its last failure, and was asked to send."""
    res = s.get("refresh_result") or {}
    answered, failed = parse_ts(res.get("at")), parse_ts((s.get("failure") or {}).get("at"))
    return bool(s.get("status") == "approved" and s.get("send_after_refresh") is True
                and res.get("same_fix") is True and answered and (not failed or answered > failed))


def size_ok(before: int, after: int) -> bool:
    gap = abs(after - before)
    return gap <= SIZE_SLACK_LINES or gap <= SIZE_SLACK_PCT * before


def same_shape(old: str, new: str) -> str | None:
    """Why `new` is not the same fix as `old`, or None when the files and size match."""
    if set(patch_files(old)) != set(patch_files(new)):
        return "the updated patch touches different files"
    before, after = changed_lines(old), changed_lines(new)
    if not size_ok(before, after):
        return f"the change went from {before} to {after} lines"
    return None


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _run_tests(command: str, cwd: Path, timeout: int) -> tuple[str, str]:
    """(passed|failed|timeout, the last of the output). The project's code runs here, so no secrets."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "timeout", ""
    return ("passed" if proc.returncode == 0 else "failed"), (proc.stdout + proc.stderr).strip()[-400:]


def check(data: Path, key: str, out: Path, phase: str = "all", run_tests: bool = True,
          timeout: int = TEST_TIMEOUT) -> dict:
    """Try the saved draft on the project's current code and say whether it is the same fix.

    phase "apply" writes <out>/new.patch and the structural verdict only; "tests" also runs
    the briefing's test command; "all" does both. Always writes <out>/verdict.json.
    """
    m = KEY.match(key)
    if not m:
        raise RefreshError("bad key")
    repo, folder = m.group(1), data / "briefings" / briefings.slug(key)
    pr = briefings.read_json(folder / "pr.json")
    b = briefings.read_json(folder / "briefing.json")
    base = str(pr.get("base", ""))
    if not (folder / "draft.patch").exists() or not sane_ref(base):
        raise RefreshError("the item has no draft.patch or no valid base branch")
    old = (folder / "draft.patch").read_text()
    out.mkdir(parents=True, exist_ok=True)

    verdict: dict = {"key": key, "base": base, "same_fix": False, "structural_ok": False,
                     "files_before": patch_files(old), "lines_before": changed_lines(old),
                     "tests": {"status": "skipped", "command": None, "tail": ""}}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        cloned = _git(["clone", "--filter=blob:none", "--no-tags", "-b", base, upstream_url(repo), str(work)])
        if cloned.returncode:
            raise RefreshError(f"could not clone {repo}: {cloned.stderr.strip()[-200:]}")
        verdict["head"] = _git(["rev-parse", "HEAD"], work).stdout.strip()
        applied = _git(["apply", "--3way", str((folder / "draft.patch").resolve())], work)
        if applied.returncode:
            hit = [f for f in verdict["files_before"] if f in applied.stderr] or verdict["files_before"]
            verdict["reason"] = f"the saved change conflicts with newer code in {', '.join(hit[:3])}"
            verdict["conflicts"] = hit
        else:
            new = _git(["diff", "--cached", "--no-color"], work).stdout
            verdict.update(files_after=patch_files(new), lines_after=changed_lines(new))
            if phase in ("apply", "all"):
                (out / "new.patch").write_text(new)
            if why := same_shape(old, new):
                verdict["reason"] = why
            else:
                verdict["structural_ok"] = True
        if verdict["structural_ok"]:
            command = str((b.get("tests") or {}).get("command") or "").strip()
            verdict["tests"]["command"] = command or None
            if phase == "apply" or not run_tests:
                verdict["same_fix"] = None  # not known until the tests have run
            elif not command:
                verdict.update(same_fix=True, tests={**verdict["tests"], "status": "no command"})
            else:
                status, tail = _run_tests(command, work, timeout)
                verdict["tests"].update(status=status, tail=tail)
                verdict["same_fix"] = status == "passed"
                if not verdict["same_fix"]:
                    verdict["reason"] = f"the tests {'timed out' if status == 'timeout' else 'failed'} on the newer code"
    verdict.setdefault("reason", "")
    statemod.write_json(out / "verdict.json", verdict)
    return verdict


def _verdict_text(path: Path, field: str, limit: int) -> str:
    """A short plain string from a verdict file; the file came from a job that ran project code."""
    try:
        value = json.loads(path.read_text()).get(field)
    except (OSError, ValueError):
        return ""
    return " ".join(str(value or "").split())[:limit] if not isinstance(value, (dict, list)) else ""


def apply_result(data: Path, key: str, patch: Path, verdict: Path, now: datetime | None = None) -> dict:
    """Swap in the refreshed patch and record the result. Re-checks the patch against the old one."""
    now = now or datetime.now(timezone.utc)
    folder = data / "briefings" / briefings.slug(key)
    st = statemod.load(data)
    s = st["suggestions"].get(key) or _missing(key)
    old, new = (folder / "draft.patch").read_text(), patch.read_text()
    why = "it is empty" if not new.strip() else same_shape(old, new)
    if why:
        raise RefreshError(f"refusing the refreshed patch: {why}")
    info = json.loads(verdict.read_text()) if verdict.exists() else {}
    status = str((info.get("tests") or {}).get("status") or "")
    tests = "passed" if status == "passed" else "no test command" if status in ("no command", "") else status
    (folder / "draft.patch").write_text(new)
    s["refresh_result"] = {"at": now.isoformat(timespec="seconds"), "same_fix": True, "by": "check", "tests": tests,
                           "what_changed": "The project changed nearby code; the fix itself is the same."}
    s["send_after_refresh"] = True
    statemod.save(data, st)
    return s["refresh_result"]


def request_result(data: Path, key: str, verdict: Path, now: datetime | None = None) -> dict:
    """The check said it is not the same fix: ask for a rebuild. The routine's answer is what has an `at`."""
    now = now or datetime.now(timezone.utc)
    st = statemod.load(data)
    s = st["suggestions"].get(key) or _missing(key)
    reason = _verdict_text(verdict, "reason", 300) or "the draft needs to be rebuilt"
    s["refresh_requested_at"] = now.isoformat(timespec="seconds")
    s["refresh_result"] = {"same_fix": False, "reason": reason, "by": "check",
                           "checked_at": now.isoformat(timespec="seconds")}
    s["send_after_refresh"] = True
    statemod.save(data, st)
    return s["refresh_result"]


def _missing(key: str):
    raise RefreshError(f"no suggestion for {key}")
