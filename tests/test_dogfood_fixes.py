"""Regression tests for the omnitool dogfood bugs fixed 2026-10-01 (agent-memory/fleet-memory.db bugs 78, 79, 80/98/100,
96, 97). Same safety rule as test_fleet_exec_integration.py: guard checks go through check_operation (classify only, never
executed); anything executed is inert (echo, /tmp files)."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bin" / "fleet-exec").exists())   # dev tree or release export
FLEET_EXEC = ROOT / "bin" / "fleet-exec"
sys.path.insert(0, str(ROOT / "bin"))

from omnitool_guard import check_operation  # noqa: E402
import fleet_sql  # noqa: E402


def run_fleet_exec(payload, timeout=30):
    proc = subprocess.run([sys.executable, str(FLEET_EXEC), "-"], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=timeout)
    return json.loads(proc.stdout.strip().splitlines()[-1])


# bugs 80 / 98 / 100 — parallel ops shared one ms-timestamped log: mixed stdout + dead log_path
def test_parallel_ops_get_their_own_output():
    ops = [{"type": "bash", "command": f"echo dogfood-marker-{i}"} for i in range(12)]
    res = run_fleet_exec({"concurrency": "parallel", "operations": ops})
    assert res["ok"] is True
    for i, r in enumerate(res["results"]):
        out = r.get("stdout") or r.get("output") or ""
        assert f"dogfood-marker-{i}" in out, (i, r)
        others = [j for j in range(12) if j != i and f"dogfood-marker-{j}\n" in out + "\n"]
        assert not others, (i, others)


def test_new_log_path_is_unique():
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader("fleet_exec_mod", str(FLEET_EXEC))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_exec_mod", loader))
    loader.exec_module(mod)
    paths = {mod._new_log_path("t") for _ in range(200)}
    try:
        assert len(paths) == 200
    finally:
        for p in paths:
            Path(p).unlink(missing_ok=True)


# bug 97 — an out-of-scope write failed alone while its siblings ran
def test_out_of_scope_write_refuses_whole_batch(tmp_path):
    marker = Path("/tmp") / f"dogfood-97-{tmp_path.name}"
    res = run_fleet_exec({"operations": [
        {"type": "bash", "command": f"touch {marker}"},
        {"type": "write", "path": "/etc/dogfood-97-never", "content": "x"}]})
    try:
        assert res["ok"] is False
        assert any("write scope" in e for e in res.get("errs", [])), res
        assert not marker.exists()
    finally:
        marker.unlink(missing_ok=True)


# bug 96 — multi-statement SQL raised "You can only execute one statement at a time"
def test_multi_statement_sql(tmp_path):
    db = tmp_path / "t.db"
    res = fleet_sql.execute_omni_batch({"database": str(db), "operations": [
        {"sql": "CREATE TABLE a(x TEXT); INSERT INTO a VALUES ('p;q'); INSERT INTO a VALUES ('r');"}]})
    assert res["ok"] is True, res
    assert sqlite3.connect(db).execute("SELECT group_concat(x, '|') FROM a").fetchone()[0] == "p;q|r"


def test_split_statements_respects_quotes_and_triggers():
    s = fleet_sql._split_statements(
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN UPDATE a SET x='1;2'; END; SELECT 1")
    assert len(s) == 2 and s[0].endswith("END")


# bug 78 — guard matched dangerous words inside quoted arguments (grep patterns, commit messages)
def _level(cmd):
    return check_operation({"type": "bash", "command": cmd})["level"]


def test_quoted_power_word_not_blocked():
    assert _level('grep -n "a\\|halt" notes.txt') != "block"
    assert _level("git commit -m 'do not reboot the pod'") != "block"


def test_nested_shell_still_blocked():
    assert _level('bash -c "halt"') == "block"


def test_quoted_device_argument_still_blocked():
    assert _level('dd if=x of="/dev/sda"') == "block"


# bug 79 — rm-live-db spanned `&&` into the next command
def test_rm_live_db_does_not_span_commands():
    r = check_operation({"type": "bash",
                         "command": "rm -rf dg && sqlite3 control-center/dispatch.db 'select 1'"})
    assert "rm-live-db" not in [x["id"] for x in r["rules"]]


def test_rm_live_db_still_blocked():
    assert _level("rm control-center/dispatch.db") == "block"
