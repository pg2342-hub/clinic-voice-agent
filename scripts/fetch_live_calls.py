"""Save real calls to the agent (browser or phone) as calls/live/<call id>.json, with their audio.

These are the calls a person made, so speech recognition, interruptions and phone latency are real.
They have no scripted case, so the page shows them with what changed in the scheduler but no checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_test_calls import KEEP, _duration, download, fetch_json  # noqa: E402
from voiceagent import ROOT  # noqa: E402
from voiceagent.vapi import Vapi  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="only calls created after this ISO time")
    ap.add_argument("--min-seconds", type=float, default=20, help="skip calls shorter than this")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config" / "vapi.json").read_text())
    vapi = Vapi()
    query = f"/call?assistantId={cfg['assistant_id']}&limit=50"
    if args.since:
        query += f"&createdAtGt={args.since}"
    out_dir = ROOT / "calls" / "live"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for summary in vapi.get(query):
        if summary.get("status") != "ended" or (out_dir / f"{summary['id']}.json").exists():
            continue
        call = vapi.get(f"/call/{summary['id']}")
        dur = _duration(call)
        if dur is None or dur < args.min_seconds:
            continue
        art = call.get("artifact") or {}
        rec = art.get("recordingUrl") or (art.get("recording") or {}).get("mono", {}).get("combinedUrl")
        audio = None
        if rec:
            ext = rec.split("?")[0].rsplit(".", 1)[-1][:4] or "wav"
            audio = f"audio/live/{call['id']}.{ext}"
            download(rec, ROOT / "docs" / audio)
        result = {
            "case": None,
            "mode": "phone",
            "kind": call.get("type"),
            "prompt_version": (call.get("assistant") or {}).get("metadata", {}).get("prompt_version")
            or cfg.get("prompt_version"),
            "model": cfg.get("model"),
            "agent_call_id": call["id"],
            "started_at": call.get("startedAt"),
            "ended_reason": call.get("endedReason"),
            "duration_s": dur,
            "cost_usd": call.get("cost"),
            "audio": audio,
            "messages": [{k: m[k] for k in KEEP if k in m} for m in art.get("messages", []) if m.get("role") != "system"],
            "performance": art.get("performanceMetrics") or {},
            "state": fetch_json(f"{cfg['worker_url']}/calls/{call['id']}"),
        }
        (out_dir / f"{call['id']}.json").write_text(json.dumps(result, indent=2) + "\n")
        saved += 1
        print(f"saved {call['id']}: {call.get('type')}, {dur} s", flush=True)
    print(f"{saved} new call(s)")


if __name__ == "__main__":
    main()
