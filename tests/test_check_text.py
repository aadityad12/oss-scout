import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scout import __main__ as cli, act, briefings, config

DISCLOSURE = "I used an AI assistant while investigating this; I reviewed and tested every change myself."


def run_cli(argv, stdin_text=None, isatty=False, monkeypatch=None):
    if stdin_text is not None:
        buf = io.StringIO(stdin_text)
        buf.isatty = lambda: isatty
        monkeypatch.setattr(sys, "stdin", buf)
    with pytest.raises(SystemExit) as e:
        cli.main(["check-text", *argv])
    return e.value.code


# -- pure text_problems() -----------------------------------------------------

def test_text_problems_clean():
    assert act.text_problems("Fix crash", "Handles empty input.") == []
    assert act.text_problems(None, "Handles empty input.") == []
    assert act.text_problems("Fix crash", None) == []
    assert act.text_problems(None, None) == []


def test_text_problems_ai_markers_case_insensitive():
    assert act.text_problems("Fix crash", "Co-Authored-By: x") == ["the text contains an AI marker"]
    assert act.text_problems("Fix crash", "GENERATED WITH a helper") == ["the text contains an AI marker"]
    assert act.text_problems("Fix crash", "gEnErAtEd WiTh a tool") == ["the text contains an AI marker"]
    assert act.text_problems("Fix crash", "Hello \U0001F916") == ["the text contains an AI marker"]
    assert act.text_problems("Title \U0001F916", "Clean body.") == ["the text contains an AI marker"]


def test_text_problems_title_constraints():
    expected = f"the title must be one line, 1-{act.MAX_TITLE} characters"
    assert act.text_problems("line1\nline2", "body") == [expected]
    assert act.text_problems("   ", "body") == [expected]
    assert act.text_problems("", "body") == [expected]
    assert act.text_problems("x" * (act.MAX_TITLE + 1), "body") == [expected]
    assert act.text_problems("x" * act.MAX_TITLE, "body") == []


def test_text_problems_body_constraints():
    expected = f"the text must be 1-{act.MAX_BODY} characters"
    assert act.text_problems("Title", "y" * 61000) == [expected]
    assert act.text_problems("Title", "y" * (act.MAX_BODY + 1)) == [expected]
    assert act.text_problems("Title", "   ") == [expected]
    assert act.text_problems("Title", "") == [expected]
    assert act.text_problems("Title", "y" * act.MAX_BODY) == []


def test_text_problems_disclosure():
    msg = "the text must keep the AI-disclosure sentence this project requires"
    assert act.text_problems("Title", "Fixes #7.") == []
    assert act.text_problems("Title", "Fixes #7.", DISCLOSURE) == [msg]
    assert act.text_problems("Title", f"Fixes #7.\n\n{DISCLOSURE}", DISCLOSURE) == []
    assert act.text_problems("Title", None, DISCLOSURE) == []


def test_text_problems_reports_all_simultaneous_problems_in_order():
    problems = act.text_problems("bad\ntitle", "   ", DISCLOSURE)
    assert problems == [
        f"the title must be one line, 1-{act.MAX_TITLE} characters",
        f"the text must be 1-{act.MAX_BODY} characters",
        "the text must keep the AI-disclosure sentence this project requires",
    ]
    all_four = act.text_problems("bad\ntitle", "Co-Authored-By: x" + " " * 61000, DISCLOSURE)
    assert all_four == [
        f"the title must be one line, 1-{act.MAX_TITLE} characters",
        f"the text must be 1-{act.MAX_BODY} characters",
        "the text must keep the AI-disclosure sentence this project requires",
        "the text contains an AI marker",
    ]


# -- CLI behaviour ------------------------------------------------------------

def test_cli_clean_plain_and_json(monkeypatch, capsys):
    assert run_cli(["--title", "Fix crash"], stdin_text="Handles empty input.", monkeypatch=monkeypatch) == 0
    assert capsys.readouterr().out.strip() == "ok"

    assert run_cli(["--title", "Fix crash", "--json"], stdin_text="Handles empty input.", monkeypatch=monkeypatch) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "problems": []}


def test_cli_problems_plain_and_json(monkeypatch, capsys):
    assert run_cli(["--title", "a\nb", "--disclosure", DISCLOSURE], stdin_text="Co-Authored-By: x", monkeypatch=monkeypatch) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        f"the title must be one line, 1-{act.MAX_TITLE} characters",
        "the text must keep the AI-disclosure sentence this project requires",
        "the text contains an AI marker",
    ]

    assert run_cli(["--title", "a\nb", "--disclosure", DISCLOSURE, "--json"], stdin_text="Co-Authored-By: x", monkeypatch=monkeypatch) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "problems": lines}


