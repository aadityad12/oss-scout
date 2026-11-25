"""Score with the trained merge-prediction model, standard library only.

The model is a logistic regression trained offline by scripts/train_merge_model.py (which needs
scikit-learn; nothing in scout/ ever imports it). Its file, data/merge_model.json, holds everything
needed to score here: the feature list, the train-split means and scales, the weights, the
categories and the values used for missing inputs.

`load` returns None unless the file is readable, complete, younger than `max_age_days`, and the
model beat the baselines on held-out data (`beats_baseline` is true). The ranking only uses the
model when `use_merge_model = true` in targets.toml AND `load` returned a model.

The feature transforms live here and the training script imports them, so training and scoring
compute exactly the same inputs.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

from .prdata import FILE, prior_stats, static_features
from .score import parse_ts

# Numeric inputs, each derived from one row of pr_outcomes.jsonl (see `numeric_value`).
# `draft`, `title_wip_marker` and `author_assoc` are stored but are NOT inputs: GitHub reports them
# as they are now, not as they were when the PR was opened, and they move with the outcome (a PR
# must leave draft before it is merged, WIP is dropped from titles before merging, and a first-timer
# whose PR was merged becomes a CONTRIBUTOR). On the real data they made the model look far better
# than it is.
NUMERIC = (
    "author_prior_prs", "log_title_len", "log_body_len", "body_has_issue_ref",
    "weekend", "hour_sin", "hour_cos",
    "log_repo_prior_n", "repo_prior_thin", "repo_prior_merge_rate", "log_repo_prior_median_days",
    "stars_log10", "cla", "has_contributing",
)
CATEGORICAL = ("language", "owner_type", "ai_policy")
LEAKY = ("draft", "title_wip_marker", "author_assoc")  # stored, never model inputs
OTHER = "other"  # the level for a missing or unseen category
IMPUTED = ("repo_prior_merge_rate", "repo_prior_median_days_to_merge", "stars_log10")
# per-PR fields a "typical outside PR" needs (the rest come from the project)
PR_FIELDS = ("author_assoc", "author_prior_prs_in_sample", "draft", "title_len", "body_len",
             "body_has_issue_ref", "title_wip_marker", "created_weekday", "created_hour_utc")
THIN = 5  # fewer earlier closed PRs than this and the project's prior rate counts as unknown
REQUIRED = ("version", "trained_at", "features", "numeric", "means", "scales", "weights", "intercept",
            "categories", "imputations", "project_defaults", "beats_baseline")


def _num(v, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return default


def _present(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v))
    except OverflowError:
        return False


def numeric_value(name: str, row: dict, imputations: dict) -> float:
    """One numeric input from a pr_outcomes row; missing project values take `imputations`."""
    def imputed(field: str) -> float:
        v = row.get(field)
        return float(v) if _present(v) else _num(imputations.get(field))

    hour = _num(row.get("created_hour_utc"))
    if name == "author_prior_prs":
        return _num(row.get("author_prior_prs_in_sample"))
    if name == "log_title_len":
        return math.log1p(max(0.0, _num(row.get("title_len"))))
    if name == "log_body_len":
        return math.log1p(max(0.0, _num(row.get("body_len"))))
    if name in ("draft", "body_has_issue_ref", "title_wip_marker", "cla", "has_contributing"):
        return 1.0 if row.get(name) is True else 0.0
    if name == "weekend":
        return 1.0 if _num(row.get("created_weekday")) >= 5 else 0.0
    if name == "hour_sin":
        return math.sin(2 * math.pi * hour / 24)
    if name == "hour_cos":
        return math.cos(2 * math.pi * hour / 24)
    if name == "log_repo_prior_n":
        return math.log1p(max(0.0, _num(row.get("repo_prior_n"))))
    if name == "repo_prior_thin":
        return 0.0 if _present(row.get("repo_prior_merge_rate")) else 1.0
    if name == "repo_prior_merge_rate":
        return imputed("repo_prior_merge_rate")
    if name == "log_repo_prior_median_days":
        return math.log1p(max(0.0, imputed("repo_prior_median_days_to_merge")))
    if name == "stars_log10":
        return imputed("stars_log10")
    raise KeyError(name)


def category(row: dict, field: str, levels: list[str]) -> str:
    v = row.get(field)
    return v if isinstance(v, str) and v in levels else OTHER


def feature_names(numeric: list[str], categories: dict[str, list[str]]) -> list[str]:
    return list(numeric) + [f"{c}={lvl}" for c in CATEGORICAL if c in categories for lvl in categories[c]]


def raw_vector(model: dict, row: dict) -> list[float]:
    """Inputs before standardization: numeric values, then one-hot categories."""
    x = [numeric_value(n, row, model["imputations"]) for n in model["numeric"]]
    for c in CATEGORICAL:
        if c in model["categories"]:
            levels = model["categories"][c]
            got = category(row, c, levels)
            x += [1.0 if got == lvl else 0.0 for lvl in levels]
    return x


def vector(model: dict, row: dict) -> list[float]:
    """Model inputs: numeric values standardized with the training means and scales."""
    x = raw_vector(model, row)
    for i, n in enumerate(model["numeric"]):
        x[i] = (x[i] - model["means"][n]) / model["scales"][n]
    return x


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def predict_proba(model: dict, feature_row: dict) -> float:
    """Chance the PR is merged within the horizon: 1 / (1 + exp(-(intercept + w . x)))."""
    x = vector(model, feature_row)
    return sigmoid(model["intercept"] + sum(w * v for w, v in zip(model["weights"], x)))


def _valid(model) -> bool:
    try:
        if not isinstance(model, dict) or any(k not in model for k in REQUIRED):
            return False
        if not isinstance(model["trained_at"], str):
            return False
        numeric, cats = model["numeric"], model["categories"]
        if not isinstance(numeric, list) or any(n not in NUMERIC for n in numeric):
            return False
        if not isinstance(cats, dict) or any(c not in CATEGORICAL or not isinstance(v, list) or not v
                                             or not all(isinstance(lvl, str) for lvl in v) for c, v in cats.items()):
            return False
        if model["features"] != feature_names(numeric, cats):
            return False
        w = model["weights"]
        if not isinstance(w, list) or len(w) != len(model["features"]) or not all(_present(v) for v in w):
            return False
        if not _present(model["intercept"]):
            return False
        means, scales = model["means"], model["scales"]
        if not isinstance(means, dict) or not isinstance(scales, dict):
            return False
        for n in numeric:
            if not _present(means.get(n)) or not _present(scales.get(n)):
                return False
            if float(scales[n]) <= 0:
                return False
        if not isinstance(model["imputations"], dict) or not isinstance(model["project_defaults"], dict):
            return False
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def load(path: Path, now: datetime | None = None, max_age_days: float = 90) -> dict | None:
    """The model in `path`, or None if it is missing, unreadable, incomplete, older than
    `max_age_days`, or did not beat the baselines."""
    if not _present(max_age_days) or float(max_age_days) < 0:
        return None
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _valid(model) or model.get("beats_baseline") is not True:
        return None
    try:
        trained = parse_ts(model["trained_at"])
    except (AttributeError, TypeError, ValueError):
        return None
    if trained is None:
        return None
    if trained.tzinfo is None:
        trained = trained.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = (now - trained).total_seconds() / 86400
    if age > float(max_age_days) or age < -1:
        return None
    return model


def load_project_rows(data_dir: Path) -> dict[str, list[dict]]:
    """Rows of data_dir/pr_outcomes.jsonl grouped by repo ({} if the file is missing)."""
    from .prdata import read
    out: dict[str, list[dict]] = {}
    try:
        rows = read(Path(data_dir) / FILE)
    except OSError:
        return {}
    for r in rows:
        out.setdefault(r["repo"], []).append(r)
    return out


def _typical(rows: list[dict], defaults: dict) -> dict:
    """Median numbers and most common flags/categories of a project's PRs; defaults when too few."""
    if len(rows) < THIN:
        return {f: defaults.get(f) for f in PR_FIELDS}
    out = {}
    for f in PR_FIELDS:
        vals = [r.get(f) for r in rows if r.get(f) is not None]
        if not vals:
            out[f] = defaults.get(f)
        elif all(_present(v) for v in vals) and f not in ("created_weekday", "created_hour_utc"):
            out[f] = statistics.median(vals)
        else:
            counts: dict = {}
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
            out[f] = max(sorted(counts, key=str), key=counts.get)
    return out


