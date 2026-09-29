"""Dispatch Doctor quote repair must not change meaning (09-29: `echo it's fine` printed `its fine`)."""
import os
import subprocess
from importlib.machinery import SourceFileLoader

FE = os.environ.get("FLEET_EXEC_PATH") or os.path.join(os.path.dirname(__file__), "..", "bin", "fleet-exec")
ERR = {"ok": False, "exit_code": 2, "stderr": "/bin/sh: -c: line 1: unexpected EOF while looking for matching `''"}


def _fix(cmd):
    m = SourceFileLoader("fleet_exec_doc", FE).load_module()
    op, cat = m._doctor_diagnose_and_fix({"type": "bash", "command": cmd}, ERR)
    return op["command"], cat


def _run(cmd):
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip()


def test_apostrophe_in_word_is_escaped_not_closed():
    cmd, cat = _fix("echo it's fine")
    assert cat == "escape_apostrophe" and _run(cmd) == "it's fine"


def test_apostrophe_inside_single_quotes():
    cmd, cat = _fix("echo 'don't know'")
    assert cat == "escape_apostrophe_in_quotes" and _run(cmd) == "don't know"


def test_missing_closing_quote_still_closed():
    cmd, cat = _fix("echo 'x' 'y")
    assert cat == "close_trailing_quote" and _run(cmd) == "x y"
