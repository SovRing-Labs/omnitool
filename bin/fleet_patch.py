#!/usr/bin/env python3
# ┌─ CORE FILE · governed by control-center/core-changelog.jsonl ──────────
# │ ⚠  LOAD-BEARING. 1 logged change(s). Read the ledger before editing:
# │     bin/core-changelog show bin/fleet_patch.py
# │ Last: 2026-10-06T06:51Z · claude-seat · 8d8c85764 · KERNEL-DOGFOOD-1 (bugs #130 #125 #123 #128 #121 #124 #105): plan ...
# └─────────────────────────────────────────────
"""
fleet-patch — High-Efficiency Unified Diff Applier for Markdown & Code Files

Replaces fragile full-file rewrites and exact string replacement loops
with standard unified diff (patch) application.

Usage:
  fleet-patch "<unified_diff_string>"
  fleet-patch --file <patch_file>
  cat patch.diff | fleet-patch
  fleet-patch --target <file> "<diff_without_headers>"
"""

import sys
import os
import re
import json
import argparse
import tempfile
import subprocess
from pathlib import Path


def extract_target_paths(diff_text: str) -> list[str]:
    """Extract candidate target file paths from unified diff headers."""
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            # e.g., '--- a/path/to/file.md\t2026-09-12...' or '+++ path/to/file.md'
            part = line[4:].strip()
            # Strip trailing tab timestamp if present
            part = part.split("\t")[0].strip()
            # Strip a/ or b/ git prefixes if present
            clean_part = re.sub(r"^[ab]/", "", part)
            if clean_part and clean_part != "/dev/null" and clean_part not in paths:
                paths.append(clean_part)
    return paths


