"""The only code that writes to GitHub, and only for `scout act`.

It runs in the data repo's act workflow with the owner's token (GH_TOKEN), after
the owner tapped a button. It POSTs to exactly three endpoints: fork a repo, open
a pull request, and comment on an issue or pull request. Nothing else passes.
The read-only client in github.py is unchanged and is not used for writes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.github.com"
NAME = r"[A-Za-z0-9_.-]+"
ALLOWED = [re.compile(p) for p in (
    rf"^repos/{NAME}/{NAME}/forks$",
    rf"^repos/{NAME}/{NAME}/pulls$",
    rf"^repos/{NAME}/{NAME}/issues/[0-9]+/comments$",
)]


class WriteError(Exception):
    pass


def _send(path: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode(), method="POST")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "oss-scout")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise WriteError(f"GitHub said {e.code} for POST {path}: {detail}") from e


def post(path: str, body: dict) -> dict:
    if not any(p.match(path) for p in ALLOWED):
        raise WriteError(f"refusing to POST to {path}")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise WriteError("GH_TOKEN is not set")
    return _send(path, body, token)


def fork(repo: str) -> dict:
    return post(f"repos/{repo}/forks", {})


def create_pr(repo: str, title: str, body: str, head: str, base: str) -> dict:
    return post(f"repos/{repo}/pulls", {"title": title, "body": body, "head": head, "base": base,
                                        "maintainer_can_modify": True})


def comment(repo: str, number: int, body: str) -> dict:
    return post(f"repos/{repo}/issues/{number}/comments", {"body": body})
