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


def test_cli_tty_stdin_is_read_consistently(monkeypatch, capsys):
    assert run_cli(["--title", "Fix crash", "--json"], stdin_text="Generated with a tool",
                   isatty=True, monkeypatch=monkeypatch) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "problems": ["the text contains an AI marker"]}


def test_cli_no_body_checks_title_only_without_reading_stdin(monkeypatch, capsys):
    class UnreadableStdin:
        def read(self):
            raise AssertionError("--no-body must not read stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    assert run_cli(["--title", "Fix crash", "--no-body"]) == 0
    assert capsys.readouterr().out.strip() == "ok"

    assert run_cli(["--title", "bad\ntitle", "--no-body", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "problems": [f"the title must be one line, 1-{act.MAX_TITLE} characters"],
    }


def test_cli_closed_stdin_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", None)
    assert run_cli(["--title", "Fix crash", "--json"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "error: could not read stdin" in out.err


def test_subprocess_stdin_utf8_regardless_of_locale(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "LC_ALL", "LANG")}
    env["SCOUT_DATA_DIR"] = str(tmp_path / "no-data")
    env["PYTHONPATH"] = str(config.ROOT)
    env["PYTHONIOENCODING"] = "latin-1"
    env["LC_ALL"] = "C"
    env["PYTHONUTF8"] = "0"

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "scout", "check-text", "--title", "Fix crash", "--json"],
        input="hi \U0001F916".encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout.decode("utf-8")) == {"ok": False, "problems": ["the text contains an AI marker"]}
    assert proc.stderr == b""


def test_subprocess_stdin_invalid_utf8_exits_2(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env["SCOUT_DATA_DIR"] = str(tmp_path / "no-data")
    env["PYTHONPATH"] = str(config.ROOT)

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "scout", "check-text", "--title", "Fix crash", "--json"],
        input=b"hi \xff\xfe",
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 2
    assert proc.stdout == b""
    stderr = proc.stderr.decode("utf-8")
    assert stderr.startswith("error: could not read stdin:")
    assert "Traceback" not in stderr


def test_subprocess_no_body_with_devnull_stdin(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env["SCOUT_DATA_DIR"] = str(tmp_path / "no-data")
    env["PYTHONPATH"] = str(config.ROOT)

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "scout", "check-text", "--title", "Fix crash", "--no-body", "--json"],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"ok": True, "problems": []}
    assert proc.stderr == ""


def test_subprocess_crlf_parity_between_stdin_and_body_file(tmp_path):
    body_bytes = (b"a" * (act.MAX_BODY - 1)) + b"\r\n"
    body_file = tmp_path / "crlf.txt"
    body_file.write_bytes(body_bytes)
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env["SCOUT_DATA_DIR"] = str(tmp_path / "no-data")
    env["PYTHONPATH"] = str(config.ROOT)

    from_file = subprocess.run(
        [sys.executable, "-B", "-m", "scout", "check-text", "--title", "Fix crash", "--body-file", str(body_file), "--json"],
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    from_stdin = subprocess.run(
        [sys.executable, "-B", "-m", "scout", "check-text", "--title", "Fix crash", "--json"],
        input=body_bytes,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert from_file.returncode == from_stdin.returncode == 0
    assert json.loads(from_file.stdout) == json.loads(from_stdin.stdout) == {"ok": True, "problems": []}


def test_subprocess_readonly_checkout_writes_nothing(tmp_path):
    import shutil

    ro_root = tmp_path / "ro-scout"
    shutil.copytree(config.ROOT / "scout", ro_root / "scout", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Verify both writable checkout with -B and read-only checkout without -B write zero bytecode or data files.
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env["SCOUT_DATA_DIR"] = str(tmp_path / "no-data")
    env["PYTHONPATH"] = str(ro_root)
    proc_b = subprocess.run(
        [sys.executable, "-B", "-m", "scout", "check-text", "--title", "Fix crash", "--json"],
        input="fix a crash",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert proc_b.returncode == 0
    assert json.loads(proc_b.stdout) == {"ok": True, "problems": []}
    assert not (ro_root / "scout" / "__pycache__").exists()
    assert not (tmp_path / "no-data").exists()

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return
    for p in [ro_root / "scout", ro_root]:
        p.chmod(0o555)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "scout", "check-text", "--title", "Fix crash", "--json"],
            input="fix a crash",
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=env,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {"ok": True, "problems": []}
        assert proc.stderr == ""
        assert not (ro_root / "scout" / "__pycache__").exists()
        assert not (tmp_path / "no-data").exists()
    finally:
        for p in [ro_root, ro_root / "scout"]:
            p.chmod(0o755)


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
