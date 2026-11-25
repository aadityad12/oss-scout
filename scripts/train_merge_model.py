"""Train and evaluate the merge-prediction model, then write merge_model.json and docs/merge-model.md.

  pip install -r requirements-train.txt
  python3 scripts/train_merge_model.py --dataset dataset.jsonl --meta dataset_meta.json \\
      --out <data>/merge_model.json --doc docs/merge-model.md

The dataset comes from scripts/build_merge_dataset.py. Steps:
  1. Sort by (created_at, repo, number) and split by time: the oldest 60% train, the next 20%
     validation, the newest 20% test. A split boundary never falls between rows with the same
     created_at, so every test row was opened after every train row.
  2. Pick the regularization C on validation (AUC), then refit on train + validation. Missing values,
     category levels, means and scales come from the rows being fit, never from the test split.
  3. Score the test split once: AUC with a 2,000-resample bootstrap 95% interval, Brier score,
     a 10-bin calibration table and ECE, next to the baselines on the same rows.
  4. Leave-one-project-out: for each project, fit on all the others and predict it.
  5. Apply the decision rule (`decide`). The ranking uses the model only if it passes.

scikit-learn and numpy are needed here only. Scoring at run time is scout/mergemodel.py, which
uses the standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout import metrics, mergemodel  # noqa: E402
from scout.score import _scale  # noqa: E402

VERSION = 1
C_GRID = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0)
MIN_LEVEL = 30  # a language / policy / owner type needs this many fit rows to get its own weight
N_RESAMPLES, SEED = 2000, 42
THIN = mergemodel.THIN
BASELINES = {
    "B0": "constant: the merge rate of the training split",
    "B1": "the project's own merge rate in the training split (the training rate when it has fewer than 5)",
    "B1_prior": "the project's raw historical merge rate over its PRs closed before this one was opened "
                "(the training rate when fewer than 5; uses any-time merges, while the label y is 90-day merges)",
    "B2": "the merge-record part of the scout's friendliness: (0.45 * merge rate + 0.25 * speed) / 0.70 "
          "from the PRs closed before this one was opened (0.5 when fewer than 5; uses any-time merges)",
}


# -- data and split -------------------------------------------------------------

def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def order_key(r: dict) -> tuple:
    return (r["created_at"], r["repo"], r["number"])


def _boundary(rows: list[dict], i: int) -> int:
    """Move a split index forward past rows sharing the previous row's created_at."""
    while 0 < i < len(rows) and rows[i]["created_at"] == rows[i - 1]["created_at"]:
        i += 1
    return i


def time_split(rows: list[dict], train: float = 0.6, val: float = 0.2) -> tuple[list, list, list]:
    """Oldest `train` share, next `val` share, the rest. Deterministic; no created_at straddles a boundary."""
    rows = sorted(rows, key=order_key)
    n = len(rows)
    a = _boundary(rows, round(n * train))
    b = _boundary(rows, max(a, round(n * (train + val))))
    return rows[:a], rows[a:b], rows[b:]


# -- the model's inputs -------------------------------------------------------

def spec_from(rows: list[dict]) -> dict:
    """Missing-value fills, category levels and typical-PR defaults, from the rows being fit only."""
    imputations = {}
    for f in mergemodel.IMPUTED:
        vals = [r[f] for r in rows if mergemodel._present(r.get(f))]
        imputations[f] = statistics.median(vals) if vals else 0.0
    categories = {}
    for c in mergemodel.CATEGORICAL:
        counts: dict[str, int] = {}
        for r in rows:
            if isinstance(r.get(c), str):
                counts[r[c]] = counts.get(r[c], 0) + 1
        categories[c] = sorted(v for v, k in counts.items() if k >= MIN_LEVEL and v != mergemodel.OTHER)
        categories[c].append(mergemodel.OTHER)
    defaults = mergemodel._typical(rows, {}) if rows else {}
    numeric = list(mergemodel.NUMERIC)
    base = {"numeric": numeric, "categories": categories, "imputations": imputations}
    raw = [mergemodel.raw_vector(base, r) for r in rows]
    means, scales = {}, {}
    for i, n in enumerate(numeric):
        col = [x[i] for x in raw]
        m = sum(col) / len(col)
        sd = math.sqrt(sum((v - m) ** 2 for v in col) / len(col))
        means[n], scales[n] = m, (sd if sd > 0 else 1.0)  # StandardScaler's population sd, 1 when constant
    return {**base, "features": mergemodel.feature_names(numeric, categories), "means": means,
            "scales": scales, "project_defaults": defaults}


