#!/usr/bin/env node
// Tarsus MCP server — the public release entry (TARSUS-0 B1, 2026-10-06). Serves ONLY the core tool set from
// tarsus-core.js: fleet_exec, fleet_exec_validate, read, read_many, write, edit, grep, glob, bash, sql.
// The fleet's sovereign-kernel (src/index.js) imports the same core and adds fleet-only tools; nothing here
// knows about the fleet. Register with an MCP client: {"command": "node", "args": ["<checkout>/mcp/src/tarsus-server.js"]}.
// Env: OMNITOOL_ROOT (tool root; default = the checkout), TARSUS_MCP_USAGE_LOG (optional JSONL usage log),
// MCP_MAX_CHARS / MCP_SPILL_DIR (output condensing), TARSUS_APPROVAL_DIR (human approvals, default ~/.config/tarsus/approvals).
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
process.env.TARSUS_AUTOSTART ??= "1";   // B3: start the warm fork server on first use (no systemd needed)
const { makeCoreTools, makeHandler, ROOT } = await import("./tarsus-core.js");

export const TOOLS = makeCoreTools();
export const server = new Server({ name: "tarsus", version: "0.2.0" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: Object.entries(TOOLS).map(([name, t]) => ({ name, description: t.description || "", inputSchema: t.inputSchema || {},
    ...(t.annotations ? { annotations: t.annotations } : {}) })),
}));

export const handleToolCall = makeHandler({
  getTool: (name) => TOOLS[name], server, subset: "release", usageLogPath: process.env.TARSUS_MCP_USAGE_LOG || "",
});
server.setRequestHandler(CallToolRequestSchema, handleToolCall);

export { ROOT };
if (process.env.MCP_TEST_MODE !== "1") await server.connect(new StdioServerTransport());