def project_score(model: dict, repo: str, repo_info: dict, pr_rows: list[dict] | None = None,
                  now: datetime | None = None) -> float | None:
    """Chance a typical outside PR opened in `repo` now is merged within the horizon.

    The typical PR takes the median/most common per-PR values of the project's rows in
    pr_outcomes.jsonl (the model's defaults when there are fewer than 5), and the project's record
    from those rows' closed PRs (else from `repo_info`'s measurement). None for projects the model
    was not trained on: archived ones and ones that merge outside GitHub."""
    repo_info = repo_info or {}
    if repo_info.get("archived") or repo_info.get("merges_elsewhere"):
        return None
    now = now or datetime.now(timezone.utc)
    rows = [r for r in pr_rows or [] if r.get("repo") == repo]
    row = _typical(rows, model.get("project_defaults") or {})
    hist = []
    for r in rows:
        try:
            created, closed = parse_ts(r.get("created_at")), parse_ts(r.get("closed_at"))
        except (AttributeError, TypeError, ValueError):
            continue
        if r.get("state_at_cut") == "closed" and created and closed:
            hist.append((created, closed, r.get("merged") is True))
    n, rate, med = prior_stats(hist, now) if hist else (0, None, None)
    if n < THIN:
        # Avoid dropping a full state.json measurement when only 1-4 closed rows have accumulated yet.
        sampled = int(_num(repo_info.get("outside_prs_sampled")))
        if sampled >= THIN:
            n = sampled
            rate, med = repo_info.get("merge_rate"), repo_info.get("median_days_to_merge")
        else:
            rate = med = None
    row.update(repo_prior_n=n, repo_prior_merge_rate=rate if _present(rate) else None,
               repo_prior_median_days_to_merge=med if _present(med) else None,
               **static_features(repo_info))
    try:
        p = predict_proba(model, row)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return p if math.isfinite(p) else None
