"""TARSUS-0 B2: `tarsus init` / `tarsus doctor` (consumer setup from a checkout)."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bin" / "fleet-exec").exists())   # dev tree or release
TARSUS = ROOT / "bin" / "tarsus"


def run(*args, conf_dir):
    env = {**os.environ, "TARSUS_CONFIG_DIR": str(conf_dir)}
    return subprocess.run([sys.executable, str(TARSUS), *args], capture_output=True, text=True, env=env, timeout=60)


def test_init_writes_policy_and_prints_entry_without_touching_client_configs(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    p = run("init", "--project", str(proj), "--client", "claude-code", conf_dir=tmp_path / "conf")
    assert p.returncode == 0, p.stderr
    scopes = (tmp_path / "conf" / "write-scopes.conf").read_text()
    assert str(proj.resolve()) in scopes and "/tmp" in scopes
    assert "sandbox =" in (tmp_path / "conf" / "tarsus.conf").read_text()
    assert '"tarsus"' in p.stdout and "tarsus-server.js" in p.stdout
    assert not (proj / ".mcp.json").exists(), "nothing written to a client config without --write"


def test_init_write_merges_and_keeps_other_servers(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    p = run("init", "--project", str(proj), "--client", "claude-code", "--write", conf_dir=tmp_path / "conf")
    assert p.returncode == 0, p.stderr
    cfg = json.loads((proj / ".mcp.json").read_text())
    assert set(cfg["mcpServers"]) == {"other", "tarsus"}
    env = cfg["mcpServers"]["tarsus"]["env"]
    assert env["OMNITOOL_SCOPES_FILE"].endswith("write-scopes.conf") and env["TARSUS_CONF"].endswith("tarsus.conf")
    assert (proj / ".mcp.json.bak-tarsus").exists()


def test_init_keeps_existing_policy(tmp_path):
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "write-scopes.conf").write_text("/srv/mine\n")
    run("init", conf_dir=conf)
    assert (conf / "write-scopes.conf").read_text() == "/srv/mine\n"


def test_doctor_reports_every_check(tmp_path):
    p = run("doctor", conf_dir=tmp_path / "conf")
    for name in ("python", "node", "rg", "patch", "Landlock", "MCP SDK", "policy files"):
        assert name in p.stdout
