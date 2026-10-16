> **Status: under development — pre-alpha.** Interfaces, numbers and file layout can change without notice.

# tarsus — status

Last checked: 2026-10-16, Linux laptop (Intel i5-8365U, 8 threads), on a clean copy of this repo. 8 of 11 suite tests work today; 10 of 11 can run without special hardware. Each line below names its test id in `suite.json` and the evidence file it rests on.
Quick test `python3 bin/omnitool_guard.py selftest`: PASS (`OMNITOOL-GUARD SELFTEST: PASS (62/62)`, fresh copy, empty HOME).


## Works today

- **T1** Guard self-test: 62/62 named must-block / must-pass cases — PASS 2026-10-16 (`OMNITOOL-GUARD SELFTEST: PASS (62/62)`); evidence: `TECHNICAL-DISCLOSURE-PART2.md`, `bin/omnitool_guard.py`.
- **T2** Guard verdicts: five destructive commands BLOCK, ls and pytest pass — PASS 2026-10-16 (`ls -la, pytest: ok`); evidence: `TECHNICAL-DISCLOSURE.md`, `bin/omnitool_guard.py`.
- **T3** Kernel write confinement (Landlock): writes outside the scope are denied — PASS 2026-10-16 (`6 passed, 1 skipped in 12.67s`); evidence: `TECHNICAL-DISCLOSURE-PART2.md`, `tests/test_sandbox.py`, `bin/tarsus_sandbox.py`.
- **T4** Preflight repair of malformed agent payloads (unit tests) — PASS 2026-10-16 (`27 passed, 3 warnings in 6.08s`); evidence: `TECHNICAL-DISCLOSURE.md`, `tests/test_preflight.py`, `tests/test_doctor_quotes.py`.
- **T5** Human approval, policy and audit trail tests — PASS 2026-10-16 (`25 passed, 1 skipped in 37.69s`); evidence: `README.md`, `tests/test_policy.py`, `tests/test_audit.py`, `tests/test_fleet_exec_integration.py`.
- **T6** Whole Python suite (except the model-dependent classifier tests) — PASS 2026-10-16 (`69 passed, 4 skipped, 3 warnings in 56.98s`); evidence: `Makefile`, `release-scan.txt`.
- **T7** MCP output truncation tests (node:test, no install) — PASS 2026-10-16 (`# fail 0`); evidence: `release-scan.txt`, `mcp/test/truncate.test.js`.
- **T11** Release hygiene: no secrets, no home paths, no vendored binaries — PASS 2026-10-16 (`found Vendored third-party binaries/sources: none`); evidence: `release-scan.txt`, `PROVENANCE.md`.

## Not yet tested

- **T8** MCP server tool tests (needs npm install of the MCP SDK) — external: needs something the test laptop does not have; needs: node >= 22, npm and network access to install @modelcontextprotocol/sdk

## Known failures and honest negatives

- **T9** Honest negative: the embedding classifier does not separate destructive from safe. One embedding model, a few commands. A negative about this classifier, not about learned judges in general. Re-measuring needs the model download. Recorded and re-checked 2026-10-16 (PASS); evidence: `TECHNICAL-DISCLOSURE.md`, `DOSSIER.md`
- **T10** Honest negative: preflight limits and a repair that changed meaning. Status after 0.2.0 of these two items is not stated in the repo. Recorded and re-checked 2026-10-16 (PASS); evidence: `TECHNICAL-DISCLOSURE.md`, `DOSSIER.md`

## Corrections

- No corrections touch this repo.

## How to re-check

- One file, any python3 >= 3.8: download [`sovring-suite.pyz`](https://github.com/SovRing-Labs/SovRing/releases/latest/download/sovring-suite.pyz), then `python3 sovring-suite.pyz --run tarsus` (headless) or `python3 sovring-suite.pyz` (local page on 127.0.0.1).
- From this checkout: `bash install.sh` (checks python, prepares a venv, runs the suite headless, then opens the page; `--no-gui` to skip the page).
- Quick test only: `python3 bin/omnitool_guard.py selftest`.

## Defensive publication

- `TECHNICAL-DISCLOSURE.md` — on public GitHub since 2026-09-29
- `TECHNICAL-DISCLOSURE-PART2.md` — on public GitHub since 2026-10-10
- DOI: [10.5281/zenodo.23282013](https://doi.org/10.5281/zenodo.23282013) (Zenodo, see `CITATION.cff`)
- Licence: PolyForm Small Business 1.0.0 OR PolyForm Noncommercial 1.0.0, your choice (LICENSING.md)
