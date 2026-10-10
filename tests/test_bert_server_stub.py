"""The C++ fast-path server (omnitool_bert_server.cpp) has no real inference
implemented yet (see the comment above classify_text() in that file). It
must fail *honestly* — an explicit "error" key — rather than fabricate a
result, since bin/fleet-exec's fallback chain only proceeds to the real
classifier when it sees an "error" key or the connection fails outright.
A silently "successful" fake response would short-circuit that fallback and
resurrect the exact bug this test guards against.

Skipped entirely if the socket isn't up — this is a live-process integration
check, not something that should block a normal test run.
"""
import pytest

from conftest import bert_socket_request


def test_health_check_works(bert_socket_available):
    if not bert_socket_available:
        pytest.skip("experiment: omnitool_bert_server is off the production path (SYSTEMS.yaml intended off; FLEET_EXEC_BERT_SOCKET=1 to opt in)")
    resp = bert_socket_request({"health": True})
    assert resp.get("status") == "ok"


def test_classify_honestly_errors_instead_of_fabricating(bert_socket_available):
    if not bert_socket_available:
        pytest.skip("experiment: omnitool_bert_server is off the production path (SYSTEMS.yaml intended off; FLEET_EXEC_BERT_SOCKET=1 to opt in)")
    resp = bert_socket_request({"payload": {"operations": [
        {"type": "bash", "command": "rm -rf / --no-preserve-root"}
    ]}})
    assert "error" in resp, (
        "classify_text() returned a result instead of an error — either real "
        "inference was implemented (update this test and the fallback-chain "
        "comment in bin/fleet-exec) or the stub regressed back to fabricating output"
    )
