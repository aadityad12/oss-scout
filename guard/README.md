# The guard

`guard.py` is a Claude Code `PreToolUse` hook: a program Claude Code runs before every tool call. It can block the call by exiting with code 2, and the reason it prints is shown to Claude. It uses only the Python standard library.

It has two profiles. The profile is chosen only by the command line the hook is installed with, never by an environment variable, so the agent cannot switch it from a tool call.

| Profile | For | Install |
|---|---|---|
| `cloud` (the default) | the nightly cloud routine | `python -m scout init-data` copies it into the private data repo |
| `agent` | a local agent on your own machine | `settings.agent.json`, below |

`python3 guard.py --version` prints `guard 1`, so an installer can check which copy is in place.

## What it blocks

**Both profiles** (the scout reads GitHub and never writes to it):

- `gh` writes (`gh pr/issue create|comment|review|merge|...`, `gh repo fork`, `gh release`, `gh label`, `gh auth`, ...), and `gh api` with a write method, body fields or a GraphQL mutation
- `curl`, `wget` and scripted HTTP writes to GitHub
- changing git remotes or rewriting remote URLs through `git config`
- GitHub MCP tools that are not read verbs (`get`, `list`, `search`, `read`, `view`, `fetch`, `download`, `show`, `check`)

**`cloud` only:** one write is allowed, `git push origin claude/scout-data` from the checkout of the `oss-scout-data` repo.

**`agent` adds** (all of the above, plus the rules below). It errs on blocking:

- **Text rules.** The agent profile reads the whole command as plain text, without trying to understand the shell, so pipes, heredocs, `$( )`, backticks, `{ }`, `if` and `sh -c` all look the same to it. A command is blocked if its text contains any of these:
  - `git` and `push` as separate words anywhere (`git push`, `git -C x push`, `git 2>&1 push`, `echo 'git push' | sh`, `git submodule foreach git push`, `git subtree push`, `git rebase -x 'git push'`, a Python script that runs `["git", "push"]`), or `git-push`, `send-pack`, `http-push`, `receive-pack`, or `alias.` together with the word `git` (a git alias on the command line or in `git config`), or `GIT_CONFIG_PARAMETERS` or `GIT_CONFIG_KEY_`. The message is "pushes are done by the owner, not the agent".
  - `sudo`, `doas`, `pkexec` or `run0` as words.
  - `socat`, `--unix-socket`, `UNIX-CONNECT`, `UNIX-CLIENT` or `UNIX-SENDTO`, or `nc`, `ncat` or `netcat` together with a `-U` flag (also inside a combined flag such as `-Uv`).
  - a Holt folder as whole path parts: `/run/holt`, `/var/lib/holt`, `/etc/holt` or `/srv/holt` (also written `/etc//holt` or `/etc/./holt`), and also one of `/run`, `/var/lib`, `/etc` or `/srv` together with the word `holt` (`cd /etc && cat holt/credentials/x`). Longer paths and names are fine: `tests/fixtures/etc/holt` and `/srv/holt-src` pass.
  - `CREDENTIALS_DIRECTORY` or `/run/credentials`.
- **The words are enough, even in harmless text.** A command that only mentions `git` and `push`, or `sudo`, is blocked: `git commit -m 'fix the git push bug'`, `git log --grep=sudo`. Write such text into a file and use `git commit -F file`.
- **GitHub writes behind a wrapper.** The cloud rules above are also run on the commands inside `sh -c`/`bash -c`/`zsh -c`/`dash -c`, `eval`, `$( )`, backticks, `find -exec`, and after `env`, `timeout`, `nice`, `nohup`, `time`, `exec`, `command`, `xargs`, `stdbuf`, `if`/`then`/`do`, `{` and `!`. So `bash -c 'gh pr create ...'` and `timeout 5 gh pr create ...` are blocked. (Only the agent profile does this; the cloud profile is the same as it always was.)
- **Claude's own file tools.** `Read`, `Edit`, `MultiEdit`, `Write`, `NotebookEdit`, `NotebookRead`, `LS`, `Grep` and `Glob` are blocked when their path (`file_path`, `notebook_path`, `path`, and for `Glob` the `pattern`) is inside a Holt folder or `/run/credentials`, after relative paths are resolved against the session's folder, `..` is removed and symlinks are followed. `Grep`, `Glob` and `LS` without a `path` look in the session's folder, so that is checked too. `Grep`'s `pattern` is a regular expression, not a path, so it is not checked; its `glob` filter gets the text rules. The text of `Edit` and `Write` is not scanned. Every string given to an MCP tool gets the text rules.
- **Any tool call at all** is blocked if the session's own folder is inside a Holt folder or `/run/credentials`.
- **A call it cannot check is blocked.** Unreadable or non-object JSON, a tool name or input of the wrong type, a `Bash` call without command text, a NUL byte in any string, or any error while checking exits 2. (An error that exited 1 would let Claude Code run the tool.)

A bad `--profile` value, `--profile` with no value, or any other argument blocks every tool call and exits 2 with a "Blocked by oss-scout guard" message, so a typo in the settings file stops the agent instead of loosening the guard.

## Limits

This is a speed bump for a well-behaved agent, not a sandbox. It is a hard stop only together with a separate user account that has no tokens, no GitHub login and no access to Holt's files. Blocking network access in general is a container concern, not the guard's.

What it does not catch, because it never runs anything:

- strings built at run time (`"gi"+"t pu"+"sh"`, a variable that holds the command, a script written by one command and run by the next)
- globs and patterns for a path (`/etc/ho*`, `/etc/h?lt`), and a search of a parent folder (`grep -r token /etc`)
- obfuscation such as base64 or hex that is decoded and run
- a git alias for `push` that is already in a git config file, or a config file chosen with `GIT_CONFIG_GLOBAL`
- a GitHub write sent by a program the guard does not know, or by a script (it only reads `gh`, `curl`, `wget`, `http` and a few Python patterns)

Because it reads text and not meaning, it also blocks some harmless commands (see above). That is on purpose.

## Install (agent profile)

Copy the `guard/` folder somewhere only root can write, for example `/usr/local/lib/holt/guard/`, then merge `settings.agent.json` into Claude Code's managed settings (on Linux, `/etc/claude-code/managed-settings.json`). The agent user must not be able to edit either one. The fragment sets the hook command to `python3 /usr/local/lib/holt/guard/guard.py --profile agent`, turns off the commit, pull request and session-link attribution, and also denies the same GitHub writes and `git push` in Claude's own permission list. Change the path in `command` if you install elsewhere.

## Test

```
/home/you/.cache/oss-scout-venv/bin/python -m pytest tests/test_guard.py      # or python3 -m pytest
python3 guard/guard.py --version                                               # guard 1
echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' \
  | python3 guard/guard.py --profile agent; echo $?                            # block message, then 2
```

To try it in Claude Code: copy `guard/` to a temporary folder, make a throwaway project whose `.claude/settings.json` is `settings.agent.json` with the path in `command` pointed at that copy, and ask the agent to run `git push`. The call is blocked and the message is shown.
