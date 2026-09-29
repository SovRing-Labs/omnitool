# omnitool — Provenance

**Licence:** PolyForm Small Business 1.0.0 **or** PolyForm Noncommercial 1.0.0, at the user's choice (`LICENSING.md`).
**Disclosure:** published deliberately as prior art; the authors claim and file no patent on it (decision 2026-09-29).
Full description: `TECHNICAL-DISCLOSURE.md`.

## How this code was written
Written for the Sovereign Raptor agent fleet by Juan Carlos De Santiago with AI coding assistants (the fleet's own
agents), from the fleet's operating needs and its own measurements — not from anyone else's source code. No
third-party source code is included in this release. The executor and its helpers use the Python standard library
only. This release is a curated, single-commit export of the development tree (2026-09-29), not its history.

## Third-party components (none shipped; used at run or build time)
| Component | Used by | Licence | Shipped? |
|---|---|---|---|
| Python 3 standard library | everything under `bin/` | PSF-2.0 | no (runtime) |
| GNU patch (external program) | `patch` ops (`bin/fleet_patch.py` runs `patch --batch`) | GPL-3.0-or-later | no — invoked as a separate program, not linked |
| ripgrep `rg` (external program) | `grep` ops | MIT OR Unlicense | no |
| ruff / prettier / gofmt (external programs) | optional post-write formatter (`"format": true`) | MIT / MIT / BSD-3-Clause | no |
| pytest | `tests/` | MIT | no |
| sentence-transformers, NumPy, PyTorch | optional advisory classifier | Apache-2.0, BSD-3-Clause, BSD-3-Clause | no |
| `sentence-transformers/all-MiniLM-L6-v2` model | optional advisory classifier (downloaded at first use) | Apache-2.0 | no |
| FastAPI, Uvicorn, Pydantic, faiss-cpu | optional classifier daemon | MIT, BSD-3-Clause, MIT, MIT | no |
| `@modelcontextprotocol/sdk` (npm) | `mcp/` server | MIT | no (`npm install`) |
| Node.js ≥ 22 (`node:test`, `node:sqlite`) | `mcp/` and its tests | MIT | no |
| ONNX Runtime 1.18 C/C++ API | optional `interceptor/` build | MIT | **no — excluded, see below** |
| nlohmann/json 3.11.2 | optional `interceptor/` build | MIT | **no — excluded, see below** |

## Excluded from this release, and why
| Development-tree item | Size | Why excluded | How a user gets it |
|---|---|---|---|
| `onnxruntime_lib/libonnxruntime.so*` (2 files) | 15.3 MB each | vendored third-party binary | ONNX Runtime release tarball (README "Build") |
| `onnxruntime_include/*.h` (12 headers) | 0.57 MB | vendored third-party source | same tarball's `include/` |
| `onnxruntime_include/nlohmann/json.hpp` | 0.9 MB | vendored third-party source | nlohmann/json release |
| `bert_onnx/bert_interceptor.onnx` + `.onnx.data` | 0.67 MB + 90.9 MB | model of undocumented origin (producer `pytorch 2.14.0`, opset 18, inputs `input_ids`/`attention_mask`, outputs `logits`/`tanh`; sha256 `5228f591…` / `853ca16b…`); the author recalls that an AI agent session downloaded a pretrained BERT-family model (believed all-MiniLM-L6-v2, Apache-2.0) and exported it to ONNX; no record of the exact source was kept, so it is not shipped | export your own (README "Build"); the interceptor runs no inference anyway |
| `bin/omnitool_bert_server` (compiled) | 0.2 MB | build artefact | `make interceptor` |
| `bin/omnibreak.db`, `db/omnitool_daemon/fixmap.db`, `*.log`, `db/*.db` | small | runtime state / logs from the development machine | created at run time |
| `config/daemon.yaml` | 1 KB | read by nothing (documented dead config) | — |
| `swarm-bus`, `doctor`, `agent-plan`, `router.py`, `task_state.py`, `lane-verify`, `lane-health-hud`, `lane-revive-daemon`, `lane-decay-heal` | 114 KB | fleet orchestration tools (dispatch database, worker lanes), not part of the executor | — (fleet-specific) |
| MCP server's other 26 tools (commander, board, HUD, knowledge-base search, file tools, …) | — | fleet-specific | — |
| test `test_acts_leave_field_marks` | — | exercises the fleet's stigmergy field store (`weaver/marks.py`), not shipped | — |

