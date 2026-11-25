"""The /contribute skill's Holt mode, checked against the guard it runs behind.

A guard change that blocks what Holt mode runs, or lets through what it only prints,
fails here.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "guard" / "guard.py"
SKILL = ROOT / ".claude" / "skills" / "contribute" / "SKILL.md"


def agent_bash(cmd):
    event = {"tool_name": "Bash", "cwd": "/tmp/x", "tool_input": {"command": cmd}}
    p = subprocess.run([sys.executable, str(GUARD), "--profile", "agent"], input=json.dumps(event),
                       capture_output=True, text=True)
    return p.returncode, p.stderr


@pytest.mark.parametrize("text", [
    "OSS_BRIEFING_DIR",
    "OSS_HOLT_MODE",
    "## Holt mode",
    "Publish this from Holt's Publish button or the owner shell.",
])
def test_skill_describes_holt_mode(text):
    assert text in SKILL.read_text()


@pytest.mark.parametrize("cmd", [
    # local git
    "git clone --filter=blob:none https://github.com/o/r /tmp/x",
    "git -C /tmp/x fetch origin",
    "git -C /tmp/x switch -c fix/12-slug",
    "git -C /tmp/x apply --3way draft.patch",
    'git -C /tmp/x apply --3way "$OSS_BRIEFING_DIR/draft.patch"',
    "git -C /tmp/x commit -m msg",
    "git -C /tmp/x commit -F /tmp/x/.git/message",
    "git -C /tmp/x config --local --get user.name",
    "git -C /tmp/x config --local --get user.email",
    "git -C /tmp/x config --local user.name 'Owner Name'",
    "git -C /tmp/x config --local user.email '12345+me@users.noreply.github.com'",
    "git -C /tmp/x fetch origin pull/12/head",
    "git -C /tmp/x switch -c fix/12-slug FETCH_HEAD",
    # the mode and the briefing folder
    "printenv OSS_HOLT_MODE",
    "printenv OSS_BRIEFING_DIR",
    # reads with gh
    "gh issue view 12 --repo o/r --comments",
    "gh api repos/o/r/issues/12/timeline --paginate --jq '.[] | select(.event==\"cross-referenced\") | .source.issue | select(.pull_request) | {number, state, user: .user.login, url: .html_url}'",
    "gh pr view https://github.com/o/r/pull/12 --comments",
    "gh api repos/o/r/pulls/12/comments",
    # reads without a GitHub login: the "still free" checks (including repo names with gh write words)
    "curl -s https://api.github.com/repos/o/r/issues/12",
    "curl -s https://api.github.com/repos/o/review/issues/12",
    "curl -s https://api.github.com/repos/o/r/issues/12/timeline",
    'curl -s "https://api.github.com/repos/o/r/issues/12/timeline?per_page=100"',
    'curl -s "https://api.github.com/repos/o/review/issues/12/timeline?per_page=100"',
    'curl -s "https://api.github.com/repos/o/r/issues/12/timeline?per_page=100&page=2"',
    "curl -s https://api.github.com/users/me",
    # reads without a GitHub login: review feedback
    "curl -s https://api.github.com/repos/o/r/pulls/12",
    'curl -s "https://api.github.com/repos/o/r/issues/12/comments?per_page=100"',
    'curl -s "https://api.github.com/repos/o/r/pulls/12/comments?per_page=100"',
    'curl -s "https://api.github.com/repos/o/r/pulls/12/reviews?per_page=100"',
])
def test_guard_allows_what_holt_mode_runs(cmd):
    code, err = agent_bash(cmd)
    assert code == 0, (cmd, err)


@pytest.mark.parametrize("cmd", [
    "git -C /tmp/x push -u fork fix/12-slug",
    "git -C /tmp/x push fork fix/12-slug",
    "gh pr create --repo o/r --title t --body b",
    "gh issue comment 12 --repo o/r --body hi",
    "gh pr comment https://github.com/o/r/pull/12 --body-file reply.md",
    "gh api repos/o/r/pulls/12/comments/34/replies -F body=@reply.md",
    "gh repo fork o/r --clone=false",
    "git -C /tmp/x remote add fork https://github.com/me/r",
    # Holt mode says not to run these either
    "gh auth status",
    "echo 'git -C /tmp/x push -u fork fix/12-slug'",
])
def test_guard_blocks_what_holt_mode_only_prints(cmd):
    code, err = agent_bash(cmd)
    assert code == 2 and err.startswith("Blocked by oss-scout guard"), (cmd, err)
