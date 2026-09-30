import json

from scout import briefings, config, render, state as statemod


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


def test_guide_mode_never_shows_a_patch(tmp_path):
    b = {**good_briefing(), "mode": "guide"}
    write_briefing(tmp_path, b, patch="+secret draft\n")
    loaded = briefings.load_all(tmp_path)[0]
    assert loaded["_patch"] == "" and loaded["_problems"] == []
    assert briefings.validate({**b, "mode": "yolo"}) == ["mode should be one of ['draft', 'guide']"]


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


def test_render_embeds_data_safely(tmp_path):
    b = good_briefing()
    b["summary"] = "breaks on </script><script>alert(1)</script>"
    write_briefing(tmp_path, b)
    (tmp_path / "picks").mkdir()
    (tmp_path / "picks" / "2026-09-26.json").write_text(json.dumps(
        {"date": "2026-09-26", "picked": ["o/r#7"], "considered": [], "note": "n"}))
    st = statemod.load(tmp_path)
    briefings.ingest(tmp_path, st)
    out = render.render(config.load(), tmp_path, st)
    html = out.read_text()
    assert "/*__SCOUT_DATA__*/null" not in html
    assert "</script><script>alert(1)" not in html
    payload = render.build_payload(config.load(), tmp_path, st)
    assert payload["today"]["picks"][0]["patch"] == "+x\n"
    assert payload["today"]["picks"][0]["briefing"]["summary"].startswith("breaks on")
