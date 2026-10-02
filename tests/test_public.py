import json
from datetime import datetime, timezone

from scout import config, public, state as statemod
from scout.__main__ import main

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # a Wednesday


def pr(repo, number, status="merged", created="2026-09-01T10:00:00Z", merged="2026-09-03T10:00:00Z",
       **extra):
    return {"repo": repo, "number": number, "title": f"Fix thing {number}",
            "url": f"https://github.com/{repo}/pull/{number}", "status": status,
            "created_at": created, "updated_at": merged or created,
            "merged_at": merged if status == "merged" else None,
            "author_association": "CONTRIBUTOR", "body": "PR_BODY_TEXT", **extra}


def state_with(tmp_path, prs=(), reviews=(), issues=()):
    st = statemod.load(tmp_path)
    st["contributions"] = {
        "prs": list(prs), "reviews": list(reviews), "issues": list(issues),
        "per_repo": {"duckdb/duckdb": {"merged": 2, "open": 1, "closed": 0, "maintainer": False,
                                        "ladder": "Contributor"}},
        "team_prs": [{"repo": "someone/private-thing", "number": 1, "title": "internal",
                      "url": "https://github.com/someone/private-thing/pull/1", "status": "merged"}],
    }
    st["repos"] = {"duckdb/duckdb": {"stars": 30000, "language": "C++", "friendliness": 0.9}}
    return st


def sample(tmp_path):
    return state_with(
        tmp_path,
        prs=[pr("duckdb/duckdb", 1, merged="2026-09-03T10:00:00Z", summary="Stops a crash on empty input"),
             pr("duckdb/duckdb", 2, merged="2026-09-10T10:00:00Z"),
             pr("duckdb/duckdb", 3, status="open", created="2026-09-28T10:00:00Z", merged=None),
             pr("o/r", 4, merged="2026-08-20T10:00:00Z"),
             pr("o/r", 5, status="closed", merged=None),
             pr("x/y", 6, status="open", created="2026-09-29T10:00:00Z", merged=None)],
        reviews=[{"repo": "o/r", "number": 9, "title": "Review me", "url": "https://github.com/o/r/pull/9",
                  "updated_at": "2026-09-01T00:00:00Z"}],
        issues=[{"repo": "o/r", "number": 8, "title": "An issue", "url": "https://github.com/o/r/issues/8",
                 "state": "open", "created_at": "2026-07-01T00:00:00Z"}])


# -- payload ------------------------------------------------------------------

def test_build_payload_counts_and_project_order(tmp_path):
    p = public.build_payload(config.load(), sample(tmp_path), "oss.example.dev", NOW)
    assert p["merged_prs"] == 3
    assert p["open_prs"] == 2
    assert p["url"] == "https://oss.example.dev/"
    assert [(x["repo"], x["merged"], x["open"]) for x in p["projects"]] == [
        ("duckdb/duckdb", 2, 1), ("o/r", 1, 0), ("x/y", 0, 1)]
    top = p["projects"][0]
    assert (top["stars"], top["language"]) == (30000, "C++")
    assert p["projects"][1]["stars"] is None


def test_build_payload_keeps_only_merged_and_open_prs(tmp_path):
    p = public.build_payload(config.load(), sample(tmp_path), None, NOW)
    assert {x["status"] for x in p["prs"]} == {"merged", "open"}
    assert p["url"] == "https://oss.aadityad.dev/"


def test_build_payload_excludes_team_prs_and_pr_bodies(tmp_path):
    dumped = json.dumps(public.build_payload(config.load(), sample(tmp_path), None, NOW))
    assert "private-thing" not in dumped
    assert "PR_BODY_TEXT" not in dumped


def test_build_payload_uses_summary_when_present(tmp_path):
    p = public.build_payload(config.load(), sample(tmp_path), None, NOW)
    by_number = {x["number"]: x for x in p["prs"]}
    assert by_number[1]["summary"] == "Stops a crash on empty input"
    assert by_number[2]["summary"] is None

# -- featured PRs and plain-language summaries --------------------------------------

def url(repo, number):
    return f"https://github.com/{repo}/pull/{number}"


