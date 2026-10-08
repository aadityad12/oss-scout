import json
from datetime import datetime, timezone

import pytest

from scout import briefings, config, digest, plain

NOW = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)  # a Saturday

PATCH = """\
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 568ae2a..697c934 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -91,6 +91,9 @@ jobs:
+      - name: Test converter
+        run: python3 -m unittest tools/test_conv.py
+
diff --git a/tools/conv.py b/tools/conv.py
index 5783475..ad0bc5c 100644
--- a/tools/conv.py
+++ b/tools/conv.py
@@ -46,3 +46,5 @@ def find_params(doc):
-    old = 1
+    if not isinstance(doc, dict):
+        raise SystemExit("empty")
diff --git a/tools/test_conv.py b/tools/test_conv.py
new file mode 100644
--- /dev/null
+++ b/tools/test_conv.py
@@ -0,0 +1,3 @@
""" + "".join(f"+line {i}\n" for i in range(100))

BRIEFING = {
    "key": "o/r#7", "repo": "o/r", "number": 7, "title": "Crash on empty input",
    "url": "https://github.com/o/r/issues/7", "summary": "s", "why": "w", "difficulty": "easy",
    "time_estimate": "20-40 min", "walkthrough": "w", "change_explained": "c", "alternatives": [],
    "maintainer_qa": [], "tests": {"ran": False}, "claim_comment": "x", "submit_steps": [], "ai_disclosure": "none",
}


def b(**kw):
    return {**BRIEFING, **kw}


# -- the briefing fields ------------------------------------------------------

def test_plain_problem_and_files_explained_are_optional_and_checked():
    assert briefings.validate(b()) == []
    assert briefings.validate(b(plain_problem="A tool crashes.", files_explained=["a.py: a fix"])) == []
    assert briefings.validate(b(plain_problem="  ")) == ["plain_problem should be a non-empty string"]
    assert briefings.validate(b(plain_problem=3)) == ["plain_problem should be a non-empty string"]
    assert briefings.validate(b(files_explained="a.py: x")) == ["files_explained should be a list of strings"]
    assert briefings.validate(b(files_explained=["ok", 3])) == ["files_explained should be a list of strings"]


# -- what's broken ------------------------------------------------------------

def test_problem_prefers_the_routines_sentence():
    assert plain.problem(b(plain_problem=" A script crashes on empty files. ", summary="`x()` fails.")) == "A script crashes on empty files."


def test_problem_falls_back_to_the_first_sentence_without_code_words():
    summary = "Preparing a query crashes `PREPARE` for `nextval()` calls in tools/x.py. Then everything fails."
    assert plain.problem(b(summary=summary)) == "Preparing a query crashes for calls in."
    # nothing readable left: use the title
    assert plain.problem(b(summary="`a_b()` `c_d()`.")) == "Crash on empty input"
    assert plain.problem(b(summary="(Ctrl+F12) hides every part of the interface but the badge still shows.")) == \
        "Hides every part of the interface but the badge still shows."
    assert plain.first_sentence("Use e.g. a flag. Then more.") == "Use e.g. a flag."


# -- what you'd send ----------------------------------------------------------

def test_a_pull_request_is_described_by_its_files_and_size():
    ready = b(ready=True)
    assert plain.sending(ready, PATCH) == "A pull request: 1 new test file and a small fix (3 files, ~110 lines)"
    tiny = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1 +1 @@\n-x\n+y\n"
    assert plain.sending(ready, tiny) == "A pull request: a small fix (1 file, 2 lines)"
    only_new = "diff --git a/n.txt b/n.txt\nnew file mode 100644\n--- /dev/null\n+++ b/n.txt\n+a\n"
    assert plain.sending(ready, only_new) == "A pull request: 1 new file (1 file, 1 line)"
    big = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n" + "+x\n" * 60
    assert plain.sending(ready, big) == "A pull request: a fix (1 file, ~60 lines)"


