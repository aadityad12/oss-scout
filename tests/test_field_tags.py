"""Field tag, project intro and plan steps: briefing validation, digest fallbacks, dashboard payload."""

import json
from datetime import datetime, timezone

import pytest

from scout import briefings, config, digest, fields, render, state as statemod

NOW = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)

BASE = {
    "key": "o/r#7", "repo": "o/r", "number": 7, "title": "Crash on empty input", "url": "https://github.com/o/r/issues/7",
    "summary": "It crashes.", "why": "w", "difficulty": "easy", "time_estimate": "1h", "walkthrough": "w",
    "change_explained": "c", "alternatives": [], "maintainer_qa": [], "tests": {"ran": False},
    "claim_comment": "I'd like to fix this.", "submit_steps": ["Fork"], "ai_disclosure": "none",
}


def b(**kw):
    return {**BASE, **kw}


# -- validation -------------------------------------------------------------------

def test_old_briefings_without_the_new_fields_still_validate():
    assert briefings.validate(b()) == []


def test_good_new_fields_validate():
    good = b(field={"id": "embedded", "also": "security", "why": "It runs on a tiny chip."},
             project_intro="Zephyr is a small operating system for gadgets. The Linux Foundation backs it.",
             plan_steps=["Read the bug", "Write a fix", "Run the tests"])
    assert briefings.validate(good) == []
    assert briefings.validate(b(field={"id": "web", "why": "A web server."})) == []  # `also` is optional
    assert briefings.validate(b(plan_steps=["one"])) == [] and briefings.validate(b(plan_steps=["x"] * 6)) == []


@pytest.mark.parametrize("field", [
    "embedded", None, ["embedded"], {}, {"why": "w"},
    {"id": "not-a-field", "why": "w"}, {"id": 3, "why": "w"},
    {"id": "embedded"}, {"id": "embedded", "why": ""}, {"id": "embedded", "why": "  "}, {"id": "embedded", "why": 5},
    {"id": "embedded", "also": "nope", "why": "w"}, {"id": "embedded", "also": 4, "why": "w"},
])
def test_malformed_field_is_a_problem(field):
    problems = briefings.validate(b(field=field))
    assert problems and all(p.startswith("field") for p in problems)


@pytest.mark.parametrize("intro", ["", "   ", 3, None, ["a"]])
def test_malformed_project_intro_is_a_problem(intro):
    assert briefings.validate(b(project_intro=intro)) == ["project_intro should be a non-empty string"]


@pytest.mark.parametrize("steps", [[], ["x"] * 7, "one step", None, [1], ["ok", ""], ["ok", "  "], {"a": "b"}])
def test_malformed_plan_steps_are_a_problem(steps):
    assert briefings.validate(b(plan_steps=steps)) == ["plan_steps should be a list of 1-6 non-empty strings"]


def test_a_briefing_with_a_bad_field_is_not_ingested(tmp_path):
    d = tmp_path / "briefings" / "o__r__7"
    d.mkdir(parents=True)
    (d / "briefing.json").write_text(json.dumps(b(field={"id": "nope", "why": "w"})))
    st = {"suggestions": {}}
    assert briefings.ingest(tmp_path, st) == 0


# -- digest -------------------------------------------------------------------------

def put(data, key, **extra):
    d = data / "briefings" / briefings.slug(key)
    d.mkdir(parents=True)
    repo, n = key.split("#")
    (d / "briefing.json").write_text(json.dumps(b(key=key, repo=repo, number=int(n), **extra)))


def build(tmp_path, sugg, repos=None, cfg=None):
    st = {"suggestions": sugg, "contributions": {"prs": []}, "repos": repos or {}}
    return digest.build(cfg or config.load(), tmp_path, st, NOW)


def sg(status="suggested", at="2026-10-10T01:00:00+00:00", **kw):
    return {"status": status, "title": "T", "suggested_at": at, **kw}


