"""The tracker's new jobs: competing pull requests, and drafts that went stale after a failed send."""

import pytest

from datetime import datetime, timedelta, timezone

from scout import briefings, state as statemod, track
from scout.github import GitHub, RateLimited
from test_ready import PR_JSON, good_briefing, write
from test_scout import Fake

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
CONTRIB = {"prs": [], "commented_issues": [], "login": "aadityad12"}


def cross(number=50, state="open", login="someone", repo="o/r", kind="User", pr=True, event="cross-referenced"):
    issue = {"state": state, "html_url": f"https://github.com/{repo}/pull/{number}", "user": {"login": login, "type": kind}}
    if pr:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"}
    return {"event": event, "source": {"type": "issue", "issue": issue}}


def get_of(events):
    return lambda path: events


# -- competing_pr ---------------------------------------------------------------

def test_an_open_pr_by_someone_else_is_a_rival():
    assert track.competing_pr(get_of([cross()]), "o/r", 1, "aadityad12") == "https://github.com/o/r/pull/50"


@pytest.mark.parametrize("name,event", [
    ("your own PR", cross(login="AadityaD12")),
    ("a closed PR", cross(state="closed")),
    ("a bot's PR", cross(kind="Bot", login="dependabot[bot]")),
    ("a PR in another repo", cross(repo="x/y")),
    ("an issue, not a PR", cross(pr=False)),
    ("some other kind of event", cross(event="labeled")),
    ("a bare event", {"event": "cross-referenced"}),
])
def test_things_that_are_not_rivals(name, event):
    assert track.competing_pr(get_of([event]), "o/r", 1, "aadityad12") is None, name


def test_the_timeline_is_read_page_by_page_and_best_effort():
    pages = []

    def get(path):
        pages.append(path)
        return [cross(login="aadityad12", number=n) for n in range(100)] if "&page=1" in path else [cross(number=99)]

    assert track.competing_pr(get, "o/r", 1, "aadityad12") == "https://github.com/o/r/pull/99"
    assert len(pages) == 2 and "timeline?per_page=100&page=1" in pages[0]

    def broken(path):
        raise RuntimeError("502")

    assert track.competing_pr(broken, "o/r", 1, "aadityad12") is None

    def limited(path):
        raise RateLimited("slow down")

    with pytest.raises(RateLimited):
        track.competing_pr(limited, "o/r", 1, "aadityad12")


# -- update_suggestions -----------------------------------------------------------

def sugg(n, status, **kw):
    return {"repo": "o/r", "number": n, "status": status, "suggested_at": (NOW - timedelta(days=2)).isoformat(), **kw}


def routes(*numbers, rival=True, **issue):
    out = {}
    for n in numbers:
        out[f"repos/o/r/issues/{n}"] = {"state": "open", **issue}
        out[f"repos/o/r/issues/{n}/timeline"] = [cross(number=40 + n)] if rival else []
    return out


def test_items_with_an_open_rival_pr_become_taken_with_the_link():
    st = {"suggestions": {f"o/r#{n}": sugg(n, s) for n, s in enumerate(["suggested", "claimed", "ready", "approved", "submitting"], 1)}}
    contrib = {**CONTRIB, "commented_issues": ["o/r#2"]}
    gh = GitHub(transport=Fake(routes(1, 2, 3, 4, 5)))
    track.update_suggestions(gh, st, contrib, 14, now=NOW)
    got = {k: (v["status"], v.get("taken_by")) for k, v in st["suggestions"].items()}
    assert got["o/r#1"] == ("taken", "https://github.com/o/r/pull/41")
    assert got["o/r#2"] == ("taken", "https://github.com/o/r/pull/42")  # claimed by your comment, same
    assert got["o/r#3"] == ("taken", "https://github.com/o/r/pull/43")
    assert got["o/r#4"] == ("taken", "https://github.com/o/r/pull/44")
    assert got["o/r#5"] == ("submitting", None)  # on its way: never touched
    assert st["suggestions"]["o/r#1"]["history"][-1] == {"at": "2026-10-07T12:00:00+00:00", "from": "suggested", "to": "taken"}


def test_no_rival_means_no_change_and_comment_items_are_left_alone():
    st = {"suggestions": {"o/r#1": sugg(1, "suggested"), "o/r#2": sugg(2, "ready", kind="triage")}}
    gh = GitHub(transport=Fake(routes(1, 2, rival=False)))
    track.update_suggestions(gh, st, CONTRIB, 14, now=NOW)
    assert st["suggestions"]["o/r#1"]["status"] == "suggested"
    gh = GitHub(transport=Fake(routes(1, 2)))
    st["suggestions"]["o/r#2"]["status"] = "ready"
    track.update_suggestions(gh, st, CONTRIB, 14, now=NOW)
    assert st["suggestions"]["o/r#1"]["status"] == "taken"
    assert st["suggestions"]["o/r#2"]["status"] == "ready"  # a rival PR doesn't matter to a comment


def test_the_timeline_is_only_read_after_the_cheap_issue_check_passes():
    st = {"suggestions": {"o/r#1": sugg(1, "suggested"), "o/r#2": sugg(2, "suggested"), "o/r#3": sugg(3, "suggested")}}
    fake = Fake({**routes(3), "repos/o/r/issues/1": {"state": "closed"}, "repos/o/r/issues/2": {"state": "open", "assignees": [{"login": "x"}]}})
    track.update_suggestions(GitHub(transport=fake), st, CONTRIB, 14, now=NOW)
    assert [v["status"] for v in st["suggestions"].values()] == ["taken", "taken", "taken"]
    assert [p for p in fake.seen if "timeline" in p] == ["repos/o/r/issues/3/timeline?per_page=100&page=1"]
    assert fake.seen.count("repos/o/r/issues/3") == 1  # the issue is read once per item


