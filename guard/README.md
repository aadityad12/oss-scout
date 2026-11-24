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

**`agent` adds:**

- every `git push`, in every form: `git -C x push`, `git -c k=v push`, with env variables in front, under `env`/`timeout`, inside `sh -c`/`bash -c`, after `&&`, `;`, `|` or `&`, inside `$( )`, and a script that runs it. `git send-pack` and `git http-push` count too, and so does a git alias defined on the command line. The message is "pushes are done by the owner, not the agent"
- `sudo`, wherever it appears as a command
- reaching Holt: `--unix-socket`, `nc -U`, `socat`, and any command text that mentions `/run/holt`, `/var/lib/holt`, `/etc/holt` or `/srv/holt` (also when written as `/etc/./holt` or reached after a `cd`)
- reading credential folders by path: `/etc/holt/credentials`, `/run/credentials`, `$CREDENTIALS_DIRECTORY` and `${CREDENTIALS_DIRECTORY}`
- the same Holt folders through Claude's own tools: `Read`, `Edit`, `Write`, `Grep` and `Glob` (their `file_path`, `path`, `pattern` and `glob`), and any MCP tool argument that names one
- an unreadable tool call (the cloud profile lets it through, as before)

A bad `--profile` value, `--profile` with no value, or any other argument blocks every tool call and exits 2 with a "Blocked by oss-scout guard" message, so a typo in the settings file stops the agent instead of loosening the guard.

## Limits

This is a speed bump for a well-behaved agent, not a sandbox. A determined script can still try other ways: globbed paths (`/etc/ho*`), a pre-existing git alias for `push`, code that builds the command at run time. It is a hard stop only together with a separate user account that has no tokens, no GitHub login and no access to Holt's files. Blocking network access in general is a container concern, not the guard's.

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
