import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from scout import briefings, config, public, render, state as statemod
from scout.config import ROOT


def good_briefing(key="o/r#7"):
    repo, n = key.split("#")
    return {
        "key": key, "repo": repo, "number": int(n), "title": "Crash on empty input",
        "url": f"https://github.com/{repo}/issues/{n}", "picked_at": "2026-09-26T03:10:00+00:00",
        "summary": "s", "why": "w", "difficulty": "easy", "time_estimate": "1h",
        "walkthrough": "w", "change_explained": "c", "alternatives": [], "maintainer_qa": [],
        "tests": {"ran": False}, "claim_comment": "I'd like to fix this.",
        "submit_steps": ["Fork"], "ai_disclosure": "none",
    }


def write_briefing(data, b, patch="+x\n"):
    d = data / "briefings" / briefings.slug(b["key"])
    d.mkdir(parents=True)
    (d / "briefing.json").write_text(json.dumps(b))
    (d / "draft.patch").write_text(patch)


def test_validate_catches_missing_and_wrong_types():
    b = good_briefing()
    assert briefings.validate(b) == []
    del b["why"]
    b["number"] = "7"
    assert set(briefings.validate(b)) == {"missing why", "number should be int"}


@pytest.mark.parametrize("mode", ["guide", "pair", "own"])
def test_non_draft_modes_never_show_a_patch(tmp_path, mode):
    b = {**good_briefing(), "mode": mode}
    if mode != "guide":
        b["claim_comment"] = "- reproduced on main"
    write_briefing(tmp_path, b, patch="+secret draft\n")
    loaded = briefings.load_all(tmp_path)[0]
    assert loaded["_patch"] == "" and loaded["_pr"] == {} and loaded["_problems"] == []
    assert loaded["mode"] == ("pair" if mode == "guide" else mode)  # legacy guide is read as pair
    assert briefings.validate({**b, "mode": "yolo"}) == ["mode should be one of ['draft', 'own', 'pair']"]


def test_legacy_guide_briefing_still_validates():
    b = {**good_briefing(), "mode": "guide", "claim_comment": "I'd like to take this one, I think I know the cause."}
    assert briefings.validate(b) == [] and briefings.mode_of(b) == "pair"
    assert briefings.mode_of({}) == "draft"


def test_pair_fields_are_lists_of_strings():
    b = {**good_briefing(), "mode": "pair", "claim_comment": "- reproduced on main\n- looks like the binder"}
    good = {"fix_plan": ["1. add the check"], "code_locations": ["src/a.cpp:10 - where the check goes"],
            "explain_questions": ["Why? - because"], "pr_facts": ["- fixes #7"], "comment_facts": ["- repro attached"]}
    assert briefings.validate({**b, **good}) == []
    assert briefings.validate({**b, "fix_plan": "step one"}) == ["fix_plan should be a list of strings"]
    assert briefings.validate({**b, "pr_facts": [1]}) == ["pr_facts should be a list of strings"]
    assert briefings.validate({**b, "mode": "draft", "fix_plan": {}}) == ["fix_plan should be a list of strings"]


@pytest.mark.parametrize("mode", ["pair", "own"])
def test_pair_and_own_claim_comment_must_be_bullets(mode):
    b = {**good_briefing(), "mode": mode}  # "I'd like to fix this." is paste-ready prose
    assert len(briefings.validate(b)) == 1 and "bullet facts" in briefings.validate(b)[0]
    assert briefings.validate({**b, "claim_comment": "- seen on 1.4\n\n- empty input only"}) == []
    assert briefings.validate({**b, "claim_comment": "- seen on 1.4\nHi, I'd like to work on this."}) != []
    assert briefings.validate({**b, "mode": "draft"}) == []  # draft keeps prose


def test_ingest_adds_once(tmp_path):
    write_briefing(tmp_path, good_briefing())
    bad = good_briefing("o/r#8")
    del bad["summary"]
    write_briefing(tmp_path, bad)
    st = statemod.load(tmp_path)
    assert briefings.ingest(tmp_path, st) == 1
    assert briefings.ingest(tmp_path, st) == 0
    s = st["suggestions"]["o/r#7"]
    assert s["status"] == "suggested" and s["briefing"] == "briefings/o__r__7"


