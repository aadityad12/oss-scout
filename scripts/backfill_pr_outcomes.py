"""Fill pr_outcomes.jsonl for the projects already measured in state.json, without waiting for nightly runs.

  python3 scripts/backfill_pr_outcomes.py --state <data>/state.json --out <data>/pr_outcomes.jsonl

For each project it lists the last 300 closed and 100 open pull requests, the same sample the
nightly measurement takes (about 4 GET requests per project), and builds rows with scout.prdata.
The project facts (stars, language, owner type, AI policy, CLA, policy files) come from the
cached measurement in state.json, so they cost nothing. Archived projects and ones that merge
outside GitHub are skipped (the model is not trained on them). Read-only on GitHub.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout import prdata, score  # noqa: E402
from scout.github import GitHub, NotFound  # noqa: E402


def collect(gh: GitHub, repo: str, meta: dict, now: datetime) -> tuple[list[dict], datetime | None]:
    """The rows and the time from which the sample holds every PR (see prdata.complete_since)."""
    all_closed, all_open = score.pr_sample(gh, repo)
    counts = score.author_counts(all_closed + all_open)
    return (prdata.rows(repo, all_closed, all_open, counts, now, repo_meta=meta),
            prdata.complete_since(all_closed, all_open, score.CLOSED_SAMPLE))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--repos", nargs="*", help="only these projects (default: every eligible one in state.json)")
    ap.add_argument("--limit", type=int, default=0, help="at most this many projects (0: all)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    repos = json.loads(args.state.read_text()).get("repos", {})
    names = [r for r in sorted(repos) if "friendliness" in repos[r] and not repos[r].get("provisional")
             and not repos[r].get("archived") and not repos[r].get("merges_elsewhere")]
    if args.repos:
        names = [r for r in names if r in set(args.repos)]
    if args.limit:
        names = names[:args.limit]
    now = datetime.now(timezone.utc)

    def one(repo: str):
        gh = GitHub()
        try:
            rows, since = collect(gh, repo, repos[repo], now)
            return repo, rows, since, gh.calls, None
        except NotFound:
            return repo, [], None, gh.calls, "not found"
        except Exception as e:  # keep going; report at the end
            return repo, [], None, gh.calls, str(e)[:120]

    added, calls, failed = 0, 0, []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for repo, rows, since, n, err in pool.map(one, names):
            calls += n
            if err:
                failed.append((repo, err))
                print(f"  {repo}: failed ({err})", file=sys.stderr)
                continue
            before = len(prdata.read(args.out))
            after = prdata.merge(args.out, rows, add_since=since)
            added += after - before
            closed = sum(1 for r in rows if r["state_at_cut"] == "closed")
            print(f"  {repo}: {len(rows)} outside PRs ({closed} closed; outside_prs_sampled in state "
                  f"{repos[repo].get('outside_prs_sampled')}), complete since "
                  f"{since.date().isoformat() if since else 'the start'}, {after - before} new rows",
                  file=sys.stderr)
    print(f"{len(names) - len(failed)} projects, {added} new rows, {len(prdata.read(args.out))} rows in {args.out}, "
          f"{calls} API calls, {len(failed)} failed")


if __name__ == "__main__":
    main()
