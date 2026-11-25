"""The scan: abandoned PRs, the timeline check cap, big/company-backed ranking, field hints."""

import pytest

from scout import config, filters, rank, score
from scout import __main__ as cli
from scout.github import GitHub
from test_scout import NOW, Fake, iso


@pytest.fixture
def cfg():
    return config.load()


def pr_ref(days_since_update=None, url="https://x/pull/9", state="open"):
    issue = {"pull_request": {}, "state": state, "html_url": url}
    if days_since_update is not None:
        issue["updated_at"] = iso(days_since_update)
    return {"event": "cross-referenced", "source": {"issue": issue}}


def activity(timeline, **kw):
    gh = GitHub(transport=Fake({"repos/o/r/issues/1/timeline": timeline}))
    return filters.activity(gh, "o/r", 1, claim_window_days=21, now=NOW, **kw)


# -- stale linked PRs -------------------------------------------------------

def test_recent_linked_pr_blocks():
    act = activity([pr_ref(days_since_update=5)])
    assert act["linked_open_prs"] == ["https://x/pull/9"]
    assert act["stale_linked_prs"] == []


def test_abandoned_linked_pr_does_not_block_and_is_reported():
    act = activity([pr_ref(days_since_update=200)])
    assert act["linked_open_prs"] == []
    assert act["stale_linked_prs"] == [{"url": "https://x/pull/9", "updated_at": iso(200)}]


def test_stale_cutoff_is_a_setting():
    assert activity([pr_ref(days_since_update=40)])["linked_open_prs"]
    act = activity([pr_ref(days_since_update=40)], stale_pr_days=30)
    assert act["linked_open_prs"] == [] and len(act["stale_linked_prs"]) == 1


def test_linked_pr_with_unknown_update_time_still_blocks():
    assert activity([pr_ref()])["linked_open_prs"] == ["https://x/pull/9"]


def test_closed_pr_mentions_are_ignored_and_stale_prs_listed_once():
    act = activity([pr_ref(days_since_update=200, state="closed", url="https://x/pull/1"),
                    pr_ref(days_since_update=200), pr_ref(days_since_update=200)])
    assert act["linked_open_prs"] == []
    assert [p["url"] for p in act["stale_linked_prs"]] == ["https://x/pull/9"]


# -- connected / disconnected -----------------------------------------------

def test_disconnected_pr_no_longer_blocks():
    named = {"source": {"issue": {"html_url": "https://x/pull/7"}}}
    act = activity([{"event": "connected", **named}, {"event": "disconnected", **named}])
    assert act["linked_open_prs"] == []


def test_connected_without_identifier_counts_connects_minus_disconnects():
    assert activity([{"event": "connected"}])["linked_open_prs"]
    assert activity([{"event": "connected"}, {"event": "disconnected"}])["linked_open_prs"] == []
    two_one = [{"event": "connected"}, {"event": "connected"}, {"event": "disconnected"}]
    assert len(activity(two_one)["linked_open_prs"]) == 1
    assert activity([{"event": "disconnected"}])["linked_open_prs"] == []


def test_disconnecting_one_named_pr_keeps_the_other():
    def ev(kind, n):
        return {"event": kind, "source": {"issue": {"html_url": f"https://x/pull/{n}"}}}
    act = activity([ev("connected", 1), ev("connected", 2), ev("disconnected", 1)])
    assert act["linked_open_prs"] == ["https://x/pull/2"]


def test_same_pr_connected_and_cross_referenced_counts_once():
    act = activity([pr_ref(days_since_update=1, url="https://x/pull/7"),
                    {"event": "connected", "source": {"issue": {"html_url": "https://x/pull/7"}}}])
    assert act["linked_open_prs"] == ["https://x/pull/7"]


# -- organization-only discovery, size and company ---------------------------

