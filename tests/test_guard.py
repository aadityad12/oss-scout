import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "guard" / "guard.py"


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """Two fake checkouts: the private data repo and the public tool repo."""
    base = tmp_path_factory.mktemp("repos")
    out = {}
    for name in ("oss-scout-data", "oss-scout"):
        d = base / name
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin",
                        f"https://github.com/aadityad12/{name}.git"], check=True)
        out[name] = str(d)
    return out


def run(tool, cwd=None, **inp):
    event = {"tool_name": tool, "tool_input": inp, "cwd": cwd}
    p = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event),
                       capture_output=True, text=True)
    return p.returncode, p.stderr


def bash(cmd, cwd=None):
    return run("Bash", cwd=cwd, command=cmd)[0]


@pytest.mark.parametrize("cmd", [
    "gh pr create --title x --body y",
    "gh issue comment 12 --body 'I will take this'",
    "gh -R duckdb/duckdb issue comment 5 -b hi",
    "gh pr review 3 --approve",
    "gh repo fork duckdb/duckdb",
    "gh api repos/o/r/issues/1/comments -f body=hi",
    "gh api -X POST repos/o/r/issues",
    "gh api --method=PATCH repos/o/r",
    "gh api -XDELETE repos/o/r/labels/x",
    "gh api graphql -f query='mutation { addStar }'",
    "gh label create foo",
    "gh release create v1",
    "gh auth token",
    "cd /tmp && gh pr create",
    "curl -X POST https://api.github.com/repos/o/r/issues -d '{}'",
    "curl https://api.github.com/repos/o/r/issues --data '{\"title\":1}'",
    "python3 -c \"import requests; requests.post('https://api.github.com/repos/o/r/issues')\"",
    "git remote set-url origin https://github.com/someone/else",
    "GH_TOKEN=x gh pr merge 4",
])
def test_blocks_writes(cmd):
    assert bash(cmd) == 2, cmd


@pytest.mark.parametrize("cmd", [
    "gh api repos/duckdb/duckdb/issues?state=open",
    "gh api -X GET search/issues -f q=is:issue",
    "gh pr view 3",
    "gh issue list -R o/r",
    "gh search issues 'label:bug'",
    "curl -s https://api.github.com/repos/o/r",
    "git clone --depth 50 https://github.com/duckdb/duckdb /tmp/work/x",
    "git diff > draft.patch",
    "python3 -m scout run",
    "rg 'TODO' src/",
])
def test_allows_reads(cmd):
    assert bash(cmd) == 0, cmd


def test_push_only_data_branch_from_data_repo(repos):
    data, tool = repos["oss-scout-data"], repos["oss-scout"]
    assert bash("git push origin claude/scout-data", cwd=data) == 0
    assert bash("git push -u origin claude/scout-data", cwd=data) == 0
    assert bash("git push origin main", cwd=data) == 2
    assert bash("git push --force origin claude/scout-data", cwd=data) == 2
    assert bash("git push", cwd=data) == 2
    assert bash("git push origin claude/scout-data", cwd=tool) == 2
    assert bash(f"cd {tool} && git push origin claude/scout-data", cwd=data) == 2
    assert bash(f"git -C {tool} push origin claude/scout-data", cwd=data) == 2
    assert bash(f"cd {data} && git add -A && git commit -m x && git push origin claude/scout-data", cwd=tool) == 0


@pytest.mark.parametrize("name,code", [
    ("mcp__github__create_pull_request", 2),
    ("mcp__github__add_issue_comment", 2),
    ("mcp__github__merge_pull_request", 2),
    ("mcp__github__get_issue", 0),
    ("mcp__github__list_pull_requests", 0),
    ("mcp__slack__post_message", 0),
])
def test_mcp_github_tools(name, code):
    assert run(name)[0] == code


def test_block_message_is_explained():
    code, err = run("Bash", command="gh issue comment 1 -b hi")
    assert code == 2 and "never writes to GitHub" in err


# ---- agent profile ----

