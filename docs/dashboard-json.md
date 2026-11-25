# dashboard.json

Every time the scout renders the dashboard (`python -m scout render`) it writes two files next
to each other in the data directory: `dashboard.html` (the page) and `dashboard.json` (the same
data as a plain file, plus a version number). The file is written to a temporary file and then
renamed, so a reader never sees half of it. It is not escaped for embedding in a page.

It is built by `build_payload()` in `scout/render.py` from saved scout data and dashboard
settings. Nothing secret goes in.

## Rules

- Adding a field keeps `schema_version`.
- Removing a field, renaming it, or changing its type bumps `SCHEMA_VERSION` in `scout/render.py`.
- Readers must ignore fields they don't know and refuse a `schema_version` they don't know.

## Top-level keys

| Key | Type | What it is |
| --- | --- | --- |
| `schema_version` | int | Format version, currently `1`. |
| `generated_at` | string | When the file was built (ISO time, UTC). |
| `login` | string | The GitHub login the scout works for. |
| `digest` | object | The contents of `digest.json`, or `{}` when there is none. |
| `inbox` | list | The five groups below, in this order: `waiting`, `ready`, `pairing`, `briefings`, `snoozed`. |
| `to_do` | int | How many items need you today: everything in the inbox except `snoozed`. |
| `note` | string | Last night's note about why there is (or isn't) a ready item. |
| `in_flight` | list | Open PRs, plus claimed, submitting and approved items (and ones stuck after a failed send). |
| `portfolio` | object | Exactly what the public page shows (`public.build_payload`). |
| `limits` | object | `{featured, summary}`: the most featured PRs and the longest summary the portfolio allows. |
| `run_again` | object | `{daily_cap, cooldown_minutes}`: the extra manual nightly runs allowed per Pacific day and the minutes to wait after the nightly routine starts. Both are integers from `[settings]` in `targets.toml`, with defaults of `1` and `60`. |
| `projects` | object | `{home, repos}`: the home-project recommendation and the repo research. |
| `log` | object | `{actions, runs}`: your taps and the recent nightly scans, newest first. |

## Inbox group

| Key | Type | What it is |
| --- | --- | --- |
| `id` | string | `waiting`, `ready`, `pairing`, `briefings` or `snoozed`. |
| `label` | string | The heading shown on the page, e.g. "Waiting on you". |
| `items` | list | The group's items, in the order the page shows them. |

## Inbox item

Items in `ready`, `pairing`, `briefings` and `snoozed` are the suggestion's own fields from
`state.json` (status, repo, number, title, URLs, timestamps and so on; they vary with the
suggestion's state) plus:

| Key | Type | What it is |
| --- | --- | --- |
| `key` | string | `owner/repo#number`. |
| `slug` | string | `owner__repo__number`, the same as the briefing folder name. |
| `group` | string | The id of the group it is in. |
| `headline` | string | One line naming the work. |
| `field` | object or null | `{id, label, also, also_label, why}`: the kind of work. |
| `project_intro` | string | A short introduction to the project. |
| `plan_steps` | list of strings | The plan, at most five steps (may be empty). |
| `briefing` | object | The briefing's contents (empty if there is none). |
| `refresh_pending` | bool | A rebuild was asked for and has no answer yet. |
| `lines` | object | `{problem, sending, your_part}`: the three plain lines. |

Items whose status is `ready`, `approved` or `submitting` (what the one-tap flow can edit and
send) also carry `patch` (string), `pr` (object, the `pr.json`), `post` (string, the comment
text) and `problems` (list of strings; why it can't be sent yet).

Items in the `waiting` group have a different shape, because they come from your open PRs:
`key`, `slug`, `pr_url`, `since`, `overdue`, `group`, `title`, `repo`, `lines`, `failure`,
`pr_number`, `comments` (the review comments) and `followup` (the drafted reply, or null). They
have no `headline`, `briefing`, `field` or `refresh_pending`.