## Changes made for the release (everything else is byte-identical to the development tree, 2026-09-29)
| File | Change | Behaviour |
|---|---|---|
| all | removed internal "CORE FILE / TIER / OWNER" governance banner comments | none |
| `bin/fleet-patch`, `bin/fleet-dependency-graph.py` | renamed to `fleet_patch.py`, `fleet_dependency_graph.py` (development tree used symlinks with these names) | none |
| `bin/fleet-exec` | default write scopes: tool root + `/tmp` (was seven directories under the developer's home plus `/tmp`); `FLEET_EXEC_WRITE_SCOPES` unchanged | narrower default |
| `bin/fleet-exec` | `FLEET_EXEC_CLASSIFIER=off` skips the advisory classifier; `OMNITOOL_INTENT_URL` overrides the daemon address (was fixed `127.0.0.1:8498`, still the default) | default unchanged |
| `bin/fleet-exec` | removed a redundant function-local `import os` in the tier-3 classifier fallback (it would have shadowed the module `os` used by the new switch) | none |
| `bin/fleet-exec`, `bin/omnibreak` | `OMNIBREAK_DB` env overrides the breaker DB path (default unchanged: next to the script) | default unchanged |
| `bin/fleet_sql.py` | SQL aliases resolve under `OMNITOOL_DB_DIR` (default `<tool root>/control-center`; were absolute paths in the developer's home) | same relative layout |
| `bin/fleet_sql.py` | post-mutation hook is `OMNITOOL_SQL_WRITE_HOOK` (was an absolute path to a fleet telemetry script); unset = no hook | hook off by default |
| `bin/omnitool_guard.py` | live-database directory = `OMNITOOL_DB_DIR` or `<tool root>/control-center` (was under the developer's home); two selftest strings rewritten without the home path; refusal and rule texts say "a human" instead of the owner's initial | same rules, 59/59 |
| `interceptor/omnitool_bert_server.cpp` | moved from `bin/`; unchanged | none |
| `mcp/src/index.js` | **curated**: only `fleet_exec` and `fleet_exec_validate` kept, same schemas/coercion/timeouts; tool root from `OMNITOOL_ROOT` or the file's location (was an absolute path); dropped the fleet's knowledge-base write gate and tool-subset switch; usage log only when `OMNITOOL_MCP_USAGE_LOG` is set; tool-built self-correct replies are now returned as-is (the development handler destructured them as process output and lost the text) | fix noted |
| `mcp/src/truncate.js` | unchanged | none |
| `tests/` | paths to the release layout; per-test private state (`OMNITOOL_DB_DIR`, `OMNIBREAK_DB`, classifier off); classifier tests SKIP without sentence-transformers; interceptor tests SKIP without the socket | — |
| `systemd/` | units use `%h/omnitool` instead of absolute home paths; logs go to the journal | — |

## Ideas this code builds on (cited, not copied)
| Idea | Source |
|---|---|
| Tool/function calling with JSON arguments; typed tool schemas | Model Context Protocol specification (modelcontextprotocol.io); JSON Schema 2020-12 |
| Circuit breaker | M. Nygard, *Release It!* (2007); M. Fowler, "CircuitBreaker" (2014) |
| Exponential backoff with jitter | AWS Architecture Blog, "Exponential Backoff and Jitter" (M. Brooker, 2015) |
| Dependency layering (topological levels) of read/write sets | Kahn's algorithm (1962); classic read/write-set hazard analysis |
| Unified diff application | GNU diffutils / patch |
| Prototype (few-shot centroid) classification over sentence embeddings | Reimers & Gurevych, Sentence-BERT (2019); Snell et al., Prototypical Networks (2017) |
| Allow-by-default command guard with named deny rules | common shell-safety practice (e.g. `rm --preserve-root`, sudo policies) |

## Checks run (2026-09-29)
| Check | Result |
|---|---|
| `make test` | guard 59/59 · pytest 30 passed, 2 skipped (interceptor socket absent) · node:test 5/5 (MCP tool tests 6/6 more with the SDK installed) |
| ScanCode, gitleaks | see `release-scan.txt` |
| Absolute paths into the development machine's home directory (grep) | 0 |
