"""The merge-prediction model: per-PR rows, leakage rules, split, metrics, scoring and ranking (Issue #17)."""

import copy
import importlib.util
import json
import math
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout import config, mergemodel, metrics, policy, prdata, rank, score
from scout import __main__ as cli
from scout.github import GitHub
from test_scout import NOW, Fake, iso

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "merge_model_fixture.json").read_text())
CANARY_LOGIN = "canary-login-q7x"
CANARY_TITLE = "CANARYTITLE zebra"
CANARY_BODY = "CANARYBODY see http://evil.example/x and ping @maint-person"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = load_script("build_merge_dataset")
train = load_script("train_merge_model")


@pytest.fixture
def cfg():
    return config.load()


def pr(n, login, *, assoc="NONE", days_ago=10.0, closed_days_ago=None, merged=False, fork=True,
       kind="User", title="Fix a crash", body="Fixes #3", draft=False, repo="o/r"):
    head = {"repo": {"full_name": f"{login}/r" if fork else repo}}
    p = {"number": n, "user": {"login": login, "type": kind}, "author_association": assoc, "head": head,
         "created_at": iso(days_ago), "updated_at": iso(closed_days_ago if closed_days_ago is not None else days_ago),
         "title": title, "body": body, "draft": draft, "labels": [{"name": "bug"}], "comments": 4,
         "html_url": f"https://github.com/{repo}/pull/{n}"}
    if closed_days_ago is not None:
        p["closed_at"] = iso(closed_days_ago)
        p["merged_at"] = iso(closed_days_ago) if merged else None
    return p


def sample():
    closed = [
        pr(1, "alice", days_ago=40, closed_days_ago=38, merged=True),
        pr(2, CANARY_LOGIN, assoc="FIRST_TIME_CONTRIBUTOR", days_ago=30, closed_days_ago=29, merged=False,
           title=CANARY_TITLE, body=CANARY_BODY),
        pr(3, "alice", assoc="CONTRIBUTOR", days_ago=20, closed_days_ago=5, merged=True),
        pr(4, "dependabot[bot]", kind="Bot", days_ago=15, closed_days_ago=14, merged=True),
        pr(5, "staffer", assoc="MEMBER", days_ago=12, closed_days_ago=11, merged=True),
        pr(6, "insider", assoc="CONTRIBUTOR", fork=False, days_ago=11, closed_days_ago=10, merged=True),
        pr(7, "regular", assoc="CONTRIBUTOR", days_ago=9, closed_days_ago=8, merged=True),
        pr(8, "regular", assoc="CONTRIBUTOR", days_ago=8, closed_days_ago=7, merged=True),
    ]
    opened = [pr(9, "regular", assoc="CONTRIBUTOR", days_ago=2),
              pr(10, "bob", days_ago=3, title="[WIP] do not merge", body="", draft=True)]
    return closed, opened


META = {"stars": 999, "language": "C++", "owner_type": "Organization", "ai_policy": "none", "cla": True,
        "policy_files": [".github/CONTRIBUTING.md"]}


def make_rows(closed=None, opened=None):
    c, o = sample()
    closed, opened = closed or c, opened or o
    counts = score.author_counts(closed + opened)
    return prdata.rows("o/r", closed, opened, counts, NOW, repo_meta=META)


# -- core never needs scikit-learn or numpy ------------------------------------