def test_the_briefings_own_field_wins_over_every_fallback(tmp_path):
    put(tmp_path, "o/r#1", field={"id": "ai-ml", "also": "web", "why": "It speeds up a model server."},
        project_intro="Own intro.", plan_steps=["Read", "Fix", "Test"])
    cfg = config.load()
    cfg.tiers[0].repos.append("o/r")
    cfg.tiers[0].field = "embedded"
    cfg.repo_fields["o/r"] = "security"
    d = build(tmp_path, {"o/r#1": sg()}, {"o/r": {"description": "A thing.", "stars": 5000, "topics": ["rtos"]}}, cfg)
    [i] = d["new_today"]
    assert i["field"] == {"id": "ai-ml", "label": "AI / ML engineering", "also": "web", "also_label": "Web & backend",
                          "why": "It speeds up a model server."}
    assert i["project_intro"] == "Own intro." and i["plan_steps"] == ["Read", "Fix", "Test"]


def test_field_falls_back_from_override_to_tier_to_topics(tmp_path):
    put(tmp_path, "o/r#1")
    cfg = config.load()
    cfg.tiers[0].repos.append("o/r")
    cfg.tiers[0].field = "systems"
    repos = {"o/r": {"topics": ["rtos"], "description": "x"}}
    f = build(tmp_path, {"o/r#1": sg()}, repos, cfg)["new_today"][0]["field"]
    assert f["id"] == "systems" and f["also"] is None and f["why"] == fields.describe("systems")  # the tier beats topics
    cfg.repo_fields["o/r"] = "security"
    assert build(tmp_path, {"o/r#1": sg()}, repos, cfg)["new_today"][0]["field"]["id"] == "security"  # the override beats the tier
    cfg.tiers[0].repos.remove("o/r")
    cfg.repo_fields.clear()
    assert build(tmp_path, {"o/r#1": sg()}, repos, cfg)["new_today"][0]["field"]["id"] == "embedded"  # from topics
    assert build(tmp_path, {"o/r#1": sg()}, {"o/r": {"description": "A kubernetes proxy"}}, cfg)["new_today"][0]["field"]["id"] == "cloud"


def test_field_is_null_when_nothing_is_known(tmp_path):
    put(tmp_path, "o/r#1")
    i = build(tmp_path, {"o/r#1": sg()})["new_today"][0]
    assert i["field"] is None and i["project_intro"] == "" and i["plan_steps"] == []


def test_an_unknown_field_id_in_a_briefing_falls_back_to_the_hint(tmp_path):
    put(tmp_path, "o/r#1", field={"id": "made-up", "why": "w"})
    f = build(tmp_path, {"o/r#1": sg()}, {"o/r": {"topics": ["security"]}})["new_today"][0]["field"]
    assert f["id"] == "security" and f["why"] == fields.describe("security")


def test_intro_falls_back_to_the_repo_description_and_stars(tmp_path):
    put(tmp_path, "duckdb/duckdb#1")
    repos = {"duckdb/duckdb": {"description": "DuckDB is an analytical in-process SQL database management system",
                               "stars": 43210}}
    i = build(tmp_path, {"duckdb/duckdb#1": sg()}, repos)["new_today"][0]
    assert i["project_intro"] == "DuckDB is an analytical in-process SQL database management system. About 43k stars on GitHub."


@pytest.mark.parametrize("info,want", [
    ({"description": "A fast linter.", "stars": 850}, "r: A fast linter. About 850 stars on GitHub."),  # the name is added
    ({"description": "r is fast :rocket:", "stars": 4300}, "r is fast. About 4.3k stars on GitHub."),
    ({"stars": 1_500_000}, "About 1.5M stars on GitHub."),
    ({"description": "r is fast."}, "r is fast."),
    ({"stars": 0, "description": None}, ""),
    ({}, ""),
])
def test_intro_fallback_shapes(info, want):
    assert digest.intro_fallback("o/r", info) == want


def test_a_blank_intro_in_the_briefing_uses_the_fallback_and_plan_steps_are_capped(tmp_path):
    put(tmp_path, "o/r#1", project_intro="x", plan_steps=["a", "b", "c", "d", "e", "f"])
    i = build(tmp_path, {"o/r#1": sg()}, {"o/r": {"description": "r does things."}})["new_today"][0]
    assert i["project_intro"] == "x" and i["plan_steps"] == ["a", "b", "c", "d", "e"]