def test_summary_override_beats_contributions_summary_and_falls_back(tmp_path):
    st = sample(tmp_path)  # PR 1 has its own summary, PR 2 has none
    st["public"] = {"summaries": {url("duckdb/duckdb", 1): "  Fixed a crash on empty input  ",
                                  url("duckdb/duckdb", 2): "Sped up parsing",
                                  url("o/r", 4): "   ",         # empty: ignored
                                  url("x/y", 6): 42}}           # not a string: ignored
    by = {x["number"]: x for x in public.build_payload(config.load(), st, None, NOW)["prs"]}
    assert by[1]["summary"] == "Fixed a crash on empty input"
    assert by[2]["summary"] == "Sped up parsing"
    assert by[4]["summary"] is None and by[6]["summary"] is None


def test_blank_override_keeps_existing_summary(tmp_path):
    st = sample(tmp_path)
    st["public"] = {"summaries": {url("duckdb/duckdb", 1): " ", "not-a-pr": "x"}}
    by = {x["number"]: x for x in public.build_payload(config.load(), st, None, NOW)["prs"]}
    assert by[1]["summary"] == "Stops a crash on empty input"


def many(tmp_path):
    return state_with(tmp_path, prs=[
        pr("a/a", 1, merged="2026-09-01T10:00:00Z"), pr("b/b", 2, merged="2026-09-02T10:00:00Z"),
        pr("c/c", 3, merged="2026-09-03T10:00:00Z"), pr("d/d", 4, merged="2026-09-04T10:00:00Z"),
        pr("e/e", 5, status="open", created="2026-09-05T10:00:00Z", merged=None)])


def test_featured_keeps_given_order_and_caps_at_three(tmp_path):
    st = many(tmp_path)
    st["public"] = {"featured": [url("c/c", 3), url("a/a", 1), url("d/d", 4), url("b/b", 2)]}
    p = public.build_payload(config.load(), st, None, NOW)
    assert [x["url"] for x in p["featured"]] == [url("c/c", 3), url("a/a", 1), url("d/d", 4)]
    assert [x["number"] for x in p["prs"]] == [2, 5]


def test_featured_skips_unknown_unmerged_repeated_and_junk(tmp_path):
    st = many(tmp_path)
    st["public"] = {"featured": [url("nope/nope", 9), url("e/e", 5), url("b/b", 2), url("b/b", 2),
                                 None, 7, url("a/a", 1)]}
    p = public.build_payload(config.load(), st, None, NOW)
    assert [x["number"] for x in p["featured"]] == [2, 1]
    assert 5 in [x["number"] for x in p["prs"]]  # the open PR stays in the list


def test_featured_missing_or_malformed_is_empty(tmp_path):
    for bad in (None, {}, [], "x", {"featured": "x"}, {"featured": None}, {"summaries": []}):
        st = many(tmp_path)
        if bad is not None:
            st["public"] = bad
        p = public.build_payload(config.load(), st, None, NOW)
        assert p["featured"] == [] and len(p["prs"]) == 5, bad


def test_featured_pr_carries_its_summary_and_is_not_duplicated(tmp_path):
    st = many(tmp_path)
    st["public"] = {"featured": [url("a/a", 1), url("b/b", 2)],
                    "summaries": {url("a/a", 1): "Fixed a thing in A"}}
    p = public.build_payload(config.load(), st, None, NOW)
    assert p["featured"][0]["summary"] == "Fixed a thing in A"
    assert p["featured"][1]["summary"] is None
    featured_urls = {x["url"] for x in p["featured"]}
    assert not featured_urls & {x["url"] for x in p["prs"]}
    assert len(p["featured"]) + len(p["prs"]) == 5


def test_counts_projects_activity_and_stats_include_featured(tmp_path):
    plain = public.build_payload(config.load(), many(tmp_path), "oss.example.dev", NOW)
    st = many(tmp_path)
    st["public"] = {"featured": [url("a/a", 1), url("b/b", 2), url("c/c", 3)]}
    feat = public.build_payload(config.load(), st, "oss.example.dev", NOW)
    assert (feat["merged_prs"], feat["open_prs"]) == (4, 1) == (plain["merged_prs"], plain["open_prs"])
    assert feat["projects"] == plain["projects"] and len(feat["projects"]) == 5
    assert feat["activity"] == plain["activity"]
    assert public.build_stats(feat) == public.build_stats(plain)
    assert set(public.build_stats(feat)) == {"generated_at", "merged_prs", "open_prs", "projects",
                                             "top_projects", "url"}
    assert public.description(feat) == public.description(plain)