def test_an_issue_that_cannot_be_read_is_not_marked_taken_by_the_timeline():
    st = {"suggestions": {"o/r#2": sugg(2, "claimed")}}
    track.update_suggestions(GitHub(transport=Fake({})), st, {**CONTRIB, "commented_issues": ["o/r#2"]}, 14, now=NOW)
    assert st["suggestions"]["o/r#2"]["status"] == "claimed"


def test_the_timeline_is_cached_for_three_hours(tmp_path):
    fake = Fake(routes(1))
    st = {"suggestions": {"o/r#1": sugg(1, "approved")}}
    gh = GitHub(cache_dir=tmp_path, transport=fake)
    track.update_suggestions(gh, st, CONTRIB, 14, now=NOW)
    st["suggestions"]["o/r#1"]["status"] = "approved"
    track.update_suggestions(GitHub(cache_dir=tmp_path, transport=fake), st, CONTRIB, 14, now=NOW)
    assert sum("timeline" in p for p in fake.seen) == 1
    assert track.TIMELINE_TTL == 3 * 3600


# -- drafts that went stale after a failed send ------------------------------------

def failed(refresh_by, **kw):
    return sugg(1, "approved", failure={"code": "upstream_moved", "refresh_by": refresh_by, "plain": "x"},
                send_after_refresh=True, **kw)


def update(item):
    st = {"suggestions": {"o/r#1": item}}
    track.update_suggestions(GitHub(transport=Fake(routes(1, rival=False))), st, CONTRIB, 14, now=NOW)
    return st["suggestions"]["o/r#1"]


def test_past_its_refresh_by_date_an_approved_item_becomes_a_briefing():
    s = update(failed("2026-10-06"))
    assert s["status"] == "suggested" and "laptop" in s["demoted_reason"] and "send_after_refresh" not in s
    assert s["history"][-1] == {"at": "2026-10-07T12:00:00+00:00", "from": "approved", "to": "suggested"}
    assert s["failure"]["code"] == "upstream_moved"  # kept for the record


def test_the_refresh_by_day_itself_is_still_fine():
    assert update(failed("2026-10-07"))["status"] == "approved"
    assert update(failed("2026-10-08"))["status"] == "approved"


def test_a_refresh_in_flight_holds_off_the_demotion():
    assert update(failed("2026-10-01", refresh_requested_at="2026-10-07T09:00:00+00:00"))["status"] == "approved"
    assert update(failed("2026-10-01", refresh_requested_at="2026-10-01T09:00:00+00:00"))["status"] == "suggested"  # asked days ago, no answer
    answered = failed("2026-10-01", refresh_requested_at="2026-10-07T09:00:00+00:00",
                      refresh_result={"at": "2026-10-07T10:00:00+00:00", "same_fix": False})
    assert update(answered)["status"] == "suggested"


def test_only_approved_items_with_a_readable_date_are_demoted():
    s = failed("2026-10-01")
    s["status"] = "ready"
    assert update(s)["status"] == "ready"
    assert update(failed("soon"))["status"] == "approved"
    no_failure = sugg(1, "approved")
    assert update(no_failure)["status"] == "approved"


def test_a_demoted_item_stays_a_briefing_until_you_ask_again(tmp_path):
    write(tmp_path, good_briefing("o/r#1", ready=True), PR_JSON)
    st = statemod.load(tmp_path)
    briefings.ingest(tmp_path, st)
    s = st["suggestions"]["o/r#1"]
    s.update(status="approved", failure={"code": "upstream_moved", "refresh_by": "2026-10-01"})
    track.update_suggestions(GitHub(transport=Fake(routes(1, rival=False))), st, CONTRIB, 14, now=NOW)
    assert s["status"] == "suggested" and s["demoted_reason"]
    briefings.ingest(tmp_path, st)
    assert s["status"] == "suggested"  # the old briefing still says ready, but it is not re-promoted
    s["prepare_requested_at"] = "2026-10-07T13:00:00+00:00"  # you tapped Prepare this
    briefings.ingest(tmp_path, st)
    assert s["status"] == "ready"
    assert not {"demoted_reason", "failure", "prepare_requested_at", "send_after_refresh"} & set(s)


def test_a_briefing_the_routine_made_plain_demotes_a_prepared_item(tmp_path):
    d = write(tmp_path, good_briefing("o/r#1", ready=True), PR_JSON)
    st = statemod.load(tmp_path)
    briefings.ingest(tmp_path, st)
    s = st["suggestions"]["o/r#1"]
    s.update(status="approved", send_after_refresh=True)
    briefings.ingest(tmp_path, st)
    assert s["status"] == "approved"  # still ready on disk: nothing changes
    write(tmp_path, good_briefing("o/r#1", ready=False), PR_JSON)
    briefings.ingest(tmp_path, st)
    assert s["status"] == "suggested" and "laptop" in s["demoted_reason"] and "send_after_refresh" not in s
    assert [h["to"] for h in s["history"]] == ["ready", "suggested"]
    assert d.exists()
    # a briefing that never said ready (an old one) does not demote anything
    write(tmp_path, good_briefing("o/r#2"), None)
    briefings.ingest(tmp_path, st)
    s2 = st["suggestions"]["o/r#2"]
    s2["status"] = "claimed"
    briefings.ingest(tmp_path, st)
    assert s2["status"] == "claimed"
