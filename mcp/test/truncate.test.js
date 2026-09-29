// condense(): head-and-tail truncation of tool output (no MCP SDK needed).
// Carried from the development server's test suite.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

process.env.MCP_SPILL_DIR = mkdtempSync(join(tmpdir(), "omnitool-spill-"));
const { condense } = await import("../src/truncate.js");

test("condense: short text passes through unchanged", () => {
  assert.equal(condense("short"), "short");
});

test("condense: elided output spills in full to a private, pageable file", async () => {
  const fs = await import("node:fs");
  const big = Array.from({ length: 300 }, (_, i) => `row ${i}`).join("\n") + "z".repeat(4000);
  const out = condense(big);
  const m = out.match(/full: (\S+\.txt)/);
  assert.ok(m, "marker names the spill file");
  assert.equal(fs.readFileSync(m[1], "utf8"), big);
  assert.equal(fs.statSync(m[1]).mode & 0o777, 0o600);
});

test("condense: isFileRead bypasses truncation regardless of size", () => {
  const big = "x".repeat(70000);
  assert.equal(condense(big, { isFileRead: true }).length, 70000);
});

test("condense: single huge line (no newlines) is shrunk, not duplicated", () => {
  const big = "x".repeat(70000);
  const out = condense(big);
  assert.ok(out.length < big.length, `expected shrinkage, got ${out.length} vs ${big.length}`);
});

test("condense: normal multi-line output truncates with a real marker", () => {
  const many = Array.from({ length: 500 }, (_, i) => `line ${i} `.repeat(5)).join("\n");
  const out = condense(many);
  assert.ok(out.includes("omitted"), "expected an omitted-lines marker");
  assert.ok(out.length < many.length);
});
