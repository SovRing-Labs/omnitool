"""TARSUS-0 A3: bash ops run under Landlock — writes only beneath the write scopes (review G1, 2026-10-06)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bin" / "fleet-exec").exists())   # dev tree or release export
FLEET_EXEC = ROOT / "bin" / "fleet-exec"
SANDBOX = (ROOT / "bin" / "tarsus_sandbox.py").resolve()   # bin/ symlink in dev, real file in the release


def _landlock_abi() -> int:
    import ctypes
    return ctypes.CDLL(None, use_errno=True).syscall(444, None, 0, 1)


needs_landlock = pytest.mark.skipif(_landlock_abi() < 1, reason="kernel without Landlock")


def run_bash(cmd, scope, mode="enforce", extra="", **op):
    env = {**os.environ, "FLEET_EXEC_WRITE_SCOPES": str(scope), "FLEET_EXEC_SANDBOX": mode,
           "FLEET_EXEC_BASH_EXTRA_WRITE": extra, "FLEET_EXEC_CLASSIFIER": "off"}
    env.pop("FLEET_EXEC_WRITE_ANYWHERE", None)
    payload = {"operations": [{"type": "bash", "command": cmd, "timeout_s": 20, "cwd": str(scope), **op}]}
    out = subprocess.run([str(FLEET_EXEC), json.dumps(payload)], capture_output=True, text=True, env=env, timeout=60)
    r = json.loads(out.stdout[out.stdout.index("{"):])["results"][0]
    r["text"] = f"{r.get('stdout') or ''}{r.get('stderr') or ''}"   # some paths merge stderr into stdout
    return r


@needs_landlock
def test_write_outside_scope_is_denied_by_the_kernel(tmp_path):
    scope, outside = tmp_path / "scope", Path("/var/tmp") / f"tarsus-test-{os.getpid()}"
    scope.mkdir()
    r = run_bash(f"echo x > {outside}", scope)
    assert r["ok"] is False and "Permission denied" in r["text"]
    assert "sandbox_hint" in r
    assert not outside.exists()


@needs_landlock
def test_inside_scope_full_file_lifecycle_works(tmp_path):
    r = run_bash("echo a > f && mkdir d && mv f d/ && cat d/f && rm -r d && echo /dev/null-ok > /dev/null", tmp_path)
    assert r["ok"] is True and r.get("stdout", "").strip() == "a"


@needs_landlock
def test_child_processes_inherit_the_sandbox(tmp_path):
    outside = Path("/var/tmp") / f"tarsus-child-{os.getpid()}"
    r = run_bash(f"python3 -c \"open('{outside}','w')\"", tmp_path)
    assert r["ok"] is False and "PermissionError" in r["text"]
    assert not outside.exists()


@needs_landlock
def test_auto_detach_path_is_sandboxed_too(tmp_path):
    outside = Path("/var/tmp") / f"tarsus-detach-{os.getpid()}"
    r = run_bash(f"echo x > {outside}", tmp_path, on_timeout="detach")
    assert r["ok"] is False
    assert not outside.exists()


@needs_landlock
def test_extra_write_dirs_are_granted(tmp_path):
    scope, extra = tmp_path / "scope", tmp_path / "cache"
    scope.mkdir(); extra.mkdir()
    r = run_bash(f"echo c > {extra}/x && cat {extra}/x", scope, extra=str(extra))
    assert r["ok"] is True and r["stdout"].strip() == "c"


def _already_sandboxed() -> bool:
    """True when pytest itself runs inside a Landlock domain (e.g. launched from a sandboxed kernel bash op)."""
    probe = f"/var/tmp/tarsus-probe-{os.getpid()}"
    try:
        open(probe, "w").close(); os.unlink(probe); return False
    except PermissionError:
        return True


@pytest.mark.skipif(_already_sandboxed(), reason="test process is itself sandboxed; off mode cannot widen it")
def test_off_mode_is_the_old_behaviour(tmp_path):
    outside = f"/var/tmp/tarsus-off-{os.getpid()}"
    r = run_bash(f"echo y > {outside} && cat {outside} && rm {outside}", tmp_path, mode="off")
    assert r["ok"] is True and r["stdout"].strip() == "y"


def test_enforce_refuses_when_landlock_is_unavailable(tmp_path, monkeypatch):
    # simulate an old kernel: the launcher's apply() raises -> enforce exits 126 and the command never runs
    marker = tmp_path / "ran"
    code = (f"import sys; sys.path.insert(0, {str(SANDBOX.parent)!r}); import tarsus_sandbox as t; "
            f"t.apply = lambda dirs: (_ for _ in ()).throw(OSError(38, 'landlock not available')); "
            f"sys.exit(t.main(['--write', {str(tmp_path)!r}, '--', '/bin/touch', {str(marker)!r}]))")
    p = subprocess.run(["python3", "-c", code], capture_output=True, text=True)
    assert p.returncode == 126 and "refusing to run unsandboxed" in p.stderr
    assert not marker.exists()
