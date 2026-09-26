"""Set up the private data repo that the cloud routine opens.

The routine opens only this repo, because a cloud session honors a repo's hooks
and permission rules only when it has a single repository. So the guard lives
here, and the public tool is cloned at run time.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import ROOT

SETTINGS = {
    "attribution": {"commit": "", "pr": "", "sessionUrl": False},
    "hooks": {
        "PreToolUse": [{
            "matcher": "*",
            "hooks": [{"type": "command",
                       "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/guard.py"'}],
        }],
    },
    "permissions": {
        "deny": [
            "Bash(gh pr create:*)", "Bash(gh pr comment:*)", "Bash(gh pr review:*)",
            "Bash(gh pr merge:*)", "Bash(gh issue create:*)", "Bash(gh issue comment:*)",
            "Bash(gh repo fork:*)", "Bash(gh repo create:*)", "Bash(git push --force:*)",
            "Bash(git push -f:*)",
        ],
    },
}

CLAUDE_MD = """# oss-scout-data

Private state for OSS Scout: scan results, briefings, draft patches, and the
dashboard. The tool itself is public at https://github.com/{login}/oss-scout.

## If you are the nightly routine

1. `git clone --depth 1 https://github.com/{login}/oss-scout /tmp/oss-scout`
2. Read `/tmp/oss-scout/routine/PROMPT.md` and follow it exactly.

This repository's `.claude/hooks/guard.py` blocks every write to GitHub except
`git push origin claude/scout-data` from this checkout. If it blocks something,
don't look for a way around it: note it in your summary and move on.
"""

GITIGNORE = ".cache/\n*.tmp\n__pycache__/\n"


def init(data: Path, login: str) -> list[str]:
    written = []
    hooks = data / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "guard" / "guard.py", hooks / "guard.py")
    written.append(".claude/hooks/guard.py")
    (data / ".claude" / "settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")
    written.append(".claude/settings.json")
    (data / "CLAUDE.md").write_text(CLAUDE_MD.format(login=login))
    written.append("CLAUDE.md")
    gi = data / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
        written.append(".gitignore")
    for d in ("briefings", "picks"):
        (data / d).mkdir(exist_ok=True)
        keep = data / d / ".gitkeep"
        if not keep.exists():
            keep.touch()
    return written