def test_record_passes_keeps_latest_reason(tmp_path):
    picks = tmp_path / "picks"
    picks.mkdir()
    (picks / "2026-09-26.json").write_text(json.dumps({"date": "2026-09-26", "picked": ["o/r#7"], "considered": [
        {"key": "o/r#1", "decision": "skipped", "reason": "claimed"},
        {"key": "o/r#2", "decision": "deferred", "reason": "good, but over budget tonight"},
        {"key": "o/r#7", "decision": "skipped", "reason": "picked by a later run"}]}))
    (picks / "2026-09-28.json").write_text(json.dumps({"date": "2026-09-28", "picked": [], "considered": [
        {"key": "o/r#1", "decision": "skipped", "reason": "still claimed"}]}))
    (picks / "2026-09-27.json").write_text(json.dumps({"date": "2026-09-27", "picked": [], "considered": [],
                                                       "note": "Scan missing"}))
    st = statemod.load(tmp_path)
    st["suggestions"]["o/r#7"] = {}
    assert briefings.record_passes(tmp_path, st) == 1
    assert briefings.record_passes(tmp_path, st) == 0
    assert st["passed"] == {"o/r#1": {"at": "2026-09-28T00:00:00+00:00", "reason": "still claimed"}}



# -- the inbox ------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ago = lambda **kw: (NOW - timedelta(**kw)).isoformat(timespec="seconds")
PATCH = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"


def sugg(key, status="suggested", **extra):
    repo, n = key.split("#")
    return {"repo": repo, "number": int(n), "title": f"Title of {key}", "url": f"https://github.com/{repo}/issues/{n}",
            "status": status, "suggested_at": ago(hours=10), "briefing": f"briefings/{briefings.slug(key)}",
            "difficulty": "easy", "mode": "draft", "kind": extra.pop("kind", "pr"), "history": [], **extra}


def ready_briefing(data, key, disclosure=None, kind="pr", **kw):
    b = {**good_briefing(key), "ready": True, "kind": kind, "models_used": [{"model": "sonnet", "did": "draft"}], **kw}
    d = data / "briefings" / briefings.slug(key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "briefing.json").write_text(json.dumps(b))
    if kind == "pr":
        (d / "draft.patch").write_text(PATCH)
        body = "Fixes it." + (f" {disclosure}" if disclosure else "")
        (d / "pr.json").write_text(json.dumps({"title": "Fix it", "body": body, "base": "main", "branch": "fix/x",
                                               "fixes": int(key.split("#")[1]), "disclosure": disclosure,
                                               "commit_message": "Fix it", "signoff": False}))
    else:
        (d / "post.md").write_text("Reproduced on main.")
        (d / "briefing.json").write_text(json.dumps({**b, "post_target": f"https://github.com/{b['repo']}/issues/{b['number']}"}))
    return d


def open_pr(repo, n, waiting=False, **kw):
    return {"repo": repo, "number": n, "title": f"PR {n}", "url": f"https://github.com/{repo}/pull/{n}",
            "status": "open", "created_at": ago(days=3), "updated_at": ago(hours=2), "merged_at": None,
            "waiting_on_you": waiting, "author_association": "CONTRIBUTOR", **kw}


def comment(at, body="please rename x", **kw):
    return {"author": "rev", "kind": "comment", "at": at, "body": body, "url": "https://github.com/o/r/pull/1#c", **kw}


def followup(data, key, pr_url, **fu):
    rel = f"briefings/{briefings.slug(key)}/followups/2026-10-01"
    d = data / rel
    d.mkdir(parents=True)
    (d / "followup.json").write_text(json.dumps({"pr_url": pr_url, "comments_addressed": ["rename x"], **fu}))
    if fu.get("patch"):
        (d / fu["patch"]).write_text("+fix\n")
    return rel


