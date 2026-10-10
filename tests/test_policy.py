"""TARSUS-0 C1: the policy file (rule overrides, org custom rules, approval TTL) — read per check."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bin" / "fleet-exec").exists())
GUARD = (ROOT / "bin" / "omnitool_guard.py").resolve()


@pytest.fixture
def guard(tmp_path, monkeypatch):
    conf = tmp_path / "tarsus.conf"
    monkeypatch.setenv("TARSUS_CONF", str(conf))
    sys.path.insert(0, str(GUARD.parent))
    import omnitool_guard as g
    g = importlib.reload(g)
    return g, conf


def test_rule_override_raises_warn_to_block(guard):
    g, conf = guard
    assert g.check_operation({"type": "bash", "command": "curl -s example.com"})["level"] == "warn"
    conf.write_text("rule.network = block\n")
    assert g.check_operation({"type": "bash", "command": "curl -s example.com"})["level"] == "block"


def test_rule_off_drops_a_warning(guard):
    g, conf = guard
    conf.write_text("rule.network = off\n")
    assert g.check_operation({"type": "bash", "command": "curl -s example.com"})["level"] == "ok"


def test_custom_org_rule_blocks(guard):
    g, conf = guard
    conf.write_text("custom.no-prod-db = block :: any :: prod[-_]?db :: touches the production database\n")
    r = g.check_operation({"type": "bash", "command": "psql -h prod-db.internal -c 'select 1'"})
    assert r["level"] == "block" and r["rules"][0]["id"] == "no-prod-db"
    assert g.check_operation({"type": "bash", "command": "psql -h staging-db"})["level"] == "ok"


def test_self_approve_can_never_be_relaxed(guard):
    g, conf = guard
    conf.write_text("rule.self-approve = off\n")
    assert g.check_operation({"type": "bash", "command": "tarsus-approve 0123456789abcdef"})["level"] == "block"


def test_approval_ttl_from_policy(guard):
    g, conf = guard
    conf.write_text("approval_ttl_s = 5\n")
    assert g.load_policy()["ttl"] == 60          # clamped to the 60 s floor
    conf.write_text("approval_ttl_s = 3600\n")
    assert g.load_policy()["ttl"] == 3600


def test_policy_errors_are_reported_not_silently_ignored(tmp_path):
    conf = tmp_path / "tarsus.conf"
    conf.write_text("rule.network = maybe\ncustom.x = block :: bash :: ([ :: broken\n")
    p = subprocess.run([sys.executable, str(GUARD), "policy", "show"], capture_output=True, text=True,
                       env={**os.environ, "TARSUS_CONF": str(conf)})
    assert p.returncode == 1 and p.stdout.count("ERROR") == 2


def test_policy_check_cli_runs_nothing(tmp_path):
    marker = tmp_path / "ran"
    payload = json.dumps({"operations": [{"type": "bash", "command": f"touch {marker}"}]})
    p = subprocess.run([sys.executable, str(GUARD), "policy", "check", payload], capture_output=True, text=True,
                       env={**os.environ, "TARSUS_CONF": os.devnull})
    assert p.returncode == 0 and "op[0] OK" in p.stdout and not marker.exists()
