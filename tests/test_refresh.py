"""Refresh & send: the estimate, the check against a real (local) upstream, and the records it leaves."""

import json
import shlex
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout import __main__ as cli, act, briefings, config, failures, refresh, state as statemod, track
from scout.github import GitHub
from test_ready import PR_JSON, good_briefing, write
from test_scout import Fake

KEY = "o/r#7"
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
PY = shlex.quote(sys.executable)


# -- the refresh-by estimate --------------------------------------------------

@pytest.mark.parametrize("commits,days", [(0, 14), (1, 14), (2, 7), (3, 5), (5, 3), (7, 2), (8, 2), (30, 2), (100, 2)])
def test_refresh_days(commits, days):
    assert failures.refresh_days(commits) == days


def test_refresh_by_is_an_iso_date_and_falls_back_when_github_cannot_be_asked():
    today = date(2026, 10, 7)
    assert failures.refresh_by(today, 0) == "2026-10-21"
    assert failures.refresh_by(today, 3) == "2026-10-12"
    assert failures.refresh_by(today, None) == "2026-10-14"  # unknown: a week


def test_commit_count_is_distinct_commits_over_30_days_for_the_patch_files():
    seen = []

    def get(path):
        seen.append(path)
        return [{"sha": "a"}, {"sha": "b"}] if "path=x.py" in path else [{"sha": "b"}, {"sha": "c"}]

    assert failures.recent_commit_count(get, "o/r", "main", ["x.py", "y.py"], NOW) == 3
    assert all("since=2026-09-07T12%3A00%3A00Z" in p and "sha=main" in p for p in seen)

    def broken(path):
        raise RuntimeError("down")

    assert failures.recent_commit_count(broken, "o/r", "main", ["x.py"], NOW) is None


def test_latest_touch_reads_one_commit_per_file_and_keeps_the_newest():
    def get(path):
        assert "per_page=1" in path
        day = "2026-10-02" if "path=a.py" in path else "2026-10-05"
        return [{"commit": {"committer": {"date": f"{day}T08:00:00Z"}}}]

    f, at = failures.latest_touch(get, "o/r", "main", ["a.py", "b.py"])
    assert f == "b.py" and failures.day(at) == "Oct 5"
    assert failures.latest_touch(lambda p: [], "o/r", "main", ["a.py"]) is None


def test_patch_reading_helpers():
    patch = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
             "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n a\n+b\n")
    assert failures.patch_files(patch) == ["a.py", ".github/workflows/ci.yml"]
    assert failures.changed_lines(patch) == 3  # the +++ and --- headers don't count
    assert failures.touches_workflows(patch) and not failures.touches_workflows("diff --git a/a b/a\n")
    assert failures.names(["d/ci.yml"]) == "ci.yml"
    assert failures.names(["a/x.py", "b.py"]) == "x.py and 1 other file"
    assert failures.names(["a", "b", "c"]) == "a and 2 other files"


@pytest.mark.parametrize("before,after,ok", [(10, 10, True), (10, 15, True), (10, 16, False), (100, 110, True),
                                             (100, 111, False), (100, 89, False), (0, 5, True), (0, 6, False)])
def test_size_rule_is_ten_percent_or_five_lines(before, after, ok):
    assert refresh.size_ok(before, after) is ok


def test_same_shape_names_the_difference():
    a = "diff --git a/a b/a\n+1\n+2\n"
    assert refresh.same_shape(a, a) is None
    assert "different files" in refresh.same_shape(a, "diff --git a/b b/b\n+1\n+2\n")
    assert "2 to 30 lines" in refresh.same_shape(a, "diff --git a/a b/a\n" + "+x\n" * 30)


# -- check, against a local upstream that moves ------------------------------

def sh(*args, cwd=None):
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout


class Upstream:
    """A bare repo standing in for the project, with a clone to make commits from."""

    def __init__(self, tmp: Path):
        self.bare, self.work = tmp / "upstream.git", tmp / "work"
        sh("init", "--bare", "-b", "main", str(self.bare))
        sh("clone", str(self.bare), str(self.work))
        (self.work / "src.py").write_text("".join(f"line {i}\n" for i in range(1, 31)))
        (self.work / "ci.yml").write_text("steps:\n  - run: a\n")
        self.commit("start")

    def commit(self, msg):
        sh("add", "-A", cwd=self.work)
        sh("commit", "-m", msg, cwd=self.work)
        sh("push", "origin", "main", cwd=self.work)

    def edit_line(self, n, text, msg):
        lines = (self.work / "src.py").read_text().splitlines(keepends=True)
        lines[n - 1] = text + "\n"
        (self.work / "src.py").write_text("".join(lines))
        self.commit(msg)

    def draft(self) -> str:
        """The owner's fix: change line 15 and add a test file, as a diff against the current state."""
        lines = (self.work / "src.py").read_text().splitlines(keepends=True)
        lines[14] = "line 15 fixed\n"
        (self.work / "src.py").write_text("".join(lines))
        (self.work / "check.py").write_text("print('ok')\n")
        sh("add", "-A", cwd=self.work)
        patch = sh("diff", "--cached", cwd=self.work)
        sh("reset", "--hard", cwd=self.work)
        return patch


