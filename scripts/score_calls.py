"""Score every recorded test call in calls/runs/<label>/ and write scores.json there."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceagent import ROOT
from voiceagent.score import score


def score_run(label: str) -> dict:
    cases = {c["id"]: c for c in yaml.safe_load((ROOT / "calls" / "cases.yml").read_text())}
    run = ROOT / "calls" / "runs" / label
    results = []
    for path in sorted(run.glob("*.json")):
        if path.name == "scores.json":
            continue
        call = json.loads(path.read_text())
        results.append(score(cases[call["case"]], call))
    out = {"label": label, "passed": sum(r["passed"] for r in results), "total": len(results), "calls": results}
    (run / "scores.json").write_text(json.dumps(out, indent=2) + "\n")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    s = score_run(ap.parse_args().label)
    print(f"{s['label']}: {s['passed']} of {s['total']} calls did their job")
    for r in s["calls"]:
        print(
            ("PASS " if r["passed"] else "MISS ")
            + r["case"]
            + "".join(f"\n   - {c['name']}: {c['why']}" for c in r["checks"] if not c["ok"])
        )