def agent(tool="Bash", cwd=None, args=("--profile", "agent"), env=None, **inp):
    event = {"tool_name": tool, "tool_input": inp, "cwd": cwd}
    p = subprocess.run([sys.executable, str(GUARD), *args], input=json.dumps(event),
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stderr


def agent_bash(cmd, cwd=None):
    return agent(command=cmd, cwd=cwd)[0]


@pytest.mark.parametrize("cmd", [
    "git push origin main",
    "git push origin claude/scout-data",
    "git push --force",
    "git push",
    "curl --unix-socket /run/holt/web.sock http://localhost/api/actions -X POST",
    "python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/holt/agent.sock')\"",
    "cat /etc/holt/credentials/github-read",
    "sudo -n true",
    "gh pr create --title x --body y",
    # every form of push
    "git -C /tmp/x push origin main",
    "git -c user.name=x push origin main",
    "git --no-pager push",
    "GIT_TERMINAL_PROMPT=0 git push origin main",
    "env -i git push origin main",
    "timeout 30 git push origin main",
    "/usr/bin/git push origin main",
    "sh -c 'git push origin main'",
    "bash -c \"git status; git push origin main\"",
    "git status && git push origin main",
    "git status; git push origin main",
    "git status | git push origin main",
    "git status & git push origin main",
    "echo $(git push origin main)",
    "(git push origin main)",
    "git -c alias.p=push p",
    "git send-pack origin main",
    "python3 -c \"import subprocess; subprocess.run(['git', 'push'])\"",
    # sudo anywhere
    "sudo systemctl restart x",
    "env sudo ls",
    "ls && sudo ls",
    "bash -c 'sudo ls'",
    # holt sockets and folders
    "curl --unix-socket=/x http://localhost",
    "nc -U /tmp/s",
    "nc -lU /tmp/s",
    "socat - UNIX-CONNECT:/tmp/s",
    "ls /var/lib/holt",
    "cat /srv/holt/notes",
    "ls /etc/holt",
    "cat /etc/./holt/credentials/x",
    "cd /etc && cat holt/credentials/x",
    "cd /run && ls holt",
    "bash -c 'cat /etc/holt/credentials/x'",
    # credential folders
    "cat $CREDENTIALS_DIRECTORY/github-read",
    "cat ${CREDENTIALS_DIRECTORY}/github-read",
    "ls /run/credentials/holt.service",
    # existing blocks stay
    "gh api -X POST repos/o/r/issues",
    "curl -X POST https://api.github.com/repos/o/r/issues -d '{}'",
    "git remote set-url origin https://github.com/someone/else",
])
def test_agent_blocks(cmd):
    code, err = agent(command=cmd)
    assert code == 2, cmd
    assert err.startswith("Blocked by oss-scout guard"), cmd


@pytest.mark.parametrize("cmd", [
    "git status",
    "git commit -m x",
    "git fetch origin",
    "gh issue view 1 --repo o/r",
    "gh api repos/o/r/issues",
    "ls /home/agent",
    "ls /etc",
    "echo done 2>&1",
    "cd /tmp && git diff > draft.patch",
    "python3 -m scout run",
    "rg 'TODO' src/",
])
def test_agent_allows(cmd):
    assert agent_bash(cmd) == 0, cmd


def test_agent_push_message():
    code, err = agent(command="git push origin main")
    assert code == 2 and "pushes are done by the owner, not the agent" in err


def test_agent_blocks_push_that_cloud_allows(repos):
    data = repos["oss-scout-data"]
    assert bash("git push origin claude/scout-data", cwd=data) == 0
    assert agent_bash("git push origin claude/scout-data", cwd=data) == 2


@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": "/etc/holt/credentials/github-read"}),
    ("Read", {"file_path": "/etc/holt/../holt/credentials/x"}),
    ("Read", {"file_path": "/var/lib/holt/state.db"}),
    ("Edit", {"file_path": "/srv/holt/app.py", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "/run/holt/agent.sock", "content": "x"}),
    ("Grep", {"pattern": "token", "path": "/etc/holt"}),
    ("Glob", {"pattern": "/etc/holt/credentials/*"}),
    ("Glob", {"pattern": "*", "path": "/var/lib/holt"}),
    ("Read", {"file_path": "/run/credentials/holt.service/github-read"}),
    ("mcp__files__read", {"path": "/etc/holt/credentials/x"}),
])
def test_agent_blocks_file_tools(tool, inp):
    code, err = agent(tool, **inp)
    assert code == 2 and err.startswith("Blocked by oss-scout guard"), (tool, inp)


