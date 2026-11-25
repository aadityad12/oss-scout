"""Check manual nightly runs against the Pacific daily cap and routine cooldown."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

DEFAULT_DAILY_CAP = 1
DEFAULT_COOLDOWN_MINUTES = 60
TARGETS = Path(__file__).resolve().parents[1] / "targets.toml"
PACIFIC = ZoneInfo("America/Los_Angeles")
ROUTINE_STEP = "Start the Claude routine"
UNVERIFIED = "Run again could not verify the limits. Try again later."
ALLOWED = "Run again is within the limits."
API = "https://api.github.com"
ACTIVE_STATUSES = {"queued", "in_progress", "pending", "requested", "waiting"}
CONCLUSIONS = {"action_required", "cancelled", "failure", "neutral", "skipped", "stale",
               "startup_failure", "success", "timed_out"}


def timestamp(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.utcoffset() is None:
        raise ValueError("Expected a timezone-aware timestamp")
    return at.astimezone(timezone.utc)


def conclusion_of(record: dict) -> str | None:
    conclusion = record.get("conclusion")
    if conclusion is None and record.get("status") in ACTIVE_STATUSES:
        return None
    if conclusion not in CONCLUSIONS:
        raise ValueError("Expected a job or step conclusion")
    return conclusion


def parse_attempt(value: int | str | None) -> int:
    if value is None:
        return 1
    if type(value) is int:
        attempt = value
    elif isinstance(value, str) and value.isdigit():
        attempt = int(value)
    else:
        raise ValueError("Expected a positive integer run attempt")
    if attempt < 1:
        raise ValueError("Expected a positive integer run attempt")
    return attempt


def decide(
    now: datetime,
    runs: Iterable[dict],
    jobs_of: Callable[[int | str], list[dict]],
    cap: int,
    cooldown_minutes: int,
    current_run_id: int | str,
    *,
    current_run_attempt: int | str | None = None,
) -> tuple[bool, str]:
    try:
        if any(type(limit) is not int or limit < 0 for limit in (cap, cooldown_minutes)):
            return False, UNVERIFIED
        current_attempt = parse_attempt(current_run_attempt)
        if cap == 0:
            return False, "Daily limit reached: extra runs are disabled (0 per Pacific day)."

        midnight = now.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_utc = midnight.astimezone(timezone.utc)
        now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(hours=24)
        current_key = str(current_run_id)
        seen: set[str] = set()
        used = 0
        newest_routine = None

        for run in runs:
            run_id = run["id"]
            if type(run_id) not in (int, str) or not run_id:
                raise ValueError("Expected a workflow run ID")
            run_key = str(run_id)
            if run_key in seen:
                continue
            seen.add(run_key)
            is_current = run_key == current_key
            if is_current and current_attempt == 1:
                continue
            created = timestamp(run["created_at"])
            updated = run.get("updated_at")
            event = run["event"]
            if not isinstance(event, str) or not event:
                raise ValueError("Expected a workflow run event")
            counts_today = created >= midnight_utc and event == "workflow_dispatch"
            check_cooldown = cooldown_minutes > 0 and (
                created >= cutoff or (updated is not None and timestamp(updated) >= cutoff)
            )
            if not (counts_today or check_cooldown):
                continue

            jobs = jobs_of(run_id)
            if not isinstance(jobs, list):
                raise ValueError("Expected a list of jobs")
            used_slot = False
            for job in jobs:
                if not isinstance(job, dict) or not isinstance(job.get("name"), str) or not job["name"]:
                    raise ValueError("Expected a named job")
                if is_current:
                    raw_attempt = job.get("run_attempt")
                    if type(raw_attempt) is not int or raw_attempt < 1:
                        raise ValueError("Expected a positive integer job run_attempt")
                    if raw_attempt >= current_attempt:
                        continue
                if counts_today and job["name"] == "gate":
                    conclusion = conclusion_of(job)
                    if conclusion == "success" or (
                        conclusion is None and job.get("status") == "in_progress"
                    ):
                        used_slot = True
                if not check_cooldown:
                    continue
                if job["name"] == "scan" and "steps" not in job and conclusion_of(job) == "success":
                    raise ValueError("Expected steps for the successful scan")
                steps = job.get("steps", [])
                if not isinstance(steps, list):
                    raise ValueError("Expected a list of steps")
                for step in steps:
                    if not isinstance(step, dict) or not isinstance(step.get("name"), str) or not step["name"]:
                        raise ValueError("Expected a named step")
                    if step["name"] == ROUTINE_STEP and conclusion_of(step) == "success":
                        completed = timestamp(step["completed_at"])
                        if completed >= cutoff and (newest_routine is None or completed > newest_routine):
                            newest_routine = completed
            used += used_slot

        if used >= cap:
            noun = "run" if cap == 1 else "runs"
            return False, (f"Daily limit reached: {cap} extra {noun} per Pacific day, {used} used. "
                           "The next one is allowed after midnight Pacific.")
        cooldown = timedelta(minutes=cooldown_minutes)
        if newest_routine is not None and now - newest_routine < cooldown:
            minutes = max(0, (now - newest_routine) // timedelta(minutes=1))
            unit = "minute" if minutes == 1 else "minutes"
            wait_until = (newest_routine + cooldown).astimezone(PACIFIC)
            return False, (f"The routine started {minutes} {unit} ago. "
                           f"Wait until {wait_until:%H:%M} Pacific ({cooldown_minutes} minute cooldown).")
    except Exception:
        # API responses and exceptions may include request details. Report a fixed reason.
        return False, UNVERIFIED
    return True, ALLOWED


def api_items(url: str, key: str, token: str) -> Iterator[dict]:
    seen = set()
    while url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or url in seen:
            raise ValueError("Invalid Actions API page URL")
        seen.add(url)
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oss-scout-run-again-gate",
        })
        request.add_unredirected_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=15) as response:
            page = json.load(response)[key]
            next_link = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))
        if not isinstance(page, list):
            raise ValueError("Expected an Actions API result list")
        yield from page
        url = next_link[1] if next_link else ""


def main() -> int:
    try:
        with TARGETS.open("rb") as f:
            settings = tomllib.load(f).get("settings", {})
        cap = settings.get("run_again_daily_cap", DEFAULT_DAILY_CAP)
        cooldown_minutes = settings.get("run_again_cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
        repo = os.environ["GITHUB_REPOSITORY"]
        current_run_id = os.environ["GITHUB_RUN_ID"]
        current_run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        token = os.environ["GH_TOKEN"]
        if not (repo and current_run_id and token):
            raise ValueError("Missing GitHub Actions environment")
        actions = f"{API}/repos/{quote(repo)}/actions"

        def jobs_of(run_id: int | str) -> list[dict]:
            url = f"{actions}/runs/{quote(str(run_id), safe='')}/jobs?filter=all&per_page=100"
            return list(api_items(url, "jobs", token))

        now = datetime.now(timezone.utc)
        # GitHub Actions only allows re-running workflows within 30 days of creation,
        # so filtering to the last 31 days bounds pagination while catching all re-runs.
        since = (now - timedelta(days=31)).strftime("%Y-%m-%d")
        runs = api_items(
            f"{actions}/workflows/nightly.yml/runs?created={quote(f'>={since}', safe='')}&per_page=100",
            "workflow_runs",
            token,
        )
        allowed, reason = decide(
            now,
            runs,
            jobs_of,
            cap,
            cooldown_minutes,
            current_run_id,
            current_run_attempt=current_run_attempt,
        )
    except Exception:
        allowed, reason = False, UNVERIFIED

    if allowed:
        print(reason)
        return 0
    print(f"::error title=Run again refused::{reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with Path(summary).open("a", encoding="utf-8") as f:
                f.write(f"{reason}\n")
        except (OSError, ValueError):
            pass  # Keep the workflow annotation if the summary is unavailable.
    return 1


if __name__ == "__main__":
    sys.exit(main())
