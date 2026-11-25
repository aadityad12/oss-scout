"""Per-pull-request rows for the merge-prediction model, built from what `score.measure` already fetched.

Every nightly measurement lists a project's last 300 closed and 100 open pull requests. Each outside
contributor's PR in those lists becomes one row in `data/pr_outcomes.jsonl`: a few numbers and flags
known when the PR was opened, the project's record before that moment, and the outcome. No extra
API calls are made.

Privacy rules (tests enforce them):
  - fixed fields only (`FIELDS`), nothing else is ever written
  - no logins, names, URLs, labels, comments, reviews or text: titles and bodies become lengths and
    two yes/no flags. The author's login is used only to count their earlier PRs, then dropped.

Rows are keyed by `repo#number`. A later collection replaces the earlier row for the same PR (an open
PR may have closed since), and PRs that fell out of the 300/100 window keep their last row, so the
file grows over time. The file is rewritten atomically, like `state.write_json`.

Which PRs get a row at all: the closed list is the last 300 by *update* time, so in a busy project an
old PR only shows up if something touched it lately, usually a late close. Counting those would
teach the model that old PRs in busy projects fail. So a PR gets its first row only if it was opened
after `complete_since`: the point from which the lists hold every PR. Rows already in the file are
always updated.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .score import is_outside, from_fork, parse_ts

FILE = "pr_outcomes.jsonl"

FIELDS = (
    # keys and label
    "repo", "number", "created_at", "closed_at", "merged", "state_at_cut", "collected_at",
    # about the PR, as GitHub reports it at collection (draft, author_assoc and the title can change later)
    "author_assoc", "author_prior_prs_in_sample", "from_fork", "draft", "title_len", "body_len",
    "body_has_issue_ref", "title_wip_marker", "created_weekday", "created_hour_utc",
    # the project's record before the PR was opened (only PRs closed before it)
    "repo_prior_n", "repo_prior_merge_rate", "repo_prior_median_days_to_merge",
    # the project as of collection
    "stars_log10", "language", "owner_type", "ai_policy", "cla", "has_contributing",
)

ISSUE_NUMBER = re.compile(r"#\d+")
# GitHub's closing keywords followed by an issue link (a bare "#123" is caught by ISSUE_NUMBER)
CLOSING_LINK = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\b:?\s+\S*/issues/\d+", re.I)
WIP = re.compile(r"\bwip\b|\bdraft\b|\bdo[\s_-]*not[\s_-]*merge\b", re.I)


def key(row: dict) -> str:
    return f"{row['repo']}#{row['number']}"


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def _days(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400


def has_issue_ref(body: str | None) -> bool:
    body = body or ""
    return bool(ISSUE_NUMBER.search(body) or CLOSING_LINK.search(body))


def has_contributing(policy_files) -> bool:
    return any("contributing" in str(f).lower() for f in policy_files or [])


# Fields captured when a PR is first seen; preserved on later updates so review-time edits
# do not overwrite opening-time observations on incremental nightly runs.
OPENING_FIELDS = ("author_assoc", "draft", "title_len", "body_len", "body_has_issue_ref", "title_wip_marker")


def _safe_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or "@" in s or "http" in s.lower() or any(ord(ch) < 32 for ch in s):
        return None
    return s


def static_features(meta: dict | None) -> dict:
    """The project-level fields, from a `measure()` result or the same keys in state.json."""
    meta = meta or {}
    stars = meta.get("stars")
    return {
        "stars_log10": round(math.log10(stars + 1), 4) if isinstance(stars, (int, float)) and not isinstance(stars, bool) and stars >= 0 else None,
        "language": _safe_label(meta.get("language")),
        "owner_type": _safe_label(meta.get("owner_type")),
        "ai_policy": _safe_label(meta.get("ai_policy")),
        "cla": bool(meta.get("cla")),
        "has_contributing": has_contributing(meta.get("policy_files")),
    }


def prior_stats(closed: list[tuple[datetime, datetime, bool]], before: datetime) -> tuple[int, float | None, float | None]:
    """(n, merge rate, median days to merge) of the outside PRs closed strictly before `before`.

    `closed` holds (created, closed, merged) for each closed outside PR. Days to merge use the close
    time, which is the merge time for a merged PR."""
    prior = [(c, d, m) for c, d, m in closed if d < before]
    if not prior:
        return 0, None, None
    merged = [_days(c, d) for c, d, m in prior if m]
    return (len(prior), round(len(merged) / len(prior), 4),
            round(statistics.median(merged), 4) if merged else None)


def rows(repo: str, all_closed: list[dict], all_open: list[dict], counts: dict[str, int],
         now: datetime | None = None, *, repo_meta: dict | None = None) -> list[dict]:
    """One row per outside contributor PR in the sample, with exactly `FIELDS`.

    `counts` is PRs per login across the whole sample (what `is_outside` uses to spot regulars).
    """
    now = now or datetime.now(timezone.utc)
    collected = _iso(now.replace(microsecond=0))
    static = static_features(repo_meta)
    picked = [(p, "closed") for p in all_closed if is_outside(p, counts, repo)]
    picked += [(p, "open") for p in all_open if is_outside(p, counts, repo)]

    closed_hist = []
    for p, state in picked:
        created, closed = parse_ts(p.get("created_at")), parse_ts(p.get("closed_at"))
        if state == "closed" and created and closed:
            closed_hist.append((created, closed, bool(p.get("merged_at"))))
    # every PR in the sample counts for "earlier PRs by this author", regulars and staff included
    by_author: dict[str, list[datetime]] = {}
    for p in all_closed + all_open:
        login, created = (p.get("user") or {}).get("login"), parse_ts(p.get("created_at"))
        if login and created:
            by_author.setdefault(login, []).append(created)

    out, seen = [], set()
    for p, state in picked:
        created = parse_ts(p.get("created_at"))
        if created is None or not isinstance(p.get("number"), int) or p["number"] in seen:
            continue
        seen.add(p["number"])
        closed = parse_ts(p.get("closed_at")) if state == "closed" else None
        login = (p.get("user") or {}).get("login")
        title, body = p.get("title") or "", p.get("body") or ""
        n, rate, med = prior_stats(closed_hist, created)
        row = {
            "repo": repo,
            "number": p["number"],
            "created_at": _iso(created),
            "closed_at": _iso(closed),
            "merged": bool(p.get("merged_at")) if state == "closed" else False,
            "state_at_cut": state,
            "collected_at": collected,
            "author_assoc": p.get("author_association"),
            "author_prior_prs_in_sample": sum(1 for t in by_author.get(login, []) if t < created),
            "from_fork": from_fork(p, repo),
            "draft": bool(p.get("draft")),
            "title_len": len(title),
            "body_len": len(body),
            "body_has_issue_ref": has_issue_ref(body),
            "title_wip_marker": bool(WIP.search(title)),
            "created_weekday": created.astimezone(timezone.utc).weekday(),
            "created_hour_utc": created.astimezone(timezone.utc).hour,
            "repo_prior_n": n,
            "repo_prior_merge_rate": rate,
            "repo_prior_median_days_to_merge": med,
            **static,
        }
        out.append({f: row[f] for f in FIELDS})
    return out


def complete_since(all_closed: list[dict], all_open: list[dict], closed_cap: int = 300,
                   open_cap: int = 100) -> datetime | None:
    """The time after which every PR opened is in the sample, or None if it holds all of them.

    A full closed list (the cap reached) holds every PR updated after its oldest update time, and a
    PR opened after that and since closed was updated after it too. A full open list (newest first)
    holds every open PR opened after its oldest one."""
    bounds = []
    if len(all_closed) >= closed_cap:
        ts = [t for p in all_closed if (t := parse_ts(p.get("updated_at") or p.get("closed_at")))]
        if ts:
            bounds.append(min(ts))
    if len(all_open) >= open_cap:
        ts = [t for p in all_open if (t := parse_ts(p.get("created_at")))]
        if ts:
            bounds.append(min(ts))
    return max(bounds) if bounds else None


def read(path: Path) -> list[dict]:
    """All rows in the file; unreadable lines are skipped."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("repo"), str) and isinstance(row.get("number"), int):
            out.append(row)
    return out


def merge(path: Path, new_rows: list[dict], add_since: datetime | None = None) -> int:
    """Merge rows into the file by `repo#number` and rewrite it atomically.

    With `add_since` (see `complete_since`), a PR not yet in the file is added only if it was opened
    after that time; PRs already in the file are always updated (preserving first-observed opening
    fields across all subsequent updates). Returns the number of rows."""
    merged = {key(r): {f: r.get(f) for f in FIELDS} for r in read(path)}
    for r in new_rows:
        k = key(r)
        if k not in merged and add_since is not None:
            created = parse_ts(r.get("created_at"))
            if created is None or created <= add_since:
                continue
        clean = {f: r.get(f) for f in FIELDS}
        for field in ("language", "owner_type", "ai_policy"):
            clean[field] = _safe_label(clean[field])
        prev = merged.get(k)
        if prev is not None:
            for field in OPENING_FIELDS:
                if prev.get(field) is not None:
                    clean[field] = prev[field]
        merged[k] = clean
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for k in sorted(merged):
                f.write(json.dumps(merged[k], sort_keys=False) + "\n")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return len(merged)
