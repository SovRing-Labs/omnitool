// Release MCP server (src/tarsus-server.js): exposes ONLY the core tool set, no fleet tools, no fleet paths.
// Runs in the dev tree and in a release export (tests resolve everything relative to the checkout).
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

process.env.MCP_TEST_MODE = "1";
process.env.FLEET_EXEC_SOCK ??= join(tmpdir(), "tarsus-test-no-daemon.sock");
process.env.TARSUS_APPROVAL_DIR ??= join(tmpdir(), `tarsus-test-approvals-${process.pid}`);
process.env.TARSUS_AUDIT ??= "off";   // C2: never write the real audit stream from tests
process.env.TARSUS_AUTOSTART ??= "0";   // only the B3 test starts a daemon, and it stops it
const { TOOLS, handleToolCall, ROOT } = await import("../src/tarsus-server.js");

const CORE = ["fleet_exec", "fleet_exec_validate", "read", "read_many", "write", "edit", "grep", "glob", "bash", "sql"];

test("release server lists exactly the core tools", () => {
  assert.deepEqual(Object.keys(TOOLS).sort(), [...CORE].sort());
});

test("release core source holds no fleet paths or fleet-only tools", async () => {
  const src = await readFile(new URL("../src/tarsus-core.js", import.meta.url), "utf8")
    + await readFile(new URL("../src/tarsus-server.js", import.meta.url), "utf8");
  for (const bad of ["/ho" + "me/", "ai-stack", "_Pads", "commander", "kb-consulted", "dispatch.db"])   // split: the release cutter scans for that literal
    assert.ok(!src.includes(bad), `found ${bad}`);
});

test("edit round-trip through the handler (write gate is a no-op in the release)", async () => {
  const dir = await mkdtemp(join(tmpdir(), "tarsus-rel-"));
  const path = join(dir, "f.txt");
  await writeFile(path, "alpha beta", "utf8");
  const r = await handleToolCall({ params: { name: "edit", arguments: { path, old_string: "beta", new_string: "gamma" } } });
  assert.ok(!r.isError, JSON.stringify(r));
  assert.equal(await readFile(path, "utf8"), "alpha gamma");
  await rm(dir, { recursive: true, force: true });
});

test("edit refuses a missing new_string and leaves the file untouched (DL-omnitool-28: it used to write the text 'undefined')", async () => {
  const dir = await mkdtemp(join(tmpdir(), "tarsus-rel-"));
  const path = join(dir, "f.txt");
  await writeFile(path, "alpha beta", "utf8");
  for (const args of [{ path, old_string: "beta" }, { path, old_string: "beta", new_string: null }, { path, old_string: "beta", new_string: 5 }]) {
    const r = await handleToolCall({ params: { name: "edit", arguments: args } });
    assert.ok(r.isError || /^ERROR/.test(r.content[0].text), JSON.stringify(r));
  }
  assert.equal(await readFile(join(dir, "f.txt"), "utf8"), "alpha beta");
  await rm(dir, { recursive: true, force: true });
});

test("a guard-blocked op names its approval fingerprint", async () => {
  const r = await handleToolCall({ params: { name: "bash", arguments: { command: "git push --force origin x", cwd: tmpdir() } } });
  assert.match(r.content[0].text, /approval fingerprint: [0-9a-f]{16}/);
});

test("B5 tools/list carries MCP annotations: read-only tools say so, writers are destructive", async () => {
  const { server } = await import("../src/tarsus-server.js");
  const { ListToolsRequestSchema } = await import("@modelcontextprotocol/sdk/types.js");
  const handler = server._requestHandlers.get(ListToolsRequestSchema.shape.method.value);
  const { tools } = await handler({ method: "tools/list", params: {} }, {});
  const by = Object.fromEntries(tools.map((t) => [t.name, t.annotations || {}]));
  for (const ro of ["read", "read_many", "grep", "glob", "fleet_exec_validate"]) assert.equal(by[ro].readOnlyHint, true, ro);
  for (const w of ["write", "edit", "bash", "sql", "fleet_exec"]) assert.equal(by[w].destructiveHint, true, w);
});

test("B3 autostart: the release server starts the warm fork server, later calls go through it", async () => {
  const { autostartDaemon } = await import("../src/tarsus-core.js");
  const { existsSync } = await import("node:fs");
  process.env.TARSUS_AUTOSTART = "1";
  const pid = autostartDaemon();
  process.env.TARSUS_AUTOSTART = "0";
  assert.ok(pid > 0, "daemon spawned");
  try {
    for (let i = 0; i < 100 && !existsSync(process.env.FLEET_EXEC_SOCK); i++) await new Promise((r) => setTimeout(r, 100));
    assert.ok(existsSync(process.env.FLEET_EXEC_SOCK), "socket appeared");
    const t0 = Date.now();
    const r = JSON.parse((await TOOLS.bash.run({ command: "echo warm", cwd: tmpdir() })).stdout);
    assert.equal(r.data.stdout.trim(), "warm");
    assert.equal(autostartDaemon(), false, "only once per process");
    console.log(`warm call ${Date.now() - t0} ms`);
  } finally {
    try { process.kill(pid, "SIGTERM"); } catch {}
    await rm(process.env.FLEET_EXEC_SOCK, { force: true });
  }
});

test("ROOT is the checkout (holds bin/fleet-exec)", async () => {
  const { existsSync } = await import("node:fs");
  assert.ok(existsSync(join(ROOT, "bin", "fleet-exec")));
});
