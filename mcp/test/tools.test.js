// fleet_exec / fleet_exec_validate through the MCP tool layer (needs @modelcontextprotocol/sdk:
// `npm install` in mcp/). Runs the real bin/fleet-exec against private state.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

const state = mkdtempSync(join(tmpdir(), "omnitool-mcp-"));
new DatabaseSync(join(state, "dispatch.db")).close();
process.env.MCP_TEST_MODE = "1";
process.env.OMNITOOL_DB_DIR = state;
process.env.OMNIBREAK_DB = join(state, "omnibreak.db");
process.env.FLEET_EXEC_CLASSIFIER = "off";
process.env.FLEET_EXEC_MARKS = "0";
const { TOOLS } = await import("../src/index.js");

test("fleet_exec schema offers glob/grep and allow_destructive", () => {
  const item = TOOLS.fleet_exec.inputSchema.properties.operations.oneOf[0].items.properties;
  for (const t of ["glob", "grep"]) assert.ok(item.type.enum.includes(t), t);
  assert.ok(item.allow_destructive);
});

test("fleet_exec runs a batch and returns per-op results", async () => {
  const { stdout } = await TOOLS.fleet_exec.run({ operations: [
    { type: "bash", command: "echo mcp-ok" },
    { type: "sql", sql: "SELECT 7 AS n" },
  ] });
  const j = JSON.parse(stdout);
  assert.equal(j.ok, true);
  assert.match(j.results[0].stdout, /mcp-ok/);
});

test("fleet_exec: empty payload gets a self-correct template, nothing runs", async () => {
  const r = await TOOLS.fleet_exec.run({});
  assert.equal(r.isError, true);
  assert.match(r.content[0].text, /missing 'operations'|'operations' array is empty/);
});

test("fleet_exec: naked single op is wrapped by preflight", async () => {
  const { stdout } = await TOOLS.fleet_exec.run({ type: "bash", command: "echo wrapped" });
  assert.match(JSON.parse(stdout).results[0].stdout, /wrapped/);
});

test("fleet_exec: guard-blocked op refuses the whole batch", async () => {
  await assert.rejects(
    TOOLS.fleet_exec.run({ operations: [{ type: "bash", command: "mkfs.ext4 /dev/sdz9" }] }),
    (e) => /Nothing in this batch ran/.test(e.stdout));
});

test("fleet_exec_validate checks without running", async () => {
  const { stdout } = await TOOLS.fleet_exec_validate.run({ operations: [{ type: "bash", command: "echo x" }] });
  assert.equal(JSON.parse(stdout).ok, true);
  await assert.rejects(TOOLS.fleet_exec_validate.run({ operations: [{ type: "nope" }] }),
    (e) => /unknown type/.test(e.stdout));
});
