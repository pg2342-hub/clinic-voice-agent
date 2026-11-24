"""Builds the Vapi assistant configs: the clinic agent, and the test callers that phone it."""

from __future__ import annotations

import hashlib
import json

from . import ROOT

AGENT_NAME = "clinic-voice-agent"
AGENT_MODEL = {
    "provider": "anthropic",
    "model": "claude-haiku-5-5",
    "reasoningEffort": "none",
    "temperature": 0.2,
    "maxTokens": 400,
}
CALLER_MODEL = {
    "provider": "anthropic",
    "model": "claude-haiku-5-5",
    "reasoningEffort": "none",
    "temperature": 0.6,
    "maxTokens": 200,
}
AGENT_VOICE = {"provider": "vapi", "voiceId": "Savannah"}
CALLER_VOICE = {"provider": "vapi", "voiceId": "Rohan"}
TRANSCRIBER = {"provider": "deepgram", "model": "nova-3", "language": "en"}
MAX_SECONDS = 180
FIRST_MESSAGE = "Thanks for calling the clinic. This is the virtual assistant. How can I help you today?"


def prompt() -> str:
    return (ROOT / "clinic" / "prompt.md").read_text()


def tools() -> list[dict]:
    return json.loads((ROOT / "clinic" / "tools.json").read_text())


def prompt_version() -> str:
    """Short hash of the prompt and tools, so every recorded call says which version it ran."""
    blob = prompt() + json.dumps(tools(), sort_keys=True) + json.dumps(AGENT_MODEL, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def agent_config(worker_url: str, tool_secret: str | None = None) -> dict:
    server = {"url": worker_url.rstrip("/") + "/vapi", "timeoutSeconds": 10}
    if tool_secret:
        server["headers"] = {"x-tool-secret": tool_secret}
    return {
        "name": AGENT_NAME,
        "firstMessage": FIRST_MESSAGE,
        "firstMessageMode": "assistant-speaks-first",
        "model": {
            **AGENT_MODEL,
            "messages": [{"role": "system", "content": prompt()}],
            "tools": [{"type": "function", "function": t, "server": server} for t in tools()] + [{"type": "endCall"}],
        },
        "voice": AGENT_VOICE,
        # no name boosting: the test calls should hear names the way a real line would
        "transcriber": TRANSCRIBER,
        "maxDurationSeconds": MAX_SECONDS,
        # barge-in: two words or 0.3 s of caller speech stops the agent mid-sentence
        "stopSpeakingPlan": {"numWords": 2, "voiceSeconds": 0.3, "backoffSeconds": 1},
        "endCallMessage": "Take care. Goodbye.",
        # what the demo page's live call listens to
        "clientMessages": ["transcript", "tool-calls", "tool-calls-result", "speech-update", "status-update", "user-interrupted"],
        "metadata": {"prompt_version": prompt_version()},
    }


def caller_config(case: dict) -> dict:
    persona = case["caller"].strip()
    system = (
        "You are role-playing a caller phoning a mental health clinic's scheduling line, as a test of its AI "
        "assistant. Speak like a real person on the phone: short, natural turns, one thing at a time. Do not "
        "volunteer everything at once; answer what you are asked. Never say you are an AI or a test.\n\n"
        f"Your character and goal:\n{persona}\n\n"
        "When your goal is done, or the assistant says goodbye, or you have been told someone will call you, "
        "say a short goodbye and end the call with the endCall tool."
    )
    return {
        "name": f"caller-{case['id']}"[:40],
        "firstMessageMode": "assistant-waits-for-user",
        "model": {**CALLER_MODEL, "messages": [{"role": "system", "content": system}], "tools": [{"type": "endCall"}]},
        "voice": CALLER_VOICE,
        "transcriber": TRANSCRIBER,
        "maxDurationSeconds": MAX_SECONDS,
    }
