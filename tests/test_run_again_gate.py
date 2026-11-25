import io
import json
import re
from datetime import datetime, timedelta, timezone
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from scripts import run_again_gate as gate

NOW = datetime(2026, 7, 10, 21, 5, tzinfo=timezone.utc)
CAP_REASON = "Daily limit reached: 1 extra run per Pacific day, 1 used. The next one is allowed after midnight Pacific."
TOKEN = "ghs_private_test_token"
RUNS_URL = f"{gate.API}/repos/owner/scout/actions/workflows/nightly.yml/runs?created=%3E%3D2026-06-09&per_page=100"
JOBS_URL = f"{gate.API}/repos/owner/scout/actions/runs/1/jobs?filter=all&per_page=100"


def run(run_id=1, *, event="workflow_dispatch", created=NOW - timedelta(hours=2), updated=None):
    return {"id": run_id, "event": event, "created_at": created.isoformat(),
            "updated_at": (updated or created).isoformat()}


def gate_job(conclusion="success", *, status="completed"):
    return {"name": "gate", "status": status, "conclusion": conclusion}


def routine_job(minutes, *, conclusion="success"):
    return {"name": "scan", "steps": [{"name": "Start the Claude routine", "conclusion": conclusion,
                                      "completed_at": (NOW - timedelta(minutes=minutes)).isoformat()}]}


@pytest.mark.parametrize("runs,jobs,reason", [
    pytest.param([], {}, None, id="no-earlier-runs"),
    pytest.param([run()], {1: [gate_job()]}, CAP_REASON, id="daily-cap"),
    pytest.param([run(created=NOW - timedelta(hours=20))], {1: [gate_job()]}, None, id="yesterday-pacific"),
    pytest.param([run()], {1: [gate_job("failure")]}, None, id="earlier-refusal"),
    pytest.param([run()], {1: [gate_job("cancelled")]}, None, id="earlier-cancellation"),
    pytest.param([run()], {1: [gate_job("timed_out")]}, None, id="earlier-timeout"),
    pytest.param([run()], {1: [gate_job(None, status="in_progress")]}, CAP_REASON, id="gate-in-progress"),
    pytest.param([run()], {1: [gate_job(None, status="queued")]}, None, id="gate-queued"),
    pytest.param([run()], {1: [routine_job(59)]},
                 "The routine started 59 minutes ago. Wait until 14:06 Pacific (60 minute cooldown).",
                 id="routine-59-minutes-ago"),
    pytest.param([run()], {1: [routine_job(60)]}, None, id="exact-cooldown"),
    pytest.param([run()], {1: [routine_job(61)]}, None, id="routine-61-minutes-ago"),
    pytest.param([run(event="schedule")], {1: [routine_job(10)]},
                 "The routine started 10 minutes ago. Wait until 14:55 Pacific (60 minute cooldown).",
                 id="scheduled-routine-cooldown"),
    pytest.param([run(event="schedule")], {1: [gate_job(), routine_job(90)]}, None, id="schedule-uses-no-slot"),
    pytest.param([run()], {1: [routine_job(10, conclusion="failure")]}, None, id="routine-failed"),
    pytest.param([run(event="schedule", created=NOW - timedelta(hours=25), updated=NOW)],
                 {1: [routine_job(10)]},
                 "The routine started 10 minutes ago. Wait until 14:55 Pacific (60 minute cooldown).",
                 id="old-run-with-recent-routine"),
    pytest.param([run(1), run(2, created=NOW - timedelta(hours=3))],
                 {1: [routine_job(59)], 2: [routine_job(1), routine_job(61)]},
                 "The routine started 1 minute ago. Wait until 15:04 Pacific (60 minute cooldown).",
                 id="newest-step-completion-across-runs-and-jobs"),
])
def test_decide(runs, jobs, reason):
    allowed, message = gate.decide(NOW, iter(runs), jobs.__getitem__, 1, 60, "100")
    assert allowed == (reason is None)
    if reason:
        assert message == reason


@pytest.mark.parametrize("midnight", ["2026-07-10T07:00:00Z", "2026-01-10T08:00:00Z"], ids=["PDT", "PST"])
@pytest.mark.parametrize("seconds", [-1, 0, 1], ids=["before", "at", "after"])
def test_midnight_pacific_boundary(midnight, seconds):
    boundary = datetime.fromisoformat(midnight)
    now = boundary + timedelta(hours=1)
    prior = run(created=boundary + timedelta(seconds=seconds))
    allowed, reason = gate.decide(now, [prior], lambda _: [gate_job()], 1, 60, 100)
    assert allowed == (seconds < 0)
    if not allowed:
        assert reason == CAP_REASON


@pytest.mark.parametrize("listed_id,current_id", [(17, "17"), ("17", 17)])
def test_current_run_is_excluded(listed_id, current_id):
    def jobs_of(run_id):
        pytest.fail(f"Unexpected lookup for the current run {run_id}")

    assert gate.decide(NOW, [run(listed_id)], jobs_of, 1, 60, current_id)[0]