def test_cli_disclosure_only_enforced_when_passed(monkeypatch, capsys):
    assert run_cli(["--title", "Fix crash", "--json"], stdin_text="Fixes #7.", monkeypatch=monkeypatch) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "problems": []}

    assert run_cli(["--title", "Fix crash", "--disclosure", DISCLOSURE, "--json"], stdin_text="Fixes #7.", monkeypatch=monkeypatch) == 1
    assert json.loads(capsys.readouterr().out)["problems"] == [
        "the text must keep the AI-disclosure sentence this project requires"
    ]

    assert run_cli(["--title", "Fix crash", "--disclosure", DISCLOSURE, "--json"],
                   stdin_text=f"Fixes #7.\n\n{DISCLOSURE}", monkeypatch=monkeypatch) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "problems": []}


def test_cli_list_markers_does_not_read_stdin(monkeypatch, capsys):
    class UnreadableStdin:
        def isatty(self):
            return False

        def read(self):
            raise AssertionError("--list-markers must not read stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    assert run_cli(["--list-markers"]) == 0
    assert json.loads(capsys.readouterr().out) == list(briefings.AI_MARKERS)


def test_cli_body_file_and_missing_file(tmp_path, capsys):
    body_file = tmp_path / "body.txt"
    body_file.write_text("Clean PR description.\n", encoding="utf-8")
    assert run_cli(["--title", "Fix crash", "--body-file", str(body_file)]) == 0
    assert capsys.readouterr().out.strip() == "ok"

    bad_file = tmp_path / "bad.txt"
    bad_file.write_text("Generated with AI\n", encoding="utf-8")
    assert run_cli(["--title", "Fix crash", "--body-file", str(bad_file), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "problems": ["the text contains an AI marker"]}

    missing = tmp_path / "does-not-exist.txt"
    assert run_cli(["--body-file", str(missing)]) == 2
    err = capsys.readouterr().err
    assert err.strip() != ""


def test_cli_tty_with_title_skips_stdin(monkeypatch, capsys):
    class TtyStdin:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("should not block reading stdin when --title is passed on a tty")

    monkeypatch.setattr(sys, "stdin", TtyStdin())
    assert run_cli(["--title", "Fix crash"]) == 0
    assert capsys.readouterr().out.strip() == "ok"


# -- end-to-end subprocess & security -----------------------------------------

def test_subprocess_e2e_offline_no_side_effects(tmp_path):
    missing_data_dir = tmp_path / "nonexistent-data-dir"
    work_dir = tmp_path / "empty-cwd"
    work_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env["SCOUT_DATA_DIR"] = str(missing_data_dir)
    env["PYTHONPATH"] = str(config.ROOT)

    proc = subprocess.run(
        [sys.executable, "-m", "scout", "check-text", "--title", "Fix crash", "--json"],
        input="fix a crash",
        text=True,
        capture_output=True,
        cwd=work_dir,
        env=env,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"ok": True, "problems": []}
    assert proc.stderr == ""
    assert not missing_data_dir.exists()
    assert list(work_dir.iterdir()) == []


def test_security_canary_never_echoed_and_nothing_written(tmp_path):
    canary_title = "CANARY_TITLE_9f3c2a1b"
    canary_body = "CANARY_BODY_7e4d8b2c"
    missing_data_dir = tmp_path / "no-data"
    work_dir = tmp_path / "cwd"
    work_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env["SCOUT_DATA_DIR"] = str(missing_data_dir)
    env["PYTHONPATH"] = str(config.ROOT)

    # Pass case
    ok_proc = subprocess.run(
        [sys.executable, "-m", "scout", "check-text", "--title", canary_title, "--json"],
        input=canary_body,
        text=True,
        capture_output=True,
        cwd=work_dir,
        env=env,
    )
    assert ok_proc.returncode == 0
    assert canary_title not in ok_proc.stdout + ok_proc.stderr
    assert canary_body not in ok_proc.stdout + ok_proc.stderr

    # Fail case (both plain and json)
    for extra in ([], ["--json"]):
        fail_proc = subprocess.run(
            [sys.executable, "-m", "scout", "check-text", "--title", f"{canary_title}\nsecond line", *extra],
            input=f"{canary_body}\nCo-Authored-By: x",
            text=True,
            capture_output=True,
            cwd=work_dir,
            env=env,
        )
        assert fail_proc.returncode == 1
        assert canary_title not in fail_proc.stdout + fail_proc.stderr
        assert canary_body not in fail_proc.stdout + fail_proc.stderr

    assert not missing_data_dir.exists()
    assert list(work_dir.iterdir()) == []
