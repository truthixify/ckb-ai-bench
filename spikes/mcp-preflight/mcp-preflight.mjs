#!/usr/bin/env node
// MCP preflight checker (SPIKE, but written to be the real harness preflight).
//
// Proves ADR-0010: before scoring an MCP-enabled run, the harness calls the MCP
// server's JSON-RPC `initialize`, reads result.serverInfo.version, and HARD-FAILS
// (nonzero exit, clear message) if it does not equal the pinned version. It also
// reports the tool count and confirms the always-on deferred-loading tools
// (search_tools / search_resources) are present.
//
// Dependency-free: node 22 global fetch only. No package.json, no npm install.
//
// Config (argv overrides env):
//   MCP_URL              endpoint, e.g. https://mcp.ckbdev.com/ckbai   (argv[2])
//   MCP_PINNED_VERSION   the version we deployed and require, e.g. 1.6.12 (argv[3])
//
// Exit codes (distinct so callers/tests can assert the REASON, not just nonzero):
//   0  version matches the pin and tool surface looks sane
//   2  version MISMATCH (the ADR-0010 refusal: wrong server version)
//   3  transport/handshake failure (unreachable, non-2xx, unparseable, bad shape)
//   4  bad usage (missing URL or pinned version)

const URL = process.argv[2] || process.env.MCP_URL || "";
const PINNED = process.argv[3] || process.env.MCP_PINNED_VERSION || "";

// Streamable HTTP transport returns text/event-stream; this header is REQUIRED
// (server answers 406 without it, verified live).
const ACCEPT = "application/json, text/event-stream";
const PROTOCOL_VERSION = "2024-11-05";
const TIMEOUT_MS = 20000;

function die(code, msg) {
  console.error(`PREFLIGHT FAIL: ${msg}`);
  process.exit(code);
}

if (!URL) die(4, "no MCP_URL (argv[2] or env MCP_URL)");
if (!PINNED) die(4, "no MCP_PINNED_VERSION (argv[3] or env MCP_PINNED_VERSION)");

// The Streamable HTTP / SSE response frames the JSON-RPC payload across one or
// more `data:` lines per event. Concatenate the data lines of the FIRST event
// that parses as a JSON-RPC message carrying our id, and return it. Plain JSON
// bodies (no SSE framing) are handled too.
function parseRpc(raw, wantId) {
  const trimmed = raw.trim();
  // Non-SSE: a bare JSON object.
  if (trimmed.startsWith("{")) {
    try { return JSON.parse(trimmed); } catch { /* fall through to SSE */ }
  }
  // SSE: events separated by blank lines; data spread over `data:` lines.
  for (const event of trimmed.split(/\r?\n\r?\n/)) {
    const data = event
      .split(/\r?\n/)
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).replace(/^ /, ""))
      .join("");
    if (!data) continue;
    try {
      const msg = JSON.parse(data);
      if (msg && (wantId === undefined || msg.id === wantId)) return msg;
    } catch { /* keep scanning events */ }
  }
  return null;
}

async function rpc(method, params, id) {
  let res;
  try {
    res = await fetch(URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: ACCEPT },
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    die(3, `cannot reach ${URL} (${method}): ${e.message}`);
  }
  if (!res.ok) die(3, `${URL} returned HTTP ${res.status} for ${method}`);
  const raw = await res.text();
  const msg = parseRpc(raw, id);
  if (!msg) die(3, `unparseable JSON-RPC response for ${method}`);
  if (msg.error) {
    die(3, `${method} returned JSON-RPC error: ${JSON.stringify(msg.error)}`);
  }
  return msg.result;
}

async function main() {
  // 1) initialize handshake -> serverInfo.version (ADR-0010's pin source).
  const init = await rpc("initialize", {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: { name: "ckb-ai-bench-preflight", version: "0" },
  }, 1);

  const serverInfo = init && init.serverInfo;
  const version = serverInfo && serverInfo.version;
  if (!version) {
    die(3, `initialize result missing serverInfo.version: ${JSON.stringify(init)}`);
  }
  const serverName = serverInfo.name || "(unnamed)";
  console.log(`server: ${serverName}  version: ${version}  pinned: ${PINNED}`);

  // 2) The ADR-0010 hard gate: version MUST equal the pin, else refuse.
  if (version !== PINNED) {
    die(2, `MCP version mismatch: server reports "${version}", suite pins "${PINNED}". Refusing to score against the wrong server.`);
  }
  console.log("VERSION OK: server matches pinned version");

  // 3) Deferred-loading signal: list tools, confirm the always-on discovery
  //    tools are present and report the catalog size. (The server's initialize
  //    `instructions` document the deferred-loading contract: search_tools /
  //    search_resources are always on; other tools load on demand when invoked.)
  // Spec requires notifications/initialized before normal operation; send it.
  // (It is a notification: no id, no response expected. The server is stateless
  //  so this is belt-and-suspenders, but it keeps us spec-correct.)
  try {
    await fetch(URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: ACCEPT },
      body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch { /* notification is best-effort; tools/list below is the real probe */ }

  const list = await rpc("tools/list", {}, 2);
  const tools = (list && list.tools) || [];
  const names = new Set(tools.map((t) => t.name));
  const hasSearchTools = names.has("search_tools");
  const hasSearchResources = names.has("search_resources");
  console.log(`tools: ${tools.length}  search_tools: ${hasSearchTools}  search_resources: ${hasSearchResources}`);

  const deferredInstructions =
    typeof init.instructions === "string" &&
    /deferred loading/i.test(init.instructions);
  console.log(`deferred-loading documented in initialize.instructions: ${deferredInstructions}`);

  if (!hasSearchTools || !hasSearchResources) {
    die(3, "deferred-loading signal missing: search_tools/search_resources not in tools/list");
  }

  console.log("TOOL SURFACE OK: deferred-loading discovery tools present");
  console.log("PREFLIGHT PASS");
  process.exit(0);
}

main();
