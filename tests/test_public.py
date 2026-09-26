import json

import pytest

from scout import briefings, config, public, state as statemod
from scout.__main__ import main


def good_briefing(key="o/r#7", mode="draft"):
    repo, n = key.split("#")
    return {
        "key": key, "repo": repo, "number": int(n), "title": "Crash on empty input",
        "url": f"https://github.com/{repo}/issues/{n}", "picked_at": "2026-09-26T03:10:00+00:00",
        "summary": "s", "why": "w", "difficulty": "easy", "time_estimate": "1h",
        "walkthrough": "w", "change_explained": "c", "alternatives": [], "maintainer_qa": [],
        "tests": {"ran": False}, "claim_comment": "SECRET_CLAIM_TEXT",
        "submit_steps": ["SECRET_STEP_TEXT"], "ai_disclosure": "none", "mode": mode,
    }


def write_briefing(data, b, patch="+x\n"):
    d = data / "briefings" / briefings.slug(b["key"])
    d.mkdir(parents=True)
    (d / "briefing.json").write_text(json.dumps(b))
    (d / "draft.patch").write_text(patch)


def suggestion(repo="o/r", number=7, status="merged", pr_url="https://github.com/o/r/pull/7"):
    return {"repo": repo, "number": number, "title": "Crash on empty input",
            "url": f"https://github.com/{repo}/issues/{number}", "status": status,
            "pr_url": pr_url, "suggested_at": "2026-09-20T00:00:00+00:00", "history": []}


# -- publishable ------------------------------------------------------------

def test_publishable_true_for_merged_or_closed_draft():
    b = good_briefing()
    assert public.publishable(suggestion(status="merged"), b, {}) is True
    assert public.publishable(suggestion(status="closed"), b, {}) is True


@pytest.mark.parametrize("status", ["suggested", "claimed", "pr_open"])
def test_publishable_false_for_in_progress_statuses(status):
    b = good_briefing()
    assert public.publishable(suggestion(status=status), b, {}) is False


def test_publishable_false_without_pr_url():
    b = good_briefing()
    assert public.publishable(suggestion(pr_url=""), b, {}) is False


def test_publishable_false_for_guide_mode():
    b = good_briefing(mode="guide")
    assert public.publishable(suggestion(), b, {}) is False


def test_publishable_false_for_restrictive_repo():
    b = good_briefing()
    assert public.publishable(suggestion(), b, {"ai_policy": "restrictive"}) is False


# -- build_payload -----------------------------------------------------------

def base_state(tmp_path):
    st = statemod.load(tmp_path)
    st["contributions"] = {
        "prs": [], "reviews": [], "issues": [],
        "per_repo": {"o/r": {"merged": 1, "open": 0, "closed": 0, "maintainer": False,
                              "ladder": "Contributor"}},
        "team_prs": [{"repo": "someone/private-thing", "number": 1, "title": "internal",
                      "url": "https://github.com/someone/private-thing/pull/1",
                      "status": "merged"}],
    }
    st["repos"] = {"o/r": {"friendliness": 0.8, "confident": True, "merge_rate": 0.5,
                           "merges_elsewhere": False, "median_days_to_merge": 2,
                           "median_hours_to_first_response": 5, "outside_prs_sampled": 10,
                           "ai_policy": "none", "cla": False, "stars": 100, "language": "Python"},
                   "o/no-friendliness": {"stars": 5, "language": "Rust"}}
    return st


def test_build_payload_excludes_team_prs(tmp_path):
    st = base_state(tmp_path)
    payload = public.build_payload(config.load(), tmp_path, st)
    assert "private-thing" not in json.dumps(payload)


def test_build_payload_excludes_open_suggestions(tmp_path):
    st = base_state(tmp_path)
    write_briefing(tmp_path, good_briefing())
    st["suggestions"] = {"o/r#7": suggestion(status="pr_open")}
    payload = public.build_payload(config.load(), tmp_path, st)
    assert payload["finished"] == []


def test_build_payload_never_contains_claim_comment_or_submit_steps(tmp_path):
    st = base_state(tmp_path)
    write_briefing(tmp_path, good_briefing())
    st["suggestions"] = {"o/r#7": suggestion(status="merged")}
    payload = public.build_payload(config.load(), tmp_path, st)
    dumped = json.dumps(payload)
    assert "SECRET_CLAIM_TEXT" not in dumped
    assert "SECRET_STEP_TEXT" not in dumped


def test_build_payload_includes_merged_draft_briefing_with_pr_url(tmp_path):
    st = base_state(tmp_path)
    write_briefing(tmp_path, good_briefing())
    st["suggestions"] = {"o/r#7": suggestion(status="merged", pr_url="https://github.com/o/r/pull/7")}
    payload = public.build_payload(config.load(), tmp_path, st)
    assert len(payload["finished"]) == 1
    f = payload["finished"][0]
    assert f["key"] == "o/r#7"
    assert f["pr_url"] == "https://github.com/o/r/pull/7"
    assert f["briefing"]["summary"] == "s"


def test_build_payload_research_excludes_repos_without_friendliness(tmp_path):
    st = base_state(tmp_path)
    payload = public.build_payload(config.load(), tmp_path, st)
    repos = {r["repo"] for r in payload["research"]}
    assert repos == {"o/r"}
    assert "o/no-friendliness" not in repos


# -- render -------------------------------------------------------------------

def test_render_writes_index_and_replaces_placeholder(tmp_path):
    st = base_state(tmp_path)
    out_dir = tmp_path / "site"
    page = public.render(config.load(), tmp_path, st, out_dir)
    html = page.read_text()
    assert "/*__SCOUT_PUBLIC__*/null" not in html
    assert page.name == "index.html"
    assert not (out_dir / "CNAME").exists()


def test_render_writes_cname_only_with_domain(tmp_path):
    st = base_state(tmp_path)
    out_dir = tmp_path / "site"
    public.render(config.load(), tmp_path, st, out_dir, domain="scout.example.dev")
    assert (out_dir / "CNAME").read_text() == "scout.example.dev\n"


def test_render_escapes_script_close_tag_in_briefing(tmp_path):
    st = base_state(tmp_path)
    b = good_briefing()
    b["summary"] = "breaks on </script><script>alert(1)</script>"
    write_briefing(tmp_path, b)
    st["suggestions"] = {"o/r#7": suggestion(status="merged")}
    out_dir = tmp_path / "site"
    page = public.render(config.load(), tmp_path, st, out_dir)
    html = page.read_text()
    assert "</script><script>alert(1)" not in html
    assert "breaks on" in html


# -- CLI ----------------------------------------------------------------------

def test_cli_render_public(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    out_dir = tmp_path / "site"
    main(["render-public", "--out", str(out_dir), "--domain", "scout.example.dev"])
    assert (out_dir / "index.html").exists()
    assert (out_dir / "CNAME").read_text() == "scout.example.dev\n"
