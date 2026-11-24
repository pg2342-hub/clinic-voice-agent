// Cloudflare Worker: the tool server for the Vapi voice agent.
// POST /vapi            Vapi server messages; answers "tool-calls" with the scheduler's results.
// GET  /calls/:id       the tool log and end state of one call (used to score the recorded test calls).
// GET  /health          liveness.
// One Durable Object per call keeps that call's bookings, so callers never see each other's changes.

import practice from "../../clinic/practice.json" with { type: "json" };
import { newState, runTool } from "./scheduler.js";

const JSON_HEADERS = { "content-type": "application/json", "access-control-allow-origin": "*" };

export class CallState {
  constructor(ctx) {
    this.ctx = ctx;
  }

  async load() {
    let state = await this.ctx.storage.get("state");
    if (!state) {
      state = newState(practice, new Date());
      state.started = new Date().toISOString();
    }
    return state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "GET") return Response.json(await this.load(), { headers: JSON_HEADERS });
    if (url.pathname !== "/tool") return new Response("not found", { status: 404 });
    const { name, args } = await request.json();
    const state = await this.load();
    const out = runTool(practice, state, name, args);
    state.log[state.log.length - 1].at = new Date().toISOString();
    await this.ctx.storage.put("state", state);
    return Response.json(out, { headers: JSON_HEADERS });
  }
}

function parseArgs(raw) {
  if (!raw) return {};
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw);
    } catch {
      return {};
    }
  }
  return raw;
}

function stub(env, callId) {
  return env.CALLS.get(env.CALLS.idFromName(callId));
}

function authorized(request, env) {
  if (!env.TOOL_SECRET) return true;
  return request.headers.get("x-tool-secret") === env.TOOL_SECRET;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: JSON_HEADERS });
    if (url.pathname === "/health") return Response.json({ ok: true }, { headers: JSON_HEADERS });

    const m = url.pathname.match(/^\/calls\/([\w-]+)$/);
    if (m && request.method === "GET") {
      if (!authorized(request, env)) return new Response("forbidden", { status: 403 });
      return stub(env, m[1]).fetch("https://do/state");
    }

    if (url.pathname === "/vapi" && request.method === "POST") {
      if (!authorized(request, env)) return new Response("forbidden", { status: 403 });
      const body = await request.json();
      const msg = body.message || {};
      if (msg.type !== "tool-calls") return Response.json({}, { headers: JSON_HEADERS });
      const callId = (msg.call && msg.call.id) || "no-call-id";
      const calls = msg.toolCallList || msg.toolWithToolCallList?.map((t) => t.toolCall) || [];
      const results = [];
      for (const tc of calls) {
        const fn = tc.function || {};
        const name = fn.name || tc.name;
        const args = parseArgs(fn.arguments ?? tc.arguments);
        const res = await stub(env, callId).fetch("https://do/tool", {
          method: "POST",
          body: JSON.stringify({ name, args }),
        });
        const out = await res.json();
        results.push({ toolCallId: tc.id, result: out.result });
      }
      return Response.json({ results }, { headers: JSON_HEADERS });
    }
    return new Response("not found", { status: 404 });
  },
};
