"""Evaluation metrics for the merge-prediction model, standard library only.

`roc_auc` raises ValueError when `y_true` holds only one class (the AUC is undefined then);
`bootstrap_auc_ci` skips resamples like that.
"""

from __future__ import annotations

import random


def _check(y_true, y_score) -> tuple[list[int], list[float]]:
    y = [int(v) for v in y_true]
    s = [float(v) for v in y_score]
    if len(y) != len(s):
        raise ValueError("y_true and y_score differ in length")
    if any(v not in (0, 1) for v in y):
        raise ValueError("y_true must hold 0 and 1 only")
    return y, s


def _auc(y: list[int], s: list[float]) -> float:
    """Mann-Whitney U: the chance a random positive scores above a random negative, ties count half."""
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        raise ValueError("AUC needs both classes")
    order = sorted(range(len(s)), key=s.__getitem__)
    rank_sum, i = 0.0, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and s[order[j + 1]] == s[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # ranks i+1..j+1 share their average
        rank_sum += avg_rank * sum(y[order[k]] for k in range(i, j + 1))
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def roc_auc(y_true, y_score) -> float:
    """Area under the ROC curve: 1.0 is a perfect ranking, 0.5 no better than chance.
    Raises ValueError if `y_true` has only one class."""
    return _auc(*_check(y_true, y_score))


def brier_score(y_true, y_prob) -> float:
    """Mean squared difference between the predicted probability and the 0/1 outcome."""
    y, p = _check(y_true, y_prob)
    if not y:
        raise ValueError("no rows")
    return sum((pi - yi) ** 2 for yi, pi in zip(y, p)) / len(y)


def calibration_table(y_true, y_prob, n_bins: int = 10) -> list[dict]:
    """Equal-count bins by predicted probability (sizes differ by at most one, larger bins first).

    Each bin: {"bin", "n", "mean_pred", "observed_rate", "abs_gap"}. With fewer rows than bins,
    there are only as many bins as rows."""
    y, p = _check(y_true, y_prob)
    order = sorted(range(len(p)), key=lambda i: (p[i], i))
    k = min(n_bins, len(order))
    out, start = [], 0
    for b in range(k):
        size = len(order) // k + (1 if b < len(order) % k else 0)
        idx = order[start:start + size]
        start += size
        mean_pred = sum(p[i] for i in idx) / size
        observed = sum(y[i] for i in idx) / size
        out.append({"bin": b + 1, "n": size, "mean_pred": mean_pred, "observed_rate": observed,
                    "abs_gap": abs(observed - mean_pred)})
    return out


def expected_calibration_error(table_or_y, y_prob=None, n_bins: int = 10) -> float:
    """Row-weighted mean of |observed rate - mean prediction| over the bins.

    Pass a `calibration_table` result, or `(y_true, y_prob)` to build one with `n_bins` bins."""
    table = table_or_y if y_prob is None else calibration_table(table_or_y, y_prob, n_bins)
    total = sum(b["n"] for b in table)
    if not total:
        raise ValueError("no rows")
    return sum(b["n"] * b["abs_gap"] for b in table) / total


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def bootstrap_auc_ci(y_true, y_score, baseline_score=None, n_resamples: int = 2000,
                     seed: int = 42, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap (1 - alpha) interval for AUC(y_score), or, with `baseline_score`, for
    AUC(y_score) - AUC(baseline_score) on the same resampled rows. Same inputs and seed give the
    same interval. Resamples holding a single class are skipped (and not redrawn)."""
    y, s = _check(y_true, y_score)
    b = _check(y_true, baseline_score)[1] if baseline_score is not None else None
    rng = random.Random(seed)
    n = len(y)
    stats = []
    for _ in range(n_resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        yy = [y[i] for i in idx]
        pos = sum(yy)
        if pos == 0 or pos == n:
            continue
        v = _auc(yy, [s[i] for i in idx])
        if b is not None:
            v -= _auc(yy, [b[i] for i in idx])
        stats.append(v)
    if not stats:
        raise ValueError("every resample held a single class")
    stats.sort()
    return _percentile(stats, alpha / 2), _percentile(stats, 1 - alpha / 2)