def test_every_item_list_carries_the_three_extras(tmp_path):
    for k in range(1, 5):
        put(tmp_path, f"o/r#{k}", field={"id": "web", "why": "w"}, plan_steps=["go"])
    sugg = {
        "o/r#1": sg("ready"),
        "o/r#2": sg("approved", failure={"plain": "p", "why": "w", "fix_action": "refresh"}),
        "o/r#3": sg(at="2026-10-06T01:00:00+00:00"),           # weekly only
        "o/r#4": sg(),                                         # today
    }
    d = build(tmp_path, sugg)
    for name in ("ready", "stuck", "weekly", "new_today"):
        assert d[name], name
        for i in d[name]:
            assert i["field"]["id"] == "web" and i["plan_steps"] == ["go"] and "project_intro" in i, (name, i["key"])
    assert [i["key"] for i in d["new_today"]] == ["o/r#4"]


def test_new_today_matches_new_briefings_and_turns_send_on(tmp_path):
    put(tmp_path, "o/r#1")
    put(tmp_path, "o/r#2")
    (tmp_path / "picks").mkdir()
    (tmp_path / "picks" / "2026-10-10.json").write_text(json.dumps({"picked": ["o/r#2"]}))
    sugg = {"o/r#1": sg(at="2026-10-10T03:00:00+00:00"), "o/r#2": sg(at="2026-10-09T03:00:00+00:00"),
            "o/r#3": sg(at="2026-10-01T03:00:00+00:00")}
    d = build(tmp_path, sugg)
    assert [i["key"] for i in d["new_today"]] == [i["key"] for i in d["new_briefings"]] == ["o/r#1", "o/r#2"]
    assert {"problem", "sending", "your_part", "headline", "slug", "title", "kind"} <= set(d["new_today"][0])
    assert d["send"] is True
    assert build(tmp_path, {"o/r#3": sg(at="2026-10-01T03:00:00+00:00")})["send"] is False


# -- the dashboard payload -------------------------------------------------------------

def test_dashboard_items_carry_the_same_extras_as_the_digest(tmp_path):
    put(tmp_path, "o/r#1", field={"id": "security", "why": "It checks certificates."}, project_intro="Intro.",
        plan_steps=["Read", "Fix"])
    put(tmp_path, "o/r#2")
    st = statemod.load(tmp_path)
    st["suggestions"] = {"o/r#1": sg(), "o/r#2": sg()}
    st["repos"] = {"o/r": {"description": "r is a thing", "stars": 2000, "topics": ["kubernetes"]}}
    statemod.save(tmp_path, st)
    st = statemod.load(tmp_path)
    p = render.build_payload(config.load(), tmp_path, st, now=NOW)
    items = {i["key"]: i for g in p["inbox"] for i in g["items"]}
    one, two = items["o/r#1"], items["o/r#2"]
    assert one["field"]["label"] == "Security & crypto" and one["field"]["why"] == "It checks certificates."
    assert one["project_intro"] == "Intro." and one["plan_steps"] == ["Read", "Fix"]
    assert two["field"]["id"] == "cloud" and two["project_intro"] == "r is a thing. About 2k stars on GitHub." \
        and two["plan_steps"] == []
    d = digest.build(config.load(), tmp_path, st, NOW)
    by_key = {i["key"]: i for i in d["new_today"]}
    for k in ("o/r#1", "o/r#2"):
        assert {n: items[k][n] for n in ("field", "project_intro", "plan_steps")} == \
               {n: by_key[k][n] for n in ("field", "project_intro", "plan_steps")}
def test_template_shows_the_tag_the_intro_and_the_plan():
    html = (config.ROOT / "dashboard" / "template.html").read_text()
    for needle in ("fieldTag(", "Why this field", "About the project", "What we'll do"):
        assert needle in html, needle
    for fn in ("detailReady", "detailBriefing"):
        body = html[html.index(f"function {fn}"):]
        body = body[:body.index("\nfunction ", 10)]
        assert "fieldHead(it)" in body and "aboutPlan(it)" in body, fn
        assert body.index("aboutPlan(it)") < body.index("folds(") if fn == "detailBriefing" else body.index("aboutPlan(it)") < body.index("readyFolds(it)")


def test_ingest_says_why_a_new_briefing_was_skipped(tmp_path, capsys):
    import json as _json
    from scout import briefings, state as statemod
    d = tmp_path / "briefings" / "o__r__1"
    d.mkdir(parents=True)
    _json.dump({"key": "o/r#1", "field": {"id": "nope", "why": "x"}}, (d / "briefing.json").open("w"))
    st = statemod.load(tmp_path)
    assert briefings.ingest(tmp_path, st) == 0
    err = capsys.readouterr().err
    assert "skipped briefings/o__r__1" in err and "field.id" in err
