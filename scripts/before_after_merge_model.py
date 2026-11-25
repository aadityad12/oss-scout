"""Show how the candidate order changes when the merge model is switched on, on real data. Read-only.

  python3 scripts/before_after_merge_model.py --data <copy of the data branch> [--model <path>]

Re-scores last night's candidates (candidates.json) twice with the same inputs: once with
`use_merge_model = false`, once with it true and the model's per-project probability, and prints
both orders side by side. The model is read from --model (default <data>/merge_model.json) through
`mergemodel.load`, so a stale model or one that did not beat the baselines is refused, exactly as
in the nightly run. --ignore-gate skips that check for a look at a failing model; the nightly run
never does. Nothing is written.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout import config, mergemodel, rank, state as statemod  # noqa: E402
from scout.score import parse_ts  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--model", type=Path, help="default: <data>/merge_model.json")
    ap.add_argument("--ignore-gate", action="store_true",
                    help="use the model even if it is stale or did not beat the baselines (preview only)")
    args = ap.parse_args()

    cfg = config.load()
    st = statemod.load(args.data)
    cands_file = json.loads((args.data / "candidates.json").read_text())
    cands = cands_file.get("candidates", [])
    now = parse_ts(cands_file.get("generated_at")) if cands_file.get("generated_at") else None
    model_path = args.model or args.data / "merge_model.json"
    max_age = cfg.settings.get("merge_model_max_age_days", 90)
    model = mergemodel.load(model_path, max_age_days=max_age)
    if model is None and args.ignore_gate:
        raw = json.loads(model_path.read_text())
        model = raw if mergemodel._valid(raw) else None
        if model is not None:
            print(f"note: --ignore-gate; beats_baseline is {raw.get('beats_baseline')}, the nightly run would not use this model")
    if model is None:
        sys.exit(f"{model_path}: missing, stale (over {max_age} days), malformed or did not beat the baselines; "
                 "the ranking would not change")
    rows = mergemodel.load_project_rows(args.data)

    off_cfg, on_cfg = copy.deepcopy(cfg), copy.deepcopy(cfg)
    off_cfg.settings["use_merge_model"] = False
    on_cfg.settings["use_merge_model"] = True
    scored = []
    for c in cands:
        repo = {**(c.get("repo_info") or {}), **st["repos"].get(c["repo"], {})}
        repo.pop("merge_prob", None)
        p = mergemodel.project_score(model, c["repo"], repo, rows.get(c["repo"]))
        before = rank.score_issue(c, repo, off_cfg, st, now=now)
        after = rank.score_issue(c, repo, on_cfg, st, now=now, merge_prob=p)
        scored.append({"key": c["key"], "before": before["score"], "after": after["score"],
                       "friendliness": before["parts"]["friendliness"], "merge_prob": p})
    old_order = sorted(scored, key=lambda s: -s["before"])
    new_order = sorted(scored, key=lambda s: -s["after"])
    old_rank = {s["key"]: i + 1 for i, s in enumerate(old_order)}
    print(f"{len(scored)} candidates from {args.data / 'candidates.json'}; model trained {model.get('trained_at')}")
    print(f"{'new':>3} {'old':>3} {'move':>4}  {'score off':>9} {'score on':>8} {'friendly':>8} {'merge p':>7}  key")
    moved = 0
    for i, s in enumerate(new_order, 1):
        o = old_rank[s["key"]]
        moved += o != i
        mp = "-" if s["merge_prob"] is None else f"{s['merge_prob']:.3f}"
        print(f"{i:3} {o:3} {o - i:+4}  {s['before']:9.1f} {s['after']:8.1f} {s['friendliness']:8.3f} {mp:>7}  {s['key']}")
    print(f"{moved} of {len(scored)} candidates change position")


if __name__ == "__main__":
    main()