def matrix(spec: dict, rows: list[dict]):
    import numpy as np
    return np.array([mergemodel.vector(spec, r) for r in rows], dtype=float)


def fit(spec: dict, rows: list[dict], C: float):
    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression(C=C, class_weight=None, max_iter=10000, tol=1e-8)
    clf.fit(matrix(spec, rows), [r["y"] for r in rows])
    return clf


def as_model(spec: dict, clf) -> dict:
    return {**spec, "weights": [float(w) for w in clf.coef_[0]], "intercept": float(clf.intercept_[0])}


def predict(model: dict, rows: list[dict]) -> list[float]:
    return [mergemodel.predict_proba(model, r) for r in rows]


# -- baselines ------------------------------------------------------------------

def baseline_scores(fit_rows: list[dict], rows: list[dict]) -> dict[str, list[float]]:
    rate = sum(r["y"] for r in fit_rows) / len(fit_rows)
    per: dict[str, list[int]] = {}
    for r in fit_rows:
        per.setdefault(r["repo"], []).append(r["y"])
    own = {p: sum(v) / len(v) for p, v in per.items() if len(v) >= THIN}

    def b2(r):
        mr = r.get("repo_prior_merge_rate")
        if not mergemodel._present(mr):
            return 0.5  # score.py's neutral friendliness for a project with fewer than 5 measured PRs
        return (0.45 * mr + 0.25 * _scale(r.get("repo_prior_median_days_to_merge"), good=2, bad=60)) / 0.70

    return {
        "B0": [rate] * len(rows),
        "B1": [own.get(r["repo"], rate) for r in rows],
        "B1_prior": [r["repo_prior_merge_rate"] if mergemodel._present(r.get("repo_prior_merge_rate")) else rate
                     for r in rows],
        "B2": [b2(r) for r in rows],
    }


# -- evaluation -------------------------------------------------------------

def _auc(y, s):
    try:
        return metrics.roc_auc(y, s)
    except ValueError:
        return None


def evaluate(y: list[int], scores: dict[str, list[float]]) -> dict:
    out = {}
    for name, s in scores.items():
        auc = _auc(y, s)
        ci = metrics.bootstrap_auc_ci(y, s, n_resamples=N_RESAMPLES, seed=SEED) if auc is not None else None
        out[name] = {"auc": auc, "auc_ci": list(ci) if ci else None, "brier": metrics.brier_score(y, s)}
    return out


def _choose_c(train_rows: list[dict], val_rows: list[dict]) -> tuple[float, list[dict]]:
    """Select C on `val_rows` from models fit on `train_rows` only."""
    spec_tr = spec_from(train_rows)
    yv = [r["y"] for r in val_rows]
    tuning = []
    for C in C_GRID:
        m = as_model(spec_tr, fit(spec_tr, train_rows, C))
        pv = predict(m, val_rows)
        tuning.append({"C": C, "val_auc": _auc(yv, pv), "val_brier": metrics.brier_score(yv, pv)})
    if any(t["val_auc"] is not None for t in tuning):
        best_c = max(tuning, key=lambda t: (t["val_auc"] if t["val_auc"] is not None else -1.0, -t["C"]))["C"]
    else:
        best_c = min(tuning, key=lambda t: (t["val_brier"], t["C"]))["C"]
    return best_c, tuning