def test_require_org_drops_personal_repos_but_not_tier_repos(cfg):
    assert cfg.discovery["require_org"] and cfg.discovery["min_stars"] >= 3000
    found = issue_for("a/b")
    big = {"stars": 9000, "owner_type": "User"}
    assert rank.eligible(found, big, cfg, {}, now=NOW) == "not an organization"
    assert rank.eligible(found, {"stars": 9000, "owner_type": "Organization"}, cfg, {}, now=NOW) is None
    assert rank.eligible(found, {"stars": 9000}, cfg, {}, now=NOW) == "not an organization"
    # too few stars is reported first, and a curated tier repo is never dropped for its owner
    assert rank.eligible(found, {"stars": 10, "owner_type": "User"}, cfg, {}, now=NOW) == "too few stars"
    assert rank.eligible(issue_for("duckdb/duckdb"), big, cfg, {}, now=NOW) is None


def test_require_org_can_be_switched_off(cfg):
    cfg.discovery["require_org"] = False
    assert rank.eligible(issue_for("a/b"), {"stars": 9000, "owner_type": "User"}, cfg, {}, now=NOW) is None


@pytest.mark.parametrize("stars,mult", [
    (None, 1.0), (0, 0.75), (1999, 0.75), (2000, 0.9), (4999, 0.9), (5000, 1.0), (19999, 1.0), (20000, 1.1),
    (150000, 1.1)])
def test_size_multiplier(cfg, stars, mult):
    repo = {"friendliness": 0.7, "stars": stars}
    got = rank.score_issue(issue_for("some/discovered"), repo, cfg, {}, now=NOW)  # tier repos are never marked down
    assert got["multipliers"]["size"] == mult


def test_company_multiplier_and_its_effect_on_the_score(cfg):
    base = {"friendliness": 0.7, "stars": 9000}
    org = rank.score_issue(issue_for("a/b"), {**base, "owner_type": "Organization"}, cfg, {}, now=NOW)
    user = rank.score_issue(issue_for("a/b"), {**base, "owner_type": "User"}, cfg, {}, now=NOW)
    unknown = rank.score_issue(issue_for("a/b"), base, cfg, {}, now=NOW)
    assert (org["multipliers"]["company"], user["multipliers"]["company"]) == (1.0, 0.8)
    assert unknown["multipliers"]["company"] == 1.0
    assert user["score"] == pytest.approx(org["score"] * 0.8, abs=0.2)


def issue_for(repo):
    return {"key": f"{repo}#1", "repo": repo, "number": 1, "title": "t", "created_at": iso(2),
            "label_groups": ["beginner"], "labels": ["good first issue"], "comments": 1, "assignees": []}


# -- topics -----------------------------------------------------------------

def test_measure_stores_topics_and_tolerates_none(monkeypatch):
    from scout import policy
    monkeypatch.setattr(policy, "fetch", lambda gh, repo: {"ai": "none", "evidence": "", "cla": False, "files": []})
    for topics, expected in ((["rtos", "embedded"], ["rtos", "embedded"]), (None, [])):
        routes = {"repos/o/r": {"pushed_at": iso(1), "topics": topics}, "repos/o/r/pulls": []}
        assert score.measure(GitHub(transport=Fake(routes)), "o/r", now=NOW)["topics"] == expected


# -- candidates: field hint, project intro, the check cap -------------------

def pick(*args):
    return cli.pick_candidates(*args, now=NOW)


def ranked(repos_and_numbers):
    out = []
    for i, (repo, n) in enumerate(repos_and_numbers):
        out.append({**issue_for(repo), "number": n, "key": f"{repo}#{n}", "score": 90 - i})
    return out


def timelines(*numbers_by_repo, blocked=()):
    routes = {}
    for repo, n in numbers_by_repo:
        routes[f"repos/{repo}/issues/{n}/timeline"] = [pr_ref(days_since_update=3)] if (repo, n) in blocked else []
    return GitHub(transport=Fake(routes))


