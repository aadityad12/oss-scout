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