def lopo(rows: list[dict], default_c: float = 0.01) -> dict:
    """Fit on every other project (choosing C inside each fold without seeing the held-out project),
    predict the held-out one, and compute AUC over all pooled predictions."""
    y, model_s, base_s, per_project = [], [], {k: [] for k in BASELINES}, {}
    for project in sorted({r["repo"] for r in rows}):
        held = [r for r in rows if r["repo"] == project]
        rest = [r for r in rows if r["repo"] != project]
        if len({r["y"] for r in rest}) < 2:
            continue
        rest_sorted = sorted(rest, key=order_key)
        split = _boundary(rest_sorted, round(len(rest_sorted) * 0.75))
        inner_tr, inner_val = rest_sorted[:split], rest_sorted[split:]
        if inner_tr and inner_val and len({r["y"] for r in inner_tr}) >= 2:
            fold_c, _ = _choose_c(inner_tr, inner_val)
        else:
            fold_c = default_c
        spec = spec_from(rest)
        m = as_model(spec, fit(spec, rest, fold_c))
        p = predict(m, held)
        b = baseline_scores(rest, held)
        y += [r["y"] for r in held]
        model_s += p
        for k in BASELINES:
            base_s[k] += b[k]
        hy = [r["y"] for r in held]
        per_project[project] = {"n": len(held), "pos": sum(hy), "C": fold_c, "model_auc": _auc(hy, p),
                                **{f"{k}_auc": _auc(hy, b[k]) for k in BASELINES}}
    out = {"n": len(y), "pos": sum(y),
           "model_auc": _auc(y, model_s) if y else None,
           "model_brier": metrics.brier_score(y, model_s) if y else None,
           **{f"{k}_auc": (_auc(y, base_s[k]) if y else None) for k in BASELINES}}
    with_both = [v for v in per_project.values() if v["model_auc"] is not None]
    out["projects"] = len(per_project)
    out["projects_with_both_outcomes"] = len(with_both)
    if with_both:
        out["mean_project_model_auc"] = statistics.mean(v["model_auc"] for v in with_both)
        for k in BASELINES:
            vals = [v[f"{k}_auc"] for v in with_both if v[f"{k}_auc"] is not None]
            out[f"mean_project_{k}_auc"] = statistics.mean(vals) if vals else None
    return out


def decide(n_test: int, pos_test: int, model_auc: float | None, best_auc: float | None,
           diff_ci_low: float | None, model_brier: float | None, best_brier: float | None,
           lopo_auc: float | None, best_lopo_auc: float | None) -> tuple[bool, list[str]]:
    """True only if every rule holds; the reasons list each check and whether it passed."""
    checks = [
        (n_test >= 150 and pos_test >= 30 and n_test - pos_test >= 30,
         f"test rows {n_test} >= 150, merged {pos_test} >= 30, not merged {n_test - pos_test} >= 30"),
        (model_auc is not None and best_auc is not None and model_auc >= best_auc + 0.03,
         f"test AUC {_f(model_auc)} >= best baseline {_f(best_auc)} + 0.03"),
        (diff_ci_low is not None and diff_ci_low > 0,
         f"95% bootstrap lower bound of (model - best baseline) AUC {_f(diff_ci_low)} > 0"),
        (model_brier is not None and best_brier is not None and model_brier <= best_brier,
         f"test Brier {_f(model_brier)} <= best baseline Brier {_f(best_brier)}"),
        (lopo_auc is not None and best_lopo_auc is not None and lopo_auc > best_lopo_auc,
         f"leave-one-project-out AUC {_f(lopo_auc)} > best baseline {_f(best_lopo_auc)}"),
    ]
    return all(ok for ok, _ in checks), [("PASS: " if ok else "FAIL: ") + why for ok, why in checks]


