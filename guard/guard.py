#!/usr/bin/env python3
"""PreToolUse hook: the scout may read GitHub, never write to it.

Installed into the private data repo's .claude/ (see `python -m scout init-data`)
because that is the only repo the cloud routine opens, and a cloud session only
honors hooks from a single-repo checkout.

Allowed write: `git push origin claude/scout-data` from a checkout whose origin
is the oss-scout-data repo. Everything else that could post, comment, open,
fork, star, label, merge or push is blocked. Exit code 2 blocks the tool call
and shows the reason to Claude.

Two profiles, chosen only by the command line the hook is installed with
(never by an environment variable, so a tool call cannot switch it):

  --profile cloud   the default, the behaviour above.
  --profile agent   for a local agent on the owner's own machine: no `git push`
                    at all, no `sudo`, nothing that talks to Holt's sockets or
                    reads Holt's folders and credentials, plus every cloud rule.

`--version` prints the guard version, so an installer can tell which copy it is.
Any other argument, or a bad profile name, blocks everything (fail closed).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

GUARD_VERSION = 1
PROFILES = {"cloud", "agent"}

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


DEFAULT_ADVICE = "The scout never writes to GitHub; leave this for the human."


def block(reason: str, advice: str = DEFAULT_ADVICE) -> None:
    print(f"Blocked by oss-scout guard: {reason}. {advice}", file=sys.stderr)
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


# ---- agent profile -------------------------------------------------------
# Extra rules for a local agent. Same idea as above: a speed bump for a
# well-behaved agent, a hard stop only together with a separate user account.

AGENT_ADVICE = "Ask the owner to do this; the agent may not."
HOLT_PATH = re.compile(r"/(?:run|srv|etc|var/lib)/holt\b|/run/credentials\b")
HOLT_TEXT = re.compile(HOLT_PATH.pattern + r"|CREDENTIALS_DIRECTORY")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "eval", "xargs", "watch", "su", "script"}
WRAPPERS = {"env", "command", "exec", "nohup", "time", "nice", "timeout", "stdbuf", "setsid",
            "ionice", "builtin", "chroot", "doas", "unbuffer"}
INTERPRETERS = re.compile(r"^(?:python[\d.]*|perl|ruby|node|php|lua)$")
GIT_PUSHES = {"push", "send-pack", "http-push"}
# global git options that take their value as the next word
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
PATH_KEYS = ("file_path", "notebook_path", "path", "pattern", "glob")
FILE_TOOLS = {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "Grep", "Glob"}


def agent_block(reason: str) -> None:
    block(reason, AGENT_ADVICE)


def tokenize(cmd: str) -> list[list[str]]:
    """Split a shell line into simple commands, respecting quotes.

    Unlike split_commands, `sh -c "a; b"` stays one word, and a lone `&` splits too.
    """
    cmd = cmd.replace("`", ";").replace("\n", ";")
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.commenters = ""
    try:
        words = list(lex)
    except ValueError:
        words = [w.strip("'\"") for w in cmd.split()]
    out: list[list[str]] = [[]]
    for w in words:
        if re.fullmatch(r"[;&|()<>]+", w):
            if w.strip("<>") == "" and w:
                continue          # a redirect, not a separator
            out.append([])
        else:
            out[-1].append(w.lstrip("{"))
    return [seg for seg in out if seg]


def check_agent_git(args: list[str]) -> None:
    """`args` are the words after `git`. Block every way to push."""
    while args and args[0].startswith("-"):
        opt = args[0]
        value = args[1] if opt in GIT_VALUE_OPTS and len(args) > 1 else ""
        if opt == "-c" and value.lower().startswith("alias."):
            agent_block("defining a git alias on the command line")
        if opt.lower().startswith("--config-env=alias.") or opt.lower().startswith("-calias."):
            agent_block("defining a git alias on the command line")
        args = args[2:] if opt in GIT_VALUE_OPTS else args[1:]
    if args and args[0] in GIT_PUSHES:
        agent_block("pushes are done by the owner, not the agent")


def check_agent_segment(toks: list[str], here: str, depth: int) -> str:
    """One simple command. Returns the folder after it (follows `cd`)."""
    toks = list(toks)
    while toks and re.match(r"^\w+=", toks[0]):
        toks = toks[1:]
    if not toks:
        return here
    prog = toks[0].rsplit("/", 1)[-1]
    if prog in {"cd", "pushd"} and len(toks) > 1:
        here = os.path.normpath(os.path.join(here, os.path.expanduser(toks[1])))
    # any word that normalises to a Holt path, even through `..` or a `cd`
    for t in toks[1:]:
        if t.startswith("/") or not t.startswith("-"):
            full = os.path.normpath(os.path.join(here, os.path.expanduser(t)))
            if HOLT_PATH.search(full):
                agent_block("Holt's folders and credentials are off limits")
    if prog == "sudo" or (prog in WRAPPERS | SHELLS
                          and any(t.rsplit("/", 1)[-1] == "sudo" for t in toks[1:])):
        agent_block("`sudo` is not allowed")
    if prog == "socat" or (prog in WRAPPERS and any(t.rsplit("/", 1)[-1] == "socat" for t in toks[1:])):
        agent_block("`socat` can reach Holt's sockets")
    if prog in {"nc", "ncat", "netcat"} and any(
            re.fullmatch(r"-[A-Za-z]*U[A-Za-z]*", t) or t.startswith("--unixsock") for t in toks[1:]):
        agent_block("`nc -U` can reach Holt's sockets")
    if prog in {"git-push", "git-send-pack", "git-http-push"}:
        agent_block("pushes are done by the owner, not the agent")
    # wrappers (env, timeout 5, ...) put the real program later in the line
    start = 0 if prog == "git" else None
    if start is None and prog in WRAPPERS | SHELLS:
        for i, t in enumerate(toks[1:], 1):
            if t.rsplit("/", 1)[-1] == "git":
                start = i
                break
    if start is not None:
        check_agent_git(toks[start + 1:])
    if INTERPRETERS.match(prog) and any(re.search(r"git['\"]?[\s,\[\]'\"]+(?:-\S+[\s,'\"]+)*push\b", t)
                                       for t in toks[1:]):
        agent_block("a script that runs `git push`")
    if depth < 5 and (prog in SHELLS or prog in WRAPPERS):
        for t in toks[1:]:
            if re.search(r"\s", t):
                agent_text(t, here, depth + 1)
    return here


def agent_text(cmd: str, here: str, depth: int = 0) -> None:
    if re.search(r"--(?:abstract-)?unix-socket", cmd):
        agent_block("`--unix-socket` can reach Holt's sockets")
    if HOLT_TEXT.search(cmd):
        agent_block("Holt's folders, sockets and credentials are off limits")
    for toks in tokenize(cmd):
        here = check_agent_segment(toks, here, depth)


def check_agent_files(tool: str, inp: dict) -> None:
    """Read/Edit/Write/Grep/Glob and MCP tools: no Holt paths, so no bypass around Bash."""
    if tool.startswith("mcp__"):
        values = list(walk_strings(inp))
    elif tool in FILE_TOOLS:
        values = [inp[k] for k in PATH_KEYS if isinstance(inp.get(k), str)]
    else:
        return
    for v in values:
        if HOLT_TEXT.search(v) or HOLT_PATH.search(os.path.normpath(v)) or HOLT_PATH.search(os.path.realpath(v)):
            agent_block("Holt's folders and credentials are off limits")


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_strings(v)


def parse_args(argv: list[str]) -> str:
    """Return the profile, or exit. Any unexpected argument blocks everything."""
    if argv == ["--version"]:
        print(f"guard {GUARD_VERSION}")
        sys.exit(0)
    if not argv:
        return "cloud"
    if len(argv) == 2 and argv[0] == "--profile":
        profile = argv[1]
    elif len(argv) == 1 and argv[0].startswith("--profile="):
        profile = argv[0].split("=", 1)[1]
    else:
        block(f"unexpected arguments {argv}", "Fix the guard command in the settings file.")
    if profile not in PROFILES:
        block(f"unknown profile {profile!r}", "Fix the guard command in the settings file.")
    return profile


def main() -> None:
    profile = parse_args(sys.argv[1:])
    agent = profile == "agent"
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("event is not an object")
    except Exception:
        if agent:
            block("could not read the tool call", "Fail closed in the agent profile.")
        return
    tool = event.get("tool_name", "")
    inp = event.get("tool_input") or {}
    if agent:
        if tool == "Bash":
            agent_text(inp.get("command", ""), event.get("cwd") or os.getcwd())
        else:
            check_agent_files(tool, inp)
    if tool == "Bash":
        check_bash(inp.get("command", ""), event.get("cwd"))
    elif tool.startswith("mcp__"):
        check_mcp(tool)


if __name__ == "__main__":
    main()
