import assert from "node:assert/strict";
import { test } from "node:test";

import worker, { CallState } from "../src/index.js";

// A tiny stand-in for the Durable Object namespace: one CallState per call id, storage in a Map.
function fakeEnv(extra = {}) {
  const objects = new Map();
  return {
    ...extra,
    CALLS: {
      idFromName: (name) => name,
      get(id) {
        if (!objects.has(id)) {
          const store = new Map();
          const ctx = { storage: { get: async (k) => structuredClone(store.get(k)), put: async (k, v) => store.set(k, structuredClone(v)) } };
          const obj = new CallState(ctx);
          objects.set(id, { fetch: (url, init) => obj.fetch(new Request(url, init)) });
        }
        return objects.get(id);
      },
    },
  };
}

function toolCalls(callId, calls) {
  return new Request("https://w/vapi", {
    method: "POST",
    body: JSON.stringify({
      message: {
        type: "tool-calls",
        call: { id: callId },
        toolCallList: calls.map(([id, name, args]) => ({ id, type: "function", function: { name, arguments: args } })),
      },
    }),
  });
}

test("answers Vapi tool calls with one result per call id, string or object arguments", async () => {
  const env = fakeEnv();
  const res = await worker.fetch(toolCalls("call-a", [
    ["tc1", "find_patient", { last_name: "Okafor", date_of_birth: "07/16/2001" }],
    ["tc2", "find_patient", JSON.stringify({ last_name: "Okafor", date_of_birth: "2001-07-16" })],
  ]), env);
  const body = await res.json();
  assert.deepEqual(body.results.map((r) => r.toolCallId), ["tc1", "tc2"]);
  assert.match(body.results[0].result, /pt_103/);
  assert.match(body.results[1].result, /pt_103/);
});

test("each call has its own state, readable at /calls/:id", async () => {
  const env = fakeEnv();
  await worker.fetch(toolCalls("call-a", [["t", "find_patient", { last_name: "Bell", date_of_birth: "1985-11-23" }]]), env);
  const a = await (await worker.fetch(new Request("https://w/calls/call-a"), env)).json();
  const b = await (await worker.fetch(new Request("https://w/calls/call-b"), env)).json();
  assert.deepEqual(a.verified, ["pt_102"]);
  assert.deepEqual(b.verified, []);
  assert.equal(a.log[0].tool, "find_patient");
  assert.ok(a.log[0].at);
});

test("other server messages get an empty 200, unknown paths 404", async () => {
  const env = fakeEnv();
  const r = await worker.fetch(new Request("https://w/vapi", { method: "POST", body: JSON.stringify({ message: { type: "status-update" } }) }), env);
  assert.equal(r.status, 200);
  assert.equal((await worker.fetch(new Request("https://w/nope"), env)).status, 404);
});

test("a tool secret, when set, is required", async () => {
  const env = fakeEnv({ TOOL_SECRET: "s3" });
  assert.equal((await worker.fetch(toolCalls("c", [["t", "find_patient", {}]]), env)).status, 403);
  const ok = toolCalls("c", [["t", "find_patient", {}]]);
  const withHeader = new Request(ok, { headers: { "x-tool-secret": "s3" } });
  assert.equal((await worker.fetch(withHeader, env)).status, 200);
});