def test_rerun_same_run_counts_earlier_attempt_for_cap_and_cooldown():
    jobs_cap = [
        {**gate_job("success"), "run_attempt": 1},
        {**gate_job(None, status="in_progress"), "run_attempt": 2},
    ]
    allowed, reason = gate.decide(NOW, [run(100)], lambda _: jobs_cap, 1, 60, "100", current_run_attempt="2")
    assert not allowed and reason == CAP_REASON

    jobs_cooldown = [
        {**gate_job("success"), "run_attempt": 1},
        {**routine_job(10), "run_attempt": 1},
        {**gate_job(None, status="in_progress"), "run_attempt": 2},
    ]
    allowed, reason = gate.decide(NOW, [run(100)], lambda _: jobs_cooldown, 2, 60, "100", current_run_attempt=2)
    assert not allowed and reason == "The routine started 10 minutes ago. Wait until 14:55 Pacific (60 minute cooldown)."

    jobs_failed_first = [
        {**gate_job("failure"), "run_attempt": 1},
        {**gate_job(None, status="in_progress"), "run_attempt": 2},
    ]
    assert gate.decide(NOW, [run(100)], lambda _: jobs_failed_first, 1, 60, "100", current_run_attempt=2)[0]


@pytest.mark.parametrize("bad_attempt", [0, -1, True, "abc"])
def test_invalid_current_run_attempt_fails_closed(bad_attempt):
    allowed, reason = gate.decide(NOW, [], lambda _: [], 1, 60, "100", current_run_attempt=bad_attempt)
    assert not allowed and reason == gate.UNVERIFIED


def test_rerun_without_job_run_attempt_fails_closed():
    allowed, reason = gate.decide(NOW, [run(100)], lambda _: [gate_job("failure")], 1, 60, "100", current_run_attempt=2)
    assert not allowed and reason == gate.UNVERIFIED


def test_custom_cap_counts_each_run_once_and_pluralizes():
    runs = [run(1), run("1"), run(2)]
    jobs_of = lambda _: [gate_job(), gate_job()]
    assert gate.decide(NOW, runs, jobs_of, 3, 60, 100)[0]
    allowed, reason = gate.decide(NOW, runs, jobs_of, 2, 60, 100)
    assert not allowed
    assert reason == "Daily limit reached: 2 extra runs per Pacific day, 2 used. The next one is allowed after midnight Pacific."


def test_decide_fails_closed_on_jobs_error():
    def jobs_of(_):
        raise RuntimeError(TOKEN)

    assert gate.decide(NOW, [run()], jobs_of, 1, 60, 100) == (False, gate.UNVERIFIED)


@pytest.mark.parametrize("jobs", [None, {}, [None], [{}], [{"name": "gate"}],
                                  [{"name": "scan", "steps": {}}],
                                  [{"name": "scan", "steps": [{"name": gate.ROUTINE_STEP, "conclusion": "success"}]}]])
