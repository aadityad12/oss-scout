"""The only code that writes to GitHub, and only for `scout act`.

It runs in the data repo's act workflow with the owner's token (GH_TOKEN), after
the owner tapped a button. It POSTs to exactly three endpoints: fork a repo, open
a pull request, and comment on an issue or pull request. Nothing else passes.
The read-only client in github.py is unchanged and is not used for writes.

It also holds one read, `token_scopes`: a GET of `user` with the same token and
headers, to see which permissions the key has before any clone starts.
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


def _request(path: str, token: str, body: dict | None = None) -> urllib.request.Request:
    req = urllib.request.Request(f"{API}/{path}", data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    req.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "oss-scout")
    req.add_header("Authorization", f"Bearer {token}")
    return req


def _send(path: str, body: dict, token: str) -> dict:
    req = _request(path, token, body)
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


def token_scopes(token: str | None = None) -> tuple[int, set[str] | None]:
    """(HTTP status, scopes) for the submit token: one GET of `user`, never printing the token.

    Scopes come from the X-OAuth-Scopes header. Fine-grained tokens send none, so the
    set is None and the caller can't tell; a 401 means the key is expired or revoked.
    A network problem returns status 0, which callers treat as "couldn't check".
    """
    token = token or os.environ.get("GH_TOKEN", "")
    if not token:
        return 401, None
    try:
        with urllib.request.urlopen(_request("user", token), timeout=30) as resp:
            header, status = resp.headers.get("X-OAuth-Scopes"), resp.status
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return 0, None
    return status, None if header is None else {s.strip() for s in header.split(",") if s.strip()}