def test_render_with_only_featured_prs_still_has_data(tmp_path):
    st = state_with(tmp_path, prs=[pr("a/a", 1)])
    st["public"] = {"featured": [url("a/a", 1)]}
    html = public.render(config.load(), st, tmp_path / "site").read_text()
    data = json.loads(html.split("const DATA = ")[1].split(";\n")[0])
    assert data["prs"] == [] and len(data["featured"]) == 1 and data["merged_prs"] == 1


def test_activity_weeks_and_streak(tmp_path):
    st = state_with(tmp_path, prs=[
        pr("o/r", 1, created="2026-09-16T00:00:00Z", merged="2026-09-17T00:00:00Z"),  # week of 09-14
        pr("o/r", 2, created="2026-09-22T00:00:00Z", merged=None, status="open"),     # week of 09-21
        pr("o/r", 3, created="2026-06-01T00:00:00Z", merged="2026-06-02T00:00:00Z")])  # isolated
    a = public.build_payload(config.load(), st, None, NOW)["activity"]
    assert len(a["weeks"]) == public.WEEKS
    assert a["weeks"][-1] == {"week": "2026-09-28", "n": 0}
    assert {w["week"]: w["n"] for w in a["weeks"]}["2026-09-14"] == 2
    assert a["streak"] == 2  # this week is quiet, but last week and the one before count
    assert a["weeks_active"] == 3


def test_activity_empty(tmp_path):
    a = public.build_payload(config.load(), state_with(tmp_path), None, NOW)["activity"]
    assert a["streak"] == 0 and a["weeks_active"] == 0
    assert all(w["n"] == 0 for w in a["weeks"])


# -- stats.json -----------------------------------------------------------------

def test_build_stats_shape(tmp_path):
    payload = public.build_payload(config.load(), sample(tmp_path), "oss.example.dev", NOW)
    s = public.build_stats(payload)
    assert set(s) == {"generated_at", "merged_prs", "open_prs", "projects", "top_projects", "url"}
    assert s["merged_prs"] == 3 and s["open_prs"] == 2
    assert s["projects"][0] == {"repo": "duckdb/duckdb", "merged": 2, "open": 1}
    assert s["top_projects"] == ["duckdb/duckdb", "o/r", "x/y"]
    assert s["url"] == "https://oss.example.dev/"


def test_build_stats_empty(tmp_path):
    s = public.build_stats(public.build_payload(config.load(), state_with(tmp_path), None, NOW))
    assert s["merged_prs"] == 0 and s["open_prs"] == 0
    assert s["projects"] == [] and s["top_projects"] == []


# -- render ---------------------------------------------------------------------

def test_render_writes_index_stats_and_cname(tmp_path):
    out = tmp_path / "site"
    page = public.render(config.load(), sample(tmp_path), out, "oss.example.dev")
    html = page.read_text()
    assert page.name == "index.html"
    assert "/*__SCOUT_PUBLIC__*/null" not in html
    assert "__PAGE_URL__" not in html and "__DESCRIPTION__" not in html
    assert '<link rel="canonical" href="https://oss.example.dev/">' in html
    assert "3 merged pull requests across 2 open source projects" in html
    assert (out / "CNAME").read_text() == "oss.example.dev\n"
    assert json.loads((out / "stats.json").read_text())["top_projects"][0] == "duckdb/duckdb"


def test_render_without_domain_writes_no_cname(tmp_path):
    out = tmp_path / "site"
    public.render(config.load(), sample(tmp_path), out)
    assert not (out / "CNAME").exists()


def test_render_empty_state(tmp_path):
    out = tmp_path / "site"
    page = public.render(config.load(), state_with(tmp_path), out, "oss.example.dev")
    html = page.read_text()
    assert "First contributions in progress" in html
    assert "Open source contributions by Aaditya Desai" in html
    stats = json.loads((out / "stats.json").read_text())
    assert stats["merged_prs"] == 0 and stats["projects"] == []
    assert json.loads(html.split("const DATA = ")[1].split(";\n")[0])["prs"] == []