def test_candidate_carries_field_hint_project_intro_and_stale_prs(cfg):
    items = ranked([("zephyrproject-rtos/zephyr", 1), ("tenstorrent/tt-metal", 2), ("new/thing", 3)])
    gh = GitHub(transport=Fake({
        "repos/zephyrproject-rtos/zephyr/issues/1/timeline": [],
        "repos/tenstorrent/tt-metal/issues/2/timeline": [pr_ref(days_since_update=400)],
        "repos/new/thing/issues/3/timeline": []}))
    repos = {
        "zephyrproject-rtos/zephyr": {"stars": 17000, "description": "Primary Zephyr RTOS", "homepage": "https://z.dev",
                                      "topics": ["rtos"], "owner_type": "Organization"},
        "tenstorrent/tt-metal": {"stars": 5000, "description": "TT kernels"},  # measured before topics existed
        "new/thing": {"stars": 9000, "description": "Compiler and runtime for tensors", "topics": ["compiler"]},
    }
    final, unchecked = pick(gh, cfg, items, repos, {})
    zephyr, tt, new = final
    assert unchecked == 0
    assert zephyr["field_hint"] == {"id": "embedded", "label": "Embedded & firmware", "source": "tier"}
    assert tt["field_hint"]["id"] == "ai-ml" and tt["field_hint"]["source"] == "repo"  # per-repo override
    assert new["field_hint"]["source"] == "topics" and new["field_hint"]["id"]  # guessed, no tier
    info = zephyr["repo_info"]
    assert (info["description"], info["homepage"], info["topics"]) == ("Primary Zephyr RTOS", "https://z.dev", ["rtos"])
    assert tt["repo_info"]["topics"] == [] and tt["repo_info"]["homepage"] is None
    assert zephyr["stale_linked_prs"] == []
    assert [p["url"] for p in tt["stale_linked_prs"]] == ["https://x/pull/9"]
    assert "stale_linked_prs" not in tt["activity"]


def test_stale_pr_days_setting_reaches_the_timeline_check(cfg):
    items = ranked([("duckdb/duckdb", 1)])
    gh = GitHub(transport=Fake({"repos/duckdb/duckdb/issues/1/timeline": [pr_ref(days_since_update=40)]}))
    final, _ = pick(gh, cfg, items, {"duckdb/duckdb": {}}, {})
    assert final == []  # 40 days old is within the default 90
    cfg.settings["stale_pr_days"] = 30
    final, _ = pick(gh, cfg, items, {"duckdb/duckdb": {}}, {})
    assert len(final) == 1 and final[0]["stale_linked_prs"]


def test_max_checked_defaults_to_twelve_per_wanted_candidate(cfg):
    cfg.settings.update(max_candidates=2, max_per_repo_wide=100, max_per_repo=100)
    cfg.settings.pop("max_checked", None)
    pairs = [("duckdb/duckdb", n) for n in range(1, 40)]
    gh = timelines(*pairs, blocked=set(pairs))
    dropped: dict = {}
    final, unchecked = pick(gh, cfg, ranked(pairs), {"duckdb/duckdb": {}}, dropped)
    assert final == []
    assert dropped["has open PR"] == 24  # 2 * 12 checked, then the cap
    assert unchecked == 15 and dropped["checked cap reached"] == 15


def test_max_checked_setting_lets_a_scan_reach_a_free_issue(cfg):
    cfg.settings.update(max_candidates=1, max_per_repo_wide=100, max_per_repo=100)
    pairs = [("duckdb/duckdb", n) for n in range(1, 61)]
    gh = timelines(*pairs, blocked=set(pairs[:55]))  # the first 55 all have an open PR
    repos = {"duckdb/duckdb": {}}
    cfg.settings["max_checked"] = 50
    dropped: dict = {}
    final, unchecked = pick(gh, cfg, ranked(pairs), repos, dropped)
    assert final == [] and unchecked == 10 and dropped["checked cap reached"] == 10
    cfg.settings["max_checked"] = 80
    dropped = {}
    final, unchecked = pick(gh, cfg, ranked(pairs), repos, dropped)
    assert [c["number"] for c in final] == [56] and unchecked == 0
    assert "checked cap reached" not in dropped and dropped["has open PR"] == 55


def test_unchecked_count_skips_issues_the_per_repo_cap_would_skip(cfg):
    cfg.settings.update(max_candidates=10, max_checked=1, max_per_repo_wide=1, max_per_repo=1)
    pairs = [("duckdb/duckdb", 1), ("duckdb/duckdb", 2), ("ClickHouse/ClickHouse", 3)]
    final, unchecked = pick(timelines(*pairs), cfg, ranked(pairs), {"duckdb/duckdb": {}}, {})
    assert [c["number"] for c in final] == [1] and unchecked == 1


# -- the whole run ------------------------------------------------------------