def world(tmp_path):
    """One of everything: waiting (small, discuss, no draft/overdue), ready, pairing, briefings, snoozed."""
    data = tmp_path
    st = statemod.load(data)
    S = st["suggestions"]
    for key in ("o/r#1", "o/r#2", "o/r#3", "o/r#4", "o/r#5", "o/r#7", "o/r#8", "o/r#9"):
        write_briefing(data, good_briefing(key))
    write_briefing(data, {**good_briefing("o/r#6"), "mode": "guide"})
    ready_briefing(data, "o/r#10", disclosure="I used an AI assistant; I checked it.")
    ready_briefing(data, "o/r#11", kind="triage")
    S["o/r#1"] = sugg("o/r#1", "waiting_on_you", pr_url="https://github.com/o/r/pull/101", followup=followup(
        data, "o/r#1", "https://github.com/o/r/pull/101", kind="small", reply="Renamed, thanks.", patch="followup.patch"))
    S["o/r#2"] = sugg("o/r#2", "waiting_on_you", pr_url="https://github.com/o/r/pull/102", followup=followup(
        data, "o/r#2", "https://github.com/o/r/pull/102", kind="discuss", talking_points=["Why connect?"]))
    S["o/r#10"] = sugg("o/r#10", "ready")
    S["o/r#11"] = sugg("o/r#11", "approved", kind="triage", post_target="https://github.com/o/r/issues/11",
                       last_error="GitHub said no")
    S["o/r#4"] = sugg("o/r#4", pairing=True, pairing_at=ago(hours=1))
    S["o/r#5"] = sugg("o/r#5", suggested_at=ago(hours=8))
    S["o/r#6"] = sugg("o/r#6", suggested_at=ago(hours=9), mode="guide")
    S["o/r#7"] = sugg("o/r#7", snoozed_until=ago(hours=-48))
    S["o/r#8"] = sugg("o/r#8", "claimed")
    S["o/r#9"] = sugg("o/r#9", "submitting", submitting_at=ago(minutes=2))
    st["contributions"] = {"prs": [
        open_pr("o/r", 101, True, review_comments=[comment(ago(hours=5))]),
        open_pr("o/r", 102, True, review_comments=[comment(ago(hours=20))]),
        open_pr("o/r", 103, True, review_comments=[comment(ago(days=3), "stranger </script><script>alert(1)</script>")]),
        open_pr("o/r", 104),
        {**open_pr("o/r", 105), "status": "merged", "merged_at": ago(days=9)},
        {**open_pr("o/r", 106), "status": "merged", "merged_at": ago(days=30)},
        {**open_pr("o/r", 107), "status": "closed"},
    ], "reviews": [], "issues": [], "per_repo": {}, "team_prs": [
        {**open_pr("team/secret", 1), "status": "merged", "merged_at": ago(days=2)}]}
    st["public"] = {"featured": ["https://github.com/o/r/pull/105"],
                    "summaries": {"https://github.com/o/r/pull/106": "Made it faster"}}
    st["actions"] = [{"at": ago(days=d), "key": "o/r#1", "action": "later", "result": "ok"} for d in range(40)]
    st["runs"] = [{"at": ago(days=d), "raw": 5, "eligible": 4, "candidates": 3, "seconds": 9} for d in range(15)]
    statemod.save(data, st)
    return data, statemod.load(data)


def payload_of(tmp_path):
    data, st = world(tmp_path)
    return data, st, render.build_payload(config.load(), data, st, now=NOW)


def groups(p):
    return {g["id"]: [i["key"] for i in g["items"]] for g in p["inbox"]}


def test_inbox_groups_come_in_urgency_order(tmp_path):
    _, _, p = payload_of(tmp_path)
    assert [g["id"] for g in p["inbox"]] == ["waiting", "ready", "pairing", "briefings", "snoozed"]
    g = groups(p)
    # overdue (3 days) first, then the oldest wait; the bare PR has no suggestion, so its key is owner/repo#n
    assert g["waiting"] == ["o/r#103", "o/r#2", "o/r#1"]
    assert g["ready"] == ["o/r#10", "o/r#11"]          # a ready PR, and an approved comment that failed to send
    assert g["pairing"] == ["o/r#4"]
    assert g["briefings"] == ["o/r#5", "o/r#6"]          # newest first; not pairing, not snoozed
    assert g["snoozed"] == ["o/r#7"]                     # the future snooze goes last
    assert p["to_do"] == 3 + 2 + 1 + 2                   # everything but the snoozed group


