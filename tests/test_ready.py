import json
from datetime import datetime, timezone

from scout import briefings, config, datarepo, digest, state as statemod, wip

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SETTINGS = {"max_ready": 1, "max_open_prs": 3, "max_open_prs_per_repo": 1, "max_unsent": 1}


def pr(repo, n, waiting=False, status="open"):
    return {"repo": repo, "number": n, "url": f"https://github.com/{repo}/pull/{n}",
            "status": status, "waiting_on_you": waiting}


def st(prs=(), suggestions=None):
    return {"contributions": {"prs": list(prs)}, "suggestions": suggestions or {}}


# -- WIP --------------------------------------------------------------------

def test_wip_clear_when_nothing_is_open():
    w = wip.compute(st(), SETTINGS)
    assert w == {"ready_allowed": True, "reason": "ok", "open_prs": 0, "open_prs_by_repo": {},
                 "waiting_on_you": [], "blocked_repos": []}


def test_wip_blocks_ready_when_a_maintainer_is_waiting():
    w = wip.compute(st([pr("a/b", 1, waiting=True)]), SETTINGS)
    assert not w["ready_allowed"] and "waiting on you" in w["reason"]
    assert w["waiting_on_you"] == [{"repo": "a/b", "number": 1, "url": "https://github.com/a/b/pull/1"}]


def test_wip_blocks_ready_at_the_open_pr_cap():
    three = [pr("a/a", 1), pr("b/b", 2), pr("c/c", 3)]
    assert not wip.compute(st(three), SETTINGS)["ready_allowed"]
    assert wip.compute(st(three[:2]), SETTINGS)["ready_allowed"]
    assert wip.compute(st(three), {**SETTINGS, "max_open_prs": 4})["ready_allowed"]


def test_wip_blocks_ready_while_one_is_unsubmitted():
    for status in ("ready", "approved", "submitting"):
        w = wip.compute(st(suggestions={"o/r#1": {"status": status}}), SETTINGS)
        assert not w["ready_allowed"] and "o/r#1" in w["reason"]
    assert wip.compute(st(suggestions={"o/r#1": {"status": "pr_open"}, "o/r#2": {"status": "posted"}}),
                       SETTINGS)["ready_allowed"]


def test_wip_allows_up_to_max_unsent_waiting_items():
    two = {"o/r#1": {"status": "approved"}, "o/r#2": {"status": "ready"}}
    w = wip.compute(st(suggestions={"o/r#1": {"status": "approved"}}), {**SETTINGS, "max_unsent": 2})
    assert w["ready_allowed"] and w["reason"] == "ok"  # one failed item no longer blocks the night
    w = wip.compute(st(suggestions=two), {**SETTINGS, "max_unsent": 2})
    assert not w["ready_allowed"] and "2 ready but not submitted yet (max 2)" in w["reason"] and "o/r#1, o/r#2" in w["reason"]
    # submitting counts as unsent, a snoozed item and finished ones do not
    three = {"o/r#1": {"status": "submitting"}, "o/r#2": {"status": "pr_open"}, "o/r#3": {"status": "ready", "snoozed_until": "2999-01-01T00:00:00+00:00"}}
    assert wip.compute(st(suggestions=three), {**SETTINGS, "max_unsent": 2}, NOW)["ready_allowed"]
    assert not wip.compute(st(suggestions=three), SETTINGS, NOW)["ready_allowed"]
    # the other rules still apply with room in the slots
    assert not wip.compute(st([pr("a/b", 1, waiting=True)]), {**SETTINGS, "max_unsent": 2})["ready_allowed"]
    assert not wip.compute(st([pr("a/a", 1), pr("b/b", 2), pr("c/c", 3)]), {**SETTINGS, "max_unsent": 2})["ready_allowed"]


def test_wip_blocks_repos_at_the_per_repo_cap():
    prs = [pr("a/a", 1), pr("a/a", 2), pr("b/b", 3), pr("c/c", 4, status="merged")]
    w = wip.compute(st(prs), {**SETTINGS, "max_open_prs_per_repo": 2})
    assert w["blocked_repos"] == ["a/a"] and w["open_prs_by_repo"] == {"a/a": 2, "b/b": 1}
    assert wip.compute(st(prs), SETTINGS)["blocked_repos"] == ["a/a", "b/b"]


def test_wip_max_ready_zero_and_reasons_combine():
    assert not wip.compute(st(), {**SETTINGS, "max_ready": 0})["ready_allowed"]
    w = wip.compute(st([pr("a/a", 1, waiting=True)], {"o/r#1": {"status": "ready"}}), SETTINGS)
    assert "waiting on you" in w["reason"] and "not submitted" in w["reason"]