def test_render_escapes_script_close_tag_in_titles(tmp_path):
    st = state_with(tmp_path, prs=[pr("o/r", 1, title="x")])
    st["contributions"]["prs"][0]["title"] = "breaks </script><script>alert(1)</script>"
    html = public.render(config.load(), st, tmp_path / "site").read_text()
    assert "</script><script>alert(1)" not in html
    assert "breaks" in html


def test_render_never_leaks_private_data(tmp_path):
    """Briefings, suggestions, candidates and repo research must not reach the site."""
    st = sample(tmp_path)
    st["suggestions"] = {"o/r#7": {
        "repo": "o/r", "number": 7, "title": "LEAK_SUGGESTION_TITLE", "status": "merged",
        "url": "https://github.com/o/r/issues/7", "pr_url": "https://github.com/o/r/pull/7",
        "suggested_at": "2026-09-20T00:00:00+00:00", "history": []}}
    st["repos"]["duckdb/duckdb"].update({
        "ai_policy": "LEAK_AI_POLICY", "merge_rate": 0.123456, "median_hours_to_first_response": 987654,
        "cla": "LEAK_CLA"})
    bdir = tmp_path / "briefings" / "o-r-7"
    bdir.mkdir(parents=True)
    (bdir / "briefing.json").write_text(json.dumps({
        "key": "o/r#7", "summary": "LEAK_BRIEFING_SUMMARY", "claim_comment": "LEAK_CLAIM",
        "submit_steps": ["LEAK_STEP"], "ai_disclosure": "LEAK_DISCLOSURE", "mode": "draft"}))
    (bdir / "draft.patch").write_text("+LEAK_PATCH\n")
    (tmp_path / "candidates.json").write_text(json.dumps([{"key": "o/r#7", "title": "LEAK_CANDIDATE"}]))
    # every other private suggestion field, and briefing files beside the first one
    st["suggestions"]["o/r#7"].update({
        "reason": "LEAK_REASON", "why": "LEAK_WHY", "score": 987.123, "note": "LEAK_NOTE",
        "claim_comment": "LEAK_SUGGESTION_CLAIM", "pr_title": "LEAK_SUGGESTION_PR_TITLE",
        "history": [{"at": "2026-09-21T00:00:00+00:00", "from": "suggested", "to": "LEAK_HISTORY"}]})
    st["suggestions"]["o/r#8"] = {"repo": "o/r", "number": 8, "title": "LEAK_SECOND_SUGGESTION",
                                  "status": "ready", "url": "https://github.com/o/r/issues/8"}
    (bdir / "notes.md").write_text("LEAK_BRIEFING_NOTES")
    (bdir / "research.json").write_text(json.dumps({"how_to_test": "LEAK_RESEARCH_TESTS"}))
    other = tmp_path / "briefings" / "x-y-1"
    other.mkdir()
    (other / "briefing.json").write_text(json.dumps({"summary": "LEAK_OTHER_BRIEFING_SUMMARY"}))
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "o-r.md").write_text("LEAK_REPO_RESEARCH")
    # public choices are published, but only for known merged PRs: a summary for an
    # unknown url (e.g. a suggestion's own pr_url) must not surface
    st["public"] = {"featured": [url("duckdb/duckdb", 2)],
                    "summaries": {url("duckdb/duckdb", 2): "PUBLIC_OK_SUMMARY",
                                  "https://github.com/o/r/pull/7": "LEAK_UNKNOWN_URL_SUMMARY"}}

    out = tmp_path / "site"
    public.render(config.load(), st, out, "oss.example.dev")
    combined = "\n".join(f.read_text() for f in out.iterdir())
    for needle in ("LEAK_", "987654", "0.123456", "987.123", "friendliness", "briefing", "suggestion",
                   "candidate", "scout", "AI-assisted"):
        assert needle not in combined, needle
    assert "PUBLIC_OK_SUMMARY" in combined


# -- CLI ------------------------------------------------------------------------

def test_cli_render_public(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    out_dir = tmp_path / "site"
    main(["render-public", "--out", str(out_dir), "--domain", "oss.example.dev"])
    assert (out_dir / "index.html").exists()
    assert (out_dir / "stats.json").exists()
    assert (out_dir / "CNAME").read_text() == "oss.example.dev\n"
