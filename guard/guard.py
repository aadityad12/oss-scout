#!/usr/bin/env python3
"""PreToolUse hook: the scout may read GitHub, never write to it.

Installed into the private data repo's .claude/ (see `python -m scout init-data`)
because that is the only repo the cloud routine opens, and a cloud session only
honors hooks from a single-repo checkout.

Allowed write: `git push origin claude/scout-data` from a checkout whose origin
is the oss-scout-data repo. Everything else that could post, comment, open,
fork, star, label, merge or push is blocked. Exit code 2 blocks the tool call
and shows the reason to Claude.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

DATA_REPO = "oss-scout-data"
DATA_BRANCH = "claude/scout-data"

GH_WRITE_SUBCOMMANDS = {
    "pr": {"create", "merge", "close", "reopen", "comment", "review", "edit", "ready", "lock", "unlock"},
    "issue": {"create", "comment", "edit", "close", "reopen", "transfer", "delete", "lock", "unlock",
              "pin", "unpin", "develop"},
    "repo": {"create", "fork", "delete", "edit", "rename", "archive", "unarchive", "sync", "deploy-key"},
    "release": None, "gist": None, "label": None, "workflow": None, "run": None, "secret": None,
    "variable": None, "ssh-key": None, "gpg-key": None, "auth": None, "codespace": None,
    "project": None, "ruleset": None, "cache": None, "api": "check", "extension": None, "alias": None,
}
WRITE_METHODS = re.compile(r"^(POST|PATCH|PUT|DELETE)$", re.I)
GH_API_BODY_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
READ_VERBS = {"get", "list", "search", "read", "view", "fetch", "download", "show", "check"}


def block(reason: str) -> None:
    print(f"Blocked by oss-scout guard: {reason}. The scout never writes to GitHub; "
          f"leave this for the human.", file=sys.stderr)
    sys.exit(2)


def split_commands(cmd: str) -> list[list[str]]:
    """Split a shell line into simple commands (on ; && || | and newlines)."""
    parts = re.split(r"(?:&&|\|\||;|\||\n|\$\(|`)", cmd)
    out = []
    for p in parts:
        try:
            toks = shlex.split(p, comments=True)
        except ValueError:
            toks = p.split()
        # drop leading env assignments, sudo, time, etc.
        while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in {"sudo", "time", "env", "command", "exec", "nohup"}):
            toks = toks[1:]
        if toks:
            out.append(toks)
    return out


def origin_url(cwd: str | None) -> str:
    try:
        return subprocess.run(["git", "-C", cwd or ".", "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def check_gh(toks: list[str]) -> None:
    args = [t for t in toks[1:]]
    # skip global flags like -R owner/repo
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in {"-R", "--repo"} else 1
    if i >= len(args):
        return
    group = args[i]
    sub = args[i + 1] if i + 1 < len(args) else ""
    rule = GH_WRITE_SUBCOMMANDS.get(group, "unknown")
    if rule is None:
        block(f"`gh {group}` can change things on GitHub")
    if rule == "check":
        rest = args[i + 1:]
        method = None
        for j, t in enumerate(rest):
            if t in {"-X", "--method"} and j + 1 < len(rest):
                method = rest[j + 1]
            elif t.startswith("--method="):
                method = t.split("=", 1)[1]
            elif re.match(r"^-X\w+$", t):
                method = t[2:]
        has_body = any(t in GH_API_BODY_FLAGS or t.split("=", 1)[0] in GH_API_BODY_FLAGS for t in rest)
        if method and WRITE_METHODS.match(method):
            block(f"`gh api` with method {method}")
        if has_body and (method or "").upper() != "GET":
            block("`gh api` with body fields (defaults to POST)")
        if any("mutation" in t.lower() for t in rest):
            block("GraphQL mutation")
        return
    if isinstance(rule, set) and sub in rule:
        block(f"`gh {group} {sub}` writes to GitHub")


def check_git(toks: list[str], cwd: str | None) -> None:
    args = toks[1:]
    workdir = cwd
    while args and args[0] in {"-C", "-c"}:
        if args[0] == "-C" and len(args) > 1:
            workdir = args[1]
        args = args[2:]
    if not args:
        return
    sub = args[0]
    if sub == "remote" and len(args) > 1 and args[1] in {"add", "set-url", "rename"}:
        block("changing git remotes")
    if sub == "config" and any("url." in a or "remote." in a or "pushurl" in a for a in args):
        block("rewriting remote URLs via git config")
    if sub == "push":
        rest = [a for a in args[1:] if a not in {"-u", "--set-upstream", "--quiet", "-q"}]
        if any(a.startswith("-") for a in rest):
            block(f"`git push` with flags {rest}")
        if rest[:1] != ["origin"] or len(rest) != 2 or rest[1] not in {DATA_BRANCH, f"HEAD:{DATA_BRANCH}"}:
            block(f"only `git push origin {DATA_BRANCH}` is allowed")
        if DATA_REPO not in origin_url(workdir):
            block(f"pushes are only allowed from the {DATA_REPO} checkout")


def check_http(toks: list[str]) -> None:
    line = " ".join(toks)
    if "github.com" not in line and "githubusercontent" not in line:
        return
    if re.search(r"(?:^|\s)(?:-X|--request)\s*(?:POST|PATCH|PUT|DELETE)\b", line, re.I):
        block("HTTP write to GitHub")
    if re.search(r"(?:^|\s)(?:-d|--data\S*|-F|--form|--json|--upload-file|-T)\b", line):
        block("HTTP request with a body to GitHub")
    if toks[0] in {"http", "https", "xh"} and re.search(r"\s(POST|PATCH|PUT|DELETE)\s", f" {line} ", re.I):
        block("HTTP write to GitHub")


def check_bash(cmd: str, cwd: str | None) -> None:
    if re.search(r"api\.github\.com", cmd) and re.search(r"requests\.(post|patch|put|delete)|urlopen\(.*data=|method=['\"](POST|PATCH|PUT|DELETE)", cmd, re.I):
        block("scripted HTTP write to GitHub")
    here = cwd
    for toks in split_commands(cmd):
        prog = toks[0].rsplit("/", 1)[-1]
        if prog in {"cd", "pushd"} and len(toks) > 1:
            here = os.path.normpath(os.path.join(here or os.getcwd(), os.path.expanduser(toks[1])))
        elif prog == "gh":
            check_gh(toks)
        elif prog == "git":
            check_git(toks, here)
        elif prog in {"curl", "wget", "http", "https", "xh"}:
            check_http(toks)


def check_mcp(name: str) -> None:
    """GitHub tools: allow read verbs, block everything else (fail closed)."""
    if "github" not in name.lower():
        return
    verb = re.split(r"[_\-]", name.split("__")[-1].lower())[0]
    if verb not in READ_VERBS:
        block(f"GitHub tool `{name}` is not read-only")


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    tool = event.get("tool_name", "")
    inp = event.get("tool_input") or {}
    if tool == "Bash":
        check_bash(inp.get("command", ""), event.get("cwd"))
    elif tool.startswith("mcp__"):
        check_mcp(tool)


if __name__ == "__main__":
    main()
