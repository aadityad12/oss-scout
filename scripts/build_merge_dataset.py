"""Turn pr_outcomes.jsonl into a leakage-free dataset for the merge-prediction model. Standard library only.

  python3 scripts/build_merge_dataset.py --rows <data>/pr_outcomes.jsonl --state <data>/state.json \\
      --out dataset.jsonl --meta dataset_meta.json [--cut 2026-11-25T00:00:00Z] [--horizon-days 90]

Rules (docs/merge-model.md explains why):
  - only outside contributors' PRs from forks, in projects that are not archived and do not merge
    outside GitHub (`merges_elsewhere` in state.json)
  - the label: y = 1 if the PR was merged within `horizon_days` of being opened, else 0 (closed
    unmerged, merged later, or still open)
  - the cut is the newest `collected_at` in the file unless --cut is given; PRs opened within
    `horizon_days` before the cut are left out, since their outcome isn't known yet
  - the project's record (`repo_prior_*`) is recomputed from the same project's PRs that were
    closed strictly before this PR was opened; with fewer than 5 of them the rate and the median
    are left empty (the training script fills them from the training split only)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout import prdata  # noqa: E402
from scout.score import OUTSIDE, parse_ts  # noqa: E402

THIN = 5
DROP = ("closed_at", "merged", "state_at_cut", "collected_at")  # the outcome and bookkeeping; never features


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def excluded_repos(state: dict | None) -> set[str]:
    repos = (state or {}).get("repos", {})
    return {r for r, m in repos.items() if m.get("archived") or m.get("merges_elsewhere")}


def label(row: dict, horizon_days: float) -> int:
    created, closed = parse_ts(row["created_at"]), parse_ts(row.get("closed_at"))
    return int(row.get("merged") is True and closed is not None
               and closed <= created + timedelta(days=horizon_days))


def build(rows: list[dict], state: dict | None = None, cut: datetime | None = None,
          horizon_days: float = 90) -> tuple[list[dict], dict]:
    """(dataset rows, metadata). Dataset rows keep the prdata feature fields plus `y`."""
    skip = excluded_repos(state)
    kept = [r for r in rows if r.get("author_assoc") in OUTSIDE and r.get("from_fork") is True
            and r.get("repo") not in skip and parse_ts(r.get("created_at"))]
    if cut is None:
        stamps = [parse_ts(r["collected_at"]) for r in kept if r.get("collected_at")]
        if not stamps:
            raise SystemExit("no collected_at in the rows; pass --cut")
        cut = max(stamps)
    limit = cut - timedelta(days=horizon_days)

    by_repo: dict[str, list[dict]] = {}
    for r in kept:
        by_repo.setdefault(r["repo"], []).append(r)
    out, too_recent, stale_open = [], 0, 0
    horizon = timedelta(days=horizon_days)
    for repo, rs in by_repo.items():
        hist = []
        for r in rs:
            created, closed = parse_ts(r["created_at"]), parse_ts(r.get("closed_at"))
            # a PR closed after the cut is treated as still open at the cut
            if r.get("state_at_cut") == "closed" and closed and closed <= cut:
                hist.append((created, closed, r.get("merged") is True))
        for r in rs:
            created = parse_ts(r["created_at"])
            if created > limit:
                too_recent += 1
                continue
            closed = parse_ts(r.get("closed_at"))
            if r.get("state_at_cut") == "open" and closed is None:
                collected = parse_ts(r.get("collected_at"))
                if collected is None or collected < created + horizon:
                    stale_open += 1
                    continue
            n, rate, med = prdata.prior_stats(hist, created)
            if n < THIN:
                rate = med = None
            row = {k: v for k, v in r.items() if k in prdata.FIELDS and k not in DROP}
            row.update(repo_prior_n=n, repo_prior_merge_rate=rate, repo_prior_median_days_to_merge=med,
                       y=label(r, horizon_days))
            if closed and closed > cut:  # outcome after the cut is unknown at the cut
                row["y"] = 0
            out.append(row)
    out.sort(key=lambda r: (r["created_at"], r["repo"], r["number"]))

    ranges = {}
    for r in out:
        p = ranges.setdefault(r["repo"], {"first_created": r["created_at"], "last_created": r["created_at"],
                                          "n": 0, "pos": 0})
        p["last_created"] = r["created_at"]
        p["n"] += 1
        p["pos"] += r["y"]
    meta = {"cut": iso(cut), "horizon_days": horizon_days, "rows_in_file": len(rows),
            "rows_outside_eligible": len(kept), "rows_too_recent": too_recent,
            "rows_stale_open": stale_open, "n": len(out),
            "pos": sum(r["y"] for r in out), "projects": len(ranges),
            "excluded_projects": sorted(skip & {r.get("repo") for r in rows}),
            "per_project": dict(sorted(ranges.items()))}
    return out, meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", required=True, type=Path, help="pr_outcomes.jsonl")
    ap.add_argument("--state", type=Path, help="state.json, to leave out archived and merges_elsewhere projects")
    ap.add_argument("--allow-no-state", action="store_true",
                    help="allow building without --state (skips archived/merges_elsewhere exclusions)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--meta", type=Path, help="metadata JSON (default: next to --out)")
    ap.add_argument("--cut", help="ISO time; default: the newest collected_at in --rows")
    ap.add_argument("--horizon-days", type=float, default=90)
    args = ap.parse_args()
    if args.state is None and not args.allow_no_state:
        ap.error("--state is required so archived and merges_elsewhere projects are excluded (or pass --allow-no-state)")

    state = json.loads(args.state.read_text()) if args.state else None
    cut = parse_ts(args.cut) if args.cut else None
    data, meta = build(prdata.read(args.rows), state, cut, args.horizon_days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in data))
    meta_path = args.meta or args.out.with_name(args.out.stem + "_meta.json")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"cut {meta['cut']}, horizon {meta['horizon_days']:g} days")
    print(f"{meta['rows_in_file']} rows in file, {meta['rows_outside_eligible']} outside rows in eligible projects, "
          f"{meta['rows_too_recent']} too recent, {meta['rows_stale_open']} stale open, "
          f"{meta['n']} in the dataset ({meta['pos']} merged in time) "
          f"from {meta['projects']} projects")
    print(f"{'project':45} {'first opened':12} {'last opened':12} {'rows':>5} {'merged':>6}")
    for repo, p in meta["per_project"].items():
        print(f"{repo:45} {p['first_created'][:10]:12} {p['last_created'][:10]:12} {p['n']:5} {p['pos']:6}")
    print(f"wrote {args.out} and {meta_path}")


if __name__ == "__main__":
    main()