def test_comments_and_unprepared_items():
    assert plain.sending(b(ready=True, kind="repro"), "").startswith("A comment on the issue")
    assert plain.sending(b(ready=True, kind="review", post_target="https://github.com/o/r/pull/3"), "").startswith("A review comment")
    assert plain.sending(b(ready=True, kind="triage"), "").startswith("A comment on the issue")
    assert plain.sending(b(mode="pair"), "") == "Nothing prepared yet: a laptop session where we write it together"
    assert plain.sending(b(mode="guide"), PATCH) == "Nothing prepared yet: a laptop session where we write it together"
    assert plain.sending(b(mode="own"), "") == "Nothing prepared yet: a laptop session where you write the fix and we check it"
    assert plain.sending(b(), PATCH) == "Nothing prepared yet: a laptop session where we write it together"  # a patch, but not ready


# -- what you do, and how long ------------------------------------------------

def test_one_tap_read_time_is_a_minute_per_50_lines_and_at_least_two():
    ready = b(ready=True, _patch=PATCH)
    assert plain.your_part({"status": "ready"}, ready) == "Read it and tap Submit PR, about 2 minutes"
    assert plain.your_part({"status": "approved"}, b(ready=True, _patch="+x\n" * 140 + "diff --git a/a b/a\n")) == \
        "Read it and tap Submit PR, about 3 minutes"
    assert plain.your_part({"status": "ready"}, b(ready=True, _patch="diff --git a/a b/a\n")) == "Read it and tap Submit PR, about 2 minutes"
    comment = b(ready=True, kind="repro", _post="word " * 24)
    assert plain.your_part({"status": "ready"}, comment) == "Read it and tap Post comment, about 2 minutes"


@pytest.mark.parametrize("estimate,expected", [
    ("20 min first build, then 60-120 min to trace the code", "Laptop: run /contribute, about 2.5 hours including the build"),
    ("1-2 h plus a debug build of DuckDB", "Laptop: run /contribute, about 2 hours including the build"),
    ("20-40 min to review", "Laptop: run /contribute, about 40 minutes"),
    ("about 1 hour", "Laptop: run /contribute, about 1 hour"),
    ("a while", "Laptop: run /contribute"),
])
def test_laptop_items_name_the_time_shortened(estimate, expected):
    for mode in ("pair", "own"):
        assert plain.your_part({"status": "suggested"}, b(mode=mode, time_estimate=estimate)) == expected


def test_a_failed_item_says_to_refresh_by_the_date():
    item = {"status": "approved", "failure": {"plain": "p", "fix_action": "refresh", "refresh_by": "2026-10-12"}}
    assert plain.your_part(item, b(ready=True, _patch=PATCH)) == "Tap Refresh & send by Oct 12"
    assert plain.your_part({**item, "failure": {**item["failure"], "refresh_by": None}}, b()) == "Tap Refresh & send soon"
    assert plain.your_part({**item, "refresh_pending": True}, b()) == "Nothing to do: it is being refreshed now"
    for action, text in (("token", "Replace your GitHub key"), ("edit", "Fix the text"), ("skip", "Tap Skip"), ("none", "Nothing to do")):
        assert plain.your_part({"status": "approved", "failure": {"plain": "p", "fix_action": action}}, b()).startswith(text)


def test_replies_get_three_lines_by_follow_up_kind():
    small = plain.reply_lines("Fix the crash", "small")
    assert small["problem"] == "A maintainer replied on your pull request: Fix the crash"
    assert "Push fix & reply" in small["your_part"]
    assert "/contribute" in plain.reply_lines("t", "discuss")["your_part"]
    assert "draft is on its way" in plain.reply_lines("", None)["sending"]


# -- the digest carries them -----------------------------------------------------

