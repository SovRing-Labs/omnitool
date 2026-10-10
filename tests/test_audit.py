"""TARSUS-0 C2: per-operation audit stream (OTel log data model JSONL) + `tarsus audit` reader."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bin" / "fleet-exec").exists())


def run(payload, audit, detail="hash", extra_env=None):
    env = {**os.environ, "TARSUS_AUDIT": str(audit), "TARSUS_AUDIT_DETAIL": detail, "FLEET_EXEC_CLASSIFIER": "off",
           **(extra_env or {})}
    return subprocess.run([sys.executable, str(ROOT / "bin" / "fleet-exec"), "-"], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)


def records(audit):
    return [json.loads(l) for l in Path(audit).read_text().splitlines()]


def test_one_record_per_op_in_otel_shape(tmp_path):
    audit = tmp_path / "a.jsonl"
    run({"operations": [{"type": "bash", "command": "echo one", "cwd": str(tmp_path)},
                        {"type": "bash", "command": "echo two", "cwd": str(tmp_path)}]}, audit,
        extra_env={"TALON_TASK_ID": "T-AUDIT"})
    rs = records(audit)
    assert len(rs) == 2
    for r in rs:
        assert {"Timestamp", "SeverityText", "Body", "TraceId", "SpanId", "Attributes", "Resource"} <= set(r)
        assert r["Resource"]["service.name"] == "tarsus"
        assert r["Attributes"]["tarsus.actor.task"] == "T-AUDIT"
        assert len(r["Attributes"]["tarsus.op.fingerprint"]) == 16
    assert rs[0]["TraceId"] == rs[1]["TraceId"] and rs[0]["SpanId"] != rs[1]["SpanId"]


def test_hash_detail_never_records_command_text(tmp_path):
    audit = tmp_path / "a.jsonl"
    run({"operations": [{"type": "bash", "command": "echo SECRET-TOKEN-123", "cwd": str(tmp_path)}]}, audit)
    assert "SECRET-TOKEN-123" not in audit.read_text()
    run({"operations": [{"type": "bash", "command": "echo visible", "cwd": str(tmp_path)}]}, audit, detail="summary")
    assert records(audit)[-1]["Attributes"]["tarsus.op.summary"] == "echo visible"


def test_refused_batch_is_audited_as_not_run(tmp_path):
    audit = tmp_path / "a.jsonl"
    run({"operations": [{"type": "sql", "sql": "DROP TABLE tasks"}]}, audit)
    r = records(audit)[0]
    assert r["SeverityText"] == "ERROR" and r["Attributes"]["tarsus.outcome"] == "not_run"
    assert r["Attributes"]["tarsus.guard.rules"] == ["sql-drop"]


def test_audit_off_writes_nothing(tmp_path):
    audit = tmp_path / "a.jsonl"
    run({"operations": [{"type": "bash", "command": "echo x", "cwd": str(tmp_path)}]}, "off")
    assert not audit.exists()


def test_reader_lists_and_aggregates(tmp_path):
    audit = tmp_path / "a.jsonl"
    run({"operations": [{"type": "bash", "command": "echo ok", "cwd": str(tmp_path)},
                        {"type": "bash", "command": "curl -s --max-time 1 http://127.0.0.1:9 || true", "cwd": str(tmp_path)}]},
        audit)
    tarsus = [sys.executable, str(ROOT / "bin" / "tarsus"), "audit", "--file", str(audit)]
    out = subprocess.run(tarsus + ["--stats"], capture_output=True, text=True).stdout
    assert out.startswith("2 operations") and "rule network: 1" in out
    warn = subprocess.run(tarsus + ["--min-severity", "WARN"], capture_output=True, text=True).stdout.strip().splitlines()
    assert len(warn) == 1 and "network" in warn[0]
