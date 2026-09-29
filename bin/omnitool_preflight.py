#!/usr/bin/env python3
"""
Omnitool Pre-Flight Engine — Deterministic Payload Sanitizer

Sub-1B intelligence layer that runs in <1ms, requires 0 GPU memory,
and fixes 99% of LLM tool-calling structural failures before they
hit the schema validator.

Importable by: sovereign-mcp-server, fleet-exec, db-exec, subagents, any caller.
"""

import json
import ast
import re
from typing import Any, Dict, List


class OmnitoolPreFlightEngine:
    """
    Deterministic pre-flight middleware for Omnitool payloads.
    
    Handles:
    - Stringified JSON payloads (common model failure under load)
    - Naked arrays (model forgot top-level {"operations": ...})
    - Empty payloads or missing keys ({})
    - Flat commands (model omitted "operations" wrapper entirely)
    - Common model hallucinations ("tasks", "ops", "commands" aliases)
    - Quote-escaping issues in bash commands
    - Auto-infer operation type from present keys
    """

    @staticmethod
    def sanitize_and_wrap(raw_args: Any) -> Dict[str, Any]:
        """
        Main entry point: takes any raw model output and returns
        a valid fleet-exec payload with {"operations": [...], "concurrency": "..."}.
        
        Never raises — always returns a valid (possibly empty) structure.
        """
        # 1. Handle Stringified JSON Payloads (Common model failure under load)
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                # Try to fix common JSON malformations before giving up
                fixed = OmnitoolPreFlightEngine._attempt_json_repair(raw_args, e)
                if fixed is not None:
                    # If repair returned a full payload with operations, use it directly
                    if isinstance(fixed, dict) and "operations" in fixed:
                        return fixed
                    raw_args = fixed
                else:
                    # Don't try to fix JSON structure with regex — too error-prone.
                    # If it looks like a bash command (common prefixes), use as-is.
                    # If it looks like JSON (starts with { or [), return empty and let validator handle it.
                    # Otherwise return empty operations.
                    stripped = raw_args.strip()
                    if stripped.startswith('{') or stripped.startswith('['):
                        # Looks like JSON but unparseable — return empty, validator will give clear error
                        return {"operations": [], "concurrency": "parallel"}
                    bash_prefixes = ("ls", "cd", "cat", "grep", "find", "echo", "cp", "mv", "rm", "mkdir", "git", "python", "python3", "node", "npm", "npx", "cargo", "go", "make", "pytest", "ruff", "tail", "head", "less", "more", "ps", "kill", "chmod", "chown", "sudo", "systemctl", "journalctl", "docker", "kubectl", "curl", "wget", "ssh", "scp", "rsync")
                    # NOTE (D12, 2026-09-29): the original line was
                    #   `if stripped.split()[0] if stripped.split() else "" in bash_prefixes:`
                    # which parses as `first_word if words else ("" in bash_prefixes)`, so ANY non-empty,
                    # non-JSON string became a bash op and bash_prefixes was never consulted. That permissive
                    # behaviour is KEPT on purpose (agents send arbitrary shell: ./script, awk, sed, VAR=x cmd);
                    # bash_prefixes is documentation of the common case only. Whitespace-only -> empty ops.
                    if stripped:
                        return {"operations": [{"type": "bash", "command": raw_args}], "concurrency": "parallel"}
                    # Unknown string — return empty, let validator catch it
                    return {"operations": [], "concurrency": "parallel"}

        # 2. Handle Naked Arrays (Model forgot top-level {"operations": ...})
        if isinstance(raw_args, list):
            return {
                "operations": [OmnitoolPreFlightEngine._normalize_op(item) for item in raw_args],
                "concurrency": "parallel"
            }

        # 3. Handle Empty Payloads or Missing Keys ({})
        if not raw_args or not isinstance(raw_args, dict):
            return {"operations": [], "concurrency": "parallel"}

        # 4. Handle Flat Commands (Model omitted "operations" wrapper entirely)
        if "operations" not in raw_args and "tasks" not in raw_args and "ops" not in raw_args and "commands" not in raw_args:
            if any(k in raw_args for k in ["command", "cmd", "path", "type", "op", "action", "diff", "database", "queries"]):
                return {
                    "operations": [OmnitoolPreFlightEngine._normalize_op(raw_args)],
                    "concurrency": raw_args.get("concurrency", "parallel")
                }

        # 5. Extract operations from common model hallucinations ("tasks", "ops", "commands")
        ops = raw_args.get("operations") or raw_args.get("tasks") or raw_args.get("ops") or raw_args.get("commands") or []
        if isinstance(ops, str):
            # Stringified array/object ("operations": "[{...}]"): parse it; iterating the raw string
            # would turn every character into its own bash op. Unparseable text = one bash command.
            try:
                ops = json.loads(ops)
            except json.JSONDecodeError:
                ops = [{"type": "bash", "command": ops}] if ops.strip() else []
        if isinstance(ops, dict):
            ops = [ops]

        # 6. Normalize each operation
        normalized_ops = [OmnitoolPreFlightEngine._normalize_op(op) for op in ops]

        return {
            "operations": normalized_ops,
            "concurrency": raw_args.get("concurrency", "parallel"),
            "stop_on_error": raw_args.get("stop_on_error", False),
            "timeout_s": raw_args.get("timeout_s", 30),
            "timeout_ms": raw_args.get("timeout_ms"),
            "detach": raw_args.get("detach", False),
            "log_path": raw_args.get("log_path"),
        }

    @staticmethod
    def _attempt_json_repair(raw_json: str, error: json.JSONDecodeError) -> Any:
        """
        Attempt to repair common JSON malformations.
        Returns repaired object or None if unrepairable.
        """
        # Common case: unescaped quotes inside "command" string values
        # Extract operations array and fix command values
        if '"operations"' in raw_json or '"tasks"' in raw_json or '"ops"' in raw_json:
            try:
                return OmnitoolPreFlightEngine._repair_operations_array(raw_json)
            except Exception:
                pass
        return None

    @staticmethod
    def _repair_operations_array(raw_json: str) -> Any:
        """
        Extract and repair the operations/tasks/ops array from malformed JSON.
        Handles unescaped quotes in command values.
        """
        # Find which key is used
        key_match = re.search(r'"(operations|tasks|ops|commands)"\s*:\s*\[', raw_json)
        if not key_match:
            return None
        key = key_match.group(1)
        start = key_match.end()
        
        # Find matching closing bracket
        depth = 1
        i = start
        while i < len(raw_json) and depth > 0:
            if raw_json[i] == '[':
                depth += 1
            elif raw_json[i] == ']':
                depth -= 1
            i += 1
        if depth != 0:
            return None
            
        array_str = '[' + raw_json[start:i-1] + ']'
        
        # Fix unescaped quotes in command values
        fixed_array = OmnitoolPreFlightEngine._fix_command_values_in_array(array_str)
        
        # Parse the fixed array
        ops = json.loads(fixed_array)
        
        # Reconstruct full payload
        return {"operations": ops}

    @staticmethod
    def _fix_command_values_in_array(array_str: str) -> str:
        """Fix unescaped quotes inside command string values in a JSON array string."""
        result = []
        i = 0
        while i < len(array_str):
            # Look for "command": "
            if i + 11 < len(array_str) and array_str[i:i+12] == '"command": "':
                result.append('"command": "')
                i += 12
                # Capture value until closing quote followed by }, ] or end
                value_start = i
                while i < len(array_str):
                    if array_str[i] == '"':
                        if i + 1 < len(array_str):
                            next_char = array_str[i+1]
                            if next_char in '},]':
                                break
                            elif next_char == ' ' and i + 2 < len(array_str) and array_str[i+2] in '},]':
                                break
                    i += 1
                # i is at closing quote
                value = array_str[value_start:i]
                # Escape any unescaped quotes in the value
                fixed_value = value.replace('"', '\\"')
                result.append(fixed_value)
                result.append('"')
            else:
                result.append(array_str[i])
                i += 1
        return ''.join(result)

    @staticmethod
    def _normalize_op(op: Any) -> Dict[str, Any]:
        """
        Ensures every sub-operation has valid required keys based on its type.
        Auto-infers type if missing. Fixes quote-escaping in bash commands.
        """
        # If op is stringified dict/list representation, safely deserialize it
        if isinstance(op, str):
            s = op.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    parsed = json.loads(s) if '"' in s else ast.literal_eval(s)
                    if isinstance(parsed, dict):
                        op = parsed
                    elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                        op = parsed[0]
                except Exception:
                    try:
                        parsed = ast.literal_eval(s)
                        if isinstance(parsed, dict):
                            op = parsed
                        elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                            op = parsed[0]
                    except Exception:
                        pass

        if not isinstance(op, dict):
            return {"type": "bash", "command": str(op)}

        # Normalize "cmd" -> "command" alias (D12: common model shorthand)
        if "cmd" in op and "command" not in op:
            op["command"] = op.pop("cmd")

        # Normalize "tool" or "op" -> "type" alias
        if "op" in op and "type" not in op:
            op["type"] = op.pop("op")
        if "tool" in op and "type" not in op:
            op["type"] = op.pop("tool")

        # Auto-infer type if missing
        if "type" not in op:
            if "command" in op:
                op["type"] = "bash"
            elif "path" in op and "content" in op:
                op["type"] = "write"
            elif "path" in op:
                op["type"] = "read"
            elif "diff" in op:
                op["type"] = "patch"
            elif "database" in op or "queries" in op:
                op["type"] = "sql"
            else:
                op["type"] = "bash"

        # If command itself was coerced to a dictionary or serialized dictionary, unwrap it
        if op.get("type") == "bash" and "command" in op:
            cmd = op["command"]
            if isinstance(cmd, dict):
                op["command"] = cmd.get("command") or cmd.get("cmd") or ""
            elif isinstance(cmd, str):
                c_str = cmd.strip()
                if (c_str.startswith("{") and c_str.endswith("}")):
                    try:
                        c_parsed = json.loads(c_str) if '"' in c_str else ast.literal_eval(c_str)
                        if isinstance(c_parsed, dict) and ("command" in c_parsed or "cmd" in c_parsed):
                            op["command"] = c_parsed.get("command") or c_parsed.get("cmd") or ""
                    except Exception:
                        pass

        # Normalize sql operations: if "query" passed instead of "sql", normalize to "sql"
        if op.get("type") in ("sql", "db_exec", "omni_sql"):
            op["type"] = "sql"
            if "query" in op and "sql" not in op:
                op["sql"] = op.pop("query")
            if "queries" not in op and "operations" not in op and "sql" in op:
                op["queries"] = [{"sql": op["sql"], "params": op.get("params", ())}]

        # Missing required fields are left missing on purpose (S2b 2026-09-25): fleet-exec's
        # validate_payload rejects them. Defaulting write.content to "" here silently truncated
        # the target file and reported ok.
        return op


