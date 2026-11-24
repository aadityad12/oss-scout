"""The shared act dispatch contract (contract/act-dispatch.json) matches the scout and the workflow.

The Worker's half is worker/test/contract.test.js. Holt keeps a copy of the file and
checks it against the one on main, so a change here has to show up in all of them.
"""

import json
import re
from pathlib import Path

from scout import act, briefings, datarepo

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "contract" / "act-dispatch.json").read_text())
KEY_SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "contract_key_samples.json").read_text())


def workflow_dispatch_block():
    """The workflow_dispatch part of act.yml's `on:` section."""
    m = re.search(r"^  workflow_dispatch:\n(.*?)^\S", datarepo.ACT_YML, re.S | re.M)
    assert m, "no workflow_dispatch section in ACT_YML"
    return m.group(1)


def test_the_file_has_a_version():
    assert CONTRACT["contract_version"] == 1


def test_actions_match_the_scout_in_order():
    assert list(act.ACTIONS) == CONTRACT["actions"]
    assert sorted(act.HANDLERS) == sorted(CONTRACT["actions"])  # every action has code behind it


def test_no_text_actions_and_summary_action_are_real_actions():
    # the scout has no list of its own for the actions that ignore their text
    assert set(CONTRACT["no_text_actions"]) <= set(CONTRACT["actions"])
    assert CONTRACT["summary_action"] in CONTRACT["actions"]
    assert CONTRACT["summary_action"] not in CONTRACT["no_text_actions"]
    assert CONTRACT["summary_action"] == act.summary.__name__


def test_limits_match_the_scout():
    limits = CONTRACT["limits"]
    assert act.MAX_TITLE == limits["max_title"]
    assert act.MAX_BODY == limits["max_body"]
    assert act.MAX_SUMMARY == limits["max_summary"]
    assert act.SNOOZE_DAYS == CONTRACT["snooze_days"]


def test_key_pattern_accepts_and_rejects_what_the_scout_does():
    pattern = re.compile(CONTRACT["key_pattern"])
    for sample in KEY_SAMPLES:
        key, valid = sample["key"], sample["valid"]
        # fullmatch, because Python's `$` also matches before a trailing newline
        assert bool(pattern.fullmatch(key)) == valid, key
        # the scout itself calls KEY.match, which also accepts a trailing newline. The Worker
        # rejects such keys, but a direct CLI call or a manual workflow dispatch skips the Worker,
        # and the workflow's grep check and KEY.match would accept a single trailing newline
        assert bool(act.KEY.fullmatch(key)) == valid, key
        assert bool(pattern.match(key)) == bool(act.KEY.match(key)), key


def test_the_workflow_checks_keys_with_the_contract_pattern():
    # only active lines (not comments) with the key check itself: echo "$KEY" | grep -Eq '...'
    for name, text in (("ACT_YML", datarepo.ACT_YML), ("TRACK_YML", datarepo.TRACK_YML)):
        patterns = re.findall(
            r"""^[ \t]*[^#\n]*echo "\$(?:KEY|key)" \| grep -Eq '([^']*)'""", text, re.M)
        assert patterns, f"no key check in {name}"
        assert set(patterns) == {CONTRACT["key_pattern"]}, name


def test_modes_match_the_briefings():
    assert len(CONTRACT["modes"]) == len(set(CONTRACT["modes"]))
    assert set(CONTRACT["modes"]) == briefings.MODES
    assert CONTRACT["legacy_modes"] == briefings.LEGACY_MODES


def test_the_workflow_options_are_the_actions():
    m = re.search(r"^        options: \[(.*?)\]$", workflow_dispatch_block(), re.M)
    assert m, "no options list in ACT_YML"
    assert [a.strip() for a in m.group(1).split(",")] == CONTRACT["actions"]


def test_the_workflow_inputs_are_the_contract_inputs():
    names = re.findall(r"^      (\w+):$", workflow_dispatch_block().split("    inputs:\n", 1)[1], re.M)
    assert names == CONTRACT["inputs"]


def test_the_run_name_is_the_contract_run_name():
    m = re.search(r'^run-name: "(.*)"$', datarepo.ACT_YML, re.M)
    assert m, "no run-name in ACT_YML"
    expected = CONTRACT["workflow"]["run_name"].replace("{action}", "${{ inputs.action }}").replace("{key}", "${{ inputs.key }}")
    assert m.group(1) == expected


def test_the_workflow_is_written_as_the_file_the_contract_names():
    # datarepo writes act.yml under .github/workflows; the Worker dispatches it by that name
    name = CONTRACT["workflow"]["file"]
    assert f'(wf / "{name}").write_text(ACT_YML)' in (ROOT / "scout" / "datarepo.py").read_text()
