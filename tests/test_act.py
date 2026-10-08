import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout import act, refresh, briefings, config, datarepo, digest, ghwrite, render, state as statemod, track, wip
from scout.github import NotFound
from test_ready import PR_JSON, SMALL, DISCUSS, good_briefing, write

KEY = "o/r#7"
EMAIL = "4242+aadityad12@users.noreply.github.com"
DISCLOSURE = "I used an AI assistant while investigating this; I reviewed and tested every change myself."


class Env:
    def __init__(self, tmp_path):
        self.data = tmp_path
        self.cfg = config.load()
        self.git_calls, self.posts = [], []
        self.git_fail = None       # substring of a git command that should fail
        self.remote_branch = ""    # what `git ls-remote` prints
        self.pulls, self.comments = [], []   # what GitHub already has: PRs from the owner's branch, comments
        self.reads = []
        self.token = (200, {"public_repo"})   # what GET user says about the key: (status, scopes)
        self.commits = []          # upstream commits any path lookup returns
        self.issue = {"state": "open"}
        self.timeline = []

    def load(self):
        return statemod.load(self.data)

    def save(self, st):
        statemod.save(self.data, st)

    def sugg(self, key=KEY):
        return self.load()["suggestions"][key]

    def run(self, action="submit", key=KEY, **kw):
        return act.run(self.cfg, self.data, key, action, **kw)

    def edit_briefing(self, key=KEY, **changes):
        f = self.data / "briefings" / briefings.slug(key) / "briefing.json"
        f.write_text(json.dumps({**json.loads(f.read_text()), **changes}))

    def edit_pr(self, **changes):
        f = self.data / "briefings" / briefings.slug(KEY) / "pr.json"
        f.write_text(json.dumps({**json.loads(f.read_text()), **changes}))

    def set_status(self, status, key=KEY, **extra):
        st = self.load()
        st["suggestions"][key].update(status=status, **extra)
        self.save(st)

    def add_followup(self, fu, day="2026-10-03", key=KEY):
        d = self.data / "briefings" / briefings.slug(key) / "followups" / day
        d.mkdir(parents=True)
        (d / "followup.json").write_text(json.dumps(fu))
        (d / "followup.patch").write_text("+fix\n")
        st = self.load()
        st["suggestions"][key].update(status="waiting_on_you", pr_url=fu["pr_url"],
                                      followup=f"briefings/{briefings.slug(key)}/followups/{day}",
                                      followup_kind=fu["kind"])
        self.save(st)
        return st["suggestions"][key]["followup"]

    def ready_ok(self):
        assert self.git_calls == [] and self.posts == []


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    write(tmp_path, good_briefing(KEY, ready=True), PR_JSON)
    st = e.load()
    briefings.ingest(tmp_path, st)
    e.save(st)
    assert st["suggestions"][KEY]["status"] == "ready"

    def fake_git(args, cwd=None, env=None):
        e.git_calls.append(args)
        if e.git_fail and e.git_fail in " ".join(args):
            raise act.ActError(f"git {args[0]} failed: https://x-access-token:sekret@github.com/x")
        return e.remote_branch if args[0] == "ls-remote" else ""

    def fake_read(path):
        e.reads.append(path)
        if "/pulls?head=" in path:
            return e.pulls
        if "/commits?" in path:
            return e.commits
        if path == "repos/o/r/issues/7":
            return e.issue
        if "/issues/7/timeline" in path:
            return e.timeline
        if "/comments?per_page=100" in path:
            return e.comments
        if path == "users/aadityad12":
            return {"id": 4242}
        if path.startswith("repos/aadityad12/"):
            return {"full_name": path[len("repos/"):]}
        if path.startswith("repos/o/r/pulls/"):
            return {"state": "open", "head": {"ref": "fix/7-empty-input", "repo": {"full_name": "aadityad12/r"}}}
        raise NotFound(path)

    def poster(name, result):
        def call(*args):
            e.posts.append((name, *args))
            if isinstance(result, Exception):
                raise result
            return result
        return call

    e.poster = poster
    monkeypatch.setenv("GH_TOKEN", "sekret")
    monkeypatch.setattr(act, "git", fake_git)
    monkeypatch.setattr(act, "read", fake_read)
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    monkeypatch.setattr(ghwrite, "token_scopes", lambda token=None: e.token)
    monkeypatch.setattr(ghwrite, "fork", poster("fork", {"full_name": "aadityad12/r"}))
    monkeypatch.setattr(ghwrite, "create_pr", poster("pr", {"html_url": "https://github.com/o/r/pull/9"}))
    monkeypatch.setattr(ghwrite, "comment", poster("comment", {"html_url": "https://github.com/o/r/issues/7#c1"}))
    return e


def refused(env, rc, needle):
    assert rc == 1
    last = env.load()["actions"][-1]
    assert last["result"].startswith("refused:") and needle in last["result"], last["result"]


# -- submit -------------------------------------------------------------------

def test_submit_happy_path(env):
    assert env.run() == 0
    names = [c[0] for c in env.git_calls]
    assert names == ["clone", "remote", "config", "config", "fetch", "ls-remote", "checkout", "apply", "-c", "push"]
    assert env.posts[0] == ("fork", "o/r")
    clone = env.git_calls[0]
    assert clone[:2] == ["clone", "--filter=blob:none"] and "x-access-token:sekret@github.com/aadityad12/r.git" in clone[2]
    assert env.git_calls[4] == ["fetch", "--filter=blob:none", "--no-tags", "upstream", "main"]
    assert env.git_calls[6][:5] == ["checkout", "--no-track", "-b", "fix/7-empty-input", "upstream/main"]
    commit = env.git_calls[8]
    assert commit[:8] == ["-c", "user.name=Aaditya Desai", "-c", f"user.email={EMAIL}", "-c", "commit.gpgsign=false",
                          "commit", "-m"]
    assert commit[8:] == ["Fix crash on empty input"]  # no -s, no trailers
    assert env.git_calls[9] == ["push", "origin", "HEAD:refs/heads/fix/7-empty-input"]
    assert env.posts[1] == ("pr", "o/r", "Fix crash on empty input", "Fixes #7.", "aadityad12:fix/7-empty-input", "main")
    s = env.sugg()
    assert s["status"] == "pr_open" and s["pr_url"] == "https://github.com/o/r/pull/9" and s["submitted_at"]
    assert [h["to"] for h in s["history"]] == ["ready", "submitting", "pr_open"]
    assert "last_error" not in s
    a = env.load()["actions"][-1]
    assert (a["key"], a["action"], a["result"], a["url"]) == (KEY, "submit", "ok", "https://github.com/o/r/pull/9")


def test_submit_uses_edited_text_signoff_and_force_with_lease(env):
    env.edit_pr(signoff=True)
    env.remote_branch = "abc123\trefs/heads/fix/7-empty-input\n"
    assert env.run(title="Fix the crash", body="Better words.") == 0
    commit = next(c for c in env.git_calls if "commit" in c)
    assert commit[-3:] == ["-s", "-m", "Fix crash on empty input"]  # title edits never change the commit message
    assert ["push", "--force-with-lease", "origin", "HEAD:refs/heads/fix/7-empty-input"] in env.git_calls
    assert env.posts[1][2:4] == ("Fix the crash", "Better words.")


def test_submit_falls_back_to_three_way_apply(env):
    env.git_fail = "apply --index"
    assert env.run() == 0
    assert [c[:2] for c in env.git_calls if c[0] == "apply"] == [["apply", "--index"], ["apply", "--3way"]]


def test_submit_applies_the_patch_by_absolute_path(env, monkeypatch):
    # The workflow sets SCOUT_DATA_DIR: ../data, but git apply runs inside the clone.
    monkeypatch.chdir(env.data.parent)
    env.data = Path(env.data.name)
    assert env.run() == 0
    patch = Path(next(c for c in env.git_calls if c[0] == "apply")[-1])
    assert patch.is_absolute() and patch.exists()


def test_data_dir_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCOUT_DATA_DIR", "data")
    assert config.data_dir() == tmp_path.resolve() / "data"


