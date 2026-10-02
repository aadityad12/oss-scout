import json
from datetime import datetime, timezone

import pytest

from scout import __main__ as cli, config, track

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
URL = "https://github.com/o/r/pull/5"


def waiting_pr(n=5, at="2026-10-02T09:00:00Z", body="please rename x", repo="o/r", **kw):
    return {"repo": repo, "number": n, "url": f"https://github.com/{repo}/pull/{n}", "status": "open",
            "waiting_on_you": True, "updated_at": at,
            "review_comments": [{"author": "maint", "at": at, "body": body}], **kw}


def state(*prs, alerted=None, suggestions=None):
    return {"contributions": {"prs": list(prs)}, "alerted": alerted or {}, "suggestions": suggestions or {}}


def alerts_file(tmp_path):
    return json.loads((tmp_path / "alerts.json").read_text())


def test_a_pr_that_starts_waiting_is_a_new_alert(tmp_path):
    st = state(waiting_pr())
    new = track.alert(st, tmp_path, NOW)
    assert new == [{"at": "2026-10-02T09:00:00Z", "key": "o/r#5", "slug": "o__r__5", "pr_url": URL,
                    "author": "maint", "excerpt": "please rename x"}]
    assert st["alerted"] == {URL: "2026-10-02T09:00:00Z"}
    assert alerts_file(tmp_path) == {"generated_at": "2026-10-02T12:00:00+00:00", "alerts": new}


def test_the_same_review_does_not_alert_twice(tmp_path):
    st = state(waiting_pr())
    track.alert(st, tmp_path, NOW)
    assert track.alert(st, tmp_path, NOW) == []
    assert alerts_file(tmp_path)["alerts"] == []  # only this run's alerts
    assert st["alerted"] == {URL: "2026-10-02T09:00:00Z"}


def test_a_newer_comment_alerts_again_and_an_older_one_does_not(tmp_path):
    st = state(waiting_pr(at="2026-10-02T10:00:00Z"), alerted={URL: "2026-10-02T09:00:00Z"})
    new = track.alert(st, tmp_path, NOW)
    assert [a["at"] for a in new] == ["2026-10-02T10:00:00Z"]
    assert st["alerted"][URL] == "2026-10-02T10:00:00Z"
    st = state(waiting_pr(at="2026-10-02T08:00:00Z"), alerted={URL: "2026-10-02T09:00:00Z"})
    assert track.alert(st, tmp_path, NOW) == []


def test_the_latest_unanswered_comment_is_the_one_that_counts(tmp_path):
    pr = waiting_pr()
    pr["review_comments"] = [{"author": "a", "at": "2026-10-02T11:00:00Z", "body": "late"},
                             {"author": "b", "at": "2026-10-01T11:00:00Z", "body": "early"}]
    new = track.alert(state(pr), tmp_path, NOW)
    assert (new[0]["at"], new[0]["author"], new[0]["excerpt"]) == ("2026-10-02T11:00:00Z", "a", "late")


def test_prs_no_longer_waiting_are_dropped_so_a_later_review_alerts(tmp_path):
    st = state(waiting_pr(), alerted={URL: "2026-10-02T09:00:00Z", "https://github.com/o/r/pull/6": "x",
                                      "https://github.com/o/r/pull/7": "x"})
    st["contributions"]["prs"] += [{**waiting_pr(6), "waiting_on_you": False},
                                   {**waiting_pr(7), "status": "merged"}]
    assert track.alert(st, tmp_path, NOW) == []
    assert st["alerted"] == {URL: "2026-10-02T09:00:00Z"}
    st["contributions"]["prs"][0]["waiting_on_you"] = False
    track.alert(st, tmp_path, NOW)
    assert st["alerted"] == {}
    st["contributions"]["prs"][0]["waiting_on_you"] = True
    assert len(track.alert(st, tmp_path, NOW)) == 1


def test_key_comes_from_the_suggestion_that_owns_the_pr(tmp_path):
    st = state(waiting_pr(), suggestions={"o/r#2": {"pr_url": URL}, "o/r#3": {"pr_url": "https://x"}})
    new = track.alert(st, tmp_path, NOW)
    assert (new[0]["key"], new[0]["slug"]) == ("o/r#2", "o__r__2")


def test_excerpt_collapses_whitespace_and_stops_at_140(tmp_path):
    new = track.alert(state(waiting_pr(body="a\n\n  b\t" + "c" * 300)), tmp_path, NOW)
    assert new[0]["excerpt"].startswith("a b ccc") and len(new[0]["excerpt"]) == 140


def test_a_bare_change_request_alerts_from_the_pr_update_time(tmp_path):
    pr = waiting_pr(at="2026-10-02T09:00:00Z")
    del pr["review_comments"]
    new = track.alert(state(pr), tmp_path, NOW)
    assert (new[0]["at"], new[0]["author"], new[0]["excerpt"]) == ("2026-10-02T09:00:00Z", "a reviewer", "")


