"""Plain-language failure records, and the estimate of how long a draft stays usable.

When a send fails, `act` stores a `failure` on the item so the dashboard and the
morning email can say what happened without anyone reading a git error:

  {"code", "plain", "why", "fix_action", "refresh_by", "at"}

`code` is one of CODES, `plain` is one short sentence, `fix_action` says what the
card offers (refresh, edit, token, skip, none), and `refresh_by` is the date after
which the draft is probably too old to patch back onto the project.

Everything here is pure or read-only: the caller passes in a `get(path)` function
that reads GitHub over REST, so tests don't need a network.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Callable

CODES = ("upstream_moved", "token_scope", "token_expired", "taken", "wip_limit",
         "text_rejected", "missing_files", "github_error")
FIX_ACTIONS = ("refresh", "edit", "token", "skip", "none")
FIX_FOR = {"upstream_moved": "refresh", "token_scope": "token", "token_expired": "token", "taken": "skip",
           "wip_limit": "none", "text_rejected": "edit", "missing_files": "none", "github_error": "none"}

WINDOW_DAYS = 30        # how far back to count upstream commits on the patch's files
MAX_FILES = 8           # files looked up per estimate, to keep the API calls few
UNKNOWN_DAYS = 7        # refresh window when GitHub couldn't be asked
REF = re.compile(r"^[A-Za-z0-9._/-]+$")

Get = Callable[[str], object]


def sane_ref(name: str) -> bool:
    return bool(REF.match(name)) and not (name[0] in "-/." or name[-1] in "/." or ".." in name
                                          or "//" in name or name.endswith(".lock"))


# -- reading a patch ------------------------------------------------------------

def patch_files(patch: str) -> list[str]:
    """The paths a patch touches, in order, read from its `diff --git` lines."""
    seen: list[str] = []
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", patch, re.M):
        if m.group(2) not in seen:
            seen.append(m.group(2))
    return seen


def changed_lines(patch: str) -> int:
    """Added plus removed lines, not counting the +++ and --- file headers."""
    return sum(1 for line in patch.splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def touches_workflows(patch: str) -> bool:
    return any(f.startswith(".github/workflows/") for f in patch_files(patch))


# -- how long the draft stays usable -------------------------------------------

def refresh_days(commits: int) -> int:
    """Days a draft is expected to stay usable, from how often its files changed in 30 days."""
    return 14 if commits <= 0 else max(2, min(14, 15 // commits))


def refresh_by(today: date, commits: int | None) -> str:
    """The date to refresh by (ISO). `commits` is None when GitHub couldn't be asked."""
    days = UNKNOWN_DAYS if commits is None else refresh_days(commits)
    return (today + timedelta(days=days)).isoformat()


def _commits(get: Get, repo: str, base: str | None, path: str, **params) -> list[dict]:
    query = urllib.parse.urlencode({"path": path, **({"sha": base} if base else {}), **params})
    return get(f"repos/{repo}/commits?{query}") or []


def recent_commit_count(get: Get, repo: str, base: str | None, files: list[str], now: datetime) -> int | None:
    """Distinct upstream commits in the last 30 days that touched any of `files` (None if unreadable)."""
    since = (now - timedelta(days=WINDOW_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    shas: set[str] = set()
    try:
        for f in files[:MAX_FILES]:
            shas.update(c.get("sha") or repr(c) for c in _commits(get, repo, base, f, since=since, per_page=100))
    except Exception:
        return None
    return len(shas)


def latest_touch(get: Get, repo: str, base: str | None, files: list[str]) -> tuple[str, datetime] | None:
    """(file, date) of the newest upstream commit on any of `files`, via `commits?path=&per_page=1`."""
    best: tuple[str, datetime] | None = None
    for f in files[:3]:
        try:
            got = _commits(get, repo, base, f, per_page=1)
        except Exception:
            continue
        when = (((got[0].get("commit") or {}).get("committer") or {}).get("date") if got else None)
        if when:
            at = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if best is None or at > best[1]:
                best = (f, at)
    return best


def day(at: datetime) -> str:
    return f"{at:%b} {at.day}"


def names(files: list[str]) -> str:
    """'ci.yml', or 'ci.yml and 2 other files'."""
    base = [f.rsplit("/", 1)[-1] for f in files]
    return base[0] if len(base) == 1 else f"{base[0]} and {len(base) - 1} other file{'s' if len(base) > 2 else ''}"


def record(code: str, plain: str, why: str, today: date, commits: int | None, at: str,
           fix_action: str | None = None) -> dict:
    return {"code": code, "plain": plain, "why": why, "fix_action": fix_action or FIX_FOR[code],
            "refresh_by": refresh_by(today, commits), "at": at}