def test_decide_fails_closed_on_malformed_jobs(jobs):
    allowed, reason = gate.decide(NOW, [run()], lambda _: jobs, 1, 60, 100)
    assert not allowed and "could not verify the limits" in reason


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz):
            return NOW.astimezone(tz)

    monkeypatch.setattr(gate, "datetime", FixedClock)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/scout")
    monkeypatch.setenv("GITHUB_RUN_ID", "100")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    summary = tmp_path / "summary.md"
    summary.write_text("Earlier summary\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    targets = tmp_path / "targets.toml"
    targets.write_text("[settings]\n")
    monkeypatch.setattr(gate, "TARGETS", targets)
    return summary


def serve_pages(monkeypatch, pages):
    calls = []

    def urlopen(request, *, timeout):
        assert timeout > 0
        assert request.get_header("Accept") == "application/vnd.github+json"
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert request.get_header("X-github-api-version") == "2022-11-28"
        assert request.get_header("User-agent")
        calls.append(request.full_url)
        body, link = pages[request.full_url]
        response = io.BytesIO(json.dumps(body).encode())
        response.headers = Message()
        if link:
            response.headers["Link"] = link
        return response

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    return calls


def check_refusal(capsys, summary, reason):
    output = capsys.readouterr()
    assert output.out == f"::error title=Run again refused::{reason}\n"
    assert output.err == ""
    assert summary.read_text() == f"Earlier summary\n{reason}\n"
    assert TOKEN not in output.out + output.err + summary.read_text()


def test_main_refusal_logs_and_appends(main_env, monkeypatch, capsys):
    calls = serve_pages(monkeypatch, {
        RUNS_URL: ({"workflow_runs": [run()]}, None),
        JOBS_URL: ({"jobs": [gate_job()]}, None),
    })
    assert gate.main() == 1
    check_refusal(capsys, main_env, CAP_REASON)
    assert calls == [RUNS_URL, JOBS_URL]


def test_main_allows_without_appending(main_env, monkeypatch, capsys):
    calls = serve_pages(monkeypatch, {RUNS_URL: ({"workflow_runs": [run(100)]}, None)})
    assert gate.main() == 0
    output = capsys.readouterr()
    assert output.out == f"{gate.ALLOWED}\n" and output.err == ""
    assert main_env.read_text() == "Earlier summary\n"
    assert TOKEN not in output.out + output.err + main_env.read_text()
    assert calls == [RUNS_URL]


@pytest.mark.parametrize("error", [URLError(TOKEN), HTTPError(RUNS_URL, 403, TOKEN, {}, None)])
def test_main_api_error_fails_closed_without_leaking_token(main_env, monkeypatch, capsys, error):
    def urlopen(*args, **kwargs):
        raise error

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    assert gate.main() == 1
    check_refusal(capsys, main_env, gate.UNVERIFIED)


def test_main_paginates_runs_and_jobs_and_uses_cooldown_override(main_env, monkeypatch, capsys):
    gate.TARGETS.write_text("[settings]\nrun_again_cooldown_minutes = 30\n")
    runs_next = f"{RUNS_URL}&page=2"
    jobs_next = f"{JOBS_URL}&page=2"
    calls = serve_pages(monkeypatch, {
        RUNS_URL: ({"workflow_runs": [run(2, created=NOW - timedelta(hours=25))]},
                   f'<{runs_next}>; rel="next", <{runs_next}>; rel="last"'),
        runs_next: ({"workflow_runs": [run(event="schedule", created=NOW - timedelta(days=2), updated=NOW)]},
                    f'<{RUNS_URL}>; rel="prev"'),
        JOBS_URL: ({"jobs": [{"name": "deploy"}]}, f'<{jobs_next}>; rel="next"'),
        jobs_next: ({"jobs": [routine_job(10)]}, None),
    })
    assert gate.main() == 1
    check_refusal(capsys, main_env, "The routine started 10 minutes ago. Wait until 14:25 Pacific (30 minute cooldown).")
    assert calls == [RUNS_URL, runs_next, JOBS_URL, jobs_next]


def test_main_uses_daily_cap_override(main_env, monkeypatch, capsys):
    gate.TARGETS.write_text("[settings]\nrun_again_daily_cap = 2\n")
    serve_pages(monkeypatch, {
        RUNS_URL: ({"workflow_runs": [run()]}, None),
        JOBS_URL: ({"jobs": [gate_job()]}, None),
    })
    assert gate.main() == 0
    assert capsys.readouterr().out == f"{gate.ALLOWED}\n"
    assert main_env.read_text() == "Earlier summary\n"


def test_main_rerun_checks_earlier_attempt_of_current_run(main_env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    current_jobs_url = f"{gate.API}/repos/owner/scout/actions/runs/100/jobs?filter=all&per_page=100"
    calls = serve_pages(monkeypatch, {
        RUNS_URL: ({"workflow_runs": [run(100)]}, None),
        current_jobs_url: ({"jobs": [
            {**gate_job("success"), "run_attempt": 1},
            {**gate_job(None, status="in_progress"), "run_attempt": 2},
        ]}, None),
    })
    assert gate.main() == 1
    check_refusal(capsys, main_env, CAP_REASON)
    assert calls == [RUNS_URL, current_jobs_url]


def test_nightly_workflow_gate_permissions_and_scan_condition():
    workflow = (gate.TARGETS.parent / ".github/workflows/nightly.yml").read_text()
    assert f"- name: {gate.ROUTINE_STEP}\n" in workflow
    gate_job_text, remaining_jobs = workflow.split("\njobs:\n  gate:\n", 1)[1].split("\n  scan:\n", 1)
    scan_job_text, deploy_job_text = remaining_jobs.split("\n  deploy:\n", 1)
    assert "    if: github.event_name == 'workflow_dispatch'\n" in gate_job_text
    permissions = re.search(r"^    permissions:\n((?:^      \S[^\n]*\n)+)", gate_job_text, re.M)
    assert permissions and set(permissions[1].splitlines()) == {"      contents: read", "      actions: read"}
    assert "persist-credentials: false" in gate_job_text
    assert "GH_TOKEN: ${{ github.token }}" in gate_job_text and "secrets." not in gate_job_text
    assert "run: python scripts/run_again_gate.py" in gate_job_text
    assert "    needs: gate\n" in scan_job_text
    assert "    if: ${{ !cancelled() && (needs.gate.result == 'success' || needs.gate.result == 'skipped') }}\n" in scan_job_text
    assert "    needs: scan\n" in deploy_job_text
    assert "    if: ${{ !cancelled() && needs.scan.result == 'success' }}\n" in deploy_job_text