def test_waiting_items_say_whether_they_are_overdue(tmp_path):
    _, _, p = payload_of(tmp_path)
    w = {i["key"]: i for i in p["inbox"][0]["items"]}
    assert w["o/r#103"]["overdue"] is True and w["o/r#2"]["overdue"] is False
    assert w["o/r#103"]["followup"] is None            # no draft yet
    assert w["o/r#103"]["comments"][0]["author"] == "rev" and w["o/r#103"]["pr_number"] == 103


def test_snoozed_and_pairing_rules(tmp_path):
    data, st = world(tmp_path)
    st["suggestions"]["o/r#10"]["pairing"] = True               # a prepared item stays in Ready
    st["suggestions"]["o/r#4"]["status"] = "skipped"            # no longer active: leaves the pairing queue
    st["suggestions"]["o/r#5"]["snoozed_until"] = ago(days=1)   # a snooze that has run out is back in the list
    st["suggestions"]["o/r#10"]["snoozed_until"] = ago(hours=-5)
    g = groups(render.build_payload(config.load(), data, st, now=NOW))
    assert g["pairing"] == [] and g["ready"] == ["o/r#11"]
    assert "o/r#5" in g["briefings"] and sorted(g["snoozed"]) == ["o/r#10", "o/r#7"]
    st["suggestions"]["o/r#10"].pop("snoozed_until")
    p = render.build_payload(config.load(), data, st, now=NOW)
    ready = {i["key"]: i for i in p["inbox"][1]["items"]}
    assert ready["o/r#10"]["pairing"] is True
    assert "o/r#10" not in groups(p)["pairing"]


def test_every_row_has_its_slug(tmp_path):
    _, _, p = payload_of(tmp_path)
    for gr in p["inbox"]:
        for i in gr["items"]:
            assert i["slug"] == briefings.slug(i["key"])
    rows = {r["key"]: r for r in p["in_flight"]}
    assert all(r["slug"] == briefings.slug(r["key"]) for r in rows.values())
    assert rows["o/r#104"]["slug"] == "o__r__104" and rows["o/r#104"]["in_inbox"] is False   # a bare PR key
    assert rows["o/r#103"]["in_inbox"] is True                  # the inbox row owns this deep link
    html = (ROOT / "dashboard" / "template.html").read_text()
    assert 'id="${esc(it.slug)}"' in html and "hashchange" in html


def test_ready_items_carry_what_their_detail_view_needs(tmp_path):
    _, _, p = payload_of(tmp_path)
    pr_item, comment_item = p["inbox"][1]["items"]
    assert pr_item["patch"] == PATCH and pr_item["pr"]["disclosure"] == "I used an AI assistant; I checked it."
    assert pr_item["pr"]["title"] == "Fix it" and pr_item["problems"] == []
    assert pr_item["briefing"]["summary"] == "s" and pr_item["briefing"]["models_used"][0]["model"] == "sonnet"
    assert comment_item["post"] == "Reproduced on main." and comment_item["last_error"] == "GitHub said no"
    assert comment_item["briefing"]["post_target"] == "https://github.com/o/r/issues/11"
    brief = p["inbox"][3]["items"][1]
    assert brief["briefing"]["mode"] == "pair" and "patch" not in brief and "pr" not in brief


def test_every_item_carries_the_three_plain_lines(tmp_path):
    _, _, p = payload_of(tmp_path)
    for gr in p["inbox"]:
        for i in gr["items"]:
            assert set(i["lines"]) == {"problem", "sending", "your_part"} and all(i["lines"].values()), i["key"]
    pr_item = p["inbox"][1]["items"][0]
    assert pr_item["lines"]["sending"].startswith("A pull request") and pr_item["lines"]["your_part"].startswith("Read it and tap Submit PR")
    brief = p["inbox"][3]["items"][1]
    assert brief["lines"]["sending"].startswith("Nothing prepared yet") and brief["lines"]["your_part"].startswith("Laptop: run /contribute")
    waiting = p["inbox"][0]["items"][0]
    assert waiting["lines"]["problem"].startswith("A maintainer replied")


