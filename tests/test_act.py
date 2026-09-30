import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from scout import act, briefings, config, datarepo, digest, ghwrite, render, state as statemod, track, wip
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

    def fake_git(args, cwd=None):
        e.git_calls.append(args)
        if e.git_fail and e.git_fail in " ".join(args):
            raise act.ActError(f"git {args[0]} failed: https://x-access-token:sekret@github.com/x")
        return e.remote_branch if args[0] == "ls-remote" else ""

    def fake_read(path):
        e.reads.append(path)
        if "/pulls?head=" in path:
            return e.pulls
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
    ("guide mode", lambda e: e.edit_briefing(mode="guide"), "guide mode"),
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
    settings = {"max_ready": 1, "max_open_prs": 3, "max_open_prs_per_repo": 1}
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
    assert len(blocks) == 4 and any("scout" in b for b in blocks)
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
    assert "workflow_dispatch:" in y and "group: act" in y and "cancel-in-progress: false" in y
    assert "contents: write" in y and "GH_TOKEN: ${{ secrets.SUBMIT_TOKEN }}" in y
    assert "ref: claude/scout-data" in y and "repository: aadityad12/oss-scout" in y
    assert "options: [submit, post, followup, approve, later, skip]" in y
    assert y.count("secrets.SUBMIT_TOKEN") == 1  # only the act step gets the token
    assert "github-actions[bot]" in y and "pull -q --rebase origin claude/scout-data" in y
    assert set(act.ACTIONS) == {"submit", "post", "followup", "approve", "later", "skip"}


def test_init_data_installs_the_workflow(tmp_path):
    written = datarepo.init(tmp_path, "me")
    assert ".github/workflows/act.yml" in written
    assert (tmp_path / ".github/workflows/act.yml").read_text() == datarepo.ACT_YML


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
    assert [r["key"] for r in payload["ready_items"]] == ["o/r#8"]
    item = payload["ready_items"][0]
    assert item["post"].startswith("Looks right") and item["kind"] == "review"
    w = payload["waiting"][0]
    assert w["key"] == KEY and w["comments"][0]["body"] == "rename x"
    assert w["followup"]["kind"] == "small" and w["followup"]["reply"] == "Renamed, thanks." and w["followup"]["patch"] == "+fix\n"
    assert payload["actions"][0]["action"] == "skip"
    assert payload["digest"]["token_warning"] is True
    html = render.render(env.cfg, env.data, st).read_text()
    assert "/api/act" in html and "</script><script>" not in html


def test_ready_payload_carries_the_editable_files(env):
    payload = render.build_payload(env.cfg, env.data, env.load())
    item = payload["ready_items"][0]
    assert item["pr"]["title"] == PR_JSON["title"] and item["patch"] == "+x\n" and item["problems"] == []


def test_cli_dry_run(env, monkeypatch, capsys):
    from scout import __main__ as cli
    monkeypatch.setenv("SCOUT_DATA_DIR", str(env.data))
    with pytest.raises(SystemExit) as e:
        cli.main(["act", "--key", KEY, "--action", "submit", "--dry-run"])
    assert e.value.code == 0 and '"dry_run": true' in capsys.readouterr().out