def test_no_prs_still_writes_an_empty_alerts_file(tmp_path):
    assert track.alert({"contributions": {}}, tmp_path, NOW) == []
    assert alerts_file(tmp_path)["alerts"] == []


# -- extra Claude runs ------------------------------------------------------------

def test_extra_drafts_are_capped_per_day_and_reset_on_a_new_day():
    st, new = {}, [{"key": "o/r#5"}]
    assert track.extra_draft(st, [], NOW) is False and st["extra_drafts"] == {"date": "2026-10-02", "n": 0}
    assert [track.extra_draft(st, new, NOW) for _ in range(4)] == [True, True, True, False]
    assert st["extra_drafts"] == {"date": "2026-10-02", "n": 3}
    assert track.extra_draft(st, new, datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc)) is True
    assert st["extra_drafts"] == {"date": "2026-10-03", "n": 1}


def test_no_new_alerts_do_not_count():
    st = {"extra_drafts": {"date": "2026-10-02", "n": 2}}
    assert track.extra_draft(st, [], NOW) is False and st["extra_drafts"]["n"] == 2


# -- the commands -------------------------------------------------------------------

@pytest.fixture
def cmd(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    prs = [waiting_pr()]
    monkeypatch.setattr(track, "collect", lambda gh, login: {"login": login, "prs": prs, "per_repo": {}})
    monkeypatch.setattr(track, "update_suggestions", lambda *a, **k: None)
    return prs


def test_track_writes_fire_to_github_output(cmd, tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    cli.cmd_track(None, config.load(), tmp_path, None)
    assert out.read_text() == "fire=true\n"
    cli.cmd_track(None, config.load(), tmp_path, None)  # nothing new
    assert out.read_text() == "fire=true\nfire=false\n"
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["extra_drafts"]["n"] == 1 and st["alerted"] == {URL: "2026-10-02T09:00:00Z"}
    assert alerts_file(tmp_path)["alerts"] == []


def test_track_prints_fire_without_github_output(cmd, tmp_path, capsys):
    cli.cmd_track(None, config.load(), tmp_path, None)
    assert capsys.readouterr().out.splitlines()[-1] == "fire=true"


def test_track_stops_firing_after_three_runs_a_day(cmd, tmp_path, capsys):
    fired = []
    for n in range(5):
        cmd[:] = [waiting_pr(at=f"2026-10-02T0{n}:00:00Z")]
        cli.cmd_track(None, config.load(), tmp_path, None)
        fired.append(capsys.readouterr().out.splitlines()[-1])
    assert fired == ["fire=true"] * 3 + ["fire=false"] * 2
    assert len(alerts_file(tmp_path)["alerts"]) == 1  # alerts are still written once the cap is hit


def test_nightly_run_updates_alerts_but_never_fires_or_counts(tmp_path, monkeypatch, capsys):
    from scout import discover
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(track, "collect", lambda gh, login: {"login": login, "prs": [waiting_pr()], "per_repo": {}})
    monkeypatch.setattr(track, "update_suggestions", lambda *a, **k: None)
    monkeypatch.setattr(discover, "from_tiers", lambda gh, cfg: [])
    class Gh:
        calls = 0
    args = type("A", (), {"no_discovery": True})()
    cli.cmd_run(args, config.load(), tmp_path, Gh())
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["alerted"] == {URL: "2026-10-02T09:00:00Z"} and "extra_drafts" not in st
    assert [a["pr_url"] for a in alerts_file(tmp_path)["alerts"]] == [URL]
    assert "fire=" not in capsys.readouterr().out
    cli.cmd_track(None, config.load(), tmp_path, None)  # the nightly already alerted this one
    assert capsys.readouterr().out.splitlines()[-1] == "fire=false"


def test_nightly_candidates_list_the_requested_items(tmp_path, monkeypatch):
    from scout import discover, state as statemod
    monkeypatch.setattr(track, "collect", lambda gh, login: {"login": login, "prs": [], "per_repo": {}})
    monkeypatch.setattr(track, "update_suggestions", lambda *a, **k: None)
    monkeypatch.setattr(discover, "from_tiers", lambda gh, cfg: [])
    st = statemod.load(tmp_path)
    st["suggestions"] = {"o/r#1": {"status": "suggested", "prepare_requested_at": "2026-10-02T09:00:00+00:00"},
                         "o/r#2": {"status": "suggested", "prepare_requested_at": "2026-10-01T09:00:00+00:00"},
                         "o/r#3": {"status": "suggested"}}
    statemod.save(tmp_path, st)
    class Gh:
        calls = 0
    cli.cmd_run(type("A", (), {"no_discovery": True})(), config.load(), tmp_path, Gh())
    assert json.loads((tmp_path / "candidates.json").read_text())["requested"] == ["o/r#2", "o/r#1"]
