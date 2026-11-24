"""Run the ten test calls as text conversations against the live scheduler.

The agent side uses the same prompt, tools and model as the phone agent; its tool calls go to the
same Worker the phone line uses. A second model plays each caller from calls/cases.yml. There is
no audio, so speech recognition, interruptions and phone latency are not tested here; the real
phone calls (scripts/fetch_live_calls.py) cover those.

Writes calls/runs/<label>/<case>.json in the same shape as a recorded phone call, so the scorer
and the demo page read both.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceagent import ROOT
from voiceagent.config import AGENT_MODEL, CALLER_MODEL, FIRST_MESSAGE, prompt, prompt_version, tools
from voiceagent.vapi import USER_AGENT

MAX_TURNS = 18
WORDS_PER_SECOND = 2.6  # for the estimated timeline only
PRICE_IN, PRICE_OUT = 0.10, 0.50  # Claude Haiku 5.5, $ per million tokens

END_CALL = {
    "name": "end_call",
    "description": "Hang up. Use only after you have said goodbye.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def agent_system() -> str:
    """The phone prompt with its Liquid date tag filled in, as Vapi does."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = f"{now:%A, %B} {now.day}, {now:%Y}"
    text = prompt()
    start = text.index("{{")
    end = text.index("}}", start) + 2
    return text[:start] + today + text[end:]


def agent_tools() -> list[dict]:
    out = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools()]
    return out + [END_CALL]


def caller_system(case: dict) -> str:
    return (
        "You are role-playing a caller phoning a mental health clinic's scheduling line, as a test of its AI "
        "assistant. Write only what you would say out loud on the phone: short, natural turns, one thing at a "
        "time. Do not volunteer everything at once; answer what you are asked. Never say you are an AI or a test.\n\n"
        f"Your character and goal:\n{case['caller'].strip()}\n\n"
        "When your goal is done, or the assistant says goodbye, or you have been told someone will call you, "
        "say a short goodbye and use the end_call tool."
    )


class Worker:
    def __init__(self, url: str, secret: str | None) -> None:
        self.url = url.rstrip("/")
        self.headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if secret:
            self.headers["x-tool-secret"] = secret

    def _req(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=self.headers, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def run_tool(self, call_id: str, tool_id: str, name: str, args: dict) -> str:
        body = {
            "message": {
                "type": "tool-calls",
                "call": {"id": call_id},
                "toolCallList": [{"id": tool_id, "function": {"name": name, "arguments": args}}],
            }
        }
        return self._req("/vapi", body)["results"][0]["result"]

    def state(self, call_id: str) -> dict:
        return self._req(f"/calls/{call_id}")


def create(client: anthropic.Anthropic, model: dict, system: str, msgs: list, tool_list: list) -> tuple:
    t0 = time.monotonic()
    resp = client.messages.create(
        model=model["model"],
        max_tokens=1024,
        system=system,
        tools=tool_list,
        messages=msgs,
        thinking={"type": "disabled"},  # matches the phone agent (reasoning effort "none")
    )
    return resp, (time.monotonic() - t0) * 1000


def text_of(resp) -> str:
    return " ".join(b.text.strip() for b in resp.content if b.type == "text" and b.text.strip())


def run_case(client: anthropic.Anthropic, worker: Worker, case: dict, label: str) -> dict:
    call_id = f"sim-{label}-{case['id']}-{uuid.uuid4().hex[:6]}"
    system, tool_list = agent_system(), agent_tools()
    agent_msgs: list = []  # agent's view: caller lines are "user"
    caller_msgs: list = [{"role": "user", "content": FIRST_MESSAGE}]  # caller's view: agent lines are "user"
    log: list[dict] = []
    t = 0.0
    latencies: list[float] = []
    tokens_in = tokens_out = 0

    def say(role: str, text: str) -> None:
        nonlocal t
        d = max(0.8, len(text.split()) / WORDS_PER_SECOND)
        log.append({"role": role, "message": text, "secondsFromStart": round(t, 2), "duration": int(d * 1000)})
        t += d + 0.4

    say("bot", FIRST_MESSAGE)
    # the agent's first line is fixed, as on the phone
    agent_msgs.append({"role": "user", "content": "(call connected)"})
    agent_msgs.append({"role": "assistant", "content": FIRST_MESSAGE})
    ended = "max-turns"

    for _ in range(MAX_TURNS):
        # caller speaks
        resp, _ms = create(client, CALLER_MODEL, caller_system(case), caller_msgs, [END_CALL])
        line = text_of(resp)
        caller_msgs.append({"role": "assistant", "content": resp.content})
        hung_up = any(b.type == "tool_use" for b in resp.content)
        if line:
            say("user", line)
        if hung_up:
            ended = "customer-ended-call"
            break
        agent_msgs.append({"role": "user", "content": line or "(silence)"})

        # agent answers, running tools until it has spoken
        spoken: list[str] = []
        agent_hung_up = False
        while True:
            resp, ms = create(client, AGENT_MODEL, system, agent_msgs, tool_list)
            latencies.append(ms)
            t += ms / 1000
            tokens_in += resp.usage.input_tokens
            tokens_out += resp.usage.output_tokens
            agent_msgs.append({"role": "assistant", "content": resp.content})
            said = text_of(resp)
            if said:
                spoken.append(said)
                say("bot", said)
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                break
            results = []
            for b in uses:
                if b.name == "end_call":
                    agent_hung_up = True
                    out = "Call ended."
                else:
                    log.append(
                        {
                            "role": "tool_calls",
                            "secondsFromStart": round(t, 2),
                            "toolCalls": [{"id": b.id, "function": {"name": b.name, "arguments": json.dumps(b.input)}}],
                        }
                    )
                    out = worker.run_tool(call_id, b.id, b.name, b.input)
                    log.append({"role": "tool_call_result", "secondsFromStart": round(t, 2), "name": b.name, "result": out})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            agent_msgs.append({"role": "user", "content": results})
            if agent_hung_up:
                break
        caller_msgs.append({"role": "user", "content": " ".join(spoken) or "(silence)"})
        if agent_hung_up:
            ended = "assistant-ended-call"
            break

    cost = (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6
    return {
        "case": case["id"],
        "label": label,
        "mode": "text",
        "prompt_version": prompt_version(),
        "model": AGENT_MODEL["model"],
        "agent_call_id": call_id,
        "started_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "ended_reason": ended,
        "duration_s": round(t, 1),
        "cost_usd": round(cost, 4),
        "audio": None,
        "messages": log,
        "performance": {"modelLatencyAverage": round(sum(latencies) / len(latencies)) if latencies else None},
        "state": worker.state(call_id),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config" / "vapi.json").read_text())
    worker = Worker(cfg["worker_url"], os.environ.get("TOOL_SECRET") or None)
    cases = yaml.safe_load((ROOT / "calls" / "cases.yml").read_text())
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    client = anthropic.Anthropic()
    out_dir = ROOT / "calls" / "runs" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for case in cases:
        print(f"simulating: {case['id']}", flush=True)
        try:
            result = run_case(client, worker, case, args.label)
        except Exception as e:  # keep going; one failed conversation should not lose the others
            failures += 1
            print(f"  failed: {type(e).__name__}: {e}", flush=True)
            continue
        (out_dir / f"{case['id']}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"  ended: {result['ended_reason']}, {len(result['messages'])} messages", flush=True)
    if failures:
        sys.exit(f"{failures} conversation(s) failed")


if __name__ == "__main__":
    main()
