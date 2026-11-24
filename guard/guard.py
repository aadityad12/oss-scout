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
                    It errs on blocking, and a tool call it cannot read or check
                    is blocked (exit 2), never let through.

`--version` prints the guard version, so an installer can tell which copy it is.
Any other argument, or a bad profile name, blocks everything (fail closed).
"""

from __future__ import annotations

import functools
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


def check_bash(cmd: str, cwd: str | None, scripted: bool = True) -> None:
    # `scripted=False`: the agent profile runs its own linear-time copy of this first rule
    if scripted and re.search(r"api\.github\.com", cmd) and re.search(r"requests\.(post|patch|put|delete)|urlopen\(.*data=|method=['\"](POST|PATCH|PUT|DELETE)", cmd, re.I):
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
#
# It errs on blocking. The first layer reads the whole command as plain text and
# does not try to understand the shell, so pipes, heredocs, `$( )` and `{ }` all
# look the same to it. A harmless command that merely mentions `git` and `push`,
# or `sudo`, is blocked too (put such text in a file and use `git commit -F`).
#
# Every regex here takes linear time, and longer text is blocked unread, so a
# crafted command cannot make the hook time out.

AGENT_ADVICE = "Ask the owner to do this; the agent may not."
PUSH_MESSAGE = "pushes are done by the owner, not the agent"
HOLT_MESSAGE = "Holt's folders and credentials are off limits"
MAX_DEPTH = 4
MAX_TEXT = 100_000


def word(*names: str) -> str:
    """A regex for `names` as whole words: not part of a longer word, path name or flag."""
    return r"(?<![\w.-])(?:" + "|".join(names) + r")(?![\w.-])"


GIT = re.compile(word("git"))
PUSH = re.compile(word("push"))
GIT_ALIAS = re.compile(r"(?<![\w./-])alias\.", re.I)     # `alias.p=push`, not `src/alias.py`
HOLT = re.compile(word("holt"))
CREDENTIALS = re.compile(word("credentials"))
HOLT_PARENT = re.compile(word("etc", "run", "lib", "srv"))
RUN = re.compile(word("run"))
TRIPWIRES = [
    (re.compile(word("git-push", "(?:git-)?(?:send-pack|http-push|receive-pack)")), PUSH_MESSAGE),
    (re.compile("GIT_CONFIG_PARAMETERS|GIT_CONFIG_KEY_"), PUSH_MESSAGE),
    (re.compile(word("sudo", "sudoedit", "doas", "pkexec", "run0")), "`sudo` and its relatives are not allowed"),
    (re.compile(word("socat") + r"|--(?:abstract-)?unix-socket|UNIX-(?:CONNECT|CLIENT|SENDTO)", re.I),
     "`socat` and unix sockets can reach Holt's sockets"),
    (re.compile("CREDENTIALS_DIRECTORY"), HOLT_MESSAGE),
]
NC = re.compile(word(r"nc(?:\.openbsd|\.traditional)?", "ncat", "netcat"))
UNIX_FLAG = re.compile(r"(?<![\w-])-[A-Za-z]*U|--unixsock")
HOLT_FOLDERS = ["/run/holt", "/var/lib/holt", "/etc/holt", "/srv/holt", "/run/credentials",
                "/var/run/holt", "/var/run/credentials"]
HOLT_DIRS = ["/var", "/run", "/etc", "/srv"]      # a session in (or below) these, or in `/`, may not name Holt
FILE_TOOLS = {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "NotebookRead", "LS", "Grep", "Glob"}
# a quoted string (`'...'`, `"..."`, `$'...'`); a backslash outside quotes escapes the next character
QUOTED = re.compile(r"""\\[\s\S]|\$'((?:[^'\\]|\\[\s\S])*)'|'([^']*)'|"((?:[^"\\]|\\[\s\S])*)\"""")

# programs that run another command given in their arguments, and the options
# of theirs that take a value (so the value is not mistaken for the command)
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir"}, "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"}, "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"},
    "stdbuf": {"-i", "-o", "-e"}, "time": {"-f", "-o"}, "exec": {"-a"}, "nohup": set(), "command": set(),
    # shell keywords that can come before a command
    "{": set(), "!": set(), "if": set(), "then": set(), "else": set(), "elif": set(), "do": set(),
    "while": set(), "until": set(),
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}


def agent_block(reason: str) -> None:
    block(reason, AGENT_ADVICE)


def names_holt(text: str, in_holt_dir: bool = False) -> bool:
    """Does `text` name Holt's folders or credentials? `holt` with `etc`, `run`, `lib` or `srv`, or
    `credentials` with `run`, each as a whole word, so `../../etc/holt` and `/var/run/holt` count.
    In a session folder below /etc, /run, /var or /srv (or in /), the name alone counts."""
    text = re.sub(r"/(?:\.?/)+", "/", text)       # `//` and `/./` are one separator
    holt, creds = HOLT.search(text), CREDENTIALS.search(text)
    return bool((holt or creds) and (in_holt_dir or holt and HOLT_PARENT.search(text) or creds and RUN.search(text)))


def tripwires(text: str, in_holt_dir: bool = False) -> None:
    """Block text that mentions a forbidden thing anywhere, however the shell would read it."""
    if len(text) > MAX_TEXT:
        agent_block("text too long to check")
    git_alias = GIT_ALIAS.search(text) and (GIT.search(text) or "GIT_CONFIG" in text)
    if (GIT.search(text) and PUSH.search(text)) or git_alias:
        agent_block(PUSH_MESSAGE)
    for pattern, reason in TRIPWIRES:
        if pattern.search(text):
            agent_block(reason)
    if NC.search(text) and UNIX_FLAG.search(text):
        agent_block("`nc -U` can reach Holt's sockets")
    if names_holt(text, in_holt_dir):
        agent_block(HOLT_MESSAGE)


def in_holt_dir(path: str) -> bool:
    return path == "/" or any(path == d or path.startswith(d + "/") for d in HOLT_DIRS)


def inside_holt(path: str, base: str) -> bool:
    """Is `path` (relative to `base`) inside a Holt folder, directly or through a symlink?"""
    joined = os.path.join(base, os.path.expanduser(path))
    # symlinks are followed before `..` is removed, as the system does
    resolved = [re.sub(r"^/+", "/", p) for p in (os.path.normpath(joined), os.path.realpath(joined))]
    return any(p == root or p.startswith(root + "/") for p in resolved for root in HOLT_FOLDERS)


def check_agent_paths(tool: str, inp: dict, cwd: str) -> None:
    """File tools: no path may lead into Holt's folders. Relative paths start from the event's cwd,
    Glob's `pattern` and Grep's `glob` from the folder they search."""
    path = inp.get("path")
    searched = os.path.join(cwd, os.path.expanduser(path)) if isinstance(path, str) else cwd
    key = {"Glob": "pattern", "Grep": "glob"}.get(tool)     # a path pattern (Grep's own `pattern` is a regex)
    pattern = inp.get(key) if key else None
    checks = [(inp.get(k), cwd) for k in ("file_path", "notebook_path", "path")] + [(pattern, searched)]
    if tool in {"Grep", "Glob", "LS"} and not path:
        checks.append((".", cwd))           # no path given: it looks in the current folder
    for value, base in checks:
        if value is None:
            continue
        if not isinstance(value, str):
            agent_block("a file path that is not text")
        if inside_holt(value, base):
            agent_block(HOLT_MESSAGE)
    if pattern is not None:                 # a pattern such as `*/holt/*` in /etc cannot be resolved: read it as text
        tripwires(os.path.join(searched, pattern))


def tokenize(cmd: str) -> list[list[str]]:
    """Split a shell line into simple commands, keeping quoted text as one word.

    `sh -c "a; b"` stays one word. `;`, `&`, `|`, `( )`, newlines and backticks
    separate commands. Redirects (`2>&1`, `> file`) are dropped, never with a quote mark.
    """
    cmd = re.sub(r"(?<!\d)\d*[<>]+&?\s*(?:\d+-?|[^\s;&|()<>'\"]+)?", " ", cmd).replace("`", ";").replace("\n", ";")
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.commenters = ""
    try:
        words = list(lex)
    except ValueError:      # an unbalanced quote: fall back to cruder splitting
        words = [w.strip("'\"") for w in re.findall(r"[;&|()]|[^\s;&|()]+", cmd)]
    out: list[list[str]] = [[]]
    for w in words:
        if re.fullmatch(r"[;&|()]+", w):
            out.append([])
        else:
            out[-1].append(w)
    return [seg for seg in out if seg]


def quoted_parts(cmd: str) -> list[str]:
    """Every quoted string in `cmd`, and, in case the quoting is misread, the text between any two quote marks."""
    if "'" not in cmd and '"' not in cmd:
        return []
    return [g for m in QUOTED.finditer(cmd) for g in m.groups() if g] + re.split("['\"]", cmd)


@functools.cache
def simple_commands(cmd: str, depth: int = 0) -> list[list[str]]:
    """Every simple command in `cmd`, also those behind `timeout 5`, `env`, `xargs` and so on,
    inside `sh -c '...'`, `$( )`, backticks and `find -exec`, and inside every quoted string
    (`echo 'gh pr create' | sh`)."""
    if depth > MAX_DEPTH:
        agent_block("commands nested too deeply to check")
    found = []
    for part in quoted_parts(cmd):
        found += simple_commands(part, depth + 1)
    for toks in tokenize(cmd):
        while toks:
            prog = toks[0].rsplit("/", 1)[-1]
            if re.match(r"^\w+=", toks[0]):
                toks = toks[1:]
            elif prog in WRAPPERS:
                i, valued = 1, WRAPPERS[prog]
                while i < len(toks) and (toks[i] in valued or toks[i].startswith("-") or re.match(r"^\w+=", toks[i])):
                    i += 2 if toks[i] in valued else 1
                toks = toks[i + (prog == "timeout"):]     # `timeout` is followed by a duration
            else:
                break
        if not toks:
            continue
        found.append(toks)
        prog = toks[0].rsplit("/", 1)[-1]
        if prog in SHELLS:
            for i, t in enumerate(toks[1:], 1):
                if re.fullmatch(r"-[A-Za-z]+", t) and "c" in t:       # -c, -lc, then maybe `--`
                    found += simple_commands(next((s for s in toks[i + 1:] if s != "--"), ""), depth + 1)
                    break
        elif prog == "eval":
            found += simple_commands(" ".join(toks[1:]), depth + 1)
        elif prog == "find":
            for i, t in enumerate(toks):
                if t in {"-exec", "-execdir", "-ok", "-okdir"}:
                    found += simple_commands(shlex.join(toks[i + 1:]), depth + 1)
    return found


def check_agent_scripted(cmd: str) -> None:
    """The cloud rule for a scripted HTTP write, in linear time (`urlopen(` and `data=` may be on different lines)."""
    if re.search(r"api\.github\.com", cmd) and (
            re.search(r"requests\.(post|patch|put|delete)|method=['\"](POST|PATCH|PUT|DELETE)", cmd, re.I)
            or re.search(r"urlopen\(", cmd, re.I) and re.search("data=", cmd, re.I)):
        block("scripted HTTP write to GitHub")


def check_agent_command(toks: list[str]) -> None:
    """Agent-only GitHub rules for one simple command: gh's `new`, and more HTTP write flags."""
    prog = toks[0].rsplit("/", 1)[-1]
    if prog == "gh":
        check_gh(["create" if t == "new" else t for t in toks])      # `gh pr new` is `gh pr create`
    if prog not in {"curl", "wget"} or not re.search(r"github\.com|githubusercontent", " ".join(toks)):
        return
    for t, after in zip(toks, toks[1:] + [""]):
        flag = re.match(r"-[A-Za-z]*?([XdFT])", t) if prog == "curl" else None    # also in a cluster: -sSXPOST, -sd
        if t.startswith(("--post-", "--body-")) or flag and flag[1] != "X":      # wget --post-data, --body-file
            block("HTTP request with a body to GitHub")
        method = re.match(r"--(?:method|request)(?:=|$)", t) or flag
        if method and (t[method.end():] or after).upper() not in {"GET", "HEAD"}:
            block("HTTP write to GitHub")


