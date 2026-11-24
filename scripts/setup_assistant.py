"""Create or update the clinic agent in Vapi, and (with --phones) the two numbers the test calls use.

    python scripts/setup_assistant.py --worker-url https://clinic-voice-agent.<you>.workers.dev [--phones]

Writes config/vapi.json (ids and numbers only, nothing secret). Needs VAPI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceagent import ROOT
from voiceagent.config import AGENT_NAME, agent_config, prompt_version
from voiceagent.vapi import Vapi

CONFIG = ROOT / "config" / "vapi.json"


def load_config() -> dict:
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def upsert_assistant(vapi: Vapi, body: dict) -> dict:
    existing = [a for a in vapi.get("/assistant?limit=100") if a.get("name") == AGENT_NAME]
    if existing:
        return vapi.patch(f"/assistant/{existing[0]['id']}", body)
    return vapi.post("/assistant", body)


def ensure_number(vapi: Vapi, name: str, assistant_id: str | None, area_code: str) -> dict:
    for n in vapi.get("/phone-number?limit=100"):
        if n.get("name") == name:
            if assistant_id and n.get("assistantId") != assistant_id:
                n = vapi.patch(f"/phone-number/{n['id']}", {"assistantId": assistant_id})
            return n
    body = {"provider": "vapi", "name": name, "numberDesiredAreaCode": area_code}
    if assistant_id:
        body["assistantId"] = assistant_id
    return vapi.post("/phone-number", body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker-url", required=True)
    ap.add_argument("--phones", action="store_true", help="also set up the agent line and the test-caller line")
    ap.add_argument("--area-code", default="702")
    args = ap.parse_args()

    vapi = Vapi()
    assistant = upsert_assistant(vapi, agent_config(args.worker_url, os.environ.get("TOOL_SECRET") or None))
    cfg = load_config()
    cfg.update(
        {
            "assistant_id": assistant["id"],
            "model": assistant.get("model", {}).get("model"),
            "prompt_version": prompt_version(),
            "worker_url": args.worker_url.rstrip("/"),
        }
    )
    if args.phones:
        agent_line = ensure_number(vapi, "clinic-agent-line", assistant["id"], args.area_code)
        caller_line = ensure_number(vapi, "test-caller-line", None, args.area_code)
        cfg.update(
            {
                "agent_number_id": agent_line["id"],
                "agent_number": agent_line.get("number"),
                "caller_number_id": caller_line["id"],
                "caller_number": caller_line.get("number"),
            }
        )
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    print(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    main()