def put(data, key, brief, patch=None):
    d = data / "briefings" / briefings.slug(key)
    d.mkdir(parents=True)
    (d / "briefing.json").write_text(json.dumps({**BRIEFING, "key": key, "repo": key.split("#")[0], "number": int(key.split("#")[1]), **brief}))
    if patch:
        (d / "draft.patch").write_text(patch)
        (d / "pr.json").write_text(json.dumps({"title": "t", "body": "b", "base": "main", "branch": "fix/7", "fixes": 7, "signoff": False}))


def test_digest_items_carry_the_three_lines_and_the_weekly_list(tmp_path):
    put(tmp_path, "o/r#1", {"ready": True, "plain_problem": "A tool crashes on empty files."}, PATCH)
    put(tmp_path, "o/r#2", {"mode": "pair", "plain_problem": "Preparing a query corrupts the session.", "time_estimate": "1-2 h"})
    put(tmp_path, "o/r#3", {"mode": "own", "plain_problem": "Too old."})
    put(tmp_path, "o/r#4", {"plain_problem": "Already ready."}, None)
    sugg = {
        "o/r#1": {"status": "approved", "title": "A", "suggested_at": "2026-10-03T00:00:00+00:00",
                  "failure": {"plain": "Couldn't send.", "why": "w", "fix_action": "refresh", "refresh_by": "2026-10-12"}},
        "o/r#2": {"status": "suggested", "title": "B", "suggested_at": "2026-10-08T00:00:00+00:00"},
        "o/r#3": {"status": "suggested", "title": "C", "suggested_at": "2026-10-01T00:00:00+00:00"},
        "o/r#4": {"status": "ready", "title": "D", "suggested_at": "2026-10-09T00:00:00+00:00"},
        "o/r#5": {"status": "suggested", "title": "E", "suggested_at": "2026-10-09T00:00:00+00:00", "snoozed_until": "2026-10-20T00:00:00+00:00"},
    }
    d = digest.build(config.load(), tmp_path, {"suggestions": sugg, "contributions": {"prs": []}}, NOW)
    [s] = d["stuck"]
    assert s["problem"] == "A tool crashes on empty files."
    assert s["sending"] == "A pull request: 1 new test file and a small fix (3 files, ~110 lines)"
    assert s["your_part"] == "Tap Refresh & send by Oct 12"
    assert [i["key"] for i in d["ready"]] == ["o/r#4"]  # a stuck item is not listed twice
    assert [i["key"] for i in d["weekly"]] == ["o/r#2"]  # the last 7 days, not ready, not snoozed
    assert d["weekly"][0]["your_part"] == "Laptop: run /contribute, about 2 hours"
    assert d["weekly"][0]["sending"].startswith("Nothing prepared yet")


def test_laptop_briefings_alone_do_not_trigger_the_daily_email(tmp_path):
    sugg = {"o/r#2": {"status": "suggested", "title": "B", "suggested_at": "2026-10-10T01:00:00+00:00", "pairing": True}}
    d = digest.build(config.load(), tmp_path, {"suggestions": sugg, "contributions": {"prs": []}}, NOW)
    assert d["send"] is False and len(d["new_briefings"]) == 1 and len(d["weekly"]) == 1


def test_waiting_items_say_what_the_reply_is_about(tmp_path):
    sugg = {"o/r#5": {"status": "waiting_on_you", "title": "E", "pr_url": "https://github.com/o/r/pull/5", "followup_kind": "small"}}
    prs = [{"repo": "o/r", "number": 5, "url": "https://github.com/o/r/pull/5", "status": "open", "waiting_on_you": True,
            "title": "Fix the crash", "updated_at": "2026-10-10T09:00:00Z"}]
    [w] = digest.build(config.load(), tmp_path, {"suggestions": sugg, "contributions": {"prs": prs}}, NOW)["waiting_on_you"]
    assert w["problem"] == "A maintainer replied on your pull request: Fix the crash" and w["title"] == "Fix the crash"
    assert "Push fix & reply" in w["your_part"]