def test_template_leads_with_the_three_lines_then_the_four_sections_in_order():
    html = (ROOT / "dashboard" / "template.html").read_text()
    for label in ("What's broken", "You'd send", "Your part"):
        assert label in html
    titles = ["What changed, file by file", "If the maintainer asks…", "What was tested, and what wasn't", "The code"]
    at = [html.index(f'fold("{t}"') for t in titles]
    assert at == sorted(at)
    ready = html[html.index("function readyFolds"):]
    assert [ready.index(f) for f in ("filesFold(b)", "qaFold(b)", "testedFold(b)", "codeFold(it.patch)")] == sorted(
        ready.index(f) for f in ("filesFold(b)", "qaFold(b)", "testedFold(b)", "codeFold(it.patch)"))
    detail = html[html.index("function detailReady"):html.index("function prepareBlock")]
    assert detail.index("threeLines(it)") < detail.index("problemsHtml(it)") < detail.index("${readyFolds(it)}") < detail.index("${fields}")
    assert 'name="title"' in detail and 'name="body"' in detail and "Submit PR" in detail  # the edit fields and Submit stay


def test_followup_content_is_embedded(tmp_path):
    _, _, p = payload_of(tmp_path)
    w = {i["key"]: i for i in p["inbox"][0]["items"]}
    small, discuss = w["o/r#1"]["followup"], w["o/r#2"]["followup"]
    assert small["kind"] == "small" and small["reply"] == "Renamed, thanks." and small["patch"] == "+fix\n"
    assert small["comments_addressed"] == ["rename x"] and small["problems"] == [] and small["done"] is False
    assert discuss["kind"] == "discuss" and discuss["talking_points"] == ["Why connect?"] and discuss["patch"] == ""


def test_in_flight_lists_open_prs_and_stuck_items(tmp_path):
    _, _, p = payload_of(tmp_path)
    rows = {r["key"]: r for r in p["in_flight"]}
    assert rows["o/r#104"]["status"] == "in_review" and rows["o/r#103"]["status"] == "waiting_on_you"
    assert rows["o/r#11"]["status"] == "approved" and rows["o/r#11"]["last_error"] == "GitHub said no"
    assert rows["o/r#9"]["status"] == "submitting" and rows["o/r#8"]["status"] == "claimed"
    assert not any(r["status"] == "merged" for r in p["in_flight"])
    assert p["in_flight"][0]["status"] == "waiting_on_you"      # what needs you first


def test_a_reviewer_approval_is_shown_when_visible(tmp_path):
    data, st = world(tmp_path)
    st["contributions"]["prs"][3]["review_comments"] = [comment(ago(hours=1), "LGTM", kind="review", state="APPROVED")]
    p = render.build_payload(config.load(), data, st, now=NOW)
    assert {r["key"]: r["status"] for r in p["in_flight"]}["o/r#104"] == "approved_by_reviewer"


# -- the portfolio ----------------------------------------------------------------

def test_portfolio_is_exactly_the_public_payload(tmp_path):
    data, st, p = payload_of(tmp_path)
    assert p["portfolio"] == public.build_payload(config.load(), st, now=NOW)
    assert [f["number"] for f in p["portfolio"]["featured"]] == [105]
    assert p["limits"] == {"featured": public.MAX_FEATURED, "summary": 200}


def test_only_merged_and_open_prs_can_have_a_summary(tmp_path):
    _, _, p = payload_of(tmp_path)
    shown = p["portfolio"]["featured"] + p["portfolio"]["prs"]
    assert {x["status"] for x in shown} == {"merged", "open"}
    assert 107 not in {x["number"] for x in shown}              # closed
    assert "team/secret" not in json.dumps(p["portfolio"])      # collaborator PRs never leave state
    assert {x["number"]: x["summary"] for x in shown}[106] == "Made it faster"


