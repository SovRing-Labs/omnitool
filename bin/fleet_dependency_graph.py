#!/usr/bin/env python3
"""
fleet-dependency-graph — Analyze operation dependencies for optimal dispatch ordering.

Computes a topological sort of operations based on file read/write relationships,
suggesting sequential vs parallel execution for correctness and performance.

Usage:
  cat payload.json | fleet-dependency-graph      # Read from stdin
  fleet-dependency-graph --file payload.json     # Or specify file
"""

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def extract_file_refs(op: Dict[str, Any]) -> Set[str]:
    """Extract all file paths referenced by an operation."""
    refs = set()
    
    # Explicit path fields
    for field in ("path", "target"):
        val = op.get(field, "")
        if val and isinstance(val, str):
            p = Path(val).resolve()
            refs.add(str(p))
    
    # Bash command: extract /path patterns
    cmd = op.get("command", "")
    if isinstance(cmd, str):
        import re
        matches = re.findall(r'(/[a-zA-Z0-9._/-]+)', cmd)
        refs.update(Path(m).resolve() for m in matches if len(m) > 1)
    
    # SQL: database path
    db = op.get("database", "")
    if isinstance(db, str) and not db.startswith(("dispatch", "lanes",)):
        try:
            refs.add(str(Path(db).expanduser().resolve()))
        except (ValueError, TypeError):
            pass
    
    return refs


class DependencyGraph:
    """Compute read-write dependencies between operations."""
    
    def __init__(self, operations: List[Dict[str, Any]]):
        self.ops = operations
        self.write_set: Dict[str, int] = {}  # path -> first writer idx
        self.read_set: Dict[str, List[int]] = defaultdict(list)  # path -> reader indices
        self.graph: Dict[int, Set[int]] = defaultdict(set)  # deps[idx] = {dependent_idxs}
        self.adjacency: Dict[int, Set[int]] = defaultdict(set)  # write_idx -> {read_after_writes}
    
    def build(self):
        """Build dependency graph from operations list."""
        for idx, op in enumerate(self.ops):
            reads = extract_file_refs(op)
            writes = set()
            
            if op.get("type") in ("write", "patch"):
                p = op.get("path") or op.get("target", "")
                if p:
                    writes.add(str(Path(p).resolve()))
            elif op.get("type") == "bash":
                # Detect write-like commands (echo > file, tee, cp to dest)
                cmd = op.get("command", "").lower()
                import re
                redirect_targets = re.findall(r'(?:>|tee)\s+(/[a-zA-Z0-9._/-]+)', cmd)
                writes.update(Path(t).resolve() for t in redirect_targets)
            
            # Record writes -- including write-after-write: two ops writing the
            # SAME path with no read between them previously got zero ordering
            # edge at all, so they'd land in the same "parallel" layer and race
            # (found 2026-09-18 while wiring this into fleet_exec's auto mode).
            for path in writes:
                prior_writer = self.write_set.get(path)
                if prior_writer is not None and prior_writer < idx:
                    self.graph[idx].add(prior_writer)
                    self.adjacency[prior_writer].add(idx)
                self.write_set[path] = idx
            
            # Record reads and check against prior writes
            for path in reads:
                writer_idx = self.write_set.get(path)
                if writer_idx is not None and writer_idx < idx:
                    # This read depends on the prior write
                    self.graph[idx].add(writer_idx)
                    self.adjacency[writer_idx].add(idx)
                self.read_set[path].append(idx)
    
    def topological_sort(self) -> List[List[int]]:
        """Return groups of ops that can run in parallel (each group has no internal deps)."""
        if not self.graph:
            return [list(range(len(self.ops)))]  # All parallel
        
        # Compute in-degree
        in_degree = defaultdict(int)
        nodes = set(self.graph.keys()) | set(n for targets in self.graph.values() for n in targets)
        for node in nodes:
            if node not in in_degree:
                in_degree[node] = 0
        for node, deps in self.graph.items():
            for dep in deps:
                in_degree[node] += 1
        
        # Kahn's algorithm
        queue = deque([n for n in nodes if in_degree[n] == 0])
        layers = []
        while queue:
            layer = sorted(queue)  # Deterministic ordering within layer
            layers.append(layer)
            next_queue = deque()
            for node in layer:
                for dependent in self.adjacency.get(node, []):
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_queue.append(dependent)
            queue = next_queue
        
        # Add any unreachable nodes
        all_ordered = set(n for layer in layers for n in layer)
        remaining = [i for i in range(len(self.ops)) if i not in all_ordered]
        if remaining:
            layers.append(sorted(remaining))
        
        return layers
    
    def analyze(self) -> Dict[str, Any]:
        """Return full analysis including conflict detection and grouping suggestions."""
        self.build()
        layers = self.topological_sort()
        
        conflicts = []
        # Check for concurrent writes to same path
        for path, writer_idx in self.write_set.items():
            writers_for_path = [writer_idx]
            for other_idx, other_op in enumerate(self.ops):
                if other_idx != writer_idx:
                    other_refs = extract_file_refs(other_op)
                    other_writes = set()
                    if other_op.get("type") in ("write", "patch"):
                        p = other_op.get("path") or other_op.get("target", "")
                        if p:
                            other_writes.add(str(Path(p).resolve()))
                    if path in other_writes:
                        writers_for_path.append(other_idx)
            if len(writers_for_path) > 1:
                conflicts.append({
                    "path": path,
                    "writers": writers_for_path,
                    "severity": "RACE" if len(writers_for_path) > 2 else "CONFLICT"
                })
        
        return {
            "has_dependencies": len(self.graph) > 0,
            "conflicts": conflicts,
            "recommended_groups": layers,
            "can_run_parallel": len(layers) == 1,
            "sequential_deps_count": sum(len(deps) for deps in self.graph.values()),
            "max_depth": len(layers),
            "summary": f"{len(layers)} sequential group(s) needed ({sum(1 for g in layers if len(g)==1)}/{len(layers)} single-ops)"
        }


def main():
    parser = argparse.ArgumentParser(description="Fleet Dependency Graph Analyzer")
    parser.add_argument("--file", "-f", help="JSON payload file")
    args = parser.parse_args()
    
    raw = ""
    if args.file and Path(args.file).exists():
        raw = Path(args.file).read_text()
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)
    
    try:
        payload = json.loads(raw)
        if isinstance(payload, list):
            payload = {"operations": payload}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
    
    ops = payload.get("operations", [])
    analyzer = DependencyGraph(ops)
    result = analyzer.analyze()
    
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