def walk_strings(value):
    """Every string in a parsed JSON value, dictionary keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk_strings(k)
            yield from walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_strings(v)


def check_agent_event(event) -> None:
    """The agent profile's rules, in order: a malformed call, Holt's folders, forbidden text and
    paths, then every cloud rule, also on the commands hidden inside the line. (The text rules
    come before the cloud rules so that a push is always refused with "pushes are done by the
    owner".)"""
    if not isinstance(event, dict):
        agent_block("the tool call is not a JSON object")
    tool, inp, cwd = event.get("tool_name"), event.get("tool_input"), event.get("cwd")
    if not tool or not isinstance(tool, str) or not isinstance(inp, dict) or not (cwd is None or isinstance(cwd, str)):
        agent_block("the tool call is malformed")
    if any("\0" in s for s in walk_strings(event)):
        agent_block("the tool call contains a NUL byte")
    cmd = inp.get("command")
    if tool == "Bash" and not isinstance(cmd, str):
        agent_block("the Bash tool call has no command text")
    here = re.sub(r"^/+", "/", os.path.abspath(cwd or "."))
    if inside_holt(".", here):
        agent_block(HOLT_MESSAGE)
    if tool == "Bash":
        tripwires(cmd, in_holt_dir(here) or in_holt_dir(os.path.realpath(here)))
    elif tool.startswith("mcp__"):
        for s in walk_strings(inp):
            tripwires(s)
    elif tool in FILE_TOOLS:
        check_agent_paths(tool, inp, here)
    # everything the cloud profile blocks
    if tool == "Bash":
        commands = simple_commands(cmd)
        for text in [cmd, *map(shlex.join, commands)]:
            check_agent_scripted(text)
            check_bash(text, cwd, scripted=False)
        for toks in commands:
            check_agent_command(toks)
    elif tool.startswith("mcp__"):
        check_mcp(tool)


def agent_main() -> None:
    """Any problem at all, even a crash in here, blocks the tool call (exit 2, not the exit 1 that lets it run)."""
    try:
        check_agent_event(json.load(sys.stdin))
    except Exception:
        block("could not check this tool call", "The agent profile blocks what it cannot check.")


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
    if parse_args(sys.argv[1:]) == "agent":
        agent_main()
        return
    # the cloud profile, exactly as it was before the agent profile existed
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
