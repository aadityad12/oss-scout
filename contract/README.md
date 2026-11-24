# The act dispatch contract

`act-dispatch.json` is the one written-down description of how a tap on the
dashboard becomes a run of the data repo's `act.yml` workflow: the workflow
file and its inputs, every allowed button action, the issue-key format, the text
length limits, how many days "Later" snoozes for, and the project modes. It holds
no secrets, no token names and no repository secret names, so it is public on
purpose.

## Who uses it

- The scout (`scout/act.py`, `scout/briefings.py`, `scout/datarepo.py`), the
  Worker (`worker/src/index.js`) and the workflow template all keep their own
  copy of these values. `tests/test_contract.py` and `worker/test/contract.test.js`
  compare each of them with this file, so a change made in one place and
  forgotten in another fails in CI (`.github/workflows/tests.yml` runs both test
  suites on every pull request and every push to `main`).
- Holt (aadityad12/Holt#9) vendors a copy of this file. Its CI compares that copy
  with the file on `main` here, and reads it for which actions are enabled and the
  snooze label ("Later (3 days)").

The raw file is served at
`https://raw.githubusercontent.com/aadityad12/oss-scout/main/contract/act-dispatch.json`.

## What the keys mean

| Key | Meaning |
| --- | --- |
| `workflow` | The workflow file and branch the Worker dispatches, and its `run-name` with `{action}` and `{key}` standing for the two inputs. |
| `inputs` | The workflow's `workflow_dispatch` inputs. |
| `actions` | Every allowed action, in the order of the workflow's `options:` list. |
| `no_text_actions` | Actions that ignore the title and body; the Worker sends them empty. |
| `summary_action` | The action whose text is the one-line summary of a PR (`limits.max_summary`). |
| `key_pattern` | What an issue key (`owner/repo#123`) must look like. |
| `limits` | `max_title`, `max_body`, `max_summary` and `max_request_bytes` are measured differently by the two sides: the Worker uses JavaScript string length (UTF-16 code units), while `scout/act.py` uses Python string length (Unicode code points), so a character outside the Basic Multilingual Plane (an emoji, say) counts as 2 in the Worker and 1 in the scout. `max_dispatch_body_b64` is the longest base64 body the Worker will send (GitHub caps all workflow inputs at 65,535 characters). `max_request_bytes` is the longest request body the Worker reads, compared with the body's string length (`raw.length`), not its bytes; the name is kept for compatibility. |
| `snooze_days` | How long "Later" hides an item. |
| `modes`, `legacy_modes` | The project modes a briefing can have, and old names read as a current mode. |

## Changing it

Edit the file, `scout/act.py`, `scout/briefings.py`, `worker/src/index.js` and
`scout/datarepo.py` together, in one change, and run both test suites. Bump `contract_version` for a
breaking change, meaning removing or renaming an action or a limit; adding an action
or changing a number keeps the version.

Nothing reads the file when the scout or the Worker runs. It is checked by tests only.