def test_targets_toml_has_the_new_settings():
    cfg = config.load()
    assert (cfg.settings["max_ready"], cfg.settings["max_open_prs_per_repo"],
            cfg.settings["max_open_prs"], cfg.settings["max_picks"]) == (1, 1, 3, 3)
    assert cfg.settings["max_unsent"] == 2
    assert cfg.submit["token_rotated"]


# -- briefings --------------------------------------------------------------

def good_briefing(key="o/r#7", **extra):
    repo, n = key.split("#")
    return {
        "key": key, "repo": repo, "number": int(n), "title": "Crash on empty input",
        "url": f"https://github.com/{repo}/issues/{n}", "picked_at": "2026-10-02T03:10:00+00:00",
        "summary": "s", "why": "w", "difficulty": "easy", "time_estimate": "1h",
        "walkthrough": "w", "change_explained": "c", "alternatives": [], "maintainer_qa": [],
        "tests": {"ran": False}, "claim_comment": "I'd like to fix this.",
        "submit_steps": ["Fork"], "ai_disclosure": "none", **extra,
    }


PR_JSON = {"title": "Fix crash on empty input", "body": "Fixes #7.", "base": "main",
           "branch": "fix/7-empty-input", "fixes": 7, "disclosure": None,
           "commit_message": "Fix crash on empty input", "signoff": False}