def test_submit_failure_goes_back_to_approved_and_can_retry(env):
    env.git_fail = "push"
    assert env.run() == 1
    s = env.sugg()
    assert s["status"] == "approved" and "failed" in s["last_error"] and "submitting_at" not in s
    assert "sekret" not in s["last_error"] and "sekret" not in env.load()["actions"][-1]["result"]
    assert env.posts == [("fork", "o/r")]
    env.git_fail = None
    assert env.run() == 0
    s = env.sugg()
    assert s["status"] == "pr_open" and "last_error" not in s
    assert [h["to"] for h in s["history"]] == ["ready", "submitting", "approved", "submitting", "pr_open"]


def test_submit_failure_when_github_rejects_the_pr(env, monkeypatch):
    monkeypatch.setattr(ghwrite, "create_pr", env.poster("pr", ghwrite.WriteError("GitHub said 422")))
    assert env.run() == 1
    assert env.sugg()["status"] == "approved" and "422" in env.sugg()["last_error"]


def test_submit_fork_must_be_the_owners(env, monkeypatch):
    monkeypatch.setattr(ghwrite, "fork", env.poster("fork", {"full_name": "someone-else/r"}))
    assert env.run() == 1
    assert env.sugg()["status"] == "approved" and env.git_calls == []


def test_submit_from_approved_is_allowed(env):
    assert env.run("approve") == 0 and env.sugg()["status"] == "approved"
    assert env.run("submit") == 0 and env.sugg()["status"] == "pr_open"