def _f(v, digits: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


# -- the whole run ----------------------------------------------------------

def run(rows: list[dict], meta: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    train, val, test = time_split(rows)

    def span(rs):
        return {"n": len(rs), "pos": sum(r["y"] for r in rs),
                "first_created": rs[0]["created_at"] if rs else "",
                "last_created": rs[-1]["created_at"] if rs else ""}

    yv = [r["y"] for r in val]
    y = [r["y"] for r in test]
    if not train or not val or not test or len({r["y"] for r in train}) < 2:
        spec = spec_from(train or rows) if (train or rows) else {
            "features": [], "numeric": [], "means": {}, "scales": {},
            "categories": {}, "imputations": {}, "project_defaults": {},
        }
        reason = ("FAIL: training split needs both merged and unmerged rows"
                  if train and val and test else "FAIL: too few rows to form train, validation and test splits")
        empty_m = {"auc": None, "auc_ci": None, "brier": None}
        return {
            "version": VERSION,
            "trained_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "data_cut": meta.get("cut"),
            "horizon_days": meta.get("horizon_days", 90),
            "n_train": len(train), "n_val": len(val), "n_test": len(test),
            "pos_train": sum(r["y"] for r in train), "pos_val": sum(yv), "pos_test": sum(y),
            "splits": {"train": span(train), "val": span(val), "test": span(test)},
            "fit_rows": "train only (60% split; C chosen on val)",
            "C": C_GRID[0], "tuning": [],
            **{k: spec[k] for k in ("features", "numeric", "means", "scales", "categories", "imputations",
                                    "project_defaults")},
            "weights": [0.0] * len(spec["features"]), "intercept": 0.0,
            "metrics": {"test": {"model": empty_m, "diff_vs_best_baseline_ci": None,
                                 "best_baseline": "B0", "best_brier_baseline": "B0", "ece": None},
                        "lopo": {"n": 0, "pos": 0, "model_auc": None, "model_brier": None,
                                 "projects": 0, "projects_with_both_outcomes": 0,
                                 **{f"{k}_auc": None for k in BASELINES}}},
            "baselines": {k: {"description": BASELINES[k], **empty_m} for k in BASELINES},
            "calibration": [],
            "per_project_ranges": meta.get("per_project", {}),
            "dataset": {k: meta.get(k) for k in ("rows_in_file", "rows_outside_eligible", "rows_too_recent",
                                                 "rows_stale_open", "n", "pos", "projects")},
            "beats_baseline": False,
            "decision_reasons": [reason],
        }

    best_c, tuning = _choose_c(train, val)
    spec = spec_from(train)
    model = as_model(spec, fit(spec, train, best_c))
    p = predict(model, test)
    base = baseline_scores(train, test)
    res = evaluate(y, {"model": p, **base})
    base_aucs = {k: res[k]["auc"] for k in BASELINES if res[k]["auc"] is not None}
    best = max(base_aucs, key=base_aucs.get) if base_aucs else "B0"
    diff_ci = metrics.bootstrap_auc_ci(y, p, base[best], n_resamples=N_RESAMPLES, seed=SEED) \
        if res["model"]["auc"] is not None else None
    best_brier_name = min(BASELINES, key=lambda k: res[k]["brier"])
    table = metrics.calibration_table(y, p, 10)
    ece = metrics.expected_calibration_error(table)
    lo = lopo(rows, default_c=best_c)
    lopo_base = {k: lo[f"{k}_auc"] for k in BASELINES if lo[f"{k}_auc"] is not None}
    best_lopo = max(lopo_base.values()) if lopo_base else None
    ok, reasons = decide(len(test), sum(y), res["model"]["auc"], res[best]["auc"],
                         diff_ci[0] if diff_ci else None, res["model"]["brier"], res[best_brier_name]["brier"],
                         lo["model_auc"], best_lopo)

    out = {
        "version": VERSION,
        "trained_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data_cut": meta.get("cut"),
        "horizon_days": meta.get("horizon_days", 90),
        "n_train": len(train), "n_val": len(val), "n_test": len(test),
        "pos_train": sum(r["y"] for r in train), "pos_val": sum(yv), "pos_test": sum(y),
        "splits": {"train": span(train), "val": span(val), "test": span(test)},
        "fit_rows": "train only (60% split; C chosen on val)",
        "C": best_c, "tuning": tuning,
        **{k: spec[k] for k in ("features", "numeric", "means", "scales", "categories", "imputations",
                                "project_defaults")},
        "weights": model["weights"], "intercept": model["intercept"],
        "metrics": {"test": {"model": res["model"], "diff_vs_best_baseline_ci": list(diff_ci) if diff_ci else None,
                             "best_baseline": best, "best_brier_baseline": best_brier_name, "ece": ece},
                    "lopo": lo},
        "baselines": {k: {"description": BASELINES[k], **res[k]} for k in BASELINES},
        "calibration": table,
        "per_project_ranges": meta.get("per_project", {}),
        "dataset": {k: meta.get(k) for k in ("rows_in_file", "rows_outside_eligible", "rows_too_recent",
                                             "rows_stale_open", "n", "pos", "projects")},
        "beats_baseline": ok,
        "decision_reasons": reasons,
    }
    return out


# -- the write-up -------------------------------------------------------------

FEATURES_DOC = [
    ("author_prior_prs", "`author_prior_prs_in_sample`", "Earlier PRs by the same person in the project's sample. Returning contributors know the ropes."),
    ("log_title_len, log_body_len", "`title_len`, `body_len`", "log(1 + length). An empty description is a common reason for a quiet close. As of collection (see the leakage rules)."),
    ("body_has_issue_ref", "`body_has_issue_ref`", "The description has `#123`, or a closing keyword (fixes/closes/resolves) with an issue link: work the project asked for."),
    ("weekend, hour_sin, hour_cos", "`created_weekday`, `created_hour_utc`", "When it was opened (UTC), as a weekend flag and a 24-hour cycle."),
    ("log_repo_prior_n, repo_prior_thin", "`repo_prior_n`", "How many outside PRs the project closed before this one was opened, and whether that is fewer than 5."),
    ("repo_prior_merge_rate", "`repo_prior_merge_rate`", "Share of those that were merged (empty below 5, then filled from the fit rows)."),
    ("log_repo_prior_median_days", "`repo_prior_median_days_to_merge`", "Median days to merge among them (empty below 5)."),
    ("stars_log10", "`stars_log10`", "log10(1 + stars) at collection time."),
    ("cla, has_contributing", "`cla`, `has_contributing`", "The project asks for a CLA; it has a CONTRIBUTING file (at collection time)."),
    ("language=…, owner_type=…, ai_policy=…", "`language`, `owner_type`, `ai_policy`", f"One-hot; levels with fewer than {MIN_LEVEL} fit rows, missing and unseen values go to `other`."),
]
# stored in pr_outcomes.jsonl but kept out of the model, and why
NOT_INPUTS = [
    ("`author_assoc`", "GitHub reports the association as it is now. A first-timer whose PR was merged is a "
     "CONTRIBUTOR afterwards, so the field partly records the outcome (in the initial exploratory run on real data, "
     "CONTRIBUTOR rows were merged about 55% of the time vs 11% for NONE)."),
    ("`draft`", "The current draft state. A PR has to leave draft before it can be merged, so a merged PR is never a "
     "draft (1 of 221 draft rows in the initial exploratory run was merged)."),
    ("`title_wip_marker`", "The current title. WIP markers are taken out before merging, the same problem as `draft`."),
    ("`from_fork`", "Always true for an outside PR, so it carries no information."),
]


def project_label(repo: str, public: set[str], anon: dict[str, str]) -> str:
    """Projects listed in targets.toml are named (that file is public already); others are numbered."""
    if repo.lower() in public:
        return f"`{repo}`"
    if repo not in anon:
        anon[repo] = f"discovered project {len(anon) + 1:02d}"
    return anon[repo]


def write_doc(m: dict, path: Path, public: set[str]) -> None:
    t = m["metrics"]["test"]
    lo = m["metrics"]["lopo"]
    sp = m["splits"]
    ok = m["beats_baseline"]

    def ci(c):
        return f"{c[0]:.3f} to {c[1]:.3f}" if c else "n/a"

    L = []
    w = L.append
    w("# Merge-prediction model")
    w("")
    w("This page is regenerated by `scripts/train_merge_model.py` from the model it trains. "
      "The numbers below are from the last run.")
    w("")
    w(f"**Verdict: {'the model beats the baselines' if ok else 'the model does not beat the baselines, so the scout does not use it'}.** "
      f"`use_merge_model` stays `false` in targets.toml{'' if ok else '; even if it is switched on, the scorer refuses a model file whose `beats_baseline` is false'}.")
    w("")
    w("## The question")
    w("")
    w(f"If an occasional outside contributor opens a pull request in this project, how likely is it to be merged "
      f"within {m['horizon_days']:g} days? The scout's `friendliness` score answers that with a hand-weighted mix of "
      "merge rate, time to merge, first-reply time and activity. This model checks whether a logistic regression "
      "trained on the scout's own pull request data does better, and only feeds the ranking if it clearly does.")
    w("")
    w("## Features")
    w("")
    w("Every input is known when the pull request is opened, or describes the project. Logins, names, URLs, labels, "
      "comments, reviews and text are never stored: `scout/prdata.py` keeps a fixed list of fields per PR in "
      "`pr_outcomes.jsonl`, and titles and bodies become lengths and two yes/no flags.")
    w("")
    w("| Model input | From | Why |")
    w("| --- | --- | --- |")
    for a, b, c in FEATURES_DOC:
        w(f"| {a} | {b} | {c} |")
    w("")
    w("Numbers are standardized with the fit rows' mean and standard deviation. Stored but **not** model inputs:")
    w("")
    w("| Field | Why it is left out |")
    w("| --- | --- |")
    for a, b in NOT_INPUTS:
        w(f"| {a} | {b} |")
    w("")
    w("## Leakage rules")
    w("")
    w(f"- **Label.** `y = 1` if the PR was merged within {m['horizon_days']:g} days of being opened; closed without "
      "merging, merged later, or still open counts as 0.")
    w(f"- **Cut.** The cut is the newest collection time ({(m['data_cut'] or '')[:16].replace('T', ' ')} UTC). PRs opened within "
      f"{m['horizon_days']:g} days before it are left out, because their outcome isn't known yet. A PR closed after the "
      "cut counts as open at the cut.")
    w("- **Project record.** `repo_prior_*` is recomputed by `scripts/build_merge_dataset.py` from the same project's "
      "PRs closed strictly before this PR was opened. Changing a later PR's outcome cannot change an earlier row "
      "(tested). With fewer than 5 such PRs the rate and median are left empty and filled from the fit rows only.")
    w("- **Fills, levels and scaling** come from the rows being fit, never from the test split; the same holds inside "
      "each leave-one-project-out fold.")
    w("- **Static fields leak a little.** `stars_log10`, `language`, `owner_type`, `ai_policy`, `cla` and "
      "`has_contributing` are as of collection, not as of the PR. A project that added an AI policy or a CLA after a "
      "wave of rejected PRs, or that grew its stars, shows today's value on old rows. These change slowly, but they "
      "are one place the future reaches back.")
    w("- **PR fields are as of collection too.** The list API returns a PR as it is now. Titles, descriptions and "
      "flags can be edited during review. The fields that move with the outcome (`author_assoc`, `draft`, "
      "`title_wip_marker`) are kept out of the model (see above); title length, description length and the issue "
      "reference remain inputs and may carry a little of the same leak.")
    w("")
    w("## The sample and its biases")
    w("")
    w("- Rows come from what the nightly measurement already lists: each project's last 300 closed PRs **by update "
      "time** and its 100 newest open PRs. In a busy project an old PR is in that list only if something touched it "
      "lately, usually a late close, so old rows from busy projects would be mostly failures. To avoid that, a PR "
      "gets its first row only if it was opened after the point from which the lists hold every PR "
      "(`prdata.complete_since`). For busy projects that point is days or weeks back, so they contribute no rows "
      "until the nightly runs have collected 90 days and more; quiet projects reach years back. The dataset leans "
      "toward quieter projects for now.")
    w("- PRs that left the 100-newest-open window keep the last row seen; if an open PR's last `collected_at` is "
      f"earlier than {m['horizon_days']:g} days after it was opened, its horizon outcome is unknown and the dataset "
      "builder excludes it instead of labelling it 0.")
    w("- Optional PR size features (`additions`, `deletions`, `changed_files`) are not collected yet because they "
      "require one extra API request per pull request; rows without size are valid and the pipeline can add them later "
      "under a per-run budget.")
    w("- **Outside contributor** uses the scout's `is_outside` heuristic: from a fork, not a bot, author association "
      "NONE / FIRST_TIMER / FIRST_TIME_CONTRIBUTOR, or CONTRIBUTOR with fewer than 3 PRs in the sample. Staff with "
      "private org membership who work from forks and open few PRs still slip in; prolific outsiders are dropped. "
      "The 3-PR count uses the whole sample, including PRs opened later.")
    w("- Projects that merge outside GitHub (`merges_elsewhere`) and archived ones are excluded. The projects are "
      "the ones the scout watches (large, company-backed, mostly C, C++ and Python), not GitHub at large.")
    w("")
    ds = m["dataset"]
    stale_note = f", {ds['rows_stale_open']} stale open excluded" if ds.get("rows_stale_open") else ""
    w(f"{ds['rows_in_file']} rows in the file, {ds['rows_outside_eligible']} outside rows in eligible projects, "
      f"{ds['rows_too_recent']} left out as too recent{stale_note}, **{ds['n']} in the dataset ({ds['pos']} merged in time) from "
      f"{ds['projects']} projects**.")
    w("")
    w("### Per-project date ranges")
    w("")
    w("Projects listed in `targets.toml` are named; others the scout found itself are numbered.")
    w("")
    w("| Project | First opened | Last opened | Rows | Merged in time |")
    w("| --- | --- | --- | ---: | ---: |")
    anon: dict[str, str] = {}
    for repo, p in sorted(m["per_project_ranges"].items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        w(f"| {project_label(repo, public, anon)} | {p['first_created'][:10]} | {p['last_created'][:10]} | {p['n']} | {p['pos']} |")
    w("")
    w("## Split")
    w("")
    w("Rows are sorted by (created_at, repo, number). The oldest 60% train, the next 20% validation, the newest 20% "
      "test. A boundary never falls between two rows opened at the same second, so every test row was opened after "
      "every train row.")
    w("")
    w("| Split | Rows | Merged in time | First opened | Last opened |")
    w("| --- | ---: | ---: | --- | --- |")
    for name in ("train", "val", "test"):
        s = sp[name]
        w(f"| {name} | {s['n']} | {s['pos']} | {s['first_created'][:10]} | {s['last_created'][:10]} |")
    w("")
    w(f"C (inverse L2 strength) was chosen on validation AUC from {', '.join(f'{c:g}' for c in C_GRID)}: "
      f"**C = {m['C']:g}**. The test model and baselines B0/B1 are fit on the training split (60%) and scored on test "
      "once; each leave-one-project-out fold chooses C on an inner time split of the other projects so no held-out "
      "project influences tuning. Gradient boosting was not pursued because logistic regression did not leave clear "
      "headroom over the baselines on this sample size.")
    w("")
    w("## Baselines")
    w("")
    w("All scored on the same test rows and in the same leave-one-project-out folds.")
    w("")
    for k, v in m["baselines"].items():
        w(f"- **{k}**: {v['description']}.")
    w("")
    w(f"Note that `B1_prior` and `B2` use raw historical merge rates over PRs closed before the target PR opened "
      f"(any-time merges), while the target label `y` counts merges within {m['horizon_days']:g} days. "
      "B2 leaves out friendliness's first-reply (0.20) and activity (0.10) parts: neither can be rebuilt as of the "
      "day an old PR was opened without new API calls, so the weights are renormalized over the 0.70 that remains.")
    w("")
    w("## Results")
    w("")
    w(f"Test split: {m['n_test']} rows, {m['pos_test']} merged in time. AUC intervals are 95% bootstrap intervals "
      f"({N_RESAMPLES} resamples, seed {SEED}).")
    w("")
    w("| Scorer | Test AUC | 95% CI | Test Brier | Leave-one-project-out AUC |")
    w("| --- | ---: | --- | ---: | ---: |")
    w(f"| model | {_f(t['model']['auc'])} | {ci(t['model']['auc_ci'])} | {_f(t['model']['brier'])} | {_f(lo['model_auc'])} |")
    for k, v in m["baselines"].items():
        w(f"| {k} | {_f(v['auc'])} | {ci(v['auc_ci'])} | {_f(v['brier'])} | {_f(lo.get(f'{k}_auc'))} |")
    w("")
    w(f"Best baseline on test AUC: **{t['best_baseline']}**. Model minus {t['best_baseline']}: "
      f"{_f((t['model']['auc'] or 0) - (m['baselines'][t['best_baseline']]['auc'] or 0))} AUC, "
      f"95% CI {ci(t['diff_vs_best_baseline_ci'])}. Lowest baseline Brier: {t['best_brier_baseline']}.")
    w("")
    w(f"Leave-one-project-out pooled {lo['n']} predictions over {lo['projects']} projects. Averaged per project "
      f"(the {lo.get('projects_with_both_outcomes', 0)} with both outcomes): model {_f(lo.get('mean_project_model_auc'))}, "
      + ", ".join(f"{k} {_f(lo.get(f'mean_project_{k}_auc'))}" for k in m["baselines"]) + ".")
    w("")
    w("B0 and B1 turn into one constant per held-out project (the other projects' rate), so their pooled AUC "
      "only says that projects with a low rate elsewhere do well here; read B1_prior and B2 for this check.")
    w("")
    w("### Calibration (test split)")
    w("")
    w(f"Ten equal-count bins by predicted probability. Expected calibration error (row-weighted mean gap): "
      f"**{_f(t['ece'])}**.")
    w("")
    w("| Bin | Rows | Mean predicted | Observed rate | Gap |")
    w("| ---: | ---: | ---: | ---: | ---: |")
    for b in m["calibration"]:
        w(f"| {b['bin']} | {b['n']} | {b['mean_pred']:.3f} | {b['observed_rate']:.3f} | {b['abs_gap']:.3f} |")
    w("")
    w("### Largest weights")
    w("")
    w("Per standard deviation for numbers, per level for categories.")
    w("")
    w("| Input | Weight |")
    w("| --- | ---: |")
    top = sorted(zip(m["features"], m["weights"]), key=lambda fw: -abs(fw[1]))[:12]
    for f, wt in top:
        w(f"| `{f}` | {wt:+.3f} |")
    w("")
    w("## Verdict")
    w("")
    w("The model is used only if all of these hold:")
    w("")
    for r in m["decision_reasons"]:
        w(f"- {r}")
    w("")
    if ok:
        w("All checks pass, so `beats_baseline` is true. The ranking still ignores the model until "
          "`use_merge_model = true` is set in targets.toml; then it takes 0.10 of friendliness's 0.40 weight.")
    else:
        w("At least one check fails, so `beats_baseline` is false and **the score is not used**: "
          "`scout/mergemodel.py` refuses to load the file, and `rank.score_issue` gives exactly the scores it gave "
          "before the model existed, whatever `use_merge_model` says. The data keeps accumulating every night; "
          "retrain when there is more of it.")
    w("")
    w("## What the model cannot know")
    w("")
    w("- Whether the change is any good, fits the project's plans, or duplicates another PR.")
    w("- Reviews, comments, CI results, labels: nothing that happens after the PR is opened is an input.")
    w("- Who the author is beyond their earlier PRs in the sample, including whether a maintainer asked for the PR.")
    w("- Whether the issue was claimed, assigned, or discussed first.")
    w("- How the project behaves outside the window the sample covers, and for projects that merge outside GitHub.")
    w("")
    w("## How to retrain")
    w("")
    w("```sh")
    w("# rows accumulate in <data>/pr_outcomes.jsonl every night; to fill it now (about 4 GET requests a project):")
    w("python3 scripts/backfill_pr_outcomes.py --state <data>/state.json --out <data>/pr_outcomes.jsonl")
    w("python3 scripts/build_merge_dataset.py --rows <data>/pr_outcomes.jsonl --state <data>/state.json \\")
    w("    --out /tmp/merge_dataset.jsonl --meta /tmp/merge_dataset_meta.json")
    w("pip install -r requirements-train.txt   # in a separate virtualenv; the scout itself never needs it")
    w("python3 scripts/train_merge_model.py --dataset /tmp/merge_dataset.jsonl --meta /tmp/merge_dataset_meta.json \\")
    w("    --out <data>/merge_model.json --doc docs/merge-model.md")
    w("# preview the ranking change on a copy of the data (needs a passing model):")
    w("python3 scripts/before_after_merge_model.py --data <copy of the data>")
    w("```")
    w("")
    w("A model file older than `merge_model_max_age_days` (default 90 in `targets.toml`) is ignored, so retrain at least "
      "that often if it is switched on.")
    w("")
    path.write_text("\n".join(L))


def public_projects() -> set[str]:
    from scout import config
    cfg = config.load()
    return {r.lower() for t in cfg.tiers for r in t.repos}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True, type=Path)
    ap.add_argument("--meta", type=Path, help="metadata from build_merge_dataset.py (default: next to --dataset)")
    ap.add_argument("--out", required=True, type=Path, help="merge_model.json to write")
    ap.add_argument("--doc", type=Path, help="docs/merge-model.md to write")
    args = ap.parse_args()
    meta_path = args.meta or args.dataset.with_name(args.dataset.stem + "_meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    m = run(load_rows(args.dataset), meta)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2) + "\n")
    tmp.replace(args.out)
    if args.doc:
        write_doc(m, args.doc, public_projects())
    t = m["metrics"]["test"]
    print(f"train {m['n_train']} ({m['pos_train']}), val {m['n_val']} ({m['pos_val']}), test {m['n_test']} ({m['pos_test']}); C={m['C']:g}")
    print(f"test AUC model {_f(t['model']['auc'])} " + " ".join(f"{k} {_f(v['auc'])}" for k, v in m["baselines"].items()))
    print(f"LOPO AUC model {_f(m['metrics']['lopo']['model_auc'])} "
          + " ".join(f"{k} {_f(m['metrics']['lopo'].get(f'{k}_auc'))}" for k in m["baselines"]))
    for r in m["decision_reasons"]:
        print(" ", r)
    print(f"beats_baseline: {m['beats_baseline']}  -> {args.out}" + (f", {args.doc}" if args.doc else ""))


if __name__ == "__main__":
    main()
