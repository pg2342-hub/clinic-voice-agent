"""Place the scripted test calls: a Vapi caller assistant phones the clinic agent's number.

    python scripts/run_test_calls.py --label v1 [--only new-patient-booking,crisis-disclosure]

For each case it waits for both legs to end, then saves the agent leg (transcript, tool calls, latency,
cost) plus the scheduler's end state from the Worker to calls/runs/<label>/<case>.json, and the
recording to docs/audio/<label>/<case>.<ext>. Needs VAPI_API_KEY and config/vapi.json from setup.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceagent import ROOT
from voiceagent.config import caller_config, prompt_version
from voiceagent.vapi import USER_AGENT, Vapi

KEEP = ("role", "message", "time", "endTime", "secondsFromStart", "duration", "toolCalls", "name", "result", "toolCallId")


def wait_ended(vapi: Vapi, call_id: str, timeout: int = 420) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        call = vapi.get(f"/call/{call_id}")
        if call.get("status") == "ended" and (call.get("artifact") or {}).get("messages") is not None:
            return call
        time.sleep(5)
    raise TimeoutError(f"call {call_id} did not end within {timeout} s")


def find_agent_leg(vapi: Vapi, cfg: dict, after: str, timeout: int = 120) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        calls = vapi.get(f"/call?assistantId={cfg['assistant_id']}&createdAtGt={after}&limit=10")
        inbound = [c for c in calls if c.get("type") == "inboundPhoneCall"]
        if inbound:
            return sorted(inbound, key=lambda c: c["createdAt"])[0]
        time.sleep(4)
    raise TimeoutError("the agent never picked up the test call")


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as r:
        return json.loads(r.read())


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
        dest.write_bytes(r.read())


def run_case(vapi: Vapi, cfg: dict, case: dict, label: str) -> dict:
    after = datetime.now(UTC).isoformat()
    caller = vapi.post(
        "/call",
        {
            "phoneNumberId": cfg["caller_number_id"],
            "customer": {"number": cfg["agent_number"]},
            "assistant": caller_config(case),
        },
    )
    agent = find_agent_leg(vapi, cfg, after)
    agent = wait_ended(vapi, agent["id"])
    wait_ended(vapi, caller["id"])
    art = agent.get("artifact") or {}
    state = fetch_json(f"{cfg['worker_url']}/calls/{agent['id']}")
    rec = art.get("recordingUrl") or (art.get("recording") or {}).get("mono", {}).get("combinedUrl")
    audio = None
    if rec:
        ext = rec.split("?")[0].rsplit(".", 1)[-1][:4] or "wav"
        audio = f"audio/{label}/{case['id']}.{ext}"
        download(rec, ROOT / "docs" / audio)
    return {
        "case": case["id"],
        "label": label,
        "prompt_version": prompt_version(),
        "model": (agent.get("assistant") or {}).get("model", {}).get("model") or cfg.get("model"),
        "agent_call_id": agent["id"],
        "started_at": agent.get("startedAt"),
        "ended_reason": agent.get("endedReason"),
        "duration_s": _duration(agent),
        "cost_usd": agent.get("cost"),
        "audio": audio,
        "messages": [{k: m[k] for k in KEEP if k in m} for m in art.get("messages", []) if m.get("role") != "system"],
        "performance": art.get("performanceMetrics") or {},
        "state": state,
    }


def _duration(call: dict) -> float | None:
    try:
        s = datetime.fromisoformat(call["startedAt"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(call["endedAt"].replace("Z", "+00:00"))
        return round((e - s).total_seconds(), 1)
    except (KeyError, TypeError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config" / "vapi.json").read_text())
    cases = yaml.safe_load((ROOT / "calls" / "cases.yml").read_text())
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    vapi = Vapi()
    out_dir = ROOT / "calls" / "runs" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for case in cases:
        print(f"calling: {case['id']}", flush=True)
        try:
            result = run_case(vapi, cfg, case, args.label)
        except Exception as e:  # keep going; one failed call should not lose the others
            failures += 1
            print(f"  failed: {e}", flush=True)
            continue
        (out_dir / f"{case['id']}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"  ended: {result['ended_reason']}, {result['duration_s']} s", flush=True)
        time.sleep(3)
    sys.exit(1 if failures == len(cases) else 0)


if __name__ == "__main__":
    main()
