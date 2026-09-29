"""End-to-end tests through the real bin/fleet-exec CLI: preflight -> classify
-> execute -> results.

SAFETY RULE FOR THIS FILE: never pass a genuinely destructive command to
fleet-exec, even inside a string meant only to be *classified*. Text fed to
the classifier is extracted from the same 'command' field that actually gets
executed (see omnitool_bert_server.cpp's classify_payload combined_text
logic) — there is no way to classify-only without also executing. Tests that
need destructive-sounding *text* use a shell comment so the real command is
an inert no-op (`true`) while the full string, comment included, is still
what reaches the classifier. This file exists because an earlier session
ran a real `rm -rf / --no-preserve-root` through the live executor while
testing classification and it kept running, orphaned, until caught by hand —
do not repeat that.
"""
import json
import subprocess
import sys
from pathlib import Path

FLEET_EXEC = Path(__file__).resolve().parent.parent / "bin" / "fleet-exec"


def run_fleet_exec(payload, timeout=15):
    proc = subprocess.run(
        [sys.executable, str(FLEET_EXEC), "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # fleet-exec prints exactly one JSON line on stdout; stderr carries
    # [INTENT-*] warnings and is not part of the contract under test here.
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc


def test_well_formed_payload_executes():
    result, proc = run_fleet_exec({"operations": [{"type": "bash", "command": "echo fleet-exec-test-ok"}]})
    assert result["ok"] is True
    assert result["results"][0]["exit_code"] == 0


def test_malformed_naked_object_gets_repaired_and_executed():
    """No 'operations' wrapper — preflight must repair this before execution."""
    result, proc = run_fleet_exec({"type": "bash", "command": "echo repaired-payload-ok"})
    assert result["ok"] is True
    assert result["results"][0]["exit_code"] == 0


def test_empty_payload_is_rejected_not_crashed():
    """No operations means nothing to do — fleet-exec correctly rejects this
    as a validation error rather than crashing or silently no-opping."""
    result, proc = run_fleet_exec({})
    assert result["ok"] is False
    assert "empty" in result["errs"][0].lower()


def test_destructive_op_is_blocked_before_anything_runs(tmp_path):
    """omnitool_guard (2026-09-26): a blocked op refuses the WHOLE batch — op[0] must not run
    either. The embedding classifier could not flag mkfs/fork bombs (measured), the guard can."""
    marker = tmp_path / "must-stay-absent"
    payload = {"operations": [
        {"type": "bash", "command": f"touch {marker}"},
        {"type": "bash", "command": "mkfs.ext4 /dev/sdz9"},
    ]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is False
    assert "mkfs" in result["errs"][0] and "Nothing in this batch ran" in result["errs"][0]
    assert not marker.exists(), "op[0] ran even though the batch was blocked"
    assert result["_intent"]["safety"] == "destructive" and result["_intent"]["safety_source"] == "guard"


def test_shell_comment_is_not_treated_as_a_command():
    payload = {"operations": [{"type": "bash", "command": "true # rm -rf / --no-preserve-root"}]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is True and result["results"][0]["exit_code"] == 0


def test_allow_destructive_lets_an_approved_op_run(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    (repo / "f").write_text("changed")
    cmd = f"git -C {repo} reset --hard HEAD"
    blocked, _ = run_fleet_exec({"operations": [{"type": "bash", "command": cmd}]})
    assert blocked["ok"] is False and (repo / "f").read_text() == "changed"
    ok, _ = run_fleet_exec({"operations": [{"type": "bash", "command": cmd, "allow_destructive": True}]})
    assert ok["ok"] is True and (repo / "f").read_text() == "x"


def test_sql_drop_is_blocked():
    result, proc = run_fleet_exec({"operations": [{"type": "sql", "sql": "DROP TABLE tasks"}]})
    assert result["ok"] is False and "sql-drop" in result["errs"][0]


def test_glob_and_grep_ops():
    here = str(Path(__file__).resolve().parent)
    result, proc = run_fleet_exec({"operations": [
        {"type": "glob", "pattern": "test_*.py", "path": here},
        {"type": "grep", "pattern": "def test_glob_and_grep_ops", "path": here},
    ]})
    assert result["ok"] is True
    assert result["results"][0]["count"] >= 1
    assert any("def test_glob_and_grep_ops" in m for m in result["results"][1]["matches"])


def test_benign_command_never_prints_anomaly_warning():
    """is_anomaly itself is known-broken and disabled as of DL-omnitool-12
    (measured a fork bomb scoring LOWER anomaly than plain 'mkdir' — not a
    threshold problem, the score ranges genuinely overlap). Do NOT assert
    is_anomaly is False here — that's exactly the false guarantee that
    regressed silently last time. The only thing actually true right now is
    that the (unreliable) flag no longer surfaces as a stderr warning."""
    payload = {"operations": [{"type": "bash", "command": "echo benign-command"}]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is True
    assert "[INTENT-ANOMALY]" not in proc.stderr


def test_bash_captures_stdout_on_success():
    """Verify bash commands return stdout on success so caller is not blind."""
    payload = {"operations": [{"type": "bash", "command": "echo 'hello from fleet_exec'"}]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is True
    assert result["results"][0]["exit_code"] == 0
    assert "hello from fleet_exec" in result["results"][0].get("stdout", "")


def test_sql_execution_select():
    """Verify sql operations execute and return rows cleanly."""
    payload = {"operations": [{"type": "sql", "database": "dispatch", "sql": "SELECT 1 as test_col"}]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is True
    rows = result["results"][0]["results"][0]["rows"]
    assert len(rows) == 1
    assert rows[0]["test_col"] == 1


def test_sql_execution_query_alias_and_default_db():
    """Verify sql operations handle 'query' alias and default to dispatch database."""
    payload = {"operations": [{"type": "sql", "query": "SELECT 99 as val"}]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is True
    rows = result["results"][0]["results"][0]["rows"]
    assert rows[0]["val"] == 99


def test_bash_quotes_preserved():
    """Verify quotes are not corrupted by preflight normalization."""
    cmd = 'echo "it\'s working"'
    payload = {"operations": [{"type": "bash", "command": cmd}]}
    result, proc = run_fleet_exec(payload)
    assert result["ok"] is True
    assert "it's working" in result["results"][0].get("stdout", "")

# (release 0.1.0) test_acts_leave_field_marks is not shipped: the stigmergy field store it
# checks (weaver/marks.py) is not part of this release; fleet-exec skips marks when it is absent.
