"""Shared fixtures for the omnitool test suite.

Puts bin/ on sys.path (modules there import each other assuming that), and
provides availability checks for the two optional live daemons so tests can
skip cleanly instead of failing when nothing is running.
"""
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN_DIR))

BERT_SOCKET = "/tmp/omnitool_bert.sock"
DAEMON_URL = "http://127.0.0.1:8498"


def _bert_socket_up() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(BERT_SOCKET)
        s.close()
        return True
    except Exception:
        return False


def _daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def bert_socket_available() -> bool:
    return _bert_socket_up()


@pytest.fixture(scope="session")
def intent_daemon_available() -> bool:
    return _daemon_up()


def bert_socket_request(payload: dict, timeout: float = 3.0) -> dict:
    """Send a raw request to the C++ classifier socket and return the parsed response."""
    import json as _json
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(BERT_SOCKET)
    s.sendall(_json.dumps(payload).encode())
    s.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    s.close()
    return _json.loads(b"".join(chunks).decode())


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test runs against private state: its own SQL alias directory (with an empty
    dispatch.db so the default `database` alias resolves), its own omnibreak-free env, no
    stigmergy marks, and the advisory embedding classifier off (the deterministic guard
    still runs; tests/test_classifier.py exercises the classifier directly)."""
    import sqlite3
    db_dir = tmp_path / "control-center"
    db_dir.mkdir()
    sqlite3.connect(db_dir / "dispatch.db").close()
    monkeypatch.setenv("OMNITOOL_DB_DIR", str(db_dir))
    monkeypatch.setenv("OMNIBREAK_DB", str(tmp_path / "omnibreak.db"))
    monkeypatch.setenv("FIELD_DB", str(tmp_path / "field.sqlite"))
    monkeypatch.setenv("FLEET_EXEC_MARKS", "0")
    monkeypatch.setenv("FLEET_EXEC_CLASSIFIER", "off")
