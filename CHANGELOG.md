# tarsus changelog

## 0.2.0 (2026-10-10)
## Tarsus 0.2.0 (formerly omnitool) — 2026-10-06

**Renamed.** "omnitool" collided with Microsoft OmniTool and omnitool-ai; the project is now **Tarsus** (the raptor's
leg that carries the talons). Environment variables keep their `OMNITOOL_*` / `FLEET_EXEC_*` names for compatibility.

### Safety
- **Kernel-enforced write scopes for bash** (Linux Landlock, unprivileged): every bash op — and every process it spawns —
  can write only beneath the write scopes (`control-center/write-scopes.conf` or `FLEET_EXEC_WRITE_SCOPES`) plus
  `bash_extra_write` in `control-center/tarsus.conf`. Before, write scopes applied to `write`/`patch` ops only.
  Modes `enforce` | `best-effort` (default without a conf file) | `off`, read per call. `bin/tarsus_sandbox.py`.
- **Approvals come from a human, never from the model.** A guard-blocked op runs only after a person approves its
  fingerprint: `tarsus-approve <fp>` in a terminal (needs a real TTY) or the MCP client's approval prompt
  (elicitation). One use, 15 minutes. The model-set `allow_destructive` flag alone no longer approves anything.
- **No silent cuts.** Edits refuse unless the whole file was read (fixes a truncation of files > 128 KB); every
  capped output (bash stdout, glob, grep, read_many, batch results) says that it was cut and where the rest is.

### Policy and audit
- **Policy file:** `tarsus.conf` re-levels built-in rules (`rule.<id> = block|warn|off`), adds organisation rules
  (`custom.<id> = …`) and sets the approval window; `tarsus policy show|check`. Agent self-approval is never relaxable.
- **Audit stream:** one OpenTelemetry-log-model JSON line per operation (actor, operation fingerprint and input hash,
  guard decision and approver, sandbox mode, outcome — refused batches included); `tarsus audit` reads and
  aggregates it; an OTel Collector `filelog` receiver ships it. Command text is recorded only with
  `audit_detail = summary`.

### MCP server
- New release entry `mcp/src/tarsus-server.js` with the core tool set only — `fleet_exec`, `fleet_exec_validate`,
  `read`, `read_many`, `write`, `edit`, `grep`, `glob`, `bash`, `sql` — built from `mcp/src/tarsus-core.js`, the
  same module the development fleet runs.
- Payloads travel on stdin (no `E2BIG` on writes > 128 KB); validation refusals come back as readable errors; bash
  `timeout_s` above 120 s is honoured; glob/grep share one ripgrep core ("no matches" is an empty answer).

### Setup
- `bin/tarsus doctor` checks prerequisites (finds a Node ≥ 22 even when the system node is older); `bin/tarsus init`
  writes a per-user policy (`~/.config/tarsus/`) and prints — or with `--write` merges — the MCP client entry for
  Claude Code, Claude Desktop or opencode, keeping every other configured server.
- The MCP server starts `bin/fleet-execd` on first use; the slow embedding classifier never cold-loads unless asked
  (`FLEET_EXEC_LOCAL_CLASSIFIER=1`).
- Tools carry MCP annotations (read-only vs destructive) so clients can auto-approve reads.

### Executor
- `bin/fleet-execd` resident fork server (tool calls ~200 ms → ~35 ms).
- Patch op: hand-written hunks with wrong line counts apply (`git apply --recount`); `a/` `b/` headers with an
  explicit target resolve correctly.
- One source with the development tree: no fleet paths in `bin/fleet-exec`, `bin/fleet_sql.py`,
  `bin/omnitool_guard.py`; configuration via `write-scopes.conf`, `tarsus.conf` and environment variables.

### Supply chain
- Releases are built, tested, packaged and signed in CI: the archive, a CycloneDX SBOM (`bin/tarsus-sbom`) and
  `SHA256SUMS`, each with a keyless Sigstore signature bound to the release workflow; the MCP server's dependency
  tree is pinned by `mcp/package-lock.json` (94 npm packages, all MIT / ISC / BSD).

Linux only for this release.