@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": "/home/agent/notes.md"}),
    ("Edit", {"file_path": "/home/agent/a.py", "old_string": "see /etc/holt", "new_string": "x"}),
    ("Write", {"file_path": "/home/agent/a.md", "content": "config lives in /etc/holt"}),
    ("Grep", {"pattern": "TODO", "path": "/home/agent/src"}),
    ("Glob", {"pattern": "**/*.py"}),
    ("mcp__slack__post_message", {"text": "hi"}),
])
def test_agent_allows_file_tools(tool, inp):
    assert agent(tool, **inp)[0] == 0, (tool, inp)


def test_cloud_profile_does_not_get_agent_rules():
    """The default profile is unchanged: it never looks at Holt paths or sudo."""
    for args in ((), ("--profile", "cloud"), ("--profile=cloud",)):
        assert agent(command="sudo ls /etc/holt", args=args)[0] == 0
        assert agent("Read", file_path="/etc/holt/x", args=args)[0] == 0
        assert agent(command="gh pr create", args=args)[0] == 2


@pytest.mark.parametrize("args", [
    ("--profile",),
    ("--profile", ""),
    ("--profile", "admin"),
    ("--profile", "Agent"),
    ("--profile=agent2",),
    ("--profile=",),
    ("--profile", "agent", "extra"),
    ("--profile", "agent", "--profile", "cloud"),
    ("--profile", "cloud", "--version"),
    ("agent",),
    ("--agent",),
    ("--version", "--profile", "agent"),
    ("-h",),
])
def test_bad_arguments_fail_closed(args):
    # even a harmless tool call is blocked when the guard is misconfigured
    code, err = agent(command="ls", args=args)
    assert code == 2 and err.startswith("Blocked by oss-scout guard"), args


def test_profile_comes_only_from_the_command_line():
    import os
    env = dict(os.environ, GUARD_PROFILE="cloud", OSS_SCOUT_GUARD_PROFILE="cloud", PROFILE="cloud")
    assert agent(command="git push origin main", env=env)[0] == 2
    env["GUARD_PROFILE"] = "agent"
    assert agent(command="sudo ls", args=(), env=env)[0] == 0


def test_agent_fails_closed_on_unreadable_input():
    p = subprocess.run([sys.executable, str(GUARD), "--profile", "agent"], input="not json",
                       capture_output=True, text=True)
    assert p.returncode == 2 and p.stderr.startswith("Blocked by oss-scout guard")
    p = subprocess.run([sys.executable, str(GUARD)], input="not json", capture_output=True, text=True)
    assert p.returncode == 0


