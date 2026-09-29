#!/usr/bin/env python3
"""
fleet_semantic_validate.py — Semantic validation layer for fleet-exec.

Analyzes operation payloads for structural conflicts and inefficiencies
BEFORE any operations execute. Integrates with fleet-exec validate_payload().

Checks performed:
  1. RACE       — parallel ops reading/writing the same file path
  2. STALE_READ — op B reads path X but a LATER op A writes X (no ordering)
  3. CYCLE      — cyclic dependency chain in sequential mode
  4. EMPTY_OP   — ops that do nothing useful (bash no cmd, empty write content)
  5. SIZE       — total payload exceeds 50 KB

Usage:
    import fleet_semantic_validate as sv
    warnings = sv.analyze(payload)
"""

import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple


MAX_PAYLOAD_BYTES = 50 * 1024  # 50 KiB

_bash_redirect_re = re.compile(r'(?:(?<=\s)|(?<=^))(\>\>?)(\s*)([^=&|;\n]+)')


def _norm_path(p: str) -> str:
    if not p:
        return ""
    import os
    return os.path.normpath(os.path.expanduser(p))


# ── Main entry point ────────────────────────────────────────────────

def analyze(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    warnings: List[Dict[str, Any]] = []

    if not isinstance(payload, dict):
        return warnings

    operations = payload.get("operations") or payload.get("tasks") or []
    if not isinstance(operations, list):
        return warnings

    concurrency = payload.get("concurrency", "parallel")

    # Build per-path access maps
    writes: Dict[str, List[int]] = defaultdict(list)
    reads: Dict[str, List[int]] = defaultdict(list)

    for idx, op in enumerate(operations):
        if not isinstance(op, dict):
            continue
        t = op.get("type") or op.get("tool", "")
        if t == "write":
            p = _norm_path(op.get("path", ""))
            if p:
                writes[p].append(idx)
        elif t == "read":
            p = _norm_path(op.get("path", ""))
            if p:
                reads[p].append(idx)
        elif t == "patch":
            target = _norm_path(op.get("target")) or ""
            if target:
                writes[target].append(idx)
        elif t == "bash":
            # Detect bash output redirects: >file >>file 2>file
            cmd = op.get("command", "") or ""
            for m in _bash_redirect_re.finditer(cmd):
                tgt = _norm_path(m.group(3).strip())
                if tgt and not tgt.startswith("&"):
                    writes[tgt].append(idx)

    # Check 1: Races (any mode)
    _check_races(reads, writes, concurrency, warnings)

    # Check 2: Stale reads (sequential mode especially)
    _check_stale_reads(reads, writes, operations, warnings)

    # Check 3: Cycles (sequential mode)
    if concurrency == "sequential":
        _check_cycles(reads, writes, operations, warnings)

    # Check 4: Empty ops
    _check_empty_ops(operations, warnings)

    # Check 5: Size
    _check_size(payload, warnings)

    return warnings


# ── Individual checks ───────────────────────────────────────────────

def _check_races(reads, writes, concurrency, warnings):
    all_paths = set(reads.keys()) | set(writes.keys())
    for path in sorted(all_paths):
        writers = writes.get(path, [])
        readers = reads.get(path, [])
        if len(writers) >= 2:
            warnings.append({
                "severity": "RACE",
                "category": "RACE",
                "message": f"Write conflict on '{path}' at ops [{', '.join(str(i) for i in writers)}]",
                "details": {"path": path, "writers": writers, "conflict": "write-write"},
            })
        elif writers and readers:
            for r_idx in readers:
                other_writers = [w for w in writers if w != r_idx]
                if other_writers:
                    warnings.append({
                        "severity": "RACE",
                        "category": "RACE",
                        "message": f"Read/write conflict on '{path}': op[{r_idx}] reads while op{other_writers} write",
                        "details": {"path": path, "reader": r_idx, "writers": other_writers, "conflict": "read-write"},
                    })


def _check_stale_reads(reads, writes, operations, warnings):
    """Flag: reader executes before its writer (reader index < writer index)."""
    for path in sorted(reads.keys()):
        writers = writes.get(path, [])
        readers = reads.get(path, [])
        for r_idx in readers:
            if r_idx in writers:
                continue
            following_writers = sorted([w for w in writers if w > r_idx])
            if not following_writers:
                continue
            # Reader has explicit deps covering all following writers? → not stale
            reader_op = operations[r_idx] if r_idx < len(operations) else {}
            dep_set = _extract_deps(reader_op)
            if dep_set >= set(following_writers):
                continue
            warnings.append({
                "severity": "STALE_READ",
                "category": "STALE_READ",
                "message": f"Stale read on '{path}': op[{r_idx}] runs before writer ops {following_writers} — add depends_on",
                "details": {"path": path, "reader": r_idx, "post_writers": following_writers, "suggestion": "add depends_on referencing " + str(following_writers)},
            })


def _extract_deps(op):
    raw = op.get("depends_on") or op.get("requires") or []
    deps = set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [raw]
    if isinstance(raw, list):
        for d in raw:
            try:
                deps.add(int(d))
            except (ValueError, TypeError):
                pass
    return deps


def _check_cycles(reads, writes, operations, warnings):
    """Detect cycles via DFS on combined implicit + explicit dependency graph."""
    n = len(operations)
    adj: Dict[int, Set[int]] = defaultdict(set)

    for idx, op in enumerate(operations):
        if not isinstance(op, dict):
            continue
        t = op.get("type") or op.get("tool", "")

        # Implicit deps: read-after-write
        if t == "read":
            p = _norm_path(op.get("path", ""))
            if p:
                for w_idx in writes.get(p, []):
                    if w_idx != idx:
                        adj[idx].add(w_idx)

        # Explicit deps
        deps = _extract_deps(op)
        for d in deps:
            if 0 <= d < n:
                adj[idx].add(d)

    # DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in range(n)}
    found: List[List[int]] = []

    def _dfs(u, stack):
        color[u] = GRAY
        stack.append(u)
        for v in adj.get(u, set()):
            if color[v] == GRAY:
                ci = stack.index(v)
                found.append(list(stack[ci:]))
            elif color[v] == WHITE:
                _dfs(v, stack)
        stack.pop()
        color[u] = BLACK

    for i in range(n):
        if color[i] == WHITE:
            _dfs(i, [])

    seen_cycles = set()
    for cyc in found:
        key = tuple(sorted(cyc))
        if key not in seen_cycles:
            seen_cycles.add(key)
            types = [(operations[i] or {}).get("type", "?") for i in cyc]
            warnings.append({
                "severity": "CYCLE",
                "category": "CYCLE",
                "message": f"Cyclic dependency: {' → '.join(f'op{i}({t})' for i, t in zip(cyc, types))} back to start",
                "details": {"cycle_ops": cyc, "types": types, "suggestion": "break cycle by removing redundant depends_on"},
            })


def _check_empty_ops(operations, warnings):
    for idx, op in enumerate(operations):
        if not isinstance(op, dict):
            continue
        t = op.get("type") or op.get("tool", "")

        if t == "bash":
            cmd = (op.get("command") or "").strip()
            if not cmd or cmd.strip() in ("true", ":", ";", "exit 0"):
                warnings.append({
                    "severity": "NOTE",
                    "category": "EMPTY_OP",
                    "message": f"op[{idx}]: bash with no effective command — likely no-op",
                    "details": {"op_index": idx, "reason": "empty or trivially-empty command"},
                })
        elif t == "write":
            content = op.get("content", None)
            if content is None or content == "" or content == "{}" or content == "[]":
                warnings.append({
                    "severity": "NOTE",
                    "category": "EMPTY_OP",
                    "message": f"op[{idx}]: write to '{op.get('path', '?')}' with empty/null content",
                    "details": {"op_index": idx, "path": op.get("path", ""), "reason": "empty content"},
                })
        elif t == "patch":
            diff = (op.get("diff") or "").strip()
            if not diff or diff == "no changes":
                warnings.append({
                    "severity": "NOTE",
                    "category": "EMPTY_OP",
                    "message": f"op[{idx}]: patch with empty/no-op diff against '{op.get('target', '?')}'",
                    "details": {"op_index": idx, "target": op.get("target", ""), "reason": "empty diff"},
                })
        elif t in ("sql", "db_exec", "omni_sql"):
            queries = op.get("queries") or op.get("operations") or []
            if not queries:
                warnings.append({
                    "severity": "NOTE",
                    "category": "EMPTY_OP",
                    "message": f"op[{idx}]: sql/db_exec with no queries specified",
                    "details": {"op_index": idx, "database": op.get("database", ""), "reason": "empty query list"},
                })


def _check_size(payload, warnings):
    raw = json.dumps(payload)
    size = len(raw.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        warnings.append({
            "severity": "SUGGEST",
            "category": "SIZE",
            "message": f"Payload ({size} bytes) exceeds {MAX_PAYLOAD_BYTES}-byte limit — suggest splitting",
            "details": {"payload_bytes": size, "limit_bytes": MAX_PAYLOAD_BYTES, "over_by": size - MAX_PAYLOAD_BYTES},
        })
