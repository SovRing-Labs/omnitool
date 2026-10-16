# AGENTS.md — tarsus

For coding assistants (and people) setting this repository up. Plain steps; every command runs from the repo root.

**Status: pre-alpha, under development.** Interfaces, numbers and file layout can change without notice. Read `STATUS.md` first: what works today (each line names its test and evidence file), what is not yet tested, the known failures and the corrections.

## What it is
Safe hands for tool-calling agents: batch tool execution with payload repair, a deterministic safety guard, kernel (Landlock) write confinement, human-only approval, audit trail and an MCP server. Linux only.

## Install and check (3 commands)
```bash
git clone --depth 1 https://github.com/SovRing-Labs/tarsus && cd tarsus
# nothing to install beyond: python3>=3.10
python3 bin/omnitool_guard.py selftest   # expect: OMNITOOL-GUARD SELFTEST: PASS (62/62)
```
Needs: python3>=3.10.
Whole suite instead: `bash install.sh --no-gui` (python check → venv → every runnable `suite.json` test, headless; without `--no-gui` it then opens the local results page on 127.0.0.1).
Measured: 0.4s on a fresh clone (Intel i5-8365U laptop, 2026-10-16, exit 0).
Full suite: `make test`.

## What the tests mean
`suite.json` lists every claim this repository makes with its verify command, the expected output, what passing means, how the number was obtained and how to use it in your own work (11 tests, in `suite.json` at the repository root). `STATUS.md` says what works today, what is not yet tested and the known failures; the umbrella's `STATUS.md` has one row per repository.

## Where the evidence is
- `DOSSIER.md` — history, metrics and limits
- `TECHNICAL-DISCLOSURE.md` — the method, as dated prior art
- `CITATION.cff` — DOI 10.5281/zenodo.23282013

## Honest limits (from DOSSIER.md)
Preflight: an `operations` value that is a JSON *string* of an array is split into one-character bash ops; a top-level Python-dict string is rejected. The C++ interceptor's inference is not implemented; it answers with an explicit error by design. The advisory classifier does not discriminate destructive commands; it is advisory only.

## Licence
PolyForm Small Business 1.0.0 OR PolyForm Noncommercial 1.0.0 (free trial for evaluation). Details: `LICENSING.md`.

## Guided setup through MCP
The SovRing umbrella ships an MCP server that explains, checks readiness, clones and runs these tests: https://github.com/SovRing-Labs/SovRing (README → "Paste this to your assistant"). Zero-install docs endpoint: https://gitmcp.io/SovRing-Labs/tarsus