def apply_patch(diff_text: str, target: str | None = None, dry_run: bool = False) -> dict:
    if not diff_text.strip():
        return {"ok": False, "error": "Empty diff provided"}

    # Extract target file if not explicitly passed
    candidate_paths = extract_target_paths(diff_text)
    resolved_target = None

    if target:
        resolved_target = Path(target).expanduser().resolve()
    elif candidate_paths:
        for c in candidate_paths:
            p = Path(c).expanduser().resolve()
            if p.exists():
                resolved_target = p
                break
        if not resolved_target:
            # Default to first candidate even if new file
            resolved_target = Path(candidate_paths[0]).expanduser().resolve()
    else:
        return {
            "ok": False,
            "error": "Could not determine target file from diff headers (--- / +++). Please provide --target."
        }

    # Write diff to temp file
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as tf:
        tf.write(diff_text)
        temp_patch_path = tf.name

    try:
        # Determine patch strip level (-p0 vs -p1)
        # Try dry-run first
        applied = False
        last_error = ""

        candidates_cmds = []
        for p_level in ["-p0", "-p1"]:
            if resolved_target and resolved_target.exists():
                candidates_cmds.append([p_level, str(resolved_target)])
            candidates_cmds.append([p_level])

        for c_args in candidates_cmds:
            cmd = ["patch", "--batch", "--dry-run"] + c_args + ["-i", temp_patch_path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                if not dry_run:
                    real_cmd = ["patch", "--batch"] + c_args + ["-i", temp_patch_path]
                    real_res = subprocess.run(real_cmd, capture_output=True, text=True)
                    if real_res.returncode == 0:
                        applied = True
                        break
                    else:
                        last_error = real_res.stderr or real_res.stdout
                else:
                    applied = True
                    break
            else:
                last_error = res.stderr or res.stdout

        # Item 10, FLEET-KB-OMNITOOL-PROTOCOL-PLAN.md Phase 2: classic `patch`
        # requires near-exact context lines; `git apply` tolerates whitespace
        # drift and fuzzy context matching, which the plain `patch` binary
        # doesn't. Try it only after both -p0/-p1 `patch` attempts fail --
        # doesn't change behavior for the common case, adds a real fallback for
        # a diff that's drifted slightly from the target's current content.
        # KERNEL-DOGFOOD-1 (bugs #121, #124): git apply ran in the target's dir but resolved the diff's OWN header
        # path from there (target ui/src/routes/X + header b/ui/src/routes/X -> ui/src/routes/b/ui/src/routes/X), and
        # rejected hand-written hunks whose @@ line counts were off. With a single-file target, re-head the diff to
        # the target's basename (-p1) and let --recount derive the counts from the hunk bodies.
        git_patch_path, git_levels = temp_patch_path, ["-p1", "-p0"]
        if resolved_target and len(candidate_paths) <= 1:
            name = resolved_target.name
            reheaded = re.sub(r"(?m)^--- (?!/dev/null).*$", lambda _m: f"--- a/{name}", diff_text)
            reheaded = re.sub(r"(?m)^\+\+\+ (?!/dev/null).*$", lambda _m: f"+++ b/{name}", reheaded)
            if not reheaded.lstrip().startswith("---"):
                reheaded = f"--- a/{name}\n+++ b/{name}\n" + reheaded
            if not reheaded.endswith("\n"):
                reheaded += "\n"
            with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as tf2:
                tf2.write(reheaded)
                git_patch_path, git_levels = tf2.name, ["-p1"]
        if not applied and resolved_target:
            git_cwd = str(resolved_target.parent)
            # Inside a repo subdir git apply resolves paths from the repo TOP; stop discovery above git_cwd so paths
            # resolve from git_cwd, like patch(1).
            git_env = {**os.environ, "GIT_CEILING_DIRECTORIES": git_cwd}
            for p_level in git_levels:
                cmd = ["git", "apply", "--recount", "--whitespace=fix", "--check", p_level, git_patch_path]
                res = subprocess.run(cmd, capture_output=True, text=True, cwd=git_cwd, env=git_env)
                if res.returncode == 0:
                    if not dry_run:
                        real_cmd = ["git", "apply", "--recount", "--whitespace=fix", p_level, git_patch_path]
                        real_res = subprocess.run(real_cmd, capture_output=True, text=True, cwd=git_cwd, env=git_env)
                        if real_res.returncode == 0:
                            applied = True
                            break
                        else:
                            last_error = real_res.stderr or real_res.stdout
                    else:
                        applied = True
                        break
                else:
                    last_error = res.stderr or res.stdout

        if not applied:
            return {
                "ok": False,
                "target": str(resolved_target),
                "error": f"Patch application failed: {last_error.strip()}",
                "dry_run": dry_run
            }

        return {
            "ok": True,
            "target": str(resolved_target),
            "size_bytes": resolved_target.stat().st_size if resolved_target.exists() else 0,
            "dry_run": dry_run
        }

    finally:
        for tp in {temp_patch_path, locals().get("git_patch_path") or temp_patch_path}:
            if os.path.exists(tp):
                os.unlink(tp)


def main():
    parser = argparse.ArgumentParser(description="High-efficiency unified diff patch applier for markdown and code files")
    parser.add_argument("diff", nargs="?", default="", help="Unified diff text, file path, or '-' for stdin")
    parser.add_argument("--file", "-f", help="Path to patch file")
    parser.add_argument("--target", "-t", help="Target file path to patch")
    parser.add_argument("--dry-run", action="store_true", help="Test patch without applying changes")
    parser.add_argument("--output", choices=["json", "pretty"], default="json", help="Output format")

    args = parser.parse_args()

    diff_text = ""
    if args.file and Path(args.file).exists():
        diff_text = Path(args.file).read_text(encoding="utf-8")
    elif args.diff == "-" or (not args.diff and not sys.stdin.isatty()):
        diff_text = sys.stdin.read()
    elif args.diff:
        p = Path(args.diff)
        if p.exists() and p.is_file():
            diff_text = p.read_text(encoding="utf-8")
        else:
            diff_text = args.diff
    else:
        parser.print_help()
        sys.exit(1)

    result = apply_patch(diff_text=diff_text, target=args.target, dry_run=args.dry_run)

    if args.output == "pretty":
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))

    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
