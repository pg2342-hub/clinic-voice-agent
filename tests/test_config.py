import re

import yaml

from voiceagent import ROOT
from voiceagent.config import FIRST_MESSAGE, agent_config, caller_config, prompt, prompt_version, tools


def test_every_tool_the_agent_can_call_exists_in_the_scheduler():
    js = (ROOT / "worker" / "src" / "scheduler.js").read_text()
    in_worker = set(re.findall(r"^  (\w+)\(practice, state", js, re.M))
    assert {t["name"] for t in tools()} == in_worker


def test_agent_config_points_every_tool_at_the_worker():
    cfg = agent_config("https://example.workers.dev/", tool_secret="s")
    fns = [t for t in cfg["model"]["tools"] if t["type"] == "function"]
    assert len(fns) == 8
    assert all(t["server"]["url"] == "https://example.workers.dev/vapi" for t in fns)
    assert all(t["server"]["headers"] == {"x-tool-secret": "s"} for t in fns)
    assert {"type": "endCall"} in cfg["model"]["tools"]
    assert cfg["model"]["messages"][0]["content"] == prompt()
    assert cfg["firstMessage"] == FIRST_MESSAGE
    assert cfg["maxDurationSeconds"] == 180
    assert cfg["metadata"]["prompt_version"] == prompt_version()


def test_no_secret_means_no_header():
    cfg = agent_config("https://x.dev")
    assert "headers" not in cfg["model"]["tools"][0]["server"]


def test_prompt_keeps_the_rules_the_test_calls_check():
    p = prompt()
    for rule in (
        "988",
        "transfer_to_staff",
        "Read back only the name and the slot",
        "Never give medical advice",
        "Do not ask for their date of birth first",
        '"now" | date',
    ):
        assert rule in p


def test_every_case_builds_a_caller_that_waits_and_can_hang_up():
    cases = yaml.safe_load((ROOT / "calls" / "cases.yml").read_text())
    assert len(cases) == 10 and len({c["id"] for c in cases}) == 10
    for c in cases:
        cfg = caller_config(c)
        assert cfg["firstMessageMode"] == "assistant-waits-for-user"
        assert {"type": "endCall"} in cfg["model"]["tools"]
        assert c["caller"].strip()[:40] in cfg["model"]["messages"][0]["content"]
        assert c["expect"]
