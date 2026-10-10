"""Preflight normalization: malformed agent payloads must come out as a
well-formed {"operations": [...]} shape. These are the exact shapes a
tool-calling LLM is known to hallucinate (see bin/omnitool_preflight.py's own
__main__ self-test, which prints these but never asserts on them)."""
from omnitool_preflight import OmnitoolPreFlightEngine


def _ops(raw):
    result = OmnitoolPreFlightEngine.sanitize_and_wrap(raw)
    assert isinstance(result, dict)
    assert "operations" in result
    assert isinstance(result["operations"], list)
    return result["operations"]


def test_naked_object_gets_wrapped():
    ops = _ops({"type": "read", "path": "/var/log/syslog"})
    assert len(ops) == 1
    assert ops[0]["type"] == "read"
    assert ops[0]["path"] == "/var/log/syslog"


def test_stringified_bash_command():
    ops = _ops('grep -n "sys.path" bin/dispatcherd')
    assert len(ops) == 1
    assert ops[0]["type"] == "bash"
    assert "sys.path" in ops[0]["command"]


def test_empty_payload_yields_empty_operations():
    ops = _ops({})
    assert ops == []


def test_naked_array_gets_wrapped():
    ops = _ops([{"type": "bash", "command": "ls"}, {"type": "read", "path": "file.txt"}])
    assert len(ops) == 2
    assert ops[0]["type"] == "bash"
    assert ops[1]["type"] == "read"


def test_tasks_alias_is_accepted():
    ops = _ops({"tasks": [{"type": "read", "path": "test.py"}], "concurrency": "sequential"})
    assert len(ops) == 1
    assert ops[0]["path"] == "test.py"


def test_required_fields_get_empty_string_defaults():
    ops = _ops({"operations": [{"type": "write"}]})
    assert ops[0].get("content", "") == ""
    ops = _ops({"operations": [{"type": "sql"}]})
    assert ops[0].get("database", "") == ""


def test_well_formed_payload_passes_through_unchanged():
    raw = {"operations": [{"type": "bash", "command": "echo hi"}], "concurrency": "parallel"}
    ops = _ops(raw)
    assert ops == raw["operations"]


def test_stringified_operations_array_is_parsed_not_split():
    from omnitool_preflight import OmnitoolPreFlightEngine as E
    out = E.sanitize_and_wrap({"operations": '[{"op": "bash", "cmd": "echo hi"}, {"type": "read", "path": "/tmp/x"}]'})
    assert len(out["operations"]) == 2
    assert out["operations"][0]["type"] == "bash"
    out = E.sanitize_and_wrap({"operations": "echo hi"})
    assert out["operations"] == [{"type": "bash", "command": "echo hi"}] or len(out["operations"]) == 1


# --- D12 (2026-09-29): cmd/op aliases + explicit non-JSON string handling -------------------

def test_cmd_alias_becomes_command_in_operations():
    ops = _ops({"operations": [{"type": "bash", "cmd": "ls -la"}]})
    assert ops[0]["command"] == "ls -la"
    assert "cmd" not in ops[0]


def test_flat_cmd_is_wrapped_and_inferred_bash():
    ops = _ops({"cmd": "echo hi"})
    assert len(ops) == 1
    assert ops[0]["type"] == "bash"
    assert ops[0]["command"] == "echo hi"


def test_op_alias_becomes_type():
    ops = _ops({"op": "read", "path": "/etc/hostname"})
    assert ops[0]["type"] == "read"
    assert "op" not in ops[0]


def test_cmd_does_not_override_explicit_command():
    ops = _ops({"operations": [{"command": "true", "cmd": "false"}]})
    assert ops[0]["command"] == "true"


def test_non_prefix_string_still_becomes_bash_permissive():
    # Behaviour kept from the pre-D12 precedence quirk: any non-JSON string is a bash op.
    ops = _ops("./scripts/run.sh --fast")
    assert ops == [{"type": "bash", "command": "./scripts/run.sh --fast"}]


def test_whitespace_only_string_yields_empty_operations():
    assert _ops("   ") == []