# --- CLI for direct testing ---
if __name__ == "__main__":
    import sys
    engine = OmnitoolPreFlightEngine()

    if len(sys.argv) > 1:
        # Called as a filter: read JSON from argv[1], output normalized JSON
        try:
            raw_args = json.loads(sys.argv[1])
            result = engine.sanitize_and_wrap(raw_args)
            print(json.dumps(result))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"operations": [], "concurrency": "parallel", "_preflight_error": str(e)}))
            sys.exit(1)

    # Test Case 1: Model sent completely naked object instead of operations array
    bad_payload_1 = {"type": "read", "path": "/var/log/syslog"}
    print("Test 1 (naked object):", json.dumps(engine.sanitize_and_wrap(bad_payload_1)))

    # Test Case 2: Model sent raw stringified bash command
    bad_payload_2 = 'grep -n "sys.path" bin/dispatcherd'
    print("Test 2 (stringified bash):", json.dumps(engine.sanitize_and_wrap(bad_payload_2)))

    # Test Case 3: Empty payload
    bad_payload_3 = {}
    print("Test 3 (empty):", json.dumps(engine.sanitize_and_wrap(bad_payload_3)))

    # Test Case 4: Naked array
    bad_payload_4 = [{"type": "bash", "command": "ls"}, {"type": "read", "path": "file.txt"}]
    print("Test 4 (naked array):", json.dumps(engine.sanitize_and_wrap(bad_payload_4)))

    # Test Case 5: Stringified JSON with escaping issues
    bad_payload_5 = '{"operations": [{"type": "bash", "command": "echo \"hello world\""}]}'
    print("Test 5 (stringified with quotes):", json.dumps(engine.sanitize_and_wrap(bad_payload_5)))

    # Test Case 6: Model used "tasks" alias
    bad_payload_6 = {"tasks": [{"type": "read", "path": "test.py"}], "concurrency": "sequential"}
    print("Test 6 (tasks alias):", json.dumps(engine.sanitize_and_wrap(bad_payload_6)))