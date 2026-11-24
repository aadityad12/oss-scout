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
    "git commit -m 'fix the git push bug'",
    "git fetch origin",
    "git log --grep=sudo",
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