def write(data, b, pr_json=None, patch="+x\n", post=None):
    d = data / "briefings" / briefings.slug(b["key"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "briefing.json").write_text(json.dumps(b))
    if patch is not None:
        (d / "draft.patch").write_text(patch)
    if pr_json is not None:
        (d / "pr.json").write_text(json.dumps(pr_json))
    if post is not None:
        (d / "post.md").write_text(post)
    return d


def test_old_briefings_stay_valid():
    assert briefings.validate(good_briefing()) == []
    assert briefings.validate(good_briefing(kind="pr", ready=False, models_used=[
        {"model": "sonnet", "did": "triage, draft, briefing"}, {"model": "opus", "did": "root cause"}])) == []


def test_validate_kind_and_types():
    assert briefings.validate(good_briefing(kind="rant")) == ["kind should be one of ['pr', 'repro', 'review', 'triage']"]
    assert briefings.validate(good_briefing(ready="yes", models_used="sonnet")) == [
        "ready should be bool", "models_used should be a list of {model, did}"]


def test_ready_pr_needs_its_files(tmp_path):
    b = good_briefing(ready=True)
    d = write(tmp_path, b, PR_JSON)
    assert briefings.validate(b, d) == []
    (d / "draft.patch").unlink()
    assert briefings.validate(b, d) == ["ready pr needs draft.patch"]
    (d / "pr.json").unlink()
    assert "ready pr needs pr.json" in briefings.validate(b, d)
    (d / "pr.json").write_text(json.dumps({**PR_JSON, "branch": " ", "base": None}))
    assert {"pr.json needs branch", "pr.json needs base"} <= set(briefings.validate(b, d))


def test_ready_pr_disclosure_and_ai_markers(tmp_path):
    b = good_briefing(ready=True)
    sentence = "I used an AI assistant while investigating this; I reviewed and tested every change myself."
    d = write(tmp_path, b, {**PR_JSON, "body": f"Fixes #7.\n\n{sentence}", "disclosure": sentence})
    assert briefings.validate(b, d) == []
    (d / "pr.json").write_text(json.dumps({**PR_JSON, "disclosure": sentence}))
    assert "pr.json disclosure should be null or a sentence that appears in body" in briefings.validate(b, d)
    (d / "pr.json").write_text(json.dumps({**PR_JSON, "commit_message": "Fix\n\nCo-Authored-By: Claude"}))
    assert "pr.json contains an AI marker" in briefings.validate(b, d)


def test_ready_pr_in_guide_mode_is_invalid(tmp_path):
    b = good_briefing(ready=True, mode="guide")
    d = write(tmp_path, b, PR_JSON)
    assert "a ready item needs mode draft" in briefings.validate(b, d)


def test_ready_item_when_posts_are_forbidden_is_invalid(tmp_path):
    b = good_briefing(ready=True, kind="triage", ai_posts_forbidden=True,
                      post_target="https://github.com/o/r/issues/7")
    d = write(tmp_path, b, patch=None, post="hello")
    assert len(briefings.validate(b, d)) == 1
    assert briefings.validate({**b, "ai_posts_forbidden": False}, d) == []
    assert briefings.validate(good_briefing(ai_posts_forbidden=True)) == []  # fine when not ready


def test_ready_mix_item_needs_post_and_target(tmp_path):
    b = good_briefing(ready=True, kind="repro", post_target="https://github.com/o/r/issues/7")
    d = write(tmp_path, b, patch=None)
    assert briefings.validate(b, d) == ["ready item needs post.md"]
    (d / "post.md").write_text("I can reproduce this on main.\n")
    assert briefings.validate(b, d) == []
    assert "ready item needs post_target (a github.com URL)" in briefings.validate(
        {k: v for k, v in b.items() if k != "post_target"}, d)
    (d / "post.md").write_text("Generated with a tool")
    assert briefings.validate(b, d) == ["post.md contains an AI marker"]


def test_ingest_makes_valid_ready_items_ready(tmp_path):
    write(tmp_path, good_briefing("o/r#7", ready=True, kind="pr"), PR_JSON)
    write(tmp_path, good_briefing("o/r#8", ready=True, kind="review",
                                  post_target="https://github.com/o/r/pull/8"), post="Looks right to me.")
    write(tmp_path, good_briefing("o/r#9", ready=True), pr_json=None)  # claims ready, no pr.json
    write(tmp_path, good_briefing("o/r#10"))
    state = statemod.load(tmp_path)
    assert briefings.ingest(tmp_path, state) == 4
    s = state["suggestions"]
    assert [s[k]["status"] for k in ("o/r#7", "o/r#8", "o/r#9", "o/r#10")] == [
        "ready", "ready", "suggested", "suggested"]
    assert s["o/r#7"]["kind"] == "pr" and s["o/r#8"]["kind"] == "review"
    assert s["o/r#8"]["post_target"] == "https://github.com/o/r/pull/8"
    assert s["o/r#7"]["history"][0]["to"] == "ready"


def test_ingest_clears_the_request_when_a_suggestion_becomes_ready(tmp_path):
    write(tmp_path, good_briefing("o/r#7"), PR_JSON)
    write(tmp_path, good_briefing("o/r#8"))
    state = statemod.load(tmp_path)
    briefings.ingest(tmp_path, state)
    for k in ("o/r#7", "o/r#8"):
        state["suggestions"][k]["prepare_requested_at"] = "2026-10-02T08:00:00+00:00"
    assert briefings.ingest(tmp_path, state) == 0
    assert state["suggestions"]["o/r#7"]["status"] == "suggested"  # not ready yet: the request stands
    write(tmp_path, good_briefing("o/r#7", ready=True), PR_JSON)
    briefings.ingest(tmp_path, state)
    s = state["suggestions"]["o/r#7"]
    assert s["status"] == "ready" and "prepare_requested_at" not in s
    assert s["history"][-1]["from"] == "suggested" and s["history"][-1]["to"] == "ready"
    assert state["suggestions"]["o/r#8"]["prepare_requested_at"]
    write(tmp_path, good_briefing("o/r#8", ready=True, kind="review", post_target="https://github.com/o/r/pull/8"),
          post="Looks right.")
    briefings.ingest(tmp_path, state)
    s = state["suggestions"]["o/r#8"]
    assert (s["status"], s["kind"], s["post_target"]) == ("ready", "review", "https://github.com/o/r/pull/8")


def test_requested_lists_suggestions_oldest_request_first():
    sugg = {"o/r#1": {"status": "suggested", "prepare_requested_at": "2026-10-02T09:00:00+00:00"},
            "o/r#2": {"status": "suggested", "prepare_requested_at": "2026-10-01T09:00:00+00:00"},
            "o/r#3": {"status": "suggested"},
            "o/r#4": {"status": "ready", "prepare_requested_at": "2026-09-01T09:00:00+00:00"}}
    assert wip.requested({"suggestions": sugg}) == ["o/r#2", "o/r#1"]
    assert wip.requested({"suggestions": {}}) == []


def test_ingest_leaves_broken_briefings_out(tmp_path):
    b = good_briefing("o/r#7", ready=True)
    del b["summary"]
    write(tmp_path, b, PR_JSON)
    state = statemod.load(tmp_path)
    assert briefings.ingest(tmp_path, state) == 0


# -- follow-ups -------------------------------------------------------------

SMALL = {"pr_url": "https://github.com/o/r/pull/3", "comments_addressed": ["rename x"], "kind": "small",
         "reply": "Renamed, thanks.", "patch": "followup.patch"}
DISCUSS = {"pr_url": "https://github.com/o/r/pull/3", "comments_addressed": ["why not Y?"],
           "kind": "discuss", "talking_points": ["Y breaks Z"], "patch": None}


def test_validate_followup(tmp_path):
    (tmp_path / "followup.patch").write_text("+x\n")
    assert briefings.validate_followup(SMALL, tmp_path) == []
    assert briefings.validate_followup(DISCUSS, tmp_path) == []
    assert briefings.validate_followup({**SMALL, "reply": ""}, tmp_path) == ["small follow-up needs reply"]
    assert briefings.validate_followup({**DISCUSS, "talking_points": []}, tmp_path) == [
        "discuss follow-up needs talking_points"]
    assert briefings.validate_followup({**DISCUSS, "patch": "followup.patch"}, tmp_path) == [
        "discuss follow-up carries no patch"]
    assert briefings.validate_followup({**SMALL, "patch": "nope.patch"}, tmp_path) == ["nope.patch is missing"]
    assert briefings.validate_followup({**SMALL, "kind": "big"}) == ["kind should be one of ['discuss', 'small']"]
    assert briefings.validate_followup({"kind": "small", "reply": "r", "patch": None}) == [
        "missing pr_url", "comments_addressed should be list"]


def test_followup_respects_project_policy():
    assert briefings.validate_followup(SMALL, None, {"ai_posts_forbidden": True}) == [
        "the project forbids AI-written posts: use discuss with talking_points"]
    assert briefings.validate_followup(SMALL, None, {"mode": "guide"}) == ["guide mode: no patch"]
    assert briefings.validate_followup(DISCUSS, None, {"ai_posts_forbidden": True, "mode": "guide"}) == []


def test_ingest_points_the_suggestion_at_the_latest_followup(tmp_path):
    d = write(tmp_path, good_briefing(), PR_JSON)
    state = statemod.load(tmp_path)
    briefings.ingest(tmp_path, state)
    assert "followup" not in state["suggestions"]["o/r#7"]
    for day, fu in (("2026-10-03", SMALL), ("2026-10-05", DISCUSS)):
        f = d / "followups" / day
        f.mkdir(parents=True)
        (f / "followup.json").write_text(json.dumps(fu))
        (f / "followup.patch").write_text("+x\n")
    bad = d / "followups" / "2026-10-09"
    bad.mkdir()
    (bad / "followup.json").write_text(json.dumps({"kind": "small"}))
    assert briefings.ingest(tmp_path, state) == 0
    assert "followup" not in state["suggestions"]["o/r#7"]  # latest one is broken: don't point at it
    (bad / "followup.json").unlink()
    (bad.parent / "2026-10-05" / "followup.json").write_text(json.dumps(DISCUSS))
    bad.rmdir()
    briefings.ingest(tmp_path, state)
    s = state["suggestions"]["o/r#7"]
    assert s["followup"] == "briefings/o__r__7/followups/2026-10-05" and s["followup_kind"] == "discuss"


# -- digest -----------------------------------------------------------------

def dstate(**kw):
    return {"suggestions": {}, "contributions": {"prs": []}, **kw}


def test_digest_quiet_day(tmp_path):
    d = digest.build(config.load(), tmp_path, dstate(), NOW)
    assert d["send"] is False and d["ready"] == [] and d["waiting_on_you"] == [] and d["new_briefings"] == []
    assert d["date"] == "2026-10-02"


def test_digest_lists_ready_waiting_and_new(tmp_path):
    (tmp_path / "picks").mkdir()
    (tmp_path / "picks" / "2026-10-02.json").write_text(json.dumps({"picked": ["o/r#2", "o/r#1"]}))
    suggestions = {
        "o/r#1": {"status": "ready", "title": "A", "kind": "repro", "suggested_at": "2026-10-02T03:00:00+00:00"},
        "o/r#2": {"status": "suggested", "title": "B", "suggested_at": "2026-10-01T03:00:00+00:00"},
        "o/r#3": {"status": "suggested", "title": "C", "suggested_at": "2026-10-02T03:00:00+00:00"},
        "o/r#4": {"status": "suggested", "title": "D", "suggested_at": "2026-09-01T03:00:00+00:00"},
        "o/r#5": {"status": "waiting_on_you", "title": "E", "pr_url": "https://github.com/o/r/pull/5"},
    }
    prs = [{**pr("o/r", 5, waiting=True), "updated_at": "2026-10-02T09:00:00Z",
            "review_comments": [{"at": "2026-09-30T09:00:00Z"}, {"at": "2026-10-02T09:00:00Z"}]},
           {**pr("x/y", 6, waiting=True), "updated_at": "2026-10-02T09:00:00Z"},
           pr("x/y", 7), pr("x/y", 8, waiting=True, status="merged")]
    d = digest.build(config.load(), tmp_path, dstate(suggestions=suggestions,
                                                     contributions={"prs": prs}), NOW)
    assert d["send"] is True
    assert d["ready"] == [{"key": "o/r#1", "title": "A", "kind": "repro", "slug": "o__r__1"}]
    assert d["new_briefings"] == [{"key": "o/r#2", "title": "B", "kind": "pr", "slug": "o__r__2"},
                                  {"key": "o/r#3", "title": "C", "kind": "pr", "slug": "o__r__3"}]
    assert d["waiting_on_you"] == [
        {"key": "o/r#5", "slug": "o__r__5", "pr_url": "https://github.com/o/r/pull/5",
         "since": "2026-09-30T09:00:00Z", "overdue": True},
        {"key": "x/y#6", "slug": "x__y__6", "pr_url": "https://github.com/x/y/pull/6",
         "since": "2026-10-02T09:00:00Z", "overdue": False}]
    assert d["pairing"] == []


def test_digest_lists_pairing_items_without_sending(tmp_path):
    suggestions = {
        "o/r#2": {"status": "suggested", "title": "B", "pairing": True, "suggested_at": "2026-09-01T00:00:00+00:00"},
        "o/r#1": {"status": "approved", "title": "A", "pairing": True},
        "o/r#3": {"status": "suggested", "title": "C", "suggested_at": "2026-09-01T00:00:00+00:00"},
    }
    d = digest.build(config.load(), tmp_path, dstate(suggestions=suggestions), NOW)
    assert d["pairing"] == [{"key": "o/r#1", "title": "A", "slug": "o__r__1"},
                            {"key": "o/r#2", "title": "B", "slug": "o__r__2"}]
    assert d["send"] is False


def test_digest_send_rules(tmp_path):
    cfg = config.load()
    only_ready = dstate(suggestions={"o/r#1": {"status": "ready", "title": "A"}})
    assert digest.build(cfg, tmp_path, only_ready, NOW)["send"]
    only_waiting = dstate(contributions={"prs": [pr("o/r", 5, waiting=True)]})
    assert digest.build(cfg, tmp_path, only_waiting, NOW)["send"]
    old = dstate(suggestions={"o/r#1": {"status": "suggested", "suggested_at": "2026-09-30T00:00:00+00:00"},
                              "o/r#2": {"status": "approved", "title": "x"}})
    assert not digest.build(cfg, tmp_path, old, NOW)["send"]


def test_digest_token_age(tmp_path):
    cfg = config.load()
    cfg.submit = {"token_rotated": "2026-07-14"}
    d = digest.build(cfg, tmp_path, dstate(), NOW)
    assert d["token_age_days"] == 80 and d["token_warning"] is False
    cfg.submit = {"token_rotated": "2026-07-13"}
    d = digest.build(cfg, tmp_path, dstate(), NOW)
    assert d["token_age_days"] == 81 and d["token_warning"] is True
    cfg.submit = {}
    d = digest.build(cfg, tmp_path, dstate(), NOW)
    assert d["token_age_days"] is None and d["token_warning"] is False


# -- data repo ----------------------------------------------------------------

def test_init_installs_both_agents(tmp_path):
    written = datarepo.init(tmp_path, "me")
    assert ".claude/agents/root-cause-analyst.md" in written and ".claude/agents/thread-summarizer.md" in written
    analyst = (tmp_path / ".claude/agents/root-cause-analyst.md").read_text()
    summarizer = (tmp_path / ".claude/agents/thread-summarizer.md").read_text()
    assert "model: opus" in analyst and "at most once per pick" in analyst
    assert "model: haiku" in summarizer and "tools: Read, Grep, Glob, Bash" in summarizer


def test_digest_pairing_lists_only_active_items(tmp_path):
    st = statemod.load(tmp_path)
    st["suggestions"] = {
        "o/r#1": {"status": "suggested", "pairing": True, "title": "a"},
        "o/r#2": {"status": "pr_open", "pairing": True, "title": "b"},
        "o/r#3": {"status": "skipped", "pairing": True, "title": "c"},
    }
    assert [p["key"] for p in digest.build(config.load(), tmp_path, st, NOW)["pairing"]] == ["o/r#1"]