def test_projects_and_log_are_trimmed(tmp_path):
    _, _, p = payload_of(tmp_path)
    assert len(p["log"]["actions"]) == render.LOG_ACTIONS and len(p["log"]["runs"]) == render.LOG_RUNS
    assert p["log"]["actions"][0]["at"] == ago(days=39)         # newest first
    assert set(p["projects"]) == {"home", "repos"}


# -- the page ---------------------------------------------------------------------

def embedded(html):
    m = re.search(r"const DATA = (.*?);\n\nconst \$ =", html, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_render_embeds_data_safely(tmp_path, monkeypatch):
    data, st = world(tmp_path)
    ready_briefing(data, "o/r#12", summary="breaks on </script><script>alert(1)</script>",
                   title="</script><img src=x onerror=alert(2)>")
    st["suggestions"]["o/r#12"] = sugg("o/r#12", "ready")
    st["contributions"]["prs"][4]["title"] = "</SCRIPT><script>alert(3)</script>"
    st["public"]["summaries"]["https://github.com/o/r/pull/106"] = "</script><script>alert(4)</script>"
    (data / "briefings" / "o__r__12" / "post.md").write_text("</script><script>alert(5)</script>")
    (data / "briefings" / "o__r__12" / "pr.json").write_text(json.dumps({
        "title": "</script>", "body": "</script><script>alert(6)</script>", "base": "main", "branch": "x", "fixes": 12,
        "disclosure": None, "commit_message": "x", "signoff": False}))
    followup(data, "o/r#12", "https://github.com/o/r/pull/112", kind="small", reply="</script><script>alert(7)</script>")
    st["suggestions"]["o/r#1"]["followup"] = f"briefings/o__r__12/followups/2026-10-01"
    monkeypatch.setenv("GH_TOKEN", "ghp_secret_sentinel")
    monkeypatch.setenv("SUBMIT_TOKEN", "ghp_secret_sentinel")
    html = render.render(config.load(), data, st).read_text()
    assert "/*__SCOUT_DATA__*/null" not in html
    assert html.lower().count("</script") == 4                   # the template's own four tags (theme, marked, purify, app), nothing from strangers
    assert "ghp_secret_sentinel" not in html
    payload = embedded(html)
    assert "</script><script>alert(1)" in json.dumps(payload)    # the text survives, just not as markup
    assert payload["portfolio"]["generated_at"] and not {"ready_items", "waiting", "actions"} & set(payload)
    ready = {i["key"]: i for i in payload["inbox"][1]["items"]}
    assert ready["o/r#12"]["post"].startswith("</script>") and ready["o/r#12"]["pr"]["body"].startswith("</script>")


def test_template_keeps_its_guards():
    html = (ROOT / "dashboard" / "template.html").read_text()
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert scripts and all(s.startswith("https://cdnjs.cloudflare.com/") for s in scripts)
    hosts = set(re.findall(r"https?://([a-z0-9.-]+)", html))
    assert hosts <= {"cdnjs.cloudflare.com", "fonts.googleapis.com", "fonts.gstatic.com", "github.com", "www.w3.org"}
    assert "DOMPurify.sanitize" in html and "const esc =" in html and "https:" in html
    assert "/*__SCOUT_DATA__*/null" in html
    for tab in ("Inbox", "In flight", "Portfolio", "Projects", "Log"):
        assert f'label: "{tab}"' in html
    for act in ("submit", "post", "followup", "later", "skip", "prepare", "pair", "unpair", "feature", "unfeature", "summary"):
        assert re.search(rf'"{act}"', html), act
    assert "act: ${action} ${key}" in html          # matches the workflow's run name


def test_dashboard_renders_with_no_data(tmp_path):
    p = render.build_payload(config.load(), tmp_path, statemod.load(tmp_path), now=NOW)
    assert [g["items"] for g in p["inbox"]] == [[], [], [], [], []] and p["to_do"] == 0 and p["in_flight"] == []
    assert p["digest"] == {} and p["note"] == ""
    assert embedded(render.render(config.load(), tmp_path, statemod.load(tmp_path)).read_text())["to_do"] == 0