def test_version():
    p = subprocess.run([sys.executable, str(GUARD), "--version"], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == "guard 1\n"


def test_settings_agent_json_matches_the_guard():
    s = json.loads((GUARD.parent / "settings.agent.json").read_text())
    hook = s["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "*"
    assert hook["hooks"][0]["command"].endswith("guard.py --profile agent")
    assert "Bash(git push:*)" in s["permissions"]["deny"]
    assert s["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}


# ---- agent profile: whole-text rules, unwrapping, fail closed, file tools ----
# The agent profile reads the command as plain text and errs on blocking, so a
# harmless command that mentions `git` and `push`, or `sudo`, is blocked too.

@pytest.mark.parametrize("cmd", [
    # ways around a token-by-token reading of `git push`
    "echo 'git push origin main' | sh",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'push'])\nEOF",
    "find . -exec git push origin main \\;",
    "git submodule foreach git push",
    "git rebase -x 'git push origin main' HEAD~1",
    "git subtree push --prefix=x origin main",
    "git config alias.p push && git p",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0=push git p",
    "git --config-env alias.p=V p",
    "git --config-env=alias.p=V p",
    "{ git push origin main; }",
    "! git push origin main",
    "if true; then git push origin main; fi",
    "git 2>&1 push origin main",
    "timeout 5 git-send-pack origin main",
    "git-push origin main",
    "git receive-pack x",
    "git http-push x",
    # the sudo family, also inside substitutions
    'echo "$(sudo -n true)"',
    'echo "`sudo -n true`"',
    "doas ls",
    "pkexec ls",
    "run0 ls",
    # unix sockets
    "env nc -U /tmp/s",
    "ncat -Uv /tmp/s",
    "netcat -lU /tmp/s",
    "xargs socat - UNIX-CONNECT:/tmp/s",
    "curl --abstract-unix-socket x http://localhost",
    "curl --unix-socket /tmp/s http://localhost",
    "echo x | python3 -c 'print(\"unix-sendto\")' UNIX-SENDTO:/tmp/s",
    # Holt folders, however they are written
    "cat //etc//holt/credentials/x",
    "cat /etc/./holt/credentials/x",
    "cat /var//lib/./holt/state",
    "cd -P /etc; cat holt/credentials/x",
    "cd /etc && cat holt/credentials/x",
    "cat /run/credentials/holt.service/x",
    "echo $CREDENTIALS_DIRECTORY",
    # GitHub writes hidden behind a wrapper
    "bash -c 'gh pr create --title x --body y'",
    "sh -c \"echo hi; gh issue comment 1 --body hi\"",
    "bash -lc 'gh pr create'",
    "timeout 5 gh pr create --title x --body y",
    "env -i FOO=1 gh pr create --title x --body y",
    "nice -n 5 gh pr merge 3",
    "nohup gh pr comment 1 --body hi",
    "xargs -n1 gh pr comment",
    "stdbuf -oL gh pr create",
    "find . -name x -exec gh pr create \\;",
    "echo $(gh pr create --title x --body y)",
    "echo `gh pr create --title x --body y`",
    "{ gh pr create --title x --body y; }",
    "! gh pr create --title x --body y",
    "if true; then gh pr create --title x --body y; fi",
    "gh 2>&1 pr create --title x --body y",
    "gh 2>/dev/null pr create --title x --body y",
    "timeout 5 bash -c \"env gh pr create\"",
    "eval 'gh pr create'",
    "time curl -X POST https://api.github.com/repos/o/r/issues -d '{}'",
    # now blocked on purpose: the words are there, put them in a file and use `git commit -F file`
    "git commit -m 'fix the git push bug'",
    "git log --grep=sudo",
    "env git commit -m sudo",
])
def test_agent_blocks_hidden_forms(cmd):
    code, err = agent(command=cmd)
    assert code == 2, cmd
    assert err.startswith("Blocked by oss-scout guard"), cmd


def test_agent_push_forms_say_who_pushes():
    for cmd in ("echo 'git push origin main' | sh", "git 2>&1 push", "git -c alias.p=push p", "timeout 5 git-send-pack x"):
        code, err = agent(command=cmd)
        assert code == 2 and "pushes are done by the owner, not the agent" in err, cmd


@pytest.mark.parametrize("cmd", [
    "ls /srv/holt-src",
    "ls /etc/holt.d",
    "ls /home/agent",
    "ls /etc",
    "timeout 60 pytest -k foo",
    "timeout 5 sleep 1",
    "env FOO=1 python3 -m scout run",
    "nice -n 5 ls",
    "xargs -n1 echo",
    "bash -c 'ls && git status'",
    "echo $(git rev-parse HEAD)",
    "find . -name '*.py' -exec grep -l TODO {} +",
    "git commit -m \"fix the parser\"",
    "git status",
    "git fetch origin",
    "git log --oneline -5",
    "git diff HEAD~1",
    "gh issue view 1 --repo o/r",
    "gh pr view 3 --comments",
    "gh 2>&1 pr view 3",
    "npm test",
    "python3 -m pytest -q",
    "cd /tmp && git diff > draft.patch",
    "git commit -F msg.txt",
])
def test_agent_allows_normal_work(cmd):
    assert agent_bash(cmd) == 0, cmd


def test_agent_checks_the_command_in_a_heredoc_the_same_way():
    assert agent_bash("cat <<'EOF' > notes.md\nnothing special\nEOF") == 0
    assert agent_bash("cat <<'EOF' > notes.md\ngit push origin main\nEOF") == 2


@pytest.mark.parametrize("event", [
    # a malformed tool call blocks; it must not crash into exit 1, which lets the tool run
    {"tool_name": "mcp__github__create_issue", "tool_input": {"title": "x\u0000"}},
    {"tool_name": "Bash", "tool_input": {"command": "ls\u0000"}},
    {"tool_name": "Read", "tool_input": {"file_path": "/home/agent/a\u0000b"}},
    {"tool_name": "mcp__slack__post_message", "tool_input": {"text": ["ok", {"k": "a\u0000"}]}},
    {"tool_name": "Bash", "tool_input": "git status"},
    {"tool_name": "Bash", "tool_input": ["git status"]},
    {"tool_name": "Bash", "tool_input": None},
    {"tool_name": "Bash"},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": {"command": None}},
    {"tool_name": "Bash", "tool_input": {"command": ["git", "status"]}},
    {"tool_name": "Bash", "tool_input": {"command": 7}},
    {"tool_name": None, "tool_input": {}},
    {"tool_name": 5, "tool_input": {}},
    {"tool_input": {}},
    {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": 5},
    {"tool_name": "Read", "tool_input": {"file_path": 5}},
    {"tool_name": "Read", "tool_input": {"file_path": ["/home/agent/a"]}},
    {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": {"a": 1}}},
    {"tool_name": "Grep", "tool_input": {"pattern": "x", "glob": 3}},
])
def test_agent_fails_closed_on_malformed_events(event):
    p = subprocess.run([sys.executable, str(GUARD), "--profile", "agent"], input=json.dumps(event),
                       capture_output=True, text=True)
    assert p.returncode == 2 and p.stderr.startswith("Blocked by oss-scout guard"), event


@pytest.mark.parametrize("raw", ["null", "[]", "[1]", "7", '"x"', "true", "{", "", "not json", '{"tool_name":'])
def test_agent_fails_closed_on_non_object_input(raw):
    p = subprocess.run([sys.executable, str(GUARD), "--profile", "agent"], input=raw,
                       capture_output=True, text=True)
    assert p.returncode == 2 and p.stderr.startswith("Blocked by oss-scout guard"), raw


def test_agent_fails_closed_on_very_deep_input():
    p = subprocess.run([sys.executable, str(GUARD), "--profile", "agent"],
                       input='{"tool_name":"mcp__x__y","tool_input":' + "[" * 100000 + "]" * 100000 + "}",
                       capture_output=True, text=True)
    assert p.returncode == 2 and p.stderr.startswith("Blocked by oss-scout guard")


def test_agent_fails_closed_when_commands_nest_too_deep():
    cmd = "ls"
    for _ in range(8):
        cmd = "sh -c " + json.dumps(cmd)
    assert agent_bash(cmd) == 2


@pytest.mark.parametrize("tool,inp,cwd", [
    ("Read", {"file_path": "holt/credentials/github-read"}, "/etc"),
    ("Read", {"file_path": "../holt/credentials/github-read"}, "/etc/ssl"),
    ("Read", {"file_path": "credentials/github-read"}, "/etc/holt"),
    ("Glob", {"pattern": "*"}, "/etc/holt"),
    ("Glob", {"pattern": "holt/*"}, "/etc"),
    ("Glob", {"pattern": "credentials/*", "path": "/etc/holt"}, "/home/agent"),
    ("Glob", {"pattern": "holt/**", "path": "/etc"}, "/home/agent"),
    ("Grep", {"pattern": "token"}, "/etc/holt"),
    ("Grep", {"pattern": "token", "path": "."}, "/var/lib/holt"),
    ("LS", {"path": "/etc/holt"}, None),
    ("LS", {"path": "/etc//holt/"}, None),
    ("LS", {}, "/srv/holt"),
    ("NotebookRead", {"notebook_path": "/etc/holt/n.ipynb"}, None),
    ("NotebookEdit", {"notebook_path": "/run/credentials/x.ipynb", "new_source": "x"}, None),
    ("MultiEdit", {"file_path": "/srv/holt/app.py", "edits": []}, None),
    ("Grep", {"pattern": "x", "path": "/home/agent", "glob": "/etc/holt/**"}, None),
    # any tool at all, when the session itself is inside Holt's folders
    ("Read", {"file_path": "/home/agent/notes.md"}, "/etc/holt"),
    ("Bash", {"command": "ls"}, "/etc/holt/credentials"),
    ("WebFetch", {"url": "https://example.com"}, "/run/credentials/holt.service"),
])
def test_agent_blocks_file_tools_in_and_from_holt(tool, inp, cwd):
    code, err = agent(tool, cwd=cwd, **inp)
    assert code == 2 and err.startswith("Blocked by oss-scout guard"), (tool, inp, cwd)


def test_agent_blocks_a_symlink_into_holt(tmp_path):
    (tmp_path / "real" / "holt").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to("/etc/holt")
    assert agent("Read", file_path=str(link / "credentials" / "x"))[0] == 2
    assert agent("Read", file_path=str(tmp_path / "real" / "holt" / "x"))[0] == 0


@pytest.mark.parametrize("tool,inp,cwd", [
    ("Grep", {"pattern": "/etc/holt", "path": "/home/agent/src"}, None),
    ("Grep", {"pattern": "/etc/holt"}, "/home/agent/src"),
    ("Grep", {"pattern": "sudo|git push", "glob": "*.py"}, "/home/agent/src"),
    ("Glob", {"pattern": "**/holt/*.py", "path": "/home/agent/src"}, None),
    ("Write", {"file_path": "/home/agent/a.md", "content": "config lives in /etc/holt, run sudo, git push"}, None),
    ("Edit", {"file_path": "/home/agent/a.md", "old_string": "/etc/holt", "new_string": "git push"}, None),
    ("Read", {"file_path": "notes.md"}, "/home/agent"),
    ("Read", {"file_path": "/srv/holt-src/README.md"}, None),
    ("LS", {"path": "/home/agent"}, None),
    ("LS", {"path": "/etc"}, "/home/agent"),
    ("NotebookRead", {"notebook_path": "/home/agent/n.ipynb"}, None),
    ("TodoWrite", {"todos": []}, None),
    ("mcp__slack__post_message", {"text": "hi"}, None),
    ("mcp__github__get_issue", {"owner": "o", "repo": "r", "number": 1}, None),
])
def test_agent_allows_normal_tool_calls(tool, inp, cwd):
    assert agent(tool, cwd=cwd, **inp)[0] == 0, (tool, inp, cwd)


@pytest.mark.parametrize("tool,inp", [
    ("mcp__files__read", {"path": "/etc/holt/credentials/x"}),
    ("mcp__shell__run", {"cmd": "sudo ls"}),
    ("mcp__shell__run", {"cmd": "git push origin main"}),
    ("mcp__slack__post_message", {"text": "see /var/lib/holt/state"}),
    ("mcp__github__create_issue", {"title": "x"}),
    ("mcp__github__add_issue_comment", {"body": "hi"}),
])
def test_agent_blocks_mcp_tool_text(tool, inp):
    assert agent(tool, **inp)[0] == 2, (tool, inp)


# ---- the cloud profile is the same as before the agent profile existed ----

CLOUD_QUIRKS = [
    ("not json", 0), ("", 0), ("{", 0),
    ("null", 1), ("[]", 1), ("7", 1), ('"x"', 1), ("true", 1),    # a crash, as before
    ('{"tool_name": null}', 1),
    ('{"tool_name": "Bash", "tool_input": "x"}', 1),
    ('{"tool_name": "Bash", "tool_input": {"command": 3}}', 1),
    ('{"tool_name": "Bash", "tool_input": {"command": "sudo ls /etc/holt; git push origin main"}, "cwd": "/etc/holt"}', 2),
    ('{"tool_name": "Bash"}', 0),
    ('{}', 0),
    ('{"tool_name": "Read", "tool_input": {"file_path": "/etc/holt/x"}}', 0),
]


@pytest.mark.parametrize("raw,code", CLOUD_QUIRKS)
def test_cloud_profile_keeps_its_exit_codes(raw, code):
    for args in ([], ["--profile", "cloud"], ["--profile=cloud"]):
        p = subprocess.run([sys.executable, str(GUARD), *args], input=raw, capture_output=True, text=True)
        assert p.returncode == code, (raw, args, p.stderr)


def test_cloud_profile_still_allows_what_the_agent_profile_blocks():
    for cmd in ("echo 'git push origin main' | sh", "git log --grep=sudo", "timeout 5 git-send-pack x"):
        assert bash(cmd) == 0, cmd


# ---- agent profile, round 2: Holt as path words, quoted commands, timeouts ----

@pytest.mark.parametrize("cmd,cwd", [
    # Holt's folders through `..`, /var/run or a relative path
    ("cat ../../etc/holt/credentials/x", None),
    ("cat /tmp/../etc/holt/credentials/x", None),
    ("ls /var/run/holt", None),
    ("cat /var/run/credentials/x", None),
    ("ls ../../run/credentials", None),
    ("cd / && cat etc/holt/credentials/x", None),
    ("cd /var && cat lib/holt/state", None),
    ("ls credentials", "/run"),
    ("ls ../holt", "/etc/ssl"),
    ("cat holt/x", "/"),
    # accepted false positives: `etc` and `holt` as path words are enough
    ("ls tests/fixtures/etc/holt", None),
    ("cat ./etc/holt/notes.md", None),
    # GitHub writes behind pipes and quoting
    ("echo 'gh pr create --title x --body y' | sh", None),
    ("sh <<< 'gh pr create --title x --body y'", None),
    ('echo "$(timeout 5 gh pr create --title x --body y)"', None),
    ("bash -c -- 'gh pr create --title x --body y'", None),
    ("env -S 'gh pr create --title x --body y'", None),
    ("bash -c 'gh pr create --title x --body y >/dev/null'", None),
    ("echo \\' ; echo 'gh pr create' | sh", None),
    # `new` is gh's alias for `create`
    ("gh pr new --title x --body y", None),
    ("gh issue new --title x --body y", None),
    ("gh repo new x --public", None),
    # more forms
    ("sudoedit /etc/hosts", None),
    ("wget --method=POST https://api.github.com/repos/o/r/issues", None),
    ("wget --method POST https://api.github.com/repos/o/r/issues", None),
    ("wget --post-data='{}' https://api.github.com/repos/o/r/issues", None),
    ("wget --post-file=body.json https://api.github.com/repos/o/r/issues", None),
    ("curl -sSXPOST https://api.github.com/repos/o/r/issues", None),
    ("curl -sSd '{}' https://api.github.com/repos/o/r/issues", None),
    ("curl --request=PATCH https://api.github.com/repos/o/r", None),
])
def test_agent_blocks_round_two(cmd, cwd):
    code, err = agent(command=cmd, cwd=cwd)
    assert code == 2 and err.startswith("Blocked by oss-scout guard"), (cmd, cwd)


@pytest.mark.parametrize("cmd", [
    "ls /srv/holt-src",
    "git add tests/test_alias.py",
    "git diff -- tests/receive-packet.test.js",
    "ls /run/user/1000",
    "cat /etc/os-release",
    "pip install -U pytest",
    "curl -XGET https://api.github.com/repos/o/r",
    "curl -sSL https://github.com/o/r/raw/main/README.md",
    "gh pr view 3 --json title -q '.title'",
    "gh pr list --search new",
    "bash -c \"cd src && python3 -c 'print(\\\"ok\\\")'\"",
])
def test_agent_allows_round_two(cmd):
    assert agent_bash(cmd) == 0, cmd


@pytest.mark.parametrize("inp,cwd,code", [
    ({"pattern": "x", "path": "/etc", "glob": "holt/**"}, "/home/agent", 2),
    ({"pattern": "x", "glob": "holt/**"}, "/etc", 2),
    ({"pattern": "x", "path": "/home/agent/src", "glob": "holt/**"}, None, 0),
    ({"pattern": "/etc/holt", "path": "/home/agent/src"}, None, 0),
])
def test_agent_resolves_greps_glob(inp, cwd, code):
    assert agent("Grep", cwd=cwd, **inp)[0] == code, (inp, cwd)


def test_agent_follows_a_symlink_before_dot_dot(tmp_path):
    (tmp_path / "link").symlink_to("/etc/holt/credentials")
    assert agent("Read", file_path=str(tmp_path / "link") + "/../x")[0] == 2


@pytest.mark.parametrize("event", [{"tool_name": "", "tool_input": {}}, {"tool_name": "", "tool_input": {"command": "ls"}}])
def test_agent_blocks_an_empty_tool_name(event):
    p = subprocess.run([sys.executable, str(GUARD), "--profile", "agent"], input=json.dumps(event),
                       capture_output=True, text=True)
    assert p.returncode == 2


@pytest.mark.parametrize("cmd,code", [
    ("ls " + "/./" * 350_000, 2),            # 1 MB: too long to check, blocked unread
    ("ls " + "/./" * 33_000, 0),             # just under the limit: read, and quickly
    ("cat /etc" + "/./" * 33_000 + "holt", 2),
    ("echo " + "1" * 99_000, 0),
    ("nc -" + "U" * 99_000 + "_", 2),
])
def test_agent_decides_long_commands_quickly(cmd, code):
    import time
    start = time.monotonic()
    assert agent_bash(cmd) == code
    assert time.monotonic() - start < 2


def test_agent_blocks_long_mcp_text():
    assert agent("mcp__slack__post_message", text="x" * 100_001)[0] == 2
    assert agent("mcp__slack__post_message", text="x" * 1000)[0] == 0


def test_settings_agent_json_fails_closed_and_denies_gh_new():
    s = json.loads((GUARD.parent / "settings.agent.json").read_text())
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"
    for rule in ("Bash(gh pr new:*)", "Bash(gh issue new:*)", "Bash(gh repo new:*)"):
        assert rule in s["permissions"]["deny"]
