"""Build the demo page (docs/index.html) from the test conversations and any recorded phone calls.

    python scripts/build_demo.py --label v2 [--before v1] [--runs-dir calls/runs] [--out docs/index.html]

The page is a working tool: talk to the agent live in the browser, or open any test conversation
with its transcript (and audio, for phone calls), tool calls and what changed in the scheduler. Every number on the page
comes from calls/runs/<label>/. Then run demo_chrome.py on the output (the workflow does).
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceagent import ROOT
from voiceagent.score import CHANGE_TOOLS, score

PRACTICE = json.loads((ROOT / "clinic" / "practice.json").read_text())
VT = {k: v["label"] for k, v in PRACTICE["visit_types"].items()}
PROVIDERS = {p["id"]: p["name"] for p in PRACTICE["providers"]}
CLINICS = {k: v["label"] for k, v in PRACTICE["clinics"].items()}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_CASE = "change-of-mind"


def when(a: dict) -> str:
    from datetime import date

    d = date.fromisoformat(a["date"])
    h, m = divmod(a.get("hhmm", 0), 100)
    t = f"{(h % 12) or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}"
    where = "video" if a.get("modality") == "virtual" else CLINICS.get(a.get("clinic") or "", "")
    return f"{DAYS[d.weekday()]} {MONTHS[d.month - 1]} {d.day}, {t}, {where}"


def changes(state: dict) -> list[str]:
    out = []
    for a in state["appointments"]:
        num = int(a["id"].split("_")[1])
        label = VT.get(a["visit_type"], a["visit_type"])
        if a["status"] == "booked" and num >= 100 and a.get("from"):
            out.append(f"Moved {label} to {when(a)}")
        elif a["status"] == "booked" and num >= 100:
            out.append(f"Booked {label}: {when(a)}")
        elif a["status"] == "cancelled":
            out.append(f"Cancelled {label}: {when(a)}")
    for t in state.get("texts", []):
        out.append(f"Text to number ending {str(t.get('to', ''))[-4:]}")
    for t in state.get("transfers", []):
        out.append(f"Hand-off to staff ({t['reason'].replace('_', ' ')}): {t.get('summary', '')}")
    return out or ["Nothing in the schedule changed"]


def short(v, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def turns(call: dict) -> list[dict]:
    """The call as a list of turns: caller, agent and tool steps, with start time and length in seconds."""
    out = []
    for m in call["messages"]:
        role = m.get("role")
        t = m.get("secondsFromStart")
        if role in ("bot", "user"):
            out.append(
                {
                    "who": "agent" if role == "bot" else "caller",
                    "t": t,
                    "d": (m.get("duration") or 0) / 1000,
                    "text": m.get("message", ""),
                }
            )
        elif role == "tool_calls":
            for tc in m.get("toolCalls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    with contextlib.suppress(ValueError):
                        args = json.loads(args)
                out.append(
                    {
                        "who": "tool",
                        "t": t,
                        "name": fn.get("name", ""),
                        "args": short(args, 140),
                        "change": fn.get("name") in CHANGE_TOOLS,
                    }
                )
        elif role == "tool_call_result":
            # attach the result to the last tool step with the same name
            for step in reversed(out):
                if step["who"] == "tool" and step.get("name") == m.get("name") and "result" not in step:
                    step["result"] = short(m.get("result", ""), 220)
                    break
    return out


def load_run(runs_dir: Path, label: str, cases: dict) -> list[dict]:
    calls = []
    for path in sorted((runs_dir / label).glob("*.json")):
        if path.name == "scores.json":
            continue
        call = json.loads(path.read_text())
        case = cases[call["case"]]
        s = score(case, call)
        perf = call.get("performance") or {}
        calls.append(
            {
                "id": case["id"],
                "title": case["title"],
                "tests": case["tests"],
                "passed": s["passed"],
                "checks": s["checks"],
                "duration": call.get("duration_s"),
                "cost": call.get("cost_usd"),
                "audio": call.get("audio"),
                "turn_ms": perf.get("turnLatencyAverage") or perf.get("modelLatencyAverage"),
                "mode": call.get("mode", "phone"),
                "label": label,
                "model_ms": perf.get("modelLatencyAverage"),
                "interrupted": perf.get("numAssistantInterrupted"),
                "ended": call.get("ended_reason"),
                "turns": turns(call),
                "changes": changes(call["state"]),
                "version": call.get("prompt_version"),
                "model": call.get("model"),
            }
        )
    order = list(cases)
    calls.sort(key=lambda c: order.index(c["id"]))
    return calls


def summary(calls: list[dict]) -> dict:
    lat = [c["turn_ms"] for c in calls if c.get("turn_ms")]
    cost = [c["cost"] for c in calls if c.get("cost") is not None]
    dur = [c["duration"] for c in calls if c.get("duration")]
    return {
        "passed": sum(c["passed"] for c in calls),
        "total": len(calls),
        "turn_ms": round(sum(lat) / len(lat)) if lat else None,
        "cost": round(sum(cost) / len(cost), 3) if cost else None,
        "minutes": round(sum(dur) / 60, 1) if dur else None,
        "model": next((c["model"] for c in calls if c.get("model")), None),
    }


CSS = r"""
/* Shared demo layout from build-standards templates/starter.html */
.head{padding:8px 0 0}
.head h1{margin:40px 0 14px}
.head .lead{max-width:38em}
/* verdict: the same 25 cases under the same gate, one square per case */
.verdict{margin:36px 0 0;border:1px solid var(--rule);border-radius:16px;background:#fbfaf6;overflow:hidden}
.vhead{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px 24px;padding:18px 24px;border-bottom:1px solid var(--rule)}
.vhead h2{all:unset;font:400 22px/1.2 var(--serif)}
.vhead p{margin:0;color:var(--ink-2);font-size:15px}
.vhead p b{color:var(--ink)}
.vrow,.arow{display:grid;grid-template-columns:minmax(0,15em) minmax(0,1fr) 7.5em 5.5em;align-items:center;gap:20px;padding:22px 24px}
.vrow+.vrow{border-top:1px solid var(--rule-soft)}
.vwho b,.awho b{display:block;font:400 20px/1.2 var(--serif)}
.vwho span,.awho span{display:block;color:var(--ink-2);font-size:14px;line-height:1.4;margin-top:4px}
.strip{display:grid;grid-template-columns:repeat(25,minmax(0,1fr));gap:4px}
.cell{display:block;aspect-ratio:1;border-radius:4px;border:1.5px solid transparent;transition:transform .2s var(--ease),box-shadow .2s}
.cell.pass{background:var(--green)}
.cell.fail{background:var(--paper);border-color:var(--ink)}
.cell.crit{background:var(--ink)}
.cell:hover,.cell:focus-visible,.cell.lit{transform:translateY(-3px);box-shadow:0 6px 12px -6px rgb(27 26 23/.5)}
.vscore b{font:400 44px/1 var(--serif)}
.vscore span{display:block;color:var(--ink-2);font-size:13px;line-height:1.35;margin-top:4px}
.gate{justify-self:end;display:inline-flex;align-items:center;justify-content:center;min-height:32px;padding:0 14px;border-radius:999px;font:500 13px/1 var(--mono);letter-spacing:.06em}
.gate.go{background:var(--green-wash);color:var(--green)}
.gate.nogo{background:var(--ink);color:var(--paper)}
.legend{display:flex;flex-wrap:wrap;gap:6px 20px;padding:14px 24px;border-top:1px solid var(--rule);color:var(--ink-2);font-size:13px}
.legend span{display:inline-flex;align-items:center;gap:8px}
.legend i{width:12px;height:12px;border-radius:3px;border:1.5px solid transparent}
.legend .pass{background:var(--green)}.legend .fail{border-color:var(--ink)}.legend .crit{background:var(--ink)}
/* page body: content and a side column */
.layout{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:56px;align-items:start;margin-top:8px}
.side{position:sticky;top:24px;margin-top:64px;display:grid;gap:22px;font-size:14px;color:var(--ink-2)}
.side h3{margin:0 0 6px;font:500 13px/1.3 var(--sans);color:var(--ink)}
.side p{margin:0}
.side nav a{display:block;padding:5px 0;color:var(--ink-2);text-decoration:none;border-bottom:1px solid var(--rule-soft)}
.side nav a:hover{color:var(--ink)}
.side pre{font-size:12px;margin:6px 0 0}
.point{font-size:17px;max-width:40em;margin:0}
.point b{font-weight:500}
.seg{display:flex;width:fit-content;max-width:100%;flex-wrap:wrap;gap:2px;padding:3px;border:1px solid var(--rule);border-radius:999px;margin:0 0 16px;background:#fbfaf6}
.seg button{min-height:34px;padding:0 14px;border:0;border-radius:999px;background:transparent;color:var(--ink-2);font:500 14px/1 var(--sans);cursor:pointer}
.seg button span{font-weight:400;opacity:.8}
.seg button[aria-checked=true]{background:var(--ink);color:var(--paper)}
.n{font-family:var(--mono);font-size:13px}
.tag{font:400 12px/1 var(--mono);border:1px solid var(--rule);border-radius:999px;padding:4px 8px;white-space:nowrap}
.tx{max-width:340px}.ch{font-size:13px;color:var(--ink-2);margin-top:4px}
.v{white-space:nowrap;font-weight:500}
.v.ok{color:var(--green)}
.v.bad{white-space:normal;color:var(--ink)}
.v.bad.crit::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--ink);margin-right:6px;vertical-align:1px}
.why{font-weight:400;font-size:13px;color:var(--ink-2);margin-top:4px;max-width:240px}
#cases tr.lit td{background:var(--green-wash)}
#cases tr{scroll-margin-top:24px}
.abl{border:1px solid var(--rule);border-radius:16px;background:#fbfaf6;overflow:hidden}
.arow{grid-template-columns:minmax(0,13em) minmax(0,1fr) 6.5em}
.arow+.arow{border-top:1px solid var(--rule-soft)}
.ascore{font:400 15px/1 var(--sans);color:var(--ink-2)}
.ascore b{font:400 30px/1 var(--serif);color:var(--ink)}
.ascore span{display:block;font-size:13px;margin-top:4px}
.note{color:var(--ink-2);font-size:15px;max-width:44em;margin-top:14px}
.rule{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.rule div{border:1px solid var(--rule);border-radius:16px;padding:20px 22px;background:#fbfaf6}
.rule div:last-child{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.rule h3{margin:0 0 6px;font:400 22px/1.2 var(--serif)}
.rule p{margin:0 0 10px;font-size:15px;opacity:.85}
.rule ul{margin:0;font-size:15px}
.rule div:last-child li::marker{color:rgb(244 242 236/.6)}
.sev{font:500 12px/1 var(--mono);padding:4px 8px;border-radius:999px;border:1px solid var(--rule)}
.sev-critical{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.sev-low{color:var(--ink-2)}
@media (max-width:1000px){
 .layout{grid-template-columns:minmax(0,1fr);gap:0}
 .side{position:static;margin-top:48px;order:2}
 .vrow{grid-template-columns:minmax(0,1fr) auto;gap:14px 16px}
 .vrow .strip{grid-column:1/-1;grid-row:2}
 .vscore{grid-row:3}.vrow .gate{grid-row:3}
 .arow{grid-template-columns:minmax(0,1fr) auto;gap:12px 16px}
 .arow .strip{grid-column:1/-1;grid-row:2}
 .ascore{grid-column:2;grid-row:1}
 .rule{grid-template-columns:1fr}
}
@media (max-width:640px){.vrow,.arow{padding:18px}.vhead{padding:16px 18px}.strip{gap:3px}.tx{max-width:none}}
@media (prefers-reduced-motion:reduce){.cell{transition:none}}
/* ---- page-specific rules ---- */
.head{padding:8px 0 0}.head h1{margin:40px 0 14px}.head .lead{max-width:44em;margin:0}
.desk{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:20px 24px;align-items:start;margin-top:28px}
.phone{border:1px solid var(--rule);border-radius:16px;background:#fbfaf6;padding:20px 22px}
.phone h2,.callv h2,.callv h3{all:unset;display:block}
.phone h2{font:500 15px/1.3 var(--sans)}
.phone p{margin:6px 0 0;font-size:14px;color:var(--ink-2)}
.talk{display:flex;align-items:center;gap:12px 16px;flex-wrap:wrap;margin-top:14px}
.go{background:var(--green);border-color:var(--green);color:#fff;min-height:46px;padding:0 22px;font-size:16px}
.go:hover{background:var(--green-deep);border-color:var(--green-deep)}
.go.live{background:var(--ink);border-color:var(--ink)}
.go:disabled{opacity:.55;cursor:not-allowed}
.lstat{font-size:13.5px;color:var(--ink-2)}
.try{margin:14px 0 0;padding:0;list-style:none;font-size:13.5px;color:var(--ink-2)}
.try li{padding:3px 0}.try b{color:var(--ink);font-weight:500}
.exlab{font:500 13px/1.3 var(--sans);margin:22px 0 8px}
.exlist{display:grid;gap:6px}
.ex{display:grid;grid-template-columns:14px minmax(0,1fr) auto;align-items:center;gap:10px;width:100%;min-height:40px;padding:6px 12px;
 border:1px solid var(--rule);border-radius:12px;background:#fff;font:400 14px/1.3 var(--sans);text-align:left;cursor:pointer;color:var(--ink)}
.ex i{width:12px;height:12px;border-radius:3px;border:1.5px solid var(--ink)}
.ex i.pass{background:var(--green);border-color:var(--green)}
.ex small{font:12px/1 var(--mono);color:var(--ink-2)}
.ex[aria-pressed=true]{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.ex[aria-pressed=true] small{color:rgb(244 242 236/.7)}
.ex[aria-pressed=true] i{border-color:var(--paper)}
.callv{border:1px solid var(--rule);border-radius:16px;background:#fff;box-shadow:0 24px 48px -32px rgb(27 26 23/.35);min-width:0;overflow:hidden}
.chead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px 20px;padding:22px 26px 16px;border-bottom:1px solid var(--rule)}
.chead>div{min-width:0}
.callv h2{font:400 26px/1.15 var(--serif)}
.csub{font:12.5px/1.5 var(--mono);color:var(--ink-2);margin-top:6px}
.badge{display:inline-flex;align-items:center;min-height:30px;padding:0 14px;border-radius:999px;font:500 12.5px/1 var(--mono);letter-spacing:.04em;white-space:nowrap}
.badge.ok{background:var(--green-wash);color:var(--green)}
.badge.miss{background:var(--ink);color:var(--paper)}
.badge.live{border:1.5px dashed var(--ink)}
.cbody{padding:14px 26px 22px}
.cbody audio{width:100%;margin:4px 0 8px}
.tl{display:block;width:100%;height:auto;margin:6px 0 2px}
.tl text{font:11px var(--mono);fill:var(--ink-2)}
.tlkey{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;color:var(--ink-2);margin:0 0 10px}
.tlkey span{display:inline-flex;align-items:center;gap:6px}.tlkey i{width:14px;height:8px;border-radius:2px}
.k-agent{background:var(--green)}.k-caller{background:var(--ink-2)}.k-change{background:var(--ink)}
.tx{max-height:430px;overflow:auto;border-top:1px solid var(--rule-soft);padding-top:8px}
.turn{display:grid;grid-template-columns:3.4em minmax(0,1fr);gap:10px;padding:7px 0;font-size:14.5px;line-height:1.45}
.turn .ts{font:12px/1.9 var(--mono);color:var(--ink-2)}
.turn b{font-weight:500}
.turn.agent b{color:var(--green)}
.turn.tool{font:12.5px/1.5 var(--mono);color:var(--ink-2)}
.turn.tool code{color:var(--ink);font-weight:500}
.turn.tool.change code::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--ink);margin-right:6px;vertical-align:0}
.turn .res{display:block;overflow-wrap:anywhere}
.out{margin:14px 0 0;padding:12px 14px;border:1px solid var(--rule);border-radius:12px;background:#fbfaf6}
.out h3{font:500 14px/1.3 var(--sans);margin-bottom:6px}
.out ul{margin:0;padding-left:18px;font-size:14px}
.chk{list-style:none;padding:0!important}
.chk li{display:grid;grid-template-columns:16px minmax(0,1fr);gap:8px;padding:2px 0}
.chk i{width:10px;height:10px;border-radius:3px;margin-top:5px;border:1.5px solid var(--ink)}
.chk i.ok{background:var(--green);border-color:var(--green)}
.chk small{display:block;color:var(--ink-2)}
.empty{padding:40px 26px;color:var(--ink-2)}
.board{border:1px solid var(--rule);border-radius:16px;background:#fbfaf6;overflow:hidden}
.brow{display:grid;grid-template-columns:minmax(0,1fr) 7em 6em 5.5em;gap:16px;align-items:center;padding:14px 20px;font-size:14.5px}
.brow+.brow{border-top:1px solid var(--rule-soft)}
.brow.h{font:500 12.5px/1.3 var(--sans);color:var(--ink-2);padding:10px 20px}
.brow b{font-weight:500;display:block}.brow span{color:var(--ink-2);font-size:13px}
.brow .num{font-variant-numeric:tabular-nums;text-align:right}
.res-ok{color:var(--green);font-weight:500}.res-miss{font-weight:500}
.chg{border:1px solid var(--rule);border-radius:16px;padding:18px 22px;margin:0 0 12px}
.chg h3{margin:0 0 6px;font:400 20px/1.25 var(--serif)}
.chg p{margin:0 0 6px;font-size:15px}
.chg .before{color:var(--ink-2);font-size:14px}
.steps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.step{border:1px solid var(--rule);border-radius:14px;padding:14px 16px;font-size:14px}
.step h3{margin:0 0 4px;font:500 14.5px/1.3 var(--sans)}
.step p{margin:0;color:var(--ink-2)}
.lim{font-size:15px;max-width:44em}
@media (max-width:1000px){.desk{grid-template-columns:minmax(0,1fr)}.steps{grid-template-columns:1fr 1fr}}
@media (max-width:640px){.brow{grid-template-columns:minmax(0,1fr) auto;}.brow .hide{display:none}.chead,.cbody{padding-left:18px;padding-right:18px}
 .steps{grid-template-columns:1fr}}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mmss = t => t == null ? '' : `${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,'0')}`;

function timeline(turns, total){
  const W = 640, H = 64, pad = 4, x = t => pad + (W - 2*pad) * Math.min(t, total) / total;
  let g = '';
  for (const s of turns){
    if (s.t == null) continue;
    if (s.who === 'tool'){ if (s.change) g += `<rect x="${x(s.t)-1.5}" y="2" width="3" height="${H-22}" fill="var(--ink)"/>`; continue; }
    const y = s.who === 'agent' ? 6 : 26, w = Math.max(2, x(s.t + (s.d || 1)) - x(s.t));
    g += `<rect x="${x(s.t)}" y="${y}" width="${w}" height="14" rx="2" fill="${s.who==='agent'?'var(--green)':'var(--ink-2)'}"/>`;
  }
  const ticks = [0, total/2, total].map(t => `<text x="${Math.min(W-30, Math.max(0, x(t)-(t?12:0)))}" y="${H-2}">${mmss(t)}</text>`).join('');
  return `<svg class="tl" viewBox="0 0 ${W} ${H}" role="img" aria-label="Who spoke when, to scale">${g}${ticks}</svg>`;
}

function turnHTML(s){
  if (s.who === 'tool') return `<div class="turn tool${s.change?' change':''}"><span class="ts">${mmss(s.t)}</span><span><code>${esc(s.name)}</code> ${esc(s.args)}${s.result?`<span class="res">→ ${esc(s.result)}</span>`:''}</span></div>`;
  return `<div class="turn ${s.who}"><span class="ts">${mmss(s.t)}</span><span><b>${s.who==='agent'?'Agent':'Caller'}</b> ${esc(s.text)}</span></div>`;
}

function showCall(c){
  const total = c.duration || Math.max(...c.turns.map(s => (s.t||0) + (s.d||0)), 30);
  $('#callv').innerHTML = `
   <div class="chead"><div><h2>${esc(c.title)}</h2><div class="csub">${esc(c.tests)}</div>
     <div class="csub">${mmss(c.duration)} long${c.mode==='text'?' · text conversation':' · phone call'}${c.turn_ms?` · ${c.turn_ms} ms average ${c.mode==='text'?'model reply':'reply time'}`:''}${c.cost!=null?` · $${c.cost.toFixed(3)}`:''}</div></div>
     <span class="badge ${c.passed?'ok':'miss'}">${c.passed?'DID ITS JOB':'MISSED'}</span></div>
   <div class="cbody">
     ${c.audio?`<audio controls preload="none" src="${esc(c.audio)}"></audio>`:''}
     ${timeline(c.turns, total)}
     <div class="tlkey"><span><i class="k-agent"></i>Agent speaking</span><span><i class="k-caller"></i>Caller speaking</span><span><i class="k-change"></i>Schedule changed</span></div>
     <div class="tx">${c.turns.map(turnHTML).join('')}</div>
     <div class="out"><h3>What changed in the schedule</h3><ul>${c.changes.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>
     <div class="out"><h3>Checks</h3><ul class="chk">${c.checks.map(k=>`<li><i class="${k.ok?'ok':''}"></i><span>${esc(k.name)}${k.ok?'':`<small>${esc(k.why)}</small>`}</span></li>`).join('')}</ul></div>
   </div>`;
}

const list = $('#exlist');
D.calls.forEach(c => {
  const b = document.createElement('button');
  b.className = 'ex'; b.type = 'button'; b.setAttribute('aria-pressed', 'false'); b.dataset.id = c.id;
  b.innerHTML = `<i class="${c.passed?'pass':''}"></i><span>${esc(c.title)}</span><small>${mmss(c.duration)}</small>`;
  b.addEventListener('click', () => pick(c.id, true));
  list.appendChild(b);
});
function pick(id, scroll){
  document.querySelectorAll('.ex').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.id === id)));
  const c = D.calls.find(x => x.id === id); if (c) showCall(c);
  if (scroll && window.innerWidth < 1000) $('#callv').scrollIntoView({behavior:'smooth', block:'start'});
}
pick(D.default, false);
document.querySelectorAll('[data-open]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); pick(a.dataset.open, false); $('#tool').scrollIntoView({behavior:'smooth'}); }));

// ---- live call in the browser ----
const btn = $('#talk'), lstat = $('#lstat');
let vapi = null, live = null;
function liveView(){
  $('#callv').innerHTML = `<div class="chead"><div><h2>Live call</h2><div class="csub">Speak when the agent finishes its greeting. Calls end after 3 minutes.</div></div><span class="badge live">LIVE</span></div>
   <div class="cbody"><div class="tx" id="livetx"></div><div class="out" id="liveout" hidden></div></div>`;
  document.querySelectorAll('.ex').forEach(b => b.setAttribute('aria-pressed','false'));
}
function addTurn(s){ const tx = $('#livetx'); if (!tx) return; tx.insertAdjacentHTML('beforeend', turnHTML(s)); tx.scrollTop = tx.scrollHeight; }
async function endView(id){
  const out = $('#liveout'); if (!out) return;
  out.hidden = false; out.innerHTML = '<h3>What changed in the schedule</h3><p class="lstat">Loading…</p>';
  try {
    const st = await (await fetch(`${D.worker}/calls/${id}`)).json();
    out.innerHTML = '<h3>What changed in the schedule</h3><ul>' + liveChanges(st).map(x=>`<li>${esc(x)}</li>`).join('') + '</ul>';
  } catch { out.innerHTML = '<h3>What changed in the schedule</h3><p class="lstat">Could not reach the scheduler.</p>'; }
}
function liveChanges(st){
  const out = [];
  for (const a of st.appointments || []){
    const n = +a.id.split('_')[1];
    if (a.status==='booked' && n>=100) out.push(`${a.from?'Moved':'Booked'} ${a.visit_type.replace('_',' ')} on ${a.date} at ${String(a.hhmm).padStart(4,'0').replace(/(\d\d)(\d\d)/,'$1:$2')}`);
    if (a.status==='cancelled') out.push(`Cancelled the appointment on ${a.date}`);
  }
  for (const t of st.transfers || []) out.push(`Hand-off to staff (${t.reason.replace('_',' ')})`);
  for (const t of st.texts || []) out.push('Confirmation text sent');
  return out.length ? out : ['Nothing in the schedule changed'];
}
if (!D.public_key || !D.assistant_id){ btn.disabled = true; lstat.textContent = 'The live line is not switched on yet. The test conversations below work.'; }
btn.addEventListener('click', async () => {
  if (live){ vapi && vapi.stop(); return; }
  btn.disabled = true; lstat.textContent = 'Connecting… allow the microphone.';
  try {
    const mod = await import('https://esm.sh/@vapi-ai/web@3.0.1');
    const Vapi = mod.default.default || mod.default;
    vapi = vapi || new Vapi(D.public_key);
    if (!vapi._wired){
      vapi._wired = true; let t0 = 0;
      vapi.on('call-start', () => { t0 = performance.now(); btn.disabled = false; btn.classList.add('live'); btn.textContent = 'End call'; lstat.textContent = 'Connected. Talk normally; you can interrupt it.'; });
      vapi.on('message', m => {
        const t = (performance.now() - t0)/1000;
        if (m.type === 'transcript' && m.transcriptType === 'final') addTurn({who: m.role==='assistant'?'agent':'caller', t, text: m.transcript});
        if (m.type === 'tool-calls') for (const tc of m.toolCallList || []) addTurn({who:'tool', t, name: tc.function?.name, args: JSON.stringify(tc.function?.arguments||{}).slice(0,140), change: ['book_appointment','reschedule_appointment','cancel_appointment'].includes(tc.function?.name)});
      });
      vapi.on('call-end', () => { btn.classList.remove('live'); btn.textContent = 'Talk to the agent'; btn.disabled = false; lstat.textContent = 'Call ended.'; const id = live; live = null; if (id) endView(id); });
      vapi.on('error', e => { lstat.textContent = 'The call could not connect. Try again, or open a test conversation.'; btn.disabled = false; live = null; console.warn(e); });
    }
    liveView();
    const call = await vapi.start(D.assistant_id);
    live = call && call.id;
  } catch (e) { console.warn(e); btn.disabled = false; lstat.textContent = 'The call could not start in this browser.'; }
});
"""


def board(calls: list[dict]) -> str:
    rows = [
        '<div class="brow h"><span>Test call</span><span class="num hide">Length</span>'
        '<span class="num hide">Model reply</span><span class="num">Result</span></div>'
    ]
    for c in calls:
        dur = f"{int(c['duration'] // 60)}:{int(c['duration'] % 60):02d}" if c.get("duration") else ""
        rt = f"{c['turn_ms']} ms" if c.get("turn_ms") else ""
        res = '<span class="res-ok">Did its job</span>' if c["passed"] else '<span class="res-miss">Missed</span>'
        rows.append(
            f'<div class="brow"><div><b><a href="#tool" data-open="{c["id"]}">{html.escape(c["title"])}</a></b>'
            f'<span>{html.escape(c["tests"])}</span></div><span class="num hide">{dur}</span>'
            f'<span class="num hide">{rt}</span><span class="num">{res}</span></div>'
        )
    return '<div class="board">' + "".join(rows) + "</div>"


def changes_section(path: Path) -> str:
    """What each round of testing changed, from calls/CHANGES.yml (written after each run)."""
    if not path.exists():
        return ""
    items = yaml.safe_load(path.read_text()) or []
    out = []
    for it in items:
        out.append(
            f'<div class="chg"><h3>{html.escape(it["title"])}</h3><p class="before"><b>Heard in testing:</b> '
            f"{html.escape(it['found'])}</p><p><b>Changed:</b> {html.escape(it['changed'])}</p>"
            + (f'<p class="before"><b>After:</b> {html.escape(it["after"])}</p>' if it.get("after") else "")
            + "</div>"
        )
    return "".join(out)


def page(calls: list[dict], cfg: dict, before: dict | None, changes_html: str) -> str:
    s = summary(calls)
    data = {
        "calls": calls,
        "default": DEFAULT_CASE if any(c["id"] == DEFAULT_CASE for c in calls) else calls[0]["id"],
        "public_key": cfg.get("public_key"),
        "assistant_id": cfg.get("assistant_id"),
        "worker": cfg.get("worker_url"),
    }
    data_json = json.dumps(data).replace("</", "<\\/")
    before_line = (
        f" The first round, before the fixes below, did its job on {before['passed']} of {before['total']}." if before else ""
    )
    desc = f"A voice agent for clinic scheduling. {s['passed']} of {s['total']} test conversations did their job."
    lat = f"{s['turn_ms']} ms average model reply" if s.get("turn_ms") else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clinic Voice Agent</title>
<meta name="description" content="{html.escape(desc)}">
<style>{CSS}</style></head><body><main>
<div class="head">
<h1>Clinic Voice Agent</h1>
<p class="lead">A phone agent for a two-clinic mental health urgent care practice. It books, moves and cancels appointments in a mock scheduling system, sends confirmation texts, and hands callers to staff, with a crisis path that drops everything else. Talk to it here, or read ten test conversations.</p>
</div>

<section class="desk" id="tool" aria-label="Talk to the agent or read a test conversation">
  <div class="phone">
    <h2>Talk to the agent</h2>
    <p>Uses your microphone. Calls end after 3 minutes. The clinic, its patients and the schedule are made up.</p>
    <div class="talk"><button class="go" id="talk" type="button">Talk to the agent</button><span class="lstat" id="lstat" role="status"></span></div>
    <ul class="try"><li><b>Existing patient:</b> say you are Marcus Bell, born November 23, 1985, and move your follow-up.</li>
    <li><b>New patient:</b> book a first evaluation at the Eastside clinic.</li>
    <li><b>Same day:</b> ask to be seen today.</li></ul>
    <div class="exlab" id="exlab">Test conversations ({s["passed"]} of {s["total"]} did their job)</div>
    <div class="exlist" id="exlist" role="group" aria-labelledby="exlab"></div>
  </div>
  <article class="callv" id="callv" aria-live="polite"><div class="empty">Pick a test conversation.</div></article>
</section>

<div class="layout">
<div class="content">
<section id="calls">
  <h2>Ten test conversations</h2>
  <p class="point">A second model played each caller in a text conversation with the agent, using the same prompt, tools and live scheduler as the phone line. Text skips speech recognition and phone latency, so the reply times here are the model alone. Each conversation is checked against what actually changed in the scheduler, and against the transcript: read-back before any change, nothing shared before a date-of-birth match, no dosing advice, 988 and a crisis hand-off, no spoken lists, and no more than two options at a time.{before_line}</p>
  {board(calls)}
  <p class="note">{html.escape(lat)}{" across the conversations" if lat else ""}{f", about ${s['cost']:.3f} of model cost per conversation" if s.get("cost") is not None else ""}{f", {s['model']}" if s.get("model") else ""}.</p>
</section>

<section id="changes">
  <h2>What testing changed</h2>
  {changes_html or '<p class="point">The first round of calls has not been reviewed yet.</p>'}
</section>

<section id="how">
  <h2>How it works</h2>
  <div class="steps">
    <div class="step"><h3>Hear</h3><p>Vapi runs the phone line. Deepgram Nova-3 turns speech into text; no patient names are boosted, so names are heard the way a real line hears them.</p></div>
    <div class="step"><h3>Decide</h3><p>Claude Haiku 5.5 follows a written call policy: safety first, one question at a time, read back only the name and the slot.</p></div>
    <div class="step"><h3>Act</h3><p>Eight tools on a Cloudflare Worker: find patient, register, openings, book, move, cancel, text, hand off. Each call gets its own copy of the schedule.</p></div>
    <div class="step"><h3>Check</h3><p>Every test conversation is scored from the scheduler's end state and the transcript, then committed to the repo.</p></div>
  </div>
</section>

<section id="limits">
  <h2>Limits</h2>
  <ul class="lim">
    <li>The scheduler is a mock with constructed patients and slot rules. There is no EHR behind it.</li>
    <li>Hand-offs and texts are logged, not placed. A real deployment would warm-transfer to the on-call clinician.</li>
    <li>The ten tests are text conversations, so they skip speech recognition, interruptions and phone latency. The live line above is where those show up.</li>
    <li>The test callers are an AI too. They are steadier than real callers, so ten passing conversations are a floor, not proof.</li>
    <li>Checks are rules on the transcript and end state; a person still reads every transcript.</li>
  </ul>
</section>
</div>

<aside class="side" aria-label="About this build">
  <nav aria-label="On this page"><a href="#tool">Talk or read</a><a href="#calls">Ten test conversations</a><a href="#changes">What testing changed</a><a href="#how">How it works</a><a href="#limits">Limits</a></nav>
  <div><h3>Data</h3><p>The clinic, its providers, patients, dates of birth, phone numbers and schedule are all invented. Phone calls run on Vapi (Deepgram Nova-3, Claude Haiku 5.5, Vapi voices). The test callers are played by Claude Haiku 5.5 in text.</p></div>
  <div><h3>Run it yourself</h3><p>Node 22 for the scheduler, Python 3.12+ for the scripts.</p><pre>cd worker &amp;&amp; node --test test/*.test.js
pip install -e ".[dev]" &amp;&amp; pytest -q
python scripts/score_calls.py --label {html.escape(calls[0].get("label", "") or "v1")}</pre></div>
  <div><a class="pill" href="https://github.com/pg2342-hub/clinic-voice-agent">Code on GitHub</a></div>
</aside>
</div>
</main>
<script type="application/json" id="data">{data_json}</script>
<script type="module">{JS}</script>
</body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--before", default="")
    ap.add_argument("--runs-dir", default=str(ROOT / "calls" / "runs"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "index.html"))
    args = ap.parse_args()
    cases = {c["id"]: c for c in yaml.safe_load((ROOT / "calls" / "cases.yml").read_text())}
    runs = Path(args.runs_dir)
    calls = load_run(runs, args.label, cases)
    if not calls:
        sys.exit(f"no calls in {runs / args.label}")
    before = summary(load_run(runs, args.before, cases)) if args.before else None
    cfg_path = ROOT / "config" / "vapi.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page(calls, cfg, before, changes_section(ROOT / "calls" / "CHANGES.yml")))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