def test_dry_run_changes_nothing_and_prints_the_plan(env, capsys):
    before = (env.data / "state.json").read_text()
    assert env.run(dry_run=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["commit_message"] == "Fix crash on empty input" and out["title"] == PR_JSON["title"]
    assert out["body"] == "Fixes #7." and any("POST repos/o/r/pulls" in c for c in out["calls"])
    assert (env.data / "state.json").read_text() == before
    env.ready_ok()


@pytest.mark.parametrize("name,setup,needle", [
    ("legacy guide mode", lambda e: e.edit_briefing(mode="guide"), "pair mode"),
    ("pair mode", lambda e: e.edit_briefing(mode="pair"), "pair mode"),
    ("own mode", lambda e: e.edit_briefing(mode="own"), "own mode"),
    ("posts forbidden", lambda e: e.edit_briefing(ai_posts_forbidden=True), "forbids AI-written posts"),
    ("not marked ready", lambda e: e.edit_briefing(ready=False), "not marked ready"),
    ("wrong status", lambda e: e.set_status("pr_open"), "status is pr_open"),
    ("repo mismatch", lambda e: e.edit_briefing(repo="evil/r"), "briefing's repo"),
    ("suggestion repo mismatch", lambda e: e.set_status("ready", repo="evil/r"), "suggestion's repo"),
    ("briefing folder", lambda e: e.set_status("ready", briefing="briefings/other"), "briefing folder"),
    ("leading dash branch", lambda e: e.edit_pr(branch="-fix"), "branch is not a safe name"),
    ("spaced branch", lambda e: e.edit_pr(branch="fix 7"), "branch is not a safe name"),
    ("dotdot branch", lambda e: e.edit_pr(branch="fix/../x"), "branch is not a safe name"),
    ("bad base", lambda e: e.edit_pr(base="-main"), "base is not a safe name"),
    ("no longer validates", lambda e: (e.data / "briefings" / "o__r__7" / "draft.patch").unlink(), "no longer validates"),
    ("marker in pr.json", lambda e: e.edit_pr(title="Fix (Generated with a tool)"), "no longer validates"),
])
def test_submit_refusals(env, name, setup, needle):
    setup(env)
    refused(env, env.run(), needle)
    assert env.sugg()["status"] in ("ready", "pr_open")
    env.ready_ok()


def test_submit_refuses_unknown_key(env):
    refused(env, env.run(key="o/r#99"), "no suggestion")


def test_submit_refuses_ai_markers_in_edits(env):
    for kw in ({"body": "Fixes #7\n\nCo-authored-by: Claude"}, {"title": "Fix \U0001F916"},
               {"body": "Generated with a tool"}):
        refused(env, env.run(**kw), "AI marker")
    env.ready_ok()
    assert env.sugg()["status"] == "ready"


def test_submit_refuses_bad_edited_text(env):
    refused(env, env.run(title="two\nlines"), "one line")
    refused(env, env.run(title="x" * 300), "one line")
    refused(env, env.run(body="y" * 60001), "characters")
    refused(env, env.run(body="   "), "characters")


def test_submit_needs_the_disclosure_to_stay(env):
    env.edit_pr(body=f"Fixes #7.\n\n{DISCLOSURE}", disclosure=DISCLOSURE)
    refused(env, env.run(body="Fixes #7, reworded."), "disclosure")
    env.ready_ok()
    assert env.run(body=f"Reworded. Fixes #7.\n\n{DISCLOSURE}") == 0


def open_pr(repo, n, status="open"):
    return {"repo": repo, "number": n, "url": f"https://github.com/{repo}/pull/{n}", "status": status}


def test_submit_respects_wip(env):
    st = env.load()
    st["contributions"] = {"prs": [open_pr("o/r", 1)]}
    env.save(st)
    refused(env, env.run(), "already open in o/r")
    st["contributions"] = {"prs": [open_pr("a/a", 1), open_pr("b/b", 2), open_pr("c/c", 3)]}
    env.save(st)
    refused(env, env.run(), "PRs are already open")
    st["contributions"] = {"prs": [open_pr("a/a", 1), open_pr("b/b", 2), open_pr("c/c", 3, "merged")]}
    env.save(st)
    assert env.run() == 0


def test_submit_counts_prs_opened_since_the_last_scan(env):
    st = env.load()
    st["suggestions"]["o/r#8"] = {"repo": "o/r", "number": 8, "status": "pr_open", "submitted_at": "2026-10-02T00:00:00+00:00",
                                  "pr_url": "https://github.com/o/r/pull/20"}
    env.save(st)
    refused(env, env.run(), "already open in o/r")


def test_actions_are_trimmed_and_unknown_input_rejected(env):
    st = env.load()
    st["actions"] = [{"at": "x", "key": KEY, "action": "later", "result": "ok"}] * 250
    env.save(st)
    assert env.run("later") == 0
    assert len(env.load()["actions"]) == 200
    assert act.run(env.cfg, env.data, "not a key", "submit") == 2
    assert act.run(env.cfg, env.data, KEY, "delete") == 2


def test_submit_adopts_a_pr_that_already_exists(env, capsys):
    env.pulls = [{"html_url": "https://github.com/o/r/pull/5", "state": "open", "merged_at": None,
                  "head": {"ref": "fix/7-empty-input"}}]
    env.set_status("approved", last_error="old")
    assert env.run(dry_run=True) == 0
    assert json.loads(capsys.readouterr().out)["existing_pr"] == "https://github.com/o/r/pull/5"
    assert env.sugg()["status"] == "approved"
    assert env.run() == 0
    env.ready_ok()  # nothing pushed, nothing created
    s = env.sugg()
    assert s["status"] == "pr_open" and s["pr_url"] == "https://github.com/o/r/pull/5" and "last_error" not in s
    a = env.load()["actions"][-1]
    assert a["result"] == "ok" and a["note"] == "already existed" and a["url"] == s["pr_url"]
    assert any(p == "repos/o/r/pulls?head=aadityad12:fix/7-empty-input&state=all" for p in env.reads)


@pytest.mark.parametrize("pr,status", [({"state": "closed", "merged_at": "2026-10-03T00:00:00Z"}, "merged"),
                                       ({"state": "closed", "merged_at": None}, "closed")])
def test_submit_adopts_finished_prs_too(env, pr, status):
    env.pulls = [{**pr, "html_url": "https://github.com/o/r/pull/5", "head": {"ref": "fix/7-empty-input"}}]
    assert env.run() == 0 and env.sugg()["status"] == status
    env.ready_ok()


def test_submit_ignores_prs_from_other_branches(env):
    env.pulls = [{"html_url": "https://github.com/o/r/pull/5", "state": "open", "head": {"ref": "something-else"}}]
    assert env.run() == 0
    assert env.posts[-1][0] == "pr"


def test_stuck_submitting_can_be_retried_after_half_an_hour(env):
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    env.set_status("submitting", submitting_at=recent)
    refused(env, env.run(), "status is submitting")
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    env.set_status("submitting", submitting_at=old)
    assert env.run() == 0
    s = env.sugg()
    assert s["status"] == "pr_open" and "submitting_at" not in s
    env.set_status("submitting")  # no timestamp: never treated as stuck
    refused(env, env.run(), "status is submitting")


# -- post ---------------------------------------------------------------------

@pytest.fixture
def mix(env):
    key = "o/r#8"
    write(env.data, good_briefing(key, ready=True, kind="triage", post_target="https://github.com/o/r/issues/8"),
          patch=None, post="Likely cause is the empty-input branch in parse().\n")
    st = env.load()
    briefings.ingest(env.data, st)
    env.save(st)
    assert st["suggestions"][key]["status"] == "ready"
    return key


def test_post_happy_path(env, mix):
    assert env.run("post", mix) == 0
    assert env.posts == [("comment", "o/r", 8, "Likely cause is the empty-input branch in parse().")]
    s = env.sugg(mix)
    assert s["status"] == "posted" and s["comment_url"] == "https://github.com/o/r/issues/7#c1"
    assert env.git_calls == []


def test_post_uses_edited_text_and_checks_it(env, mix):
    refused(env, env.run("post", mix, body="Generated with a tool"), "AI marker")
    assert env.run("post", mix, body="My own words.") == 0
    assert env.posts[0][3] == "My own words."


def test_post_refusals(env, mix):
    refused(env, env.run("post"), "pr item")          # a PR goes through submit
    refused(env, env.run("submit", mix), "triage item")
    env.edit_briefing(mix, post_target="https://github.com/evil/other/issues/8")
    refused(env, env.run("post", mix), "post_target")
    env.edit_briefing(mix, post_target="https://github.com/o/r/issues/8", ai_posts_forbidden=True)
    refused(env, env.run("post", mix), "forbids")
    env.ready_ok()


def test_post_failure_goes_back_to_approved(env, mix, monkeypatch):
    monkeypatch.setattr(ghwrite, "comment", env.poster("comment", ghwrite.WriteError("boom")))
    assert env.run("post", mix) == 1
    assert env.sugg(mix)["status"] == "approved" and "boom" in env.sugg(mix)["last_error"]


def test_post_skips_a_comment_the_owner_already_made(env, mix):
    env.comments = [{"user": {"login": "someone"}, "body": "Likely cause is the empty-input branch in parse().",
                     "html_url": "x"},
                    {"user": {"login": "AadityaD12"}, "body": "  Likely cause is the empty-input branch in parse().\n\n",
                     "html_url": "https://github.com/o/r/issues/8#c7"}]
    assert env.run("post", mix, dry_run=True) == 0
    assert env.run("post", mix) == 0
    env.ready_ok()
    s = env.sugg(mix)
    assert s["status"] == "posted" and s["comment_url"] == "https://github.com/o/r/issues/8#c7"
    a = env.load()["actions"][-1]
    assert a["note"] == "already existed" and a["url"] == s["comment_url"]
    assert "repos/o/r/issues/8/comments?per_page=100&page=1" in env.reads


def test_post_still_posts_when_only_others_or_different_text(env, mix):
    env.comments = [{"user": {"login": "someone"}, "body": "Likely cause is the empty-input branch in parse().", "html_url": "x"},
                    {"user": {"login": "aadityad12"}, "body": "something else", "html_url": "y"}]
    assert env.run("post", mix) == 0
    assert env.posts[0][0] == "comment"


# -- follow-ups ---------------------------------------------------------------

def test_followup_small_pushes_and_replies(env):
    rel = env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    assert env.run("followup") == 0
    assert env.git_calls[0][:4] == ["clone", "--filter=blob:none", "-b", "fix/7-empty-input"]
    commit = next(c for c in env.git_calls if "commit" in c)
    assert commit[:4] == ["-c", "user.name=Aaditya Desai", "-c", f"user.email={EMAIL}"]
    assert commit[-2:] == ["-m", "Address review feedback"] and "-s" not in commit
    assert ["push", "origin", "HEAD:refs/heads/fix/7-empty-input"] in env.git_calls
    assert env.posts == [("comment", "o/r", 9, "Renamed, thanks.")]
    s = env.sugg()
    assert s["followup_done"] == rel and s["followup_done_at"] and s["status"] == "pr_open"
    refused(env, env.run("followup"), "already sent")


def test_followup_overrides_and_edits(env):
    env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    assert env.run("followup", title="Rename x to count", body="Done, renamed x.") == 0
    assert next(c for c in env.git_calls if "commit" in c)[-1] == "Rename x to count"
    assert env.posts[0][3] == "Done, renamed x."


def test_followup_adopts_a_reply_already_posted(env):
    rel = env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    env.comments = [{"user": {"login": "aadityad12"}, "body": "Renamed, thanks.", "created_at": "2026-10-02T23:00:00Z",
                     "html_url": "https://github.com/o/r/pull/9#old"}]  # before the follow-up's date: doesn't count
    assert env.run("followup", dry_run=True) == 0
    env.comments.append({"user": {"login": "aadityad12"}, "body": "Renamed, thanks.\n", "created_at": "2026-10-03T08:00:00Z",
                         "html_url": "https://github.com/o/r/pull/9#c1"})
    assert env.run("followup") == 0
    env.ready_ok()
    s = env.sugg()
    assert s["followup_done"] == rel and s["followup_pushed"] == rel and s["status"] == "pr_open"
    a = env.load()["actions"][-1]
    assert a["note"] == "already existed" and a["url"] == "https://github.com/o/r/pull/9#c1"


def test_followup_ignores_an_old_identical_reply(env):
    env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    env.comments = [{"user": {"login": "aadityad12"}, "body": "Renamed, thanks.", "created_at": "2026-09-01T00:00:00Z",
                     "html_url": "old"}]
    assert env.run("followup") == 0
    assert env.posts[0][0] == "comment" and any(c[0] == "push" for c in env.git_calls)


def test_followup_discuss_is_refused(env):
    env.add_followup({**DISCUSS, "pr_url": "https://github.com/o/r/pull/9"})
    refused(env, env.run("followup"), "/contribute")
    env.ready_ok()


def test_followup_refusals(env):
    env.set_status("pr_open")
    refused(env, env.run("followup"), "no follow-up")
    env.add_followup({**SMALL, "pr_url": "https://github.com/evil/r/pull/9"})
    refused(env, env.run("followup"), "not a pull request in this repo")
    env.ready_ok()


def test_followup_retry_skips_the_push_that_already_happened(env, monkeypatch):
    env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    monkeypatch.setattr(ghwrite, "comment", env.poster("comment", ghwrite.WriteError("boom")))
    assert env.run("followup") == 1
    s = env.sugg()
    assert s["followup_pushed"] == s["followup"] and "followup_done" not in s and "boom" in s["last_error"]
    env.git_calls.clear()
    monkeypatch.setattr(ghwrite, "comment", env.poster("comment", {"html_url": "https://github.com/o/r/pull/9#c"}))
    assert env.run("followup") == 0
    assert env.git_calls == [] and env.sugg()["followup_done"]


def test_followup_needs_a_pr_on_the_owners_fork(env, monkeypatch):
    env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    monkeypatch.setattr(act, "read", lambda path: [] if "/comments" in path else {"id": 1} if path.startswith("users/") else
                        {"state": "open", "head": {"ref": "x", "repo": {"full_name": "someone/r"}}})
    assert env.run("followup") == 1
    assert "not on your fork" in env.load()["actions"][-1]["result"]
    assert not any("push" in c for c in env.git_calls)


# -- skip, later, approve -------------------------------------------------------

def test_skip_remembers_the_issue(env):
    assert env.run("skip") == 0
    assert env.sugg()["status"] == "skipped"
    p = env.load()["passed"][KEY]
    assert p["reason"] == "turned down on dashboard"
    refused(env, env.run("skip"), "status is skipped")


def test_later_snoozes_without_changing_status(env):
    assert env.run("later") == 0
    s = env.sugg()
    assert s["status"] == "ready"
    until = datetime.fromisoformat(s["snoozed_until"])
    assert timedelta(days=2.9) < until - datetime.now(timezone.utc) < timedelta(days=3.1)


def test_approve_only_from_ready(env):
    assert env.run("approve") == 0 and env.sugg()["status"] == "approved"
    refused(env, env.run("approve"), "status is approved")


def test_snoozed_items_do_not_block_new_ready_items_or_the_digest(env):
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    st = env.load()
    st["suggestions"][KEY]["snoozed_until"] = "2026-10-04T00:00:00+00:00"
    settings = {"max_ready": 1, "max_open_prs": 3, "max_open_prs_per_repo": 1, "max_unsent": 1}
    assert wip.compute(st, settings, now)["ready_allowed"]
    assert digest.build(env.cfg, env.data, st, now)["ready"] == []
    st["suggestions"][KEY]["snoozed_until"] = "2026-10-01T00:00:00+00:00"
    assert not wip.compute(st, settings, now)["ready_allowed"]
    assert [i["key"] for i in digest.build(env.cfg, env.data, st, now)["ready"]] == [KEY]


def test_digest_hides_threads_already_answered(env):
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    st = env.load()
    st["suggestions"][KEY].update(status="waiting_on_you", pr_url="https://github.com/o/r/pull/9",
                                  followup_done_at="2026-10-02T10:00:00+00:00")
    st["contributions"] = {"prs": [{**open_pr("o/r", 9), "waiting_on_you": True, "updated_at": "2026-10-02T09:00:00Z"}]}
    assert digest.build(env.cfg, env.data, st, now)["waiting_on_you"] == []
    st["contributions"]["prs"][0]["updated_at"] = "2026-10-02T11:00:00Z"  # a newer comment since
    assert len(digest.build(env.cfg, env.data, st, now)["waiting_on_you"]) == 1


def test_tracker_leaves_freshly_submitted_prs_alone():
    class Boom:
        def get(self, *a, **k):
            raise AssertionError("should not look the issue up")
    st = {"suggestions": {KEY: {"repo": "o/r", "number": 7, "status": "pr_open", "submitted_at": "x",
                                "suggested_at": "2026-09-01T00:00:00+00:00"}}}
    track.update_suggestions(Boom(), st, {"prs": [], "commented_issues": []}, 14)
    assert st["suggestions"][KEY]["status"] == "pr_open"


# -- prepare, pair, feature, summary -----------------------------------------------

PR_URL = "https://github.com/o/r/pull/9"


def with_prs(env, *statuses):
    st = env.load()
    st["contributions"] = {"prs": [{**open_pr("o/r", 9 + i), "status": s} for i, s in enumerate(statuses)]}
    env.save(st)


def test_prepare_marks_a_suggestion_only(env):
    refused(env, env.run("prepare"), "status is ready")
    env.set_status("suggested")
    assert env.run("prepare") == 0
    assert datetime.fromisoformat(env.sugg()["prepare_requested_at"])
    assert env.load()["actions"][-1]["action"] == "prepare"
    refused(env, env.run("prepare", key="o/r#99"), "no suggestion")
    env.ready_ok()


def test_pair_and_unpair(env):
    assert env.run("pair") == 0  # ready
    s = env.sugg()
    assert s["pairing"] is True and datetime.fromisoformat(s["pairing_at"]) and s["status"] == "ready"
    assert env.run("unpair") == 0
    assert "pairing" not in env.sugg() and "pairing_at" not in env.sugg()
    for status in ("suggested", "approved"):
        env.set_status(status)
        assert env.run("pair") == 0
    env.set_status("pr_open")
    refused(env, env.run("pair"), "status is pr_open")
    assert env.run("unpair") == 0 and "pairing" not in env.sugg()  # unpair works whatever the status
    refused(env, env.run("unpair", key="o/r#99"), "no suggestion")
    env.ready_ok()


def test_feature_needs_a_merged_pr_and_keeps_three(env):
    with_prs(env, "merged", "merged", "merged", "merged", "open")
    for n in (9, 10, 11):
        assert env.run("feature", key=f"o/r#{n}") == 0
    featured = env.load()["public"]["featured"]
    assert featured == [f"https://github.com/o/r/pull/{n}" for n in (9, 10, 11)]
    assert env.run("feature", key="o/r#9") == 0 and env.load()["public"]["featured"] == featured  # already there
    refused(env, env.run("feature", key="o/r#12"), "3 pull requests are already featured")
    assert env.load()["public"]["featured"] == featured
    refused(env, env.run("feature", key="o/r#13"), "the pull request is open")
    refused(env, env.run("feature", key="o/r#99"), "no pull request of yours")
    assert env.run("unfeature", key="o/r#10") == 0
    assert env.load()["public"]["featured"] == [featured[0], featured[2]]
    assert env.run("feature", key="o/r#12") == 0
    assert env.run("unfeature", key="o/r#10") == 0  # not featured: nothing to do
    refused(env, env.run("unfeature", key="o/r#99"), "no pull request of yours")
    env.ready_ok()


def test_feature_matches_the_repo_as_well_as_the_number(env):
    with_prs(env, "merged")
    refused(env, env.run("feature", key="x/y#9"), "no pull request of yours")
    assert env.run("feature", key="O/R#9") == 0


def test_summary_sets_edits_and_removes(env):
    with_prs(env, "merged", "open", "closed")
    assert env.run("summary", key="o/r#9", body="  Fixes a crash on empty input.  \n") == 0
    assert env.load()["public"]["summaries"] == {PR_URL: "Fixes a crash on empty input."}
    assert env.run("summary", key="o/r#10", body="Still in review.") == 0
    assert env.run("summary", key="o/r#9", body="Fixes the crash.") == 0
    assert env.load()["public"]["summaries"][PR_URL] == "Fixes the crash."
    assert env.run("summary", key="o/r#9", body="   \n") == 0
    assert list(env.load()["public"]["summaries"]) == ["https://github.com/o/r/pull/10"]
    assert env.run("summary", key="o/r#10") == 0  # no text at all removes it too
    assert env.load()["public"]["summaries"] == {}
    assert env.run("summary", key="o/r#9", body="") == 0  # nothing to remove
    assert env.load()["actions"][-1]["result"] == "ok"


@pytest.mark.parametrize("text,needle", [
    ("x" * 201, "one line, 1-200"),
    ("one\ntwo", "one line, 1-200"),
    ("Fixes a crash. 🤖", "AI marker"),
    ("Co-authored-by: someone", "AI marker"),
])
def test_summary_refusals(env, text, needle):
    with_prs(env, "merged", "closed")
    refused(env, env.run("summary", key="o/r#9", body=text), needle)
    assert env.load()["public"]["summaries"] == {}
    assert env.run("summary", key="o/r#9", body="x" * 200) == 0


def test_summary_refusals_by_pr(env):
    with_prs(env, "closed")
    refused(env, env.run("summary", key="o/r#9", body="Nice."), "the pull request is closed")
    refused(env, env.run("summary", key="o/r#99", body="Nice."), "no pull request of yours")
    st = env.load()
    st["public"]["summaries"][PR_URL] = "Was merged-looking."
    env.save(st)
    assert env.run("summary", key="o/r#9", body="") == 0  # a summary can always be removed
    assert env.load()["public"]["summaries"] == {}


def test_public_state_is_created_for_old_state_files(env):
    st = env.load()
    del st["public"]
    st["contributions"] = {"prs": [{**open_pr("o/r", 9), "status": "merged"}]}
    env.save(st)
    assert statemod.load(env.data)["public"] == {"featured": [], "summaries": {}}
    assert env.run("feature", key="o/r#9") == 0
    st = env.load()
    assert st["public"]["featured"] == [PR_URL] and st["alerted"] == {}


def test_cli_accepts_the_new_actions(env, monkeypatch, tmp_path, capsys):
    from scout import __main__ as cli
    monkeypatch.setenv("SCOUT_DATA_DIR", str(env.data))
    env.set_status("suggested")
    f = tmp_path / "t.txt"
    f.write_text("hi")
    for action in ("prepare", "pair", "unpair"):
        with pytest.raises(SystemExit) as e:
            cli.main(["act", "--key", KEY, "--action", action])
        assert e.value.code == 0
    assert "prepare_requested_at" in env.sugg()
    with pytest.raises(SystemExit) as e:
        cli.main(["act", "--key", KEY, "--action", "summary", "--body-file", str(f)])
    assert e.value.code == 1  # no such PR, but the action is known


# -- failure records ----------------------------------------------------------

WORKFLOW_PATCH = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,3 @@
 steps:
+  - run: new test
   - run: a
"""
OCT_5 = [{"sha": "abc", "commit": {"committer": {"date": "2026-10-05T12:00:00Z"}}}]
TIMELINE_PR = {"event": "cross-referenced", "source": {"issue": {
    "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/50"}, "state": "open", "html_url": "https://github.com/o/r/pull/50", "user": {"login": "other"}}}}
FIELDS = {"code", "plain", "why", "fix_action", "refresh_by", "at"}


def set_patch(env, text=WORKFLOW_PATCH):
    (env.data / "briefings" / briefings.slug(KEY) / "draft.patch").write_text(text)


def failure_of(env):
    f = env.sugg()["failure"]
    assert set(f) == FIELDS and f["at"] and "sekret" not in json.dumps(f)
    return f


def test_a_patch_that_no_longer_applies_says_what_moved_and_when(env):
    set_patch(env)
    env.token = (200, {"public_repo", "workflow"})
    env.commits = OCT_5
    env.git_fail = "apply"
    assert env.run() == 1
    s = env.sugg()
    assert s["status"] == "approved" and "last_error" in s and "sekret" not in s["last_error"]
    f = failure_of(env)
    assert f["code"] == "upstream_moved" and f["fix_action"] == "refresh"
    assert f["plain"] == "Couldn't send: r changed ci.yml on Oct 5, after this draft was written."
    assert "ci.yml" in f["why"] and "Oct 5" in f["why"]
    assert f["refresh_by"] == (datetime.fromisoformat(f["at"]).date() + timedelta(days=14)).isoformat()  # one commit: 15 // 1, capped at 14
    paths = [p for p in env.reads if "/commits?" in p]
    assert any("per_page=1" in p and "path=.github%2Fworkflows%2Fci.yml" in p and "sha=main" in p for p in paths)
    assert any("since=" in p for p in paths)  # the 30-day count
    assert env.posts == [("fork", "o/r")]
    env.git_fail = None
    assert env.run() == 0
    s = env.sugg()
    assert s["status"] == "pr_open" and "failure" not in s and "last_error" not in s  # success clears it


def test_busy_projects_get_a_shorter_refresh_window(env):
    set_patch(env)
    env.commits = [{"sha": str(i), "commit": {"committer": {"date": "2026-10-05T12:00:00Z"}}} for i in range(5)]
    env.git_fail = "apply"
    env.run()
    f = failure_of(env)
    assert f["refresh_by"] == (datetime.fromisoformat(f["at"]).date() + timedelta(days=3)).isoformat()  # 15 // 5


def test_a_missing_key_permission_is_caught_before_anything_is_cloned(env):
    set_patch(env)
    env.token = (200, {"public_repo"})
    assert env.run() == 1
    assert env.git_calls == [] and env.posts == []  # no fork either
    s = env.sugg()
    assert s["status"] == "approved"
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("token_scope", "token")
    assert f["plain"] == "Your GitHub key is missing the 'workflow' permission this change needs."
    env.token = (200, {"public_repo", "workflow"})
    assert env.run() == 0 and "failure" not in env.sugg()


def test_the_scope_check_only_matters_for_workflow_files_and_classic_keys(env):
    assert env.run() == 0  # a plain patch needs no workflow permission
    env.set_status("approved")
    set_patch(env)
    env.token = (200, None)  # fine-grained keys send no scopes header: can't tell, so go ahead
    assert env.run() == 0


def test_a_rejected_key_is_token_expired(env):
    env.token = (401, None)
    assert env.run() == 1
    assert env.git_calls == [] and env.posts == []
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("token_expired", "token") and "expired" in f["plain"]


def test_a_401_from_github_later_on_is_token_expired(env, monkeypatch):
    monkeypatch.setattr(ghwrite, "create_pr", env.poster("pr", ghwrite.WriteError("GitHub said 401 for POST repos/o/r/pulls: Bad credentials")))
    assert env.run() == 1
    assert env.sugg()["status"] == "approved" and failure_of(env)["code"] == "token_expired"


def test_the_token_is_never_printed_by_the_scope_check(env, capsys):
    env.token = (401, None)
    env.run()
    out = capsys.readouterr()
    assert "sekret" not in out.out + out.err


def test_a_closed_issue_or_someone_elses_open_pr_is_taken(env):
    env.issue = {"state": "closed"}
    assert env.run() == 1
    assert env.sugg()["status"] == "ready" and env.git_calls == [] and env.posts == []
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("taken", "skip") and "closed" in f["plain"]
    refused(env, env.run(), "closed")
    env.issue = {"state": "open"}
    env.timeline = [TIMELINE_PR]
    assert env.run() == 1
    f = failure_of(env)
    assert f["code"] == "taken" and "someone else" in f["plain"] and "pull/50" in f["why"]
    env.timeline = [{**TIMELINE_PR, "source": {"issue": {**TIMELINE_PR["source"]["issue"], "user": {"login": "aadityad12"}}}}]
    assert env.run() == 0  # your own PR is not a rival


def test_a_failed_read_does_not_block_a_send(env):
    env.issue = None  # the fake raises NotFound for the issue read
    env.timeline = None
    assert env.run() == 0


def test_the_pr_limit_is_recorded_as_wip_limit(env):
    st = env.load()
    st["contributions"] = {"prs": [open_pr("o/r", 1)]}
    env.save(st)
    refused(env, env.run(), "already open in o/r")
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("wip_limit", "none") and f["plain"].startswith("Couldn't send:")
    assert env.sugg()["status"] == "ready"


def test_edited_text_that_is_turned_down_is_text_rejected(env):
    refused(env, env.run(body="Co-authored-by: a tool"), "AI marker")
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("text_rejected", "edit") and "AI marker" in f["why"]
    assert env.run() == 0 and "failure" not in env.sugg()


def test_a_missing_draft_file_is_missing_files(env):
    (env.data / "briefings" / "o__r__7" / "draft.patch").unlink()
    refused(env, env.run(), "no longer validates")
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("missing_files", "none")


def test_any_other_error_is_github_error(env, monkeypatch):
    monkeypatch.setattr(ghwrite, "create_pr", env.poster("pr", ghwrite.WriteError("GitHub said 500 for POST: sekret boom")))
    assert env.run() == 1
    f = failure_of(env)
    assert (f["code"], f["fix_action"]) == ("github_error", "none") and "sekret" not in f["why"] and "500" in f["why"]


def test_post_and_followup_failures_get_records_too(env, monkeypatch):
    monkeypatch.setattr(ghwrite, "comment", env.poster("comment", ghwrite.WriteError("GitHub said 502 for POST")))
    env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    assert env.run("followup") == 1
    s = env.sugg()
    assert s["status"] == "waiting_on_you"
    f = failure_of(env)
    assert f["code"] == "github_error" and f["refresh_by"] is None  # a follow-up has no draft to go stale
    env.git_fail = "apply"
    st = env.load()
    st["suggestions"][KEY].pop("followup_pushed")  # the first try got as far as pushing
    env.save(st)
    assert env.run("followup") == 1
    f = failure_of(env)
    assert f["code"] == "upstream_moved" and f["fix_action"] == "none" and "branch changed" in f["plain"]
    refused(env, env.run("followup", body="Co-authored-by: x"), "AI marker")
    assert env.sugg()["failure"]["code"] == "text_rejected"
    monkeypatch.setattr(ghwrite, "comment", env.poster("comment", {"html_url": "https://github.com/o/r/issues/9#c2"}))
    env.git_fail = None
    assert env.run("followup") == 0 and "failure" not in env.sugg()


def test_a_comment_item_failure_is_recorded(env, monkeypatch):
    write(env.data, good_briefing("o/r#8", ready=True, kind="triage", post_target="https://github.com/o/r/issues/8"),
          patch=None, post="Looks like a dup.")
    st = env.load()
    briefings.ingest(env.data, st)
    env.save(st)
    monkeypatch.setattr(ghwrite, "comment", env.poster("comment", ghwrite.WriteError("GitHub said 401 for POST")))
    assert env.run("post", key="o/r#8") == 1
    s = env.load()["suggestions"]["o/r#8"]
    assert s["status"] == "approved" and s["failure"]["code"] == "token_expired"


def test_adopting_an_existing_pr_clears_the_failure(env):
    env.set_status("approved", failure={"code": "upstream_moved"}, send_after_refresh=True)
    env.pulls = [{"html_url": "https://github.com/o/r/pull/5", "state": "open", "merged_at": None,
                  "head": {"ref": "fix/7-empty-input"}}]
    assert env.run() == 0
    s = env.sugg()
    assert "failure" not in s and "send_after_refresh" not in s


def test_dry_run_records_nothing(env):
    before = (env.data / "state.json").read_text()
    env.issue = {"state": "closed"}
    assert env.run(dry_run=True) == 0  # the plan doesn't read the issue or the key
    assert (env.data / "state.json").read_text() == before


def test_the_digest_lists_stuck_items(env):
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    st = env.load()
    assert digest.build(env.cfg, env.data, st, now)["stuck"] == [] and digest.build(env.cfg, env.data, st, now)["send"]
    st["suggestions"][KEY].update(status="approved", title="Crash on empty input", failure={
        "code": "upstream_moved", "plain": "Couldn't send: r changed ci.yml on Oct 5, after this draft was written.",
        "why": "The project edited ci.yml.", "fix_action": "refresh", "refresh_by": "2026-10-12", "at": "2026-10-06T00:00:00+00:00"})
    d = digest.build(env.cfg, env.data, st, now)
    assert d["stuck"] == [{"key": KEY, "slug": "o__r__7", "title": "Crash on empty input",
                           "plain": "Couldn't send: r changed ci.yml on Oct 5, after this draft was written.",
                           "why": "The project edited ci.yml.", "fix_action": "refresh", "refresh_by": "2026-10-12",
                           "refreshing": False, "problem": "Crash on empty input", "sending": "A pull request",
                           "your_part": "Tap Refresh & send by Oct 12"}]
    assert d["send"] is True
    st["suggestions"][KEY]["refresh_requested_at"] = "2026-10-07T11:00:00+00:00"
    assert digest.build(env.cfg, env.data, st, now)["stuck"][0]["refreshing"] is True
    st["suggestions"][KEY]["snoozed_until"] = "2026-10-09T00:00:00+00:00"
    assert digest.build(env.cfg, env.data, st, now)["stuck"] == []
    st["suggestions"][KEY].pop("snoozed_until")
    st["suggestions"][KEY]["status"] = "skipped"
    assert digest.build(env.cfg, env.data, st, now)["stuck"] == []


def test_the_dashboard_payload_carries_the_failure_and_refresh_state(env):
    env.set_status("approved", failure={"code": "upstream_moved", "plain": "Couldn't send: x", "why": "y",
                                        "fix_action": "refresh", "refresh_by": "2999-01-01", "at": "2026-10-06T00:00:00+00:00"})
    item = next(g for g in render.build_payload(env.cfg, env.data, env.load())["inbox"] if g["id"] == "ready")["items"][0]
    assert item["failure"]["fix_action"] == "refresh" and item["refresh_pending"] is False
    html = render.render(env.cfg, env.data, env.load()).read_text()
    assert "Refresh &amp; send" in html and 'data-act="${act}"' in html
    assert "refresh:" in html  # the confirmation text


# -- ghwrite ------------------------------------------------------------------

def test_ghwrite_only_posts_to_the_three_endpoints(monkeypatch):
    sent = []
    monkeypatch.setattr(ghwrite, "_send", lambda path, body, token: sent.append((path, body, token)) or {})
    monkeypatch.setenv("GH_TOKEN", "t")
    ghwrite.fork("o/r")
    ghwrite.create_pr("o/r", "T", "B", "me:br", "main")
    ghwrite.comment("o/r", 7, "hi")
    assert [p for p, _, _ in sent] == ["repos/o/r/forks", "repos/o/r/pulls", "repos/o/r/issues/7/comments"]
    assert sent[1][1] == {"title": "T", "body": "B", "head": "me:br", "base": "main", "maintainer_can_modify": True}
    for bad in ("repos/o/r", "repos/o/r/issues/7", "repos/o/r/git/refs", "repos/o/r/pulls/1/merge",
                "repos/o/r/issues/7/comments/../../x", "user/repos", "repos/o/r/issues/x/comments"):
        with pytest.raises(ghwrite.WriteError):
            ghwrite.post(bad, {})
    assert len(sent) == 3
    monkeypatch.delenv("GH_TOKEN")
    with pytest.raises(ghwrite.WriteError):
        ghwrite.comment("o/r", 7, "hi")


def test_token_scopes_reads_the_header_with_the_same_auth_and_never_posts(monkeypatch):
    seen = []

    class Resp:
        status = 200

        def __init__(self, header):
            self.headers = {"X-OAuth-Scopes": header} if header is not None else {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(header):
        def urlopen(req, timeout=0):
            seen.append((req.get_method(), req.full_url, req.get_header("Authorization"), req.data))
            return Resp(header)
        return urlopen

    monkeypatch.setattr(ghwrite.urllib.request, "urlopen", fake("public_repo, workflow"))
    assert ghwrite.token_scopes("tok") == (200, {"public_repo", "workflow"})
    assert seen == [("GET", "https://api.github.com/user", "Bearer tok", None)]
    monkeypatch.setattr(ghwrite.urllib.request, "urlopen", fake(None))
    assert ghwrite.token_scopes("tok") == (200, None)  # a fine-grained key sends no header
    monkeypatch.setenv("GH_TOKEN", "from-env")
    ghwrite.token_scopes()
    assert seen[-1][2] == "Bearer from-env"

    def denied(req, timeout=0):
        raise ghwrite.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(ghwrite.urllib.request, "urlopen", denied)
    assert ghwrite.token_scopes("bad") == (401, None)

    def offline(req, timeout=0):
        raise OSError("no network")

    monkeypatch.setattr(ghwrite.urllib.request, "urlopen", offline)
    assert ghwrite.token_scopes("tok") == (0, None)  # couldn't check: callers go ahead
    monkeypatch.delenv("GH_TOKEN")
    assert ghwrite.token_scopes() == (401, None)  # no key at all


# -- the workflow -------------------------------------------------------------

def run_blocks(text: str) -> list[str]:
    lines, blocks = text.splitlines(), []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not m:
            continue
        indent, block = len(m.group(1)), [m.group(2)]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent + 1:
                break
            block.append(nxt)
        blocks.append("\n".join(block))
    return blocks


def test_act_workflow_never_interpolates_inputs_into_scripts():
    blocks = run_blocks(datarepo.ACT_YML)
    assert len(blocks) == 16 and any("scout" in b for b in blocks)
    for b in blocks:
        assert "${{" not in b, b
    assert "${{ inputs" not in "\n".join(blocks)
    for name in ("KEY", "ACTION", "TITLE", "BODY_B64", "DRY_RUN"):
        assert f"{name}: ${{{{ inputs." in datarepo.ACT_YML
    assert '"$KEY"' in datarepo.ACT_YML and "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$" in datarepo.ACT_YML


def test_act_workflow_retries_the_save_and_fails_loudly():
    y = datarepo.ACT_YML
    assert "for attempt in 1 2 3" in y and "git rebase --abort" in y and "sleep" in y
    assert "GitHub action happened, but saving the new state" in y and "exit 1" in y


def test_act_workflow_shape():
    y = datarepo.ACT_YML
    assert "workflow_dispatch:" in y and "group: data" in y and "cancel-in-progress: false" in y
    assert "contents: write" in y and "GH_TOKEN: ${{ secrets.SUBMIT_TOKEN }}" in y
    assert y.index("permissions:\n  contents: read\n\nconcurrency") < y.index("jobs:")  # read-only unless a job asks
    assert "ref: claude/scout-data" in y and "repository: aadityad12/oss-scout" in y
    assert "options: [submit, post, followup, approve, later, skip, prepare, pair, unpair, feature, unfeature, summary, refresh]" in y
    assert y.count("secrets.SUBMIT_TOKEN") == 2  # the act step and the send job's submit, nothing else
    assert "github-actions[bot]" in y and "pull -q --rebase origin claude/scout-data" in y
    assert set(act.ACTIONS) == {"submit", "post", "followup", "approve", "later", "skip",
                                "prepare", "pair", "unpair", "feature", "unfeature", "summary", "refresh"}


def job_text(name: str) -> str:
    y = datarepo.ACT_YML
    start = y.index(f"\n  {name}:\n") + 1
    rest = y[start + 1:]
    nxt = re.search(r"\n  [a-z]+:\n", rest)
    return y[start:start + 1 + nxt.start()] if nxt else y[start:]


def test_refresh_runs_as_check_then_send_or_request():
    y = datarepo.ACT_YML
    assert re.findall(r"^  ([a-z]+):$", y.split("\njobs:\n")[1], re.M) == ["act", "check", "send", "request"]
    check, send, request = job_text("check"), job_text("send"), job_text("request")
    assert "needs: act" in check and "inputs.action == 'refresh' && !inputs.dry_run" in check
    assert "needs: check" in send and "needs.check.outputs.same_fix == 'true'" in send
    assert "needs: check" in request and "needs.check.outputs.same_fix != 'true'" in request
    assert "same_fix: ${{ steps.tests.outputs.same_fix }}" in check


def test_the_check_job_holds_no_secrets_and_cannot_write():
    check = job_text("check")
    assert "secrets." not in check and "GH_TOKEN" not in check
    assert "contents: read" in check and "contents: write" not in check
    assert check.count("persist-credentials: false") == 2  # the data and the tool checkouts
    assert "python -m scout refresh-check" in check
    # the patch is uploaded before the project's tests run, so nothing the tests do can change it
    assert check.index("--phase apply") < check.index("name: refresh-patch") < check.index("--phase tests") < check.index("name: refresh-verdict")
    assert "git push" not in check and "git commit" not in check


def test_only_the_act_job_and_the_send_jobs_submit_step_hold_the_key():
    act_job, send, request = job_text("act"), job_text("send"), job_text("request")
    assert act_job.count("secrets.SUBMIT_TOKEN") == 1 and send.count("secrets.SUBMIT_TOKEN") == 1
    assert "secrets.SUBMIT_TOKEN" not in request
    submit_step = send[send.index("name: Submit"):send.index("Refresh the dashboard and digest")]
    assert "secrets.SUBMIT_TOKEN" in submit_step
    # the patch is swapped in and saved before the submit, with the key not yet in play
    assert send.index("refresh-apply") < send.index("Save the refreshed patch") < send.index("name: Submit")
    assert "--action submit" in send and "for attempt in 1 2 3" in send


def test_the_request_job_records_then_starts_the_routine_or_says_why_not():
    request = job_text("request")
    assert request.index("refresh-request") < request.index("git commit") < request.index("curl -sS")
    assert "ROUTINE_FIRE_URL: ${{ secrets.ROUTINE_FIRE_URL }}" in request and "ROUTINE_TOKEN: ${{ secrets.ROUTINE_TOKEN }}" in request
    assert "Routine trigger not configured" in request and "exit 0" in request  # a missing secret is a log line, not a failure
    nightly = (config.ROOT / ".github/workflows/nightly.yml").read_text()
    assert nightly[nightly.index("curl -sS"):nightly.index("-d '{}'")].replace("\\\n", "") .split() == \
        request[request.index("curl -sS"):request.index("-d \"{")].replace("\\\n", "").split()
    assert 'Refresh request: $KEY' in request


def test_track_workflow_never_interpolates_inputs_into_scripts():
    y = datarepo.TRACK_YML
    blocks = run_blocks(y)
    assert len(blocks) == 5 and any("scout track" in b for b in blocks)
    for b in blocks:
        assert "${{" not in b, b
    assert "inputs" not in y and "workflow_dispatch:" in y


def test_track_workflow_shape():
    y = datarepo.TRACK_YML
    assert "group: data" in y and "cancel-in-progress: false" in y and "contents: write" in y
    assert "ref: claude/scout-data" in y and "repository: aadityad12/oss-scout" in y
    assert "id: track" in y and "GH_TOKEN: ${{ github.token }}" in y and "SCOUT_DATA_DIR: ../data" in y
    assert y.count("secrets.SUBMIT_TOKEN") == 1  # only the step that sends refreshed drafts
    send = y[y.index("Send refreshed drafts"):y.index("Refresh the dashboard and digest")]
    assert "steps.track.outputs.send != ''" in send and "python -m scout act --key \"$key\" --action submit" in send
    assert y.index("python -m scout track") < y.index("Send refreshed drafts") < y.index("git commit")
    assert "python -m scout track" in y and "python -m scout digest" in y and "python -m scout render" in y
    assert "github-actions[bot]" in y and "for attempt in 1 2 3" in y and "git rebase --abort" in y
    assert y.index("python -m scout track") < y.index("git commit") < y.index("Start a Claude draft")
    assert "if: steps.track.outputs.fire == 'true'" in y
    assert "ROUTINE_FIRE_URL: ${{ secrets.ROUTINE_FIRE_URL }}" in y and "ROUTINE_TOKEN: ${{ secrets.ROUTINE_TOKEN }}" in y
    assert "anthropic-beta: experimental-cc-routine-2026-04-01" in y and "Routine trigger not configured" in y
    nightly = (config.ROOT / ".github/workflows/nightly.yml").read_text()
    curl = nightly[nightly.index("curl -sS"):nightly.index("-d '{}'")]
    assert curl in y and "datarepo" not in y
    assert "group: data" in datarepo.ACT_YML


def test_init_data_installs_the_workflows(tmp_path):
    written = datarepo.init(tmp_path, "me")
    assert ".github/workflows/act.yml" in written and ".github/workflows/track.yml" in written
    assert (tmp_path / ".github/workflows/act.yml").read_text() == datarepo.ACT_YML
    assert (tmp_path / ".github/workflows/track.yml").read_text() == datarepo.TRACK_YML


# -- the dashboard payload ----------------------------------------------------

def test_payload_has_ready_waiting_actions_and_digest(env):
    env.add_followup({**SMALL, "pr_url": "https://github.com/o/r/pull/9"})
    write(env.data, good_briefing("o/r#8", ready=True, kind="review", post_target="https://github.com/o/r/pull/8"),
          patch=None, post="Looks right.")
    st = env.load()
    briefings.ingest(env.data, st)
    st["contributions"] = {"prs": [{**open_pr("o/r", 9), "title": "Fix it", "waiting_on_you": True,
                                    "updated_at": "2026-10-02T09:00:00Z",
                                    "review_comments": [{"author": "rev", "kind": "comment", "at": "2026-10-02T09:00:00Z",
                                                         "body": "rename x"}]}]}
    st["actions"] = [{"at": "a", "key": KEY, "action": "later", "result": "ok"},
                     {"at": "b", "key": KEY, "action": "skip", "result": "ok"}]
    st["suggestions"]["o/r#8"]["snoozed_until"] = "2999-01-01T00:00:00+00:00"
    env.save(st)
    (env.data / "digest.json").write_text(json.dumps({"token_warning": True, "token_age_days": 85}))
    payload = render.build_payload(env.cfg, env.data, st)
    groups = {g["id"]: g["items"] for g in payload["inbox"]}
    assert groups["ready"] == [] and [r["key"] for r in groups["snoozed"]] == ["o/r#8"]
    item = groups["snoozed"][0]
    assert item["post"].startswith("Looks right") and item["kind"] == "review"
    w = groups["waiting"][0]
    assert w["key"] == KEY and w["comments"][0]["body"] == "rename x"
    assert w["followup"]["kind"] == "small" and w["followup"]["reply"] == "Renamed, thanks." and w["followup"]["patch"] == "+fix\n"
    assert payload["log"]["actions"][0]["action"] == "skip"
    assert payload["digest"]["token_warning"] is True
    html = render.render(env.cfg, env.data, st).read_text()
    assert "/api/act" in html and "</script><script>" not in html


def test_ready_payload_carries_the_editable_files(env):
    payload = render.build_payload(env.cfg, env.data, env.load())
    item = next(g for g in payload["inbox"] if g["id"] == "ready")["items"][0]
    assert item["pr"]["title"] == PR_JSON["title"] and item["patch"] == "+x\n" and item["problems"] == []


def test_cli_dry_run(env, monkeypatch, capsys):
    from scout import __main__ as cli
    monkeypatch.setenv("SCOUT_DATA_DIR", str(env.data))
    with pytest.raises(SystemExit) as e:
        cli.main(["act", "--key", KEY, "--action", "submit", "--dry-run"])
    assert e.value.code == 0 and '"dry_run": true' in capsys.readouterr().out


# -- apply_patch in a blob:none clone, with real git --------------------------

def sh(*args, cwd=None):
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout


FULL = "a" * 40
SHA256 = "b" * 64


def test_preimage_ids_reads_the_old_side_of_index_lines():
    patch = (f"diff --git a/x b/x\nindex {FULL}..{'c' * 40} 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
             f"diff --git a/y b/y\nindex {SHA256}..{'d' * 64}\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n"
             f"diff --git a/x2 b/x2\nindex {FULL}..{'e' * 40} 100644\n")  # a repeat is listed once
    assert refresh.preimage_ids(patch) == [FULL, SHA256]


def test_preimage_ids_skips_abbreviated_ids_and_new_files():
    patch = ("diff --git a/x b/x\nindex 1a2b3c4..5d6e7f8 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
             f"diff --git a/new b/new\nnew file mode 100644\nindex {'0' * 40}..{'f' * 40}\n--- /dev/null\n+++ b/new\n"
             f"diff --git a/new2 b/new2\nnew file mode 100644\nindex {'0' * 64}..{'f' * 64}\n"
             f"diff --git a/ok b/ok\nindex {FULL}..{'c' * 40} 100644\n")
    assert refresh.preimage_ids(patch) == [FULL]
    assert refresh.preimage_ids("+index " + FULL + ".." + FULL + "\nnot an index line\n") == []


def test_preimage_ids_handles_binary_patches():
    patch = (f"diff --git a/logo.png b/logo.png\nindex {FULL}..{'c' * 40} 100644\nGIT binary patch\nliteral 3\nKcmZQz0000\n\n"
             f"literal 0\nHcmV?d00001\n\n"
             f"diff --git a/new.bin b/new.bin\nnew file mode 100644\nindex {'0' * 40}..{'c' * 40}\nGIT binary patch\n"
             f"literal 3\nKcmZQz0000\n\nliteral 0\nHcmV?d00001\n\n")
    assert refresh.preimage_ids(patch) == [FULL]


class Partial:
    """A bare 'upstream' with a patch made on commit A and a partial clone sitting on newer commit B."""

    def __init__(self, tmp: Path, full_index: bool = True):
        self.bare, self.src, self.work = tmp / "up.git", tmp / "src", tmp / "clone"
        sh("init", "--bare", "-b", "main", str(self.bare))
        sh("config", "uploadpack.allowFilter", "true", cwd=self.bare)
        sh("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.bare)
        sh("clone", str(self.bare), str(self.src))
        self.lines = [f"line {i}\n" for i in range(1, 31)]
        self.write("A")
        self.old_blob = sh("rev-parse", "HEAD:code.py", cwd=self.src).strip()
        fixed = list(self.lines)
        fixed[16] = "line 17 fixed\n"
        (self.src / "code.py").write_text("".join(fixed))
        self.patch = tmp / "draft.patch"
        self.patch.write_text(sh("diff", *(["--full-index"] if full_index else []), "--binary", cwd=self.src))
        self.lines[14] = "line 15 reworded upstream\n"  # inside the fix's 3 context lines
        self.write("B")
        self.url = f"file://{self.bare}"

    def write(self, msg):
        (self.src / "code.py").write_text("".join(self.lines))
        sh("add", "-A", cwd=self.src)
        sh("commit", "-m", msg, cwd=self.src)
        sh("push", "origin", "main", cwd=self.src)

    def clone_as_submit_does(self):
        """What `submit` leaves behind: a blob:none clone with upstream fetched as a promisor remote."""
        sh("clone", "--filter=blob:none", self.url, str(self.work))
        for args in (["remote", "add", "upstream", self.url], ["config", "remote.upstream.promisor", "true"],
                     ["config", "remote.upstream.partialclonefilter", "blob:none"],
                     ["fetch", "--filter=blob:none", "--no-tags", "upstream", "main"],
                     ["checkout", "--no-track", "-b", "fix", "upstream/main"]):
            sh(*args, cwd=self.work)
        return self.work

    def has(self, blob):
        env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
        return subprocess.run(["git", "cat-file", "-e", blob], cwd=self.work, env=env).returncode == 0


@pytest.fixture
def no_lazy_fetch(monkeypatch):
    # Newer git lets `apply --3way` fetch missing blobs itself; the git on the runners does not.
    return lambda: monkeypatch.setenv("GIT_NO_LAZY_FETCH", "1")  # call after the clone: its checkout needs lazy fetch


def test_apply_patch_fetches_the_preimage_blobs_for_a_three_way_apply(tmp_path, no_lazy_fetch):
    p = Partial(tmp_path)
    work = p.clone_as_submit_does()
    no_lazy_fetch()
    assert not p.has(p.old_blob)  # the clone never downloaded the version the patch was made on
    plain = subprocess.run(["git", "apply", "--check", str(p.patch)], cwd=work, capture_output=True, text=True)
    assert plain.returncode  # the context moved, so only a 3-way merge can apply it
    raw = subprocess.run(["git", "apply", "--3way", str(p.patch)], cwd=work, capture_output=True, text=True)
    assert raw.returncode and "lacks the necessary blob" in raw.stderr  # the production failure
    act.apply_patch(work, p.patch)
    text = (work / "code.py").read_text()
    assert "line 17 fixed" in text and "line 15 reworded upstream" in text
    assert p.has(p.old_blob) and "M  code.py" in sh("status", "--short", cwd=work)


def test_apply_patch_without_full_ids_has_nothing_to_prefetch(tmp_path, no_lazy_fetch):
    p = Partial(tmp_path, full_index=False)
    work = p.clone_as_submit_does()
    no_lazy_fetch()
    with pytest.raises(act.PatchError, match="lacks the necessary blob"):
        act.apply_patch(work, p.patch)


def test_apply_patch_goes_on_to_three_way_when_the_fetch_fails(tmp_path, no_lazy_fetch):
    p = Partial(tmp_path)
    work = p.clone_as_submit_does()
    no_lazy_fetch()
    sh("remote", "set-url", "upstream", str(tmp_path / "nowhere.git"), cwd=work)
    with pytest.raises(act.PatchError, match="lacks the necessary blob"):
        act.apply_patch(work, p.patch)