@pytest.fixture
def world(tmp_path, monkeypatch):
    up = Upstream(tmp_path)
    monkeypatch.setattr(refresh, "upstream_url", lambda repo: str(up.bare))
    monkeypatch.setenv("GH_TOKEN", "sekret")
    data = tmp_path / "data"
    command = f"{PY} -c \"import sys; sys.exit(0 if 'fixed' in open('src.py').read() else 1)\""
    patch = up.draft()
    write(data, good_briefing(KEY, ready=True, tests={"ran": True, "command": command, "result": "ok"}), PR_JSON, patch=patch)
    st = statemod.load(data)
    briefings.ingest(data, st)
    st["suggestions"][KEY].update(status="approved", failure={"code": "upstream_moved", "at": "2026-10-06T00:00:00+00:00",
                                                              "refresh_by": "2999-01-01", "plain": "x", "why": "y",
                                                              "fix_action": "refresh"})
    statemod.save(data, st)

    class World:
        pass

    w = World()
    w.up, w.data, w.patch, w.out, w.command = up, data, patch, tmp_path / "out", command
    w.check = lambda **kw: refresh.check(data, KEY, w.out, **kw)
    w.state = lambda: statemod.load(data)["suggestions"][KEY]

    def set_command(cmd):
        d = data / "briefings" / briefings.slug(KEY)
        b = json.loads((d / "briefing.json").read_text())
        b["tests"]["command"] = cmd
        (d / "briefing.json").write_text(json.dumps(b))

    w.set_command = set_command
    return w


def test_the_draft_applies_to_the_original_state(world):
    v = world.check()
    assert v["same_fix"] is True and v["structural_ok"] and v["tests"]["status"] == "passed"
    assert (world.out / "new.patch").read_text().count("line 15 fixed") == 1


def test_a_context_only_move_is_the_same_fix(world):
    world.up.edit_line(13, "line 13 reworded upstream", "touch the context")  # inside the hunk's 3 context lines
    world.up.edit_line(2, "line 2 new", "far away")
    with pytest.raises(subprocess.CalledProcessError):  # a plain apply would not work any more: that is why --3way
        subprocess.run(["git", "apply", "--check", str(world.data / "briefings" / briefings.slug(KEY) / "draft.patch")],
                       cwd=world.up.work, check=True, capture_output=True)
    v = world.check()
    assert v["same_fix"] is True and v["structural_ok"] and v["reason"] == ""
    assert v["tests"]["status"] == "passed" and v["tests"]["command"] == world.command
    new = (world.out / "new.patch").read_text()
    assert failures.patch_files(new) == failures.patch_files(world.patch) and "line 15 fixed" in new
    assert "line 13 reworded upstream" in new  # the new context
    assert json.loads((world.out / "verdict.json").read_text())["same_fix"] is True


def test_a_real_conflict_is_not_the_same_fix(world):
    world.up.edit_line(15, "line 15 rewritten by the maintainer", "touch the same line")
    v = world.check()
    assert v["same_fix"] is False and not v["structural_ok"] and v["tests"]["status"] == "skipped"
    assert "conflicts with newer code in src.py" in v["reason"] and v["conflicts"] == ["src.py"]
    assert not (world.out / "new.patch").exists()


def test_failing_tests_are_not_the_same_fix(world):
    world.set_command(f"{PY} -c \"import sys; print('boom'); sys.exit(3)\"")
    v = world.check()
    assert v["structural_ok"] and v["same_fix"] is False and v["tests"]["status"] == "failed"
    assert "boom" in v["tests"]["tail"] and "tests failed" in v["reason"]


def test_no_test_command_counts_as_passing(world):
    world.set_command("")
    v = world.check()
    assert v["same_fix"] is True and v["tests"] == {"status": "no command", "command": None, "tail": ""}


