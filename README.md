# oss-scout

A nightly scout for open source contributions that keeps a human in charge.

It finds issues worth working on, measures how each project treats outside
contributors, prepares a draft fix with a thorough explanation, and puts it all
on a private dashboard. It never comments, opens a pull request, or posts
anything. The person reads the briefing, understands the change, and does the
contributing themselves.

## Why

Maintainers are drowning in low-effort, machine-generated pull requests. The
answer isn't to generate more of them faster. It's to do the tedious parts
(searching, triage, reading unfamiliar code) automatically and leave the parts
that build trust with a real person: claiming an issue, defending a change in
review, and following through.

## How it works

```mermaid
flowchart LR
  A[targets.toml<br/>tiered repos] --> S
  B[GitHub search<br/>open discovery] --> S
  S[scanner<br/>plain Python] -->|candidates.json| C[Claude step<br/>picks 0-3, drafts, explains]
  C -->|briefings + patches| D[(private data repo)]
  T[tracker<br/>your PRs, reviews, issues] --> D
  D --> R[dashboard]
  R --> H((you))
  H -->|claim, review, submit| GH[GitHub]
```

1. **Scan** (`python -m scout run`, no LLM involved). Collects open, unassigned
   issues labelled as beginner-friendly, help-wanted, bounty or confirmed-bug from a
   tiered list of projects, plus open search across GitHub at a lower weight.
2. **Measure each project** from its own recent history. From the last 300 closed
   PRs by occasional outside contributors (PRs from forks, by people with fewer
   than three recent PRs; staff push branches to the repo itself): merge rate, median days to merge, and median time until a
   maintainer first replies. Also reads CONTRIBUTING / AI-policy files for AI rules
   and CLA requirements. This becomes a 0–100 *friendliness* score.
3. **Filter** anything already claimed: linked open PRs, recent "I'll take this"
   comments, assignees, blocking labels.
4. **Rank** by friendliness, freshness, label fit, language fit, project tier and
   your own history with the project.
5. **Brief.** A scheduled Claude Code routine reads the top candidates, picks zero
   to three, reproduces and drafts a fix where it can, and writes a briefing: the
   issue in plain English, how that part of the codebase works, the change explained
   piece by piece, rejected alternatives, likely reviewer questions with answers,
   what was and wasn't tested, and the project's AI-disclosure expectations.
6. **Track** what you actually did, straight from GitHub. Suggestions move through
   `suggested → claimed → pr_open → waiting_on_you → merged`. Nothing is
   self-reported.

## Guardrails

The routine runs unattended with the owner's GitHub identity and reads text written
by strangers, so it is boxed in:

- `guard/guard.py` is a Claude Code `PreToolUse` hook that blocks every GitHub
  write: `gh pr/issue create|comment|review|merge…`, `gh api` with a write method or
  a body, `curl` writes to GitHub, remote rewrites, and every `git push` except one
  branch of the private data repo. GitHub tools are allowed only when they start with
  a read verb. 37 tests in `tests/test_guard.py`.
- Cloud sessions only honor a repo's hooks when the session has a single
  repository, so the routine opens only the private data repo (where
  `scout init-data` installs the hook) and clones this public repo read-only.
- The prompt treats issue text as data, never as instructions.

## Usage

```bash
python3 -m scout doctor        # check GitHub access
python3 -m scout run           # scan -> data/candidates.json
python3 -m scout track         # refresh contribution history only
python3 -m scout ingest        # record new briefings as suggestions
python3 -m scout render        # build data/dashboard.html
python3 -m scout init-data     # install guard + settings into the data dir
```

`SCOUT_DATA_DIR` points at the data directory (defaults to `./data`). Standard
library only, Python 3.11+. It uses `gh api` when the GitHub CLI is available and
falls back to `GH_TOKEN`. REST only, because the cloud GitHub proxy blocks most
GraphQL.

Tests: `python3 -m pytest`.

## Configuration

Everything lives in [`targets.toml`](targets.toml): your login and languages,
project tiers and weights, label patterns, discovery settings, and the date until
which the scout explores widely before recommending two or three home projects
based on what actually got merged.

## License

MIT