def test_every_scout_module_imports_with_sklearn_and_numpy_blocked():
    code = """
import importlib, pkgutil, sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("sklearn", "numpy", "scipy"):
            raise ImportError("blocked: " + name)
        return None
sys.meta_path.insert(0, Block())
import scout
names = [m.name for m in pkgutil.iter_modules(scout.__path__)]
for n in names:
    importlib.import_module("scout." + n)
assert "mergemodel" in names and "prdata" in names and "metrics" in names
assert not any(m.split(".")[0] in ("sklearn", "numpy") for m in sys.modules)
print("ok", len(names))
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("ok")


def test_train_requirements_are_separate():
    reqs = (ROOT / "requirements-train.txt").read_text()
    assert "scikit-learn" in reqs and "numpy" in reqs
    for f in (ROOT / "scout").glob("*.py"):
        text = f.read_text()
        assert "import sklearn" not in text and "from sklearn" not in text and "import numpy" not in text, f


# -- per-PR rows ------------------------------------------------------------

def test_rows_have_exactly_the_fixed_fields_and_no_text():
    rows = make_rows()
    assert rows and all(tuple(r) == prdata.FIELDS for r in rows)
    text = json.dumps(rows)
    for canary in (CANARY_LOGIN, CANARY_TITLE, "CANARYBODY", "alice", "regular", "bob", "maint-person", "zebra"):
        assert canary not in text
    assert "@" not in text and "http" not in text


def test_bots_staff_branches_and_regulars_give_no_rows():
    rows = make_rows()
    # 4 bot, 5 member, 6 branch in the project repo, 7/8/9 by someone with 3 PRs in the sample
    assert sorted(r["number"] for r in rows) == [1, 2, 3, 10]


def test_row_values():
    by = {r["number"]: r for r in make_rows()}
    one, two, three, ten = by[1], by[2], by[3], by[10]
    assert one["merged"] and one["state_at_cut"] == "closed" and one["from_fork"]
    assert two["merged"] is False and two["author_assoc"] == "FIRST_TIME_CONTRIBUTOR"
    assert two["title_len"] == len(CANARY_TITLE) and two["body_len"] == len(CANARY_BODY)
    assert ten["state_at_cut"] == "open" and ten["closed_at"] is None and ten["merged"] is False
    assert ten["draft"] and ten["title_wip_marker"] and not ten["body_has_issue_ref"] and ten["body_len"] == 0
    assert one["body_has_issue_ref"] and not one["title_wip_marker"]
    assert three["author_prior_prs_in_sample"] == 1 and one["author_prior_prs_in_sample"] == 0
    created = score.parse_ts(one["created_at"])
    assert (one["created_weekday"], one["created_hour_utc"]) == (created.weekday(), created.hour)
    assert one["stars_log10"] == round(math.log10(1000), 4) and one["cla"] and one["has_contributing"]
    assert (one["language"], one["owner_type"], one["ai_policy"]) == ("C++", "Organization", "none")
    assert one["collected_at"] == "2026-09-26T00:00:00Z"


@pytest.mark.parametrize("body,ref", [
    ("Fixes #12", True), ("see #4 for context", True), ("closes https://github.com/o/r/issues/9", True),
    ("Resolved: https://github.com/o/r/issues/9", True), ("I fixed the typo", False), ("", False), (None, False)])
def test_issue_reference(body, ref):
    assert prdata.has_issue_ref(body) == ref


@pytest.mark.parametrize("title,wip", [("[WIP] add x", True), ("Draft: y", True), ("DO NOT MERGE z", True),
                                       ("do-not-merge", True), ("Wipe the cache", False), ("Add drafts", False)])
def test_wip_marker(title, wip):
    assert bool(prdata.WIP.search(title)) == wip


def test_project_record_uses_only_prs_closed_before_opening():
    by = {r["number"]: r for r in make_rows()}
    assert by[1]["repo_prior_n"] == 0 and by[1]["repo_prior_merge_rate"] is None
    # PR 2 opened 30 days ago: only PR 1 (closed 38 days ago) was closed by then
    assert (by[2]["repo_prior_n"], by[2]["repo_prior_merge_rate"]) == (1, 1.0)
    assert by[2]["repo_prior_median_days_to_merge"] == pytest.approx(2.0)
    # PR 3 opened 20 days ago: PRs 1 and 2 (outside ones only; bots and staff never count)
    assert (by[3]["repo_prior_n"], by[3]["repo_prior_merge_rate"]) == (2, 0.5)


def test_later_outcome_does_not_change_earlier_rows():
    closed, opened = sample()
    before = {r["number"]: r for r in make_rows(closed, opened)}
    flipped = copy.deepcopy(closed)
    flipped[2]["merged_at"] = None  # PR 3, opened after PRs 1 and 2
    after = {r["number"]: r for r in make_rows(flipped, opened)}
    for n in (1, 2):
        assert {k: v for k, v in before[n].items()} == after[n]
    assert after[3]["merged"] is False


def test_merge_dedupes_updates_and_leaves_no_tmp(tmp_path):
    path = tmp_path / prdata.FILE
    rows = make_rows()
    assert prdata.merge(path, rows) == len(rows)
    assert prdata.merge(path, rows) == len(rows)
    closed, opened = sample()
    opened[1].update(closed_at=iso(1), merged_at=iso(1), updated_at=iso(1))  # PR 10 merged since
    later = prdata.rows("o/r", closed + [opened[1]], opened[:1], score.author_counts(closed + opened), NOW,
                        repo_meta=META)
    assert prdata.merge(path, later) == len(rows)
    got = {r["number"]: r for r in prdata.read(path)}
    assert got[10]["merged"] and got[10]["state_at_cut"] == "closed"
    lines = path.read_text().splitlines()
    assert len(lines) == len({(json.loads(x)["repo"], json.loads(x)["number"]) for x in lines})
    assert [p.name for p in tmp_path.iterdir()] == [prdata.FILE]


def test_merge_drops_unknown_fields(tmp_path):
    path = tmp_path / prdata.FILE
    path.write_text(json.dumps({"repo": "o/r", "number": 1, "author_login": CANARY_LOGIN}) + "\nnot json\n")
    prdata.merge(path, [])
    assert CANARY_LOGIN not in path.read_text()
    assert list(json.loads(path.read_text())) == list(prdata.FIELDS)


def test_complete_since_and_add_since(tmp_path):
    closed = [pr(i, f"u{i}", days_ago=100 - i, closed_days_ago=50 - i / 10) for i in range(5)]
    opened = [pr(10 + i, f"v{i}", days_ago=20 - i) for i in range(3)]
    assert prdata.complete_since(closed, opened) is None  # neither list is full: it holds every PR
    since = prdata.complete_since(closed, opened, closed_cap=5, open_cap=100)
    assert since == score.parse_ts(closed[0]["updated_at"])  # the oldest update in the full closed list
    assert prdata.complete_since(closed, opened, closed_cap=5, open_cap=3) == score.parse_ts(opened[0]["created_at"])
    path = tmp_path / prdata.FILE
    rows = prdata.rows("o/r", closed, opened, score.author_counts(closed + opened), NOW, repo_meta=META)
    prdata.merge(path, rows, add_since=since)
    kept = {r["number"] for r in prdata.read(path)}
    assert kept == {10, 11, 12}  # the closed ones were opened before the sample is complete
    # a PR already in the file is updated even when it was opened before add_since
    old = dict(rows[0], repo="o/r", number=10, created_at=iso(300), merged=True)
    prdata.merge(path, [old], add_since=since)
    assert {r["number"]: r for r in prdata.read(path)}[10]["merged"]


def measure_routes():
    closed, opened = sample()
    return {"repos/o/r": {"stargazers_count": 999, "language": "C++", "pushed_at": iso(1),
                          "owner": {"type": "Organization"}},
            "repos/o/r/pulls": closed + opened}


def test_measure_writes_rows_with_zero_extra_api_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "fetch", lambda gh, repo: {"ai": "none", "evidence": "", "cla": True,
                                                           "files": ["CONTRIBUTING.md"]})
    plain, rec = Fake(measure_routes()), Fake(measure_routes())
    a = score.measure(GitHub(transport=plain), "o/r", now=NOW)
    b = score.measure(GitHub(transport=rec), "o/r", now=NOW, data_dir=tmp_path)
    assert len(plain.seen) == len(rec.seen) and plain.seen == rec.seen
    assert {k: v for k, v in a.items() if k != "measured_at"} == {k: v for k, v in b.items() if k != "measured_at"}
    text = (tmp_path / prdata.FILE).read_text()
    assert text and "@" not in text and "http" not in text
    for login in ("alice", CANARY_LOGIN, "bob", "staffer", "insider", "regular", "dependabot"):
        assert login not in text
    assert not list(tmp_path.glob("*.tmp"))
    assert not (Path.cwd() / "data" / "pr_outcomes.tmp").exists()


def test_get_passes_data_dir_and_defaults_to_none(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(score, "measure", lambda gh, repo, data_dir=None: seen.append(data_dir) or {"measured_at": 0})
    score.get(None, "o/r", {}, 7)
    score.get(None, "o/q", {}, 7, data_dir=tmp_path)
    assert seen == [None, tmp_path]


# -- dataset builder: leakage rules -------------------------------------------

CUT = datetime(2026, 11, 1, tzinfo=timezone.utc)


def ts(days_before_cut: float) -> str:
    return (CUT - timedelta(days=days_before_cut)).isoformat().replace("+00:00", "Z")


def drow(n, opened, closed=None, merged=False, repo="o/r", **kw):
    base = {f: None for f in prdata.FIELDS}
    base.update(repo=repo, number=n, created_at=ts(opened), closed_at=ts(closed) if closed is not None else None,
                merged=merged, state_at_cut="closed" if closed is not None else "open", collected_at=ts(0),
                author_assoc="NONE", author_prior_prs_in_sample=0, from_fork=True, draft=False, title_len=10,
                body_len=100, body_has_issue_ref=True, title_wip_marker=False, created_weekday=1,
                created_hour_utc=3, repo_prior_n=99, repo_prior_merge_rate=0.99,
                repo_prior_median_days_to_merge=1.0, stars_log10=3.0, language="C", owner_type="Organization",
                ai_policy="none", cla=False, has_contributing=True)
    base.update(kw)
    return base


def history(n=8, repo="o/r", start=400):
    """n PRs opened 10 days apart, each closed 2 days later, alternating merged/closed."""
    return [drow(i + 1, start - 10 * i, start - 10 * i - 2, merged=i % 2 == 0, repo=repo) for i in range(n)]


def test_label_rules_and_recent_rows_left_out():
    rows = [drow(1, 300, 290, merged=True),   # merged in 10 days -> 1
            drow(2, 300, 200, merged=True),   # merged after 100 days -> 0
            drow(3, 300, 295, merged=False),  # closed unmerged -> 0
            drow(4, 300),                     # still open after 300 days -> 0
            drow(5, 89, 80, merged=True),     # opened within 90 days of the cut -> left out
            drow(6, 91, 85, merged=True),     # 91 days -> kept
            drow(7, 200, -5, merged=True)]    # merged after the cut: unknown at the cut -> 0
    out, meta = build.build(rows, cut=CUT, horizon_days=90)
    y = {r["number"]: r["y"] for r in out}
    assert y == {1: 1, 2: 0, 3: 0, 4: 0, 6: 1, 7: 0}
    assert meta["rows_too_recent"] == 1
    assert all(k not in r for r in out for k in ("merged", "closed_at", "state_at_cut", "collected_at"))


def test_default_cut_is_the_newest_collection():
    rows = [drow(1, 300, 290, merged=True), drow(2, 50, 40, merged=True, collected_at=ts(-10))]
    _, meta = build.build(rows)
    assert meta["cut"] == ts(-10)


def test_only_outside_fork_rows_in_eligible_projects():
    rows = [drow(1, 300, 290), drow(2, 300, 290, author_assoc="MEMBER"), drow(3, 300, 290, from_fork=False),
            drow(4, 300, 290, repo="gone/r"), drow(5, 300, 290, repo="mirror/r")]
    state = {"repos": {"gone/r": {"archived": True}, "mirror/r": {"merges_elsewhere": True}, "o/r": {}}}
    out, meta = build.build(rows, state, cut=CUT)
    assert [r["number"] for r in out] == [1]
    assert meta["excluded_projects"] == ["gone/r", "mirror/r"]


def test_prior_features_use_only_earlier_closes_and_ignore_the_rows_own_values():
    rows = history(8)
    out, _ = build.build(rows, cut=CUT)
    by = {r["number"]: r for r in out}
    assert by[1]["repo_prior_n"] == 0  # 99 in the input is ignored: recomputed
    assert by[6]["repo_prior_n"] == 5 and by[6]["repo_prior_merge_rate"] == pytest.approx(3 / 5)
    assert by[6]["repo_prior_median_days_to_merge"] == pytest.approx(2.0)


def test_changing_a_later_outcome_leaves_earlier_rows_alone():
    rows = history(8)
    before = {r["number"]: r for r in build.build(rows, cut=CUT)[0]}
    later = copy.deepcopy(rows)
    later[7]["merged"] = not later[7]["merged"]
    later[6].update(closed_at=ts(250), merged=True)  # PR 7 now closed much later
    after = {r["number"]: r for r in build.build(later, cut=CUT)[0]}
    for n in range(1, 8):
        for k in ("repo_prior_n", "repo_prior_merge_rate", "repo_prior_median_days_to_merge"):
            assert before[n][k] == after[n][k], (n, k)


def test_thin_projects_keep_their_count_and_take_training_fills():
    rows = history(20, start=600)
    out, _ = build.build(rows, cut=CUT)
    by = {r["number"]: r for r in out}
    assert by[4]["repo_prior_n"] == 3
    assert by[4]["repo_prior_merge_rate"] is None and by[4]["repo_prior_median_days_to_merge"] is None
    tr, _, _ = train.time_split(out)
    spec = train.spec_from(tr)
    rates = [r["repo_prior_merge_rate"] for r in tr if r["repo_prior_merge_rate"] is not None]
    assert rates and spec["imputations"]["repo_prior_merge_rate"] == pytest.approx(statistics.median(rates))
    # the thin row takes the training fill
    assert mergemodel.numeric_value("repo_prior_merge_rate", by[4], spec["imputations"]) == \
        spec["imputations"]["repo_prior_merge_rate"]
    assert mergemodel.numeric_value("repo_prior_thin", by[4], spec["imputations"]) == 1.0


# -- split -------------------------------------------------------------------

def split_rows(n=50, ties=False):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        t = start + timedelta(days=(i // 5 if ties else i))
        rows.append({"repo": f"o/r{i % 3}", "number": i, "created_at": t.isoformat().replace("+00:00", "Z"),
                     "y": i % 2})
    return rows[::-1]


@pytest.mark.parametrize("ties", [False, True])
def test_every_test_row_opened_after_every_train_row(ties):
    tr, va, te = train.time_split(split_rows(ties=ties))
    assert tr and va and te and len(tr) + len(va) + len(te) == 50
    assert max(r["created_at"] for r in tr) < min(r["created_at"] for r in va)
    assert max(r["created_at"] for r in va) < min(r["created_at"] for r in te)


def test_split_and_bootstrap_are_deterministic():
    a = train.time_split(split_rows(ties=True))
    b = train.time_split(list(reversed(split_rows(ties=True))))
    assert a == b
    y = [r["y"] for r in split_rows()]
    s = [((i * 37) % 11) / 10 for i in range(len(y))]
    base = [((i * 13) % 7) / 6 for i in range(len(y))]
    ci1 = metrics.bootstrap_auc_ci(y, s, n_resamples=300, seed=42)
    assert ci1 == metrics.bootstrap_auc_ci(y, s, n_resamples=300, seed=42)
    assert ci1 != metrics.bootstrap_auc_ci(y, s, n_resamples=300, seed=7)
    d = metrics.bootstrap_auc_ci(y, s, base, n_resamples=300, seed=42)
    assert d == metrics.bootstrap_auc_ci(y, s, base, n_resamples=300, seed=42) and d[0] <= d[1]


# -- metrics -----------------------------------------------------------------

def test_auc_hand_values():
    assert metrics.roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert metrics.roc_auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5
    assert metrics.roc_auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75
    assert metrics.roc_auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == 0.0
    assert metrics.roc_auc([0, 1, 1], [0.3, 0.3, 0.9]) == 0.75  # a tie counts half
    with pytest.raises(ValueError):
        metrics.roc_auc([1, 1], [0.2, 0.3])


def test_brier_and_calibration_by_hand():
    assert metrics.brier_score([1, 0, 1, 0], [0.9, 0.1, 0.6, 0.4]) == pytest.approx((0.01 + 0.01 + 0.16 + 0.16) / 4)
    y = [0, 0, 1, 0, 1, 1]
    p = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
    t = metrics.calibration_table(y, p, n_bins=3)
    assert [b["n"] for b in t] == [2, 2, 2]
    assert [b["mean_pred"] for b in t] == pytest.approx([0.15, 0.45, 0.8])
    assert [b["observed_rate"] for b in t] == pytest.approx([0.0, 0.5, 1.0])
    assert [b["abs_gap"] for b in t] == pytest.approx([0.15, 0.05, 0.2])
    assert metrics.expected_calibration_error(t) == pytest.approx((0.15 + 0.05 + 0.2) / 3)
    assert metrics.expected_calibration_error(y, p, 3) == pytest.approx((0.15 + 0.05 + 0.2) / 3)


def test_calibration_bins_cover_every_row():
    y = [i % 3 == 0 for i in range(103)]
    p = [(i * 7 % 103) / 103 for i in range(103)]
    t = metrics.calibration_table(y, p)
    assert len(t) == 10 and sum(b["n"] for b in t) == 103
    assert [b["n"] for b in t] == [11, 11, 11] + [10] * 7
    assert sum(b["n"] for b in metrics.calibration_table([1, 0, 1], [0.2, 0.5, 0.9])) == 3


# -- the decision rule -------------------------------------------------------

def test_decision_rule_needs_every_check():
    good = dict(n_test=200, pos_test=60, model_auc=0.75, best_auc=0.70, diff_ci_low=0.01, model_brier=0.18,
                best_brier=0.19, lopo_auc=0.72, best_lopo_auc=0.70)
    ok, reasons = train.decide(**good)
    assert ok and len(reasons) == 5 and all(r.startswith("PASS") for r in reasons)
    for change in ({"n_test": 149}, {"pos_test": 29}, {"pos_test": 171}, {"model_auc": 0.7299},
                   {"diff_ci_low": 0.0}, {"model_brier": 0.191}, {"lopo_auc": 0.70}, {"model_auc": None}):
        ok, reasons = train.decide(**{**good, **change})
        assert not ok and any(r.startswith("FAIL") for r in reasons), change


# -- scoring -----------------------------------------------------------------

def fixture_model(**kw):
    return {**copy.deepcopy(FIXTURE["model"]), **kw}


def test_predict_proba_matches_stored_sklearn_probabilities():
    model = fixture_model()
    got = [mergemodel.predict_proba(model, r) for r in FIXTURE["rows"]]
    assert got == pytest.approx(FIXTURE["expected"], abs=1e-12)


def test_predict_proba_matches_live_sklearn():
    pytest.importorskip("sklearn")
    pytest.importorskip("numpy")
    old = train.MIN_LEVEL
    train.MIN_LEVEL = FIXTURE["min_level"]
    try:
        rows = FIXTURE["rows"]
        spec = train.spec_from(rows)
        clf = train.fit(spec, rows, FIXTURE["C"])
        model = train.as_model(spec, clf)
        live = clf.predict_proba(train.matrix(spec, rows))[:, 1]
        mine = [mergemodel.predict_proba(model, r) for r in rows]
        assert mine == pytest.approx(list(live), abs=1e-12)
        assert mine == pytest.approx(FIXTURE["expected"], abs=1e-6)
    finally:
        train.MIN_LEVEL = old


def test_sigmoid_does_not_overflow():
    assert mergemodel.sigmoid(1000) == 1.0 and mergemodel.sigmoid(-1000) == 0.0
    assert mergemodel.sigmoid(0) == 0.5


def write_model(tmp_path, **kw):
    path = tmp_path / "merge_model.json"
    path.write_text(json.dumps(fixture_model(**kw)))
    return path


TRAINED = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_load_accepts_a_fresh_passing_model(tmp_path):
    assert mergemodel.load(write_model(tmp_path), now=TRAINED + timedelta(days=10)) is not None


@pytest.mark.parametrize("change", [
    {"beats_baseline": False}, {"beats_baseline": "true"}, {"weights": [0.1, 0.2]}, {"intercept": "x"},
    {"trained_at": "not a date"}, {"trained_at": 12345}, {"version": []}, {"version": 2}, {"version": True},
    {"features": ["nope"]}, {"numeric": ["unknown_input"]},
    {"numeric": [], "categories": {}, "features": [], "weights": [], "means": {}, "scales": {}},
    {"scales": {}}, {"scales": "bad"}, {"scales": True}, {"means": [1]}, {"means": 123},
    {"imputations": {**FIXTURE["model"]["imputations"], "repo_prior_merge_rate": "bad"}},
    {"imputations": {"stars_log10": 3.0}},
    {"project_defaults": {**FIXTURE["model"]["project_defaults"], "created_hour_utc": 10 ** 400}},
    {"project_defaults": {**FIXTURE["model"]["project_defaults"], "created_hour_utc": 25}},
    {"project_defaults": {**FIXTURE["model"]["project_defaults"], "created_weekday": 9}},
    {"project_defaults": {**FIXTURE["model"]["project_defaults"], "body_has_issue_ref": "yes"}},
    {"categories": {"language": []}}, {"intercept": 10 ** 400},
    {"weights": [10 ** 400] + [0.0] * (len(FIXTURE["model"]["features"]) - 1)}])
def test_load_refuses_bad_models(tmp_path, change):
    assert mergemodel.load(write_model(tmp_path, **change), now=TRAINED + timedelta(days=10)) is None


def test_load_refuses_bad_max_age_days(tmp_path):
    path = write_model(tmp_path)
    for bad in ("90", None, -1, True, float("nan")):
        assert mergemodel.load(path, now=TRAINED + timedelta(days=10), max_age_days=bad) is None


def test_load_refuses_missing_stale_and_malformed(tmp_path):
    assert mergemodel.load(tmp_path / "missing.json") is None
    path = write_model(tmp_path)
    assert mergemodel.load(path, now=TRAINED + timedelta(days=91)) is None
    assert mergemodel.load(path, now=TRAINED + timedelta(days=91), max_age_days=120) is not None
    path.write_text("{not json")
    assert mergemodel.load(path, now=TRAINED) is None
    path.write_text("[]")
    assert mergemodel.load(path, now=TRAINED) is None
    model = fixture_model()
    del model["imputations"]
    path.write_text(json.dumps(model))
    assert mergemodel.load(path, now=TRAINED) is None


def test_project_score_uses_project_rows_or_measurement():
    model = fixture_model()
    info = {"stars": 5000, "language": "C++", "owner_type": "Organization", "ai_policy": "none", "cla": False,
            "policy_files": ["CONTRIBUTING.md"], "merge_rate": 0.8, "median_days_to_merge": 2.0,
            "outside_prs_sampled": 40}
    p = mergemodel.project_score(model, "o/r", info)
    assert 0 < p < 1
    worse = mergemodel.project_score(model, "o/r", {**info, "merge_rate": 0.1})
    assert worse < p  # the fixture learned that a higher prior merge rate helps
    assert mergemodel.project_score(model, "o/r", {**info, "merges_elsewhere": True}) is None
    assert mergemodel.project_score(model, "o/r", {**info, "archived": True}) is None
    rows = [dict(r, repo="o/r", state_at_cut="closed", merged=True,
                 closed_at=r["created_at"]) for r in FIXTURE["rows"][:10]]
    from_rows = mergemodel.project_score(model, "o/r", info, rows, now=NOW)
    assert 0 < from_rows < 1


def test_load_project_rows(tmp_path):
    assert mergemodel.load_project_rows(tmp_path) == {}
    prdata.merge(tmp_path / prdata.FILE, make_rows())
    (tmp_path / prdata.FILE).write_text((tmp_path / prdata.FILE).read_text() + "garbage\n")
    got = mergemodel.load_project_rows(tmp_path)
    assert list(got) == ["o/r"] and len(got["o/r"]) == 4


# -- ranking -----------------------------------------------------------------

def golden_issue():
    return {"key": "duckdb/duckdb#1", "repo": "duckdb/duckdb", "number": 1, "title": "t", "created_at": iso(2),
            "label_groups": ["confirmed_bug"], "labels": [], "comments": 2, "assignees": []}


GOLDEN_REPO = {"friendliness": 0.7, "language": "C++", "ai_policy": "none", "stars": 5000,
               "owner_type": "Organization"}
GOLDEN = {"score": 86.0, "parts": {"friendliness": 0.7, "freshness": 1.0, "label_fit": 0.9, "language_fit": 1.0},
          "multipliers": {"tier": 1.0, "ai_policy": 1.0, "busy_thread": 1.0, "home": 1.0, "size": 1.0,
                          "company": 1.0}, "tier": "Systems & databases"}


def test_default_ranking_is_unchanged(cfg):
    assert cfg.settings["use_merge_model"] is False  # shipped off
    assert cfg.settings["merge_model_max_age_days"] == 90
    assert rank.score_issue(golden_issue(), GOLDEN_REPO, cfg, {}, now=NOW) == GOLDEN
    # off: a probability passed in or carried by the repo changes nothing
    assert rank.score_issue(golden_issue(), {**GOLDEN_REPO, "merge_prob": 0.1}, cfg, {}, now=NOW) == GOLDEN
    assert rank.score_issue(golden_issue(), GOLDEN_REPO, cfg, {}, now=NOW, merge_prob=0.1) == GOLDEN


@pytest.mark.parametrize("bad", [None, -0.1, 1.5, float("nan"), "0.5", True])
def test_switched_on_without_a_valid_probability_is_unchanged(cfg, bad):
    cfg.settings["use_merge_model"] = True
    assert rank.score_issue(golden_issue(), GOLDEN_REPO, cfg, {}, now=NOW, merge_prob=bad) == GOLDEN


def test_switched_on_moves_a_tenth_of_the_weight(cfg):
    cfg.settings["use_merge_model"] = True
    got = rank.score_issue(golden_issue(), GOLDEN_REPO, cfg, {}, now=NOW, merge_prob=0.3)
    assert got["parts"]["merge_prob"] == 0.3
    # base 0.40*0.7 + ... becomes 0.30*0.7 + 0.10*0.3 + ...: 0.10 * (0.3 - 0.7) lower, times the multipliers (all 1)
    assert got["score"] == pytest.approx(GOLDEN["score"] + 100 * 0.10 * (0.3 - 0.7), abs=0.06)
    from_repo = rank.score_issue(golden_issue(), {**GOLDEN_REPO, "merge_prob": 0.3}, cfg, {}, now=NOW)
    assert from_repo == got
    same = rank.score_issue(golden_issue(), GOLDEN_REPO, cfg, {}, now=NOW, merge_prob=0.7)
    assert same["score"] == GOLDEN["score"]


def test_run_attaches_probabilities_only_with_a_passing_model(cfg, tmp_path):
    repos = {"o/r": {"friendliness": 0.7, "merge_rate": 0.8, "median_days_to_merge": 2.0,
                     "outside_prs_sampled": 40, "stars": 5000, "language": "C++"},
             "skip/me": {"archived": True, "stars": 5}}
    cached = {"o/r": repos["o/r"], "old/one": {"friendliness": 0.5, "merge_prob": 0.9}}
    # off: nothing, and old probabilities are cleared
    assert cli.apply_merge_model(cfg, tmp_path, repos, cached) == 0
    assert all("merge_prob" not in r for r in list(repos.values()) + list(cached.values()))
    cfg.settings["use_merge_model"] = True
    trained = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    for kw in ({"beats_baseline": False}, {"trained_at": "2020-01-01T00:00:00Z"}):
        write_model(tmp_path, **{"trained_at": trained, **kw})
        assert cli.apply_merge_model(cfg, tmp_path, repos, cached) == 0
        assert "merge_prob" not in repos["o/r"]
        assert rank.score_issue(golden_issue(), {**GOLDEN_REPO, **repos["o/r"], "friendliness": 0.7}, cfg, {},
                                now=NOW) == GOLDEN
    write_model(tmp_path, trained_at=trained)
    assert cli.apply_merge_model(cfg, tmp_path, repos, cached) == 2
    assert 0 <= repos["o/r"]["merge_prob"] <= 1 and "merge_prob" not in repos["skip/me"]
    assert 0 <= cached["old/one"]["merge_prob"] <= 1
    info = cli.repo_info(repos["o/r"])
    assert info["merge_prob"] == repos["o/r"]["merge_prob"]
    assert "merge_prob" not in cli.repo_info({"friendliness": 0.5})
    scored = rank.score_issue(golden_issue(), {**GOLDEN_REPO, "merge_prob": repos["o/r"]["merge_prob"]}, cfg, {},
                              now=NOW)
    assert "merge_prob" in scored["parts"]


def test_render_hides_cached_merge_prob_when_setting_is_off(cfg, tmp_path):
    from scout import render, state as statemod
    st = statemod.load(tmp_path)
    st["repos"]["o/r"] = {"repo": "o/r", "friendliness": 0.7, "merge_prob": 0.65}
    cfg.settings["use_merge_model"] = False
    off = render.build_payload(cfg, tmp_path, st, now=NOW)
    assert "merge_prob" not in off["projects"]["repos"][0]

    # Setting on, but no passing model file: still hidden.
    cfg.settings["use_merge_model"] = True
    no_model = render.build_payload(cfg, tmp_path, st, now=NOW)
    assert "merge_prob" not in no_model["projects"]["repos"][0]

    # Setting on, with a failing model: still hidden.
    write_model(tmp_path, trained_at=(NOW - timedelta(days=1)).isoformat(), beats_baseline=False)
    failing = render.build_payload(cfg, tmp_path, st, now=NOW)
    assert "merge_prob" not in failing["projects"]["repos"][0]

    # Setting on, with a fresh passing model: exposed.
    write_model(tmp_path, trained_at=(NOW - timedelta(days=1)).isoformat(), beats_baseline=True)
    on = render.build_payload(cfg, tmp_path, st, now=NOW)
    assert on["projects"]["repos"][0]["merge_prob"] == 0.65


def test_project_score_falls_back_with_thin_rows_and_skips_bad_timestamps():
    model = fixture_model()
    info = {"stars": 5000, "language": "C++", "owner_type": "Organization", "ai_policy": "none", "cla": False,
            "policy_files": ["CONTRIBUTING.md"], "merge_rate": 0.9, "median_days_to_merge": 2.0,
            "outside_prs_sampled": 40}
    no_rows = mergemodel.project_score(model, "o/r", info, [], now=NOW)
    two_rows = [
        {**FIXTURE["rows"][0], "repo": "o/r", "state_at_cut": "closed", "merged": False, "closed_at": iso(5)},
        {**FIXTURE["rows"][1], "repo": "o/r", "state_at_cut": "closed", "merged": False, "closed_at": "not-a-date"},
    ]
    with_two = mergemodel.project_score(model, "o/r", info, two_rows, now=NOW)
    assert with_two == pytest.approx(no_rows, abs=1e-9)


def test_prdata_sanitizes_labels_preserves_opening_fields_and_score_survives_write_error(tmp_path, monkeypatch):
    # Static label sanitization blocks @ and http
    bad_meta = {"stars": 500, "language": "user@example.com", "owner_type": "https://evil.example",
                "ai_policy": "none", "cla": False, "policy_files": []}
    out = prdata.static_features(bad_meta)
    assert out["language"] is None and out["owner_type"] is None and out["ai_policy"] == "none"

    # Incremental updates preserve first-seen opening fields across open->closed AND closed->closed updates
    p = tmp_path / prdata.FILE
    open_row = make_rows()[0]
    open_row.update(state_at_cut="open", closed_at=None, merged=False, title_len=12, body_len=50,
                    draft=True, author_assoc="NONE")
    prdata.merge(p, [open_row])
    closed_row = {**open_row, "state_at_cut": "closed", "closed_at": iso(1), "merged": True,
                  "title_len": 99, "body_len": 999, "draft": False, "author_assoc": "CONTRIBUTOR"}
    prdata.merge(p, [closed_row])
    prdata.merge(p, [{**closed_row, "title_len": 150, "body_len": 1500}])
    saved = prdata.read(p)[0]
    assert saved["merged"] is True and saved["state_at_cut"] == "closed"
    assert saved["title_len"] == 12 and saved["body_len"] == 50 and saved["draft"] is True and saved["author_assoc"] == "NONE"

    # measure() survives prdata.merge errors
    monkeypatch.setattr(prdata, "merge", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(policy, "fetch", lambda gh, repo: {"ai": "none", "evidence": "", "cla": False, "files": []})
    routes = {"repos/o/r": {"pushed_at": iso(1), "stars": 100}, "repos/o/r/pulls": []}
    res = score.measure(GitHub(transport=Fake(routes)), "o/r", now=NOW, data_dir=tmp_path)
    assert res["repo"] == "o/r"


def test_builder_excludes_stale_open_snapshots():
    build = load_script("build_merge_dataset")
    cut = NOW
    stale_open = {**make_rows()[0], "number": 1, "created_at": iso(120), "closed_at": None, "merged": False,
                  "state_at_cut": "open", "collected_at": iso(110)}
    confirmed_open = {**make_rows()[0], "number": 2, "created_at": iso(120), "closed_at": None, "merged": False,
                      "state_at_cut": "open", "collected_at": iso(5)}
    ds, meta = build.build([stale_open, confirmed_open], state={"repos": {}}, cut=cut, horizon_days=90)
    assert [r["number"] for r in ds] == [2]
    assert ds[0]["y"] == 0 and meta["rows_stale_open"] == 1


def test_trainer_handles_sparse_and_single_project_datasets():
    pytest.importorskip("sklearn")
    train = load_script("train_merge_model")
    single_class = [{**r, "y": 1, "created_at": iso(200 - i)} for i, r in enumerate(FIXTURE["rows"][:15])]
    res = train.run(single_class, {"cut": iso(0), "horizon_days": 90}, now=NOW)
    assert res["beats_baseline"] is False and res["decision_reasons"]

    one_project = [{**r, "repo": "o/only", "y": i % 2, "created_at": iso(200 - i)}
                   for i, r in enumerate(FIXTURE["rows"][:20])]
    res2 = train.run(one_project, {"cut": iso(0), "horizon_days": 90}, now=NOW)
    assert res2["beats_baseline"] is False and res2["metrics"]["lopo"]["model_auc"] is None


def test_dashboard_shows_merge_chance_only_when_present():
    html = (ROOT / "dashboard" / "template.html").read_text()
    assert "r.merge_prob != null" in html and "Merge chance" in html