def test_a_test_that_hangs_times_out(world):
    world.set_command(f"{PY} -c \"import time; time.sleep(20)\"")
    v = world.check(timeout=1)
    assert v["same_fix"] is False and v["tests"]["status"] == "timeout" and "timed out" in v["reason"]


def test_the_project_tests_never_see_secrets(world):
    world.set_command(f"{PY} -c \"import os, sys; sys.exit(1 if os.environ.get('GH_TOKEN') else 0)\"")
    assert world.check()["same_fix"] is True


def test_the_apply_phase_writes_the_patch_before_any_project_code_runs(world, tmp_path):
    marker = tmp_path / "ran"
    world.set_command(f"{PY} -c \"open({str(marker)!r}, 'w').write('x')\"")
    v = world.check(phase="apply")
    assert v["same_fix"] is None and v["structural_ok"] and (world.out / "new.patch").exists() and not marker.exists()
    (world.out / "new.patch").unlink()
    v = world.check(phase="tests")
    assert v["same_fix"] is True and marker.exists()
    assert not (world.out / "new.patch").exists()  # the tests phase leaves the uploaded patch alone


def test_check_refuses_bad_input(world, tmp_path):
    with pytest.raises(refresh.RefreshError):
        refresh.check(world.data, "nope", tmp_path / "o")
    d = world.data / "briefings" / briefings.slug(KEY)
    (d / "pr.json").write_text(json.dumps({**PR_JSON, "base": "-main"}))
    with pytest.raises(refresh.RefreshError, match="base branch"):
        world.check()
    (d / "pr.json").write_text(json.dumps({**PR_JSON, "base": "nope"}))
    with pytest.raises(refresh.RefreshError, match="could not clone"):
        world.check()


# -- recording the answer -----------------------------------------------------

def test_apply_result_swaps_the_patch_and_records_it(world, tmp_path):
    world.up.edit_line(13, "line 13 reworded upstream", "touch the context")
    world.check()
    res = refresh.apply_result(world.data, KEY, world.out / "new.patch", world.out / "verdict.json", NOW)
    s = world.state()
    assert res == s["refresh_result"] and res["same_fix"] is True and res["at"] == "2026-10-07T12:00:00+00:00"
    assert res["tests"] == "passed" and res["by"] == "check" and "same" in res["what_changed"]
    assert s["send_after_refresh"] is True and s["status"] == "approved"
    assert "line 13 reworded upstream" in (world.data / "briefings" / briefings.slug(KEY) / "draft.patch").read_text()


def test_apply_result_refuses_a_patch_that_is_not_the_same_shape(world, tmp_path):
    world.check()
    tampered = tmp_path / "t.patch"
    tampered.write_text("diff --git a/evil.sh b/evil.sh\n+curl evil\n")
    with pytest.raises(refresh.RefreshError, match="different files"):
        refresh.apply_result(world.data, KEY, tampered, world.out / "verdict.json", NOW)
    tampered.write_text("")
    with pytest.raises(refresh.RefreshError, match="empty"):
        refresh.apply_result(world.data, KEY, tampered, world.out / "verdict.json", NOW)
    assert world.patch == (world.data / "briefings" / briefings.slug(KEY) / "draft.patch").read_text()
    assert "refresh_result" not in world.state()


def test_request_result_asks_for_a_rebuild_without_answering_it(world):
    world.up.edit_line(15, "line 15 rewritten by the maintainer", "touch the same line")
    world.check()
    res = refresh.request_result(world.data, KEY, world.out / "verdict.json", NOW)
    s = world.state()
    assert s["refresh_requested_at"] == "2026-10-07T12:00:00+00:00" and s["send_after_refresh"] is True
    assert res["same_fix"] is False and "conflicts" in res["reason"] and "at" not in res  # the routine's answer is what has an `at`
    assert refresh.pending(s, NOW + timedelta(hours=1))
    assert not refresh.sendable_after_refresh(s)


def test_pending_ends_with_the_routines_answer_or_after_two_days():
    s = {"refresh_requested_at": "2026-10-07T10:00:00+00:00", "refresh_result": {"same_fix": False}}
    assert refresh.pending(s, NOW)
    assert not refresh.pending(s, NOW + timedelta(days=2))
    s["refresh_result"] = {"at": "2026-10-07T11:00:00+00:00", "same_fix": True}
    assert not refresh.pending(s, NOW)
    s["refresh_result"]["at"] = "2026-10-07T09:00:00+00:00"  # an answer from before the request
    assert refresh.pending(s, NOW)
    assert not refresh.pending({}, NOW)