def test_run_skips_personal_discovered_repos_and_writes_the_new_fields(cfg, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from scout import discover, track

    def found(repo, n=1):
        return {**issue_for(repo), "number": n, "key": f"{repo}#{n}", "url": "u", "source": "x"}

    metas = {"corp/big": {"stargazers_count": 9000, "owner": {"type": "Organization"}},
             "me2/solo": {"stargazers_count": 9000, "owner": {"type": "User"}}}
    gh = GitHub(transport=Fake({"repos/corp/big/issues/1/timeline": []}))
    gh.repo = lambda name, ttl=0: metas.get(name)
    measured = []
    monkeypatch.setattr(score, "get", lambda gh, repo, cache, ttl, budget, new_budget=None, data_dir=None:
                        measured.append(repo) or {"friendliness": 0.7, "stars": 9000, "owner_type": "Organization",
                                                  "description": "Big thing", "homepage": "https://big.dev",
                                                  "topics": ["kubernetes"]})
    monkeypatch.setattr(track, "collect", lambda gh, login: {"prs": [], "per_repo": {}})
    monkeypatch.setattr(track, "update_suggestions", lambda *a, **k: None)
    monkeypatch.setattr(track, "alert", lambda *a, **k: None)
    monkeypatch.setattr(discover, "from_tiers", lambda gh, cfg: [])
    monkeypatch.setattr(discover, "open_discovery", lambda gh, cfg, exclude: [found("corp/big"), found("me2/solo")])
    cfg.tiers = []
    cli.cmd_run(SimpleNamespace(no_discovery=False), cfg, tmp_path, gh)
    assert measured == ["corp/big"]
    out = json.loads((tmp_path / "candidates.json").read_text())
    [cand] = out["candidates"]
    assert cand["key"] == "corp/big#1" and cand["stale_linked_prs"] == []
    assert cand["field_hint"]["id"] == "cloud" and cand["field_hint"]["source"] == "topics"
    assert cand["repo_info"]["homepage"] == "https://big.dev" and cand["repo_info"]["topics"] == ["kubernetes"]
    run = json.loads((tmp_path / "state.json").read_text())["runs"][-1]
    assert run["dropped"] == {"not an organization": 1} and run["unchecked"] == 0


def test_new_repos_past_the_budget_get_a_provisional_entry_and_are_not_cached():
    from scout import score
    class FakeGH:
        def repo(self, name):
            return {"stargazers_count": 17000, "owner": {"type": "Organization"}, "topics": ["rtos"]}
    cache = {}
    got = score.get(FakeGH(), "zephyrproject-rtos/zephyr", cache, 7, [4], [0])
    assert got["provisional"] and got["stars"] == 17000 and got["owner_type"] == "Organization"
    assert "zephyrproject-rtos/zephyr" not in cache


def test_tier_repos_are_not_marked_down_for_size():
    from scout import config, rank
    cfg = config.load()
    tier_repo = cfg.tiers[0].repos[0]
    issue = {"repo": tier_repo, "created_at": "2026-11-01T00:00:00Z", "label_groups": ["beginner"], "comments": 0}
    out = rank.score_issue(issue, {"stars": 136, "owner_type": "Organization"}, cfg, {})
    assert out["multipliers"]["size"] == 1.0


def test_assigned_issues_stay_in_where_the_assignee_is_the_area_maintainer(cfg):
    from scout import rank
    assert "zephyrproject-rtos/zephyr" in cfg.assignee_is_maintainer
    issue = {**issue_for("zephyrproject-rtos/zephyr"), "assignees": ["area-maintainer"]}
    assert rank.eligible(issue, {"stars": 17000, "owner_type": "Organization"}, cfg, {}, now=NOW) is None
    other = {**issue_for("duckdb/duckdb"), "assignees": ["someone"]}
    assert rank.eligible(other, {"stars": 40000}, cfg, {}, now=NOW) == "assigned"


def test_labels_named_in_targets_are_used_even_past_the_label_list(cfg):
    from scout import discover
    class FakeGH:
        def paginate(self, *a, **k):
            return iter([{"name": "Good first issue"}])  # the listing stops before "priority: low"
    got = discover.interesting_labels(FakeGH(), "zephyrproject-rtos/zephyr", cfg)
    assert got["Good first issue"] == ["beginner"] and "priority: low" in got
