// MCP_MAX_CHARS (2026-09-26 dogfood): 3000 suits small-model callers; a large-context seat
// raises it via the server env in .mcp.json. Invalid/unset -> 3000.
import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const MAX_CHARS = (() => { const n = Number(process.env.MCP_MAX_CHARS); return Number.isFinite(n) && n >= 1000 ? n : 3000; })();
const HEAD_LINES = 30;
// Bug #45 (2026-09-26): elided output had no way back. The full text now spills to a file
// named in the marker; page it with fleet_file_read {path, offset, max_lines}.
const SPILL_DIR = process.env.MCP_SPILL_DIR || "/tmp/mcp-spill";
const SPILL_KEEP = 200;   // newest files kept; older ones pruned on write

function spill(text) {
  try {
    mkdirSync(SPILL_DIR, { recursive: true, mode: 0o700 });
    const name = `${Date.now()}-${createHash("sha1").update(text).digest("hex").slice(0, 10)}.txt`;
    const path = join(SPILL_DIR, name);
    writeFileSync(path, text, { mode: 0o600 });
    const files = readdirSync(SPILL_DIR).filter((f) => f.endsWith(".txt")).sort();
    for (const f of files.slice(0, Math.max(0, files.length - SPILL_KEEP))) {
      try { unlinkSync(join(SPILL_DIR, f)); } catch {}
    }
    return path;
  } catch {
    return null;   // best-effort: a spill failure must never break the tool reply
  }
}
const TAIL_LINES = 15;

// Head-and-tail truncation for command output. File reads bypass this entirely --
// the reading tool's OWN max_lines/max_bytes params (fleet_file_read defaults to
// 64KB) are the real, explicit bound the caller chose; re-clipping to ~3000 chars
// here on top of that silently broke the tool's own advertised 64KB limit (2026-09-18).
export function condense(text, { isFileRead = false } = {}) {
  if (typeof text !== "string") return text;

  if (isFileRead) {
    return text;
  }

  if (text.length <= MAX_CHARS) {
    return text;
  }

  const full = spill(text);
  const lines = text.split("\n");
  if (lines.length > HEAD_LINES + TAIL_LINES) {
    // Enough lines for line-based head/tail to actually shrink the output.
    const head = lines.slice(0, HEAD_LINES);
    const tail = lines.slice(-TAIL_LINES);
    const omitted = lines.length - head.length - tail.length;
    return [
      ...head,
      `\n… [${omitted} lines omitted, ${text.length} chars total]` +
        (full ? ` full: ${full} — fleet_file_read {path, offset: ${HEAD_LINES}, max_lines} to page` : "") + ` …\n`,
      ...tail,
    ].join("\n");
  }

  // Too few lines for line-based truncation to help (e.g. one giant
  // single-line blob: minified JSON, base64, a long URL). The old logic fell
  // through to head=tail=[the whole line], which duplicated it instead of
  // shrinking it -- output ended up LARGER than the input. Fall back to a
  // character-based head/tail split instead.
  const half = Math.floor(MAX_CHARS / 2);
  return text.slice(0, half) +
    `\n… [${text.length - MAX_CHARS} chars omitted]` + (full ? ` full: ${full}` : "") + ` …\n` +
    text.slice(-half);
}