def test_send_after_refresh_needs_everything_to_line_up():
    def item(**kw):
        return {"status": "approved", "send_after_refresh": True,
                "failure": {"at": "2026-10-06T00:00:00+00:00"},
                "refresh_result": {"at": "2026-10-07T00:00:00+00:00", "same_fix": True}, **kw}

    assert refresh.sendable_after_refresh(item())
    assert not refresh.sendable_after_refresh(item(status="ready"))
    assert not refresh.sendable_after_refresh(item(send_after_refresh=False))
    assert not refresh.sendable_after_refresh(item(refresh_result={"at": "2026-10-07T00:00:00+00:00", "same_fix": False}))
    assert not refresh.sendable_after_refresh(item(refresh_result={"same_fix": True}))
    assert not refresh.sendable_after_refresh(item(failure={"at": "2026-10-08T00:00:00+00:00"}))  # failed again after
    assert refresh.sendable_after_refresh(item(failure=None))
    assert track.pending_sends({"suggestions": {"a/b#1": item(), "a/b#2": item(status="ready")}}) == ["a/b#1"]


# -- the act action and the command line --------------------------------------

def run_act(world, action="refresh", dry_run=False):
    return act.run(config.load(), world.data, KEY, action, dry_run=dry_run)


def test_act_refresh_marks_the_item_to_send_after_the_rebuild(world):
    assert run_act(world) == 0
    s = world.state()
    assert s["send_after_refresh"] is True and s["status"] == "approved" and "refresh_requested_at" not in s
    assert world.state()["failure"]["code"] == "upstream_moved"  # nothing else changed


def test_act_refresh_is_refused_for_the_wrong_kind_of_item(world):
    s = statemod.load(world.data)
    s["suggestions"][KEY]["status"] = "pr_open"
    statemod.save(world.data, s)
    assert run_act(world) == 1
    assert "status is pr_open" in statemod.load(world.data)["actions"][-1]["result"]


def test_act_refresh_dry_run_reports_each_path_and_changes_nothing(world, capsys):
    before = (world.data / "state.json").read_text()
    world.up.edit_line(13, "line 13 reworded upstream", "touch the context")
    assert run_act(world, dry_run=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "send" and out["reason"] is None
    assert "git apply --3way draft.patch" in out["calls"] and any("run the tests" in c for c in out["calls"])
    world.up.edit_line(15, "line 15 rewritten by the maintainer", "touch the same line")
    assert run_act(world, dry_run=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "routine" and "conflicts" in out["reason"]
    assert (world.data / "state.json").read_text() == before


def test_cli_runs_the_three_steps(world, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(world.data))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    world.up.edit_line(13, "line 13 reworded upstream", "touch the context")
    out = tmp_path / "cli-out"
    cli.main(["refresh-check", "--key", KEY, "--out", str(out), "--phase", "apply"])
    cli.main(["refresh-check", "--key", KEY, "--out", str(out), "--phase", "tests"])
    assert out_file.read_text() == "applies=true\nsame_fix=true\n"
    cli.main(["refresh-apply", "--key", KEY, "--patch", str(out / "new.patch"), "--verdict", str(out / "verdict.json")])
    assert world.state()["refresh_result"]["same_fix"] is True
    cli.main(["refresh-request", "--key", KEY, "--verdict", str(out / "verdict.json")])
    assert world.state()["refresh_requested_at"]
    with pytest.raises(SystemExit) as e:
        cli.main(["refresh-check", "--key", "bad key", "--out", str(out)])
    assert e.value.code == 1
    with pytest.raises(SystemExit) as e:
        cli.main(["refresh-apply", "--key", KEY, "--patch", str(tmp_path / "missing"), "--verdict", str(out / "verdict.json")])
    assert e.value.code == 1


def test_the_track_command_names_drafts_to_send(world, monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(world.data))
    monkeypatch.setattr(cli, "GitHub", lambda cache_dir=None: GitHub(transport=Fake({})))
    monkeypatch.setattr(track, "collect", lambda gh, login: {"prs": [], "per_repo": {}, "commented_issues": []})
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    s = statemod.load(world.data)
    s["suggestions"][KEY].update(send_after_refresh=True, refresh_result={"at": "2026-10-07T00:00:00+00:00", "same_fix": True})
    statemod.save(world.data, s)
    cli.main(["track"])
    assert f"send={KEY}\n" in out_file.read_text()
    s = statemod.load(world.data)
    s["suggestions"][KEY]["send_after_refresh"] = False
    statemod.save(world.data, s)
    out_file.write_text("")
    cli.main(["track"])
    assert "send=" not in out_file.read_text()
