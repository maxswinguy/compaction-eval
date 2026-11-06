#!/usr/bin/env python3
"""compaction-eval: score how well a Claude Code compaction kept what mattered.

Commands: list, plan, run, plant, check-pricing.

Standard library only. The transcript is read, never written: answering agents
run as headless `claude -p` resumes of temporary copies placed in a dedicated
projects folder, deleted at the end of a run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS = SKILL_DIR / "prompts"
PRICES = SKILL_DIR / "prices.json"
PROJECTS = Path.home() / ".claude" / "projects"
CONFIG_PATH = Path(os.environ.get("COMPACTION_EVAL_CONFIG",
                                  Path.home() / ".claude" / "compaction-evals" / "config.json"))

DEFAULTS = {
    "results_dir": "~/.claude/compaction-evals",
    "work_dir": "~/.claude/compaction-evals/work",
    "claude_bin": "claude",
    "extractor_model": "claude-opus-5-5",
    "extractor_effort": "high",
    "judge_model": "claude-sonnet-5-5",
    "judge_model_alt": "claude-opus-5-5",   # used when the session itself ran on judge_model
    "judge_effort": "high",
    "turns": 3,                              # counterfactual window: first N user turns after the compaction
    "noise": "first",                        # noise floor on the first scored turn, or "all"
    "counterfactual_auto_below_usd": None,   # None = always ask; a number = run without asking below it
    "price_max_age_days": 90,
    "call_timeout_s": 1800,
}

SUBSCRIPTION_NOTE = ("On a Claude subscription, this run is not billed separately; it counts against "
                     "your plan's usage limits. If usage credits are enabled, usage beyond your limit "
                     "is billed at API rates.")

CREDIT = {"kept": 1.0, "distorted": 0.5, "lost": 0.0}
WEIGHT = {0: 1, 1: 2, 2: 4}
CLAUDE_FLAGS = ["--no-session-persistence", "--tools", "", "--strict-mcp-config",
                "--disable-slash-commands", "--settings", '{"disableAllHooks":true}',
                "--autocompact", "1M", "--output-format", "json"]


# ---------------------------------------------------------------- config, io

def load_config(path=None):
    cfg = dict(DEFAULTS)
    p = Path(path) if path else CONFIG_PATH
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    for k in ("results_dir", "work_dir"):
        cfg[k] = str(Path(os.path.expanduser(cfg[k])))
    return cfg


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def prompt(name, **subs):
    text = (PROMPTS / f"{name}.md").read_text()
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def parse_json(text):
    """Pull the first JSON object out of a model reply (fenced or bare)."""
    if not text:
        raise ValueError("empty reply")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(cand)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- transcript

def text_of(rec):
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    out = []
    for x in c or []:
        if isinstance(x, dict):
            if x.get("type") == "text":
                out.append(x.get("text", ""))
            elif x.get("type") == "image":
                out.append("[image]")
    return "\n".join(out)


def is_real_user(rec):
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return False
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list) and c and all(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return False
    t = text_of(rec).strip()
    return bool(t) and not t.startswith("<")


def ctx_tokens(rec):
    u = (rec.get("message") or {}).get("usage") or {}
    return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


class Transcript:
    """Complete JSONL lines of a transcript, as they stand when read. A live session may still be
    appending, so only complete records are used and the md5 covers exactly the bytes used."""

    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.recs, self.raw, self.lineno = [], [], []
        off = end = 0
        for i, part in enumerate(raw.split(b"\n")):
            nxt = off + len(part) + 1
            if part.strip():
                try:
                    rec = json.loads(part)
                except json.JSONDecodeError:
                    break
                self.recs.append(rec)
                self.raw.append(part)
                self.lineno.append(i + 1)
                end = min(nxt, len(raw))
            off = nxt
        self.prefix_md5 = hashlib.md5(raw[:end]).hexdigest()
        self.prefix_lines = self.lineno[-1] if self.lineno else 0
        self.by = {r["uuid"]: i for i, r in enumerate(self.recs) if r.get("uuid")}
        self.boundaries = [i for i, r in enumerate(self.recs)
                           if r.get("type") == "system" and r.get("subtype") == "compact_boundary"]

    def chain_to(self, idx, stop_uuid=None):
        out, seen = [], set()
        while idx is not None and idx not in seen:
            seen.add(idx)
            out.append(idx)
            r = self.recs[idx]
            if stop_uuid and r.get("uuid") == stop_uuid:
                break
            p = r.get("parentUuid")
            idx = self.by.get(p) if p else None
        return list(reversed(out))


def find_transcript(arg_path=None, session=None):
    if arg_path:
        return Path(os.path.expanduser(arg_path))
    if session:
        hits = sorted(PROJECTS.glob(f"*/{session}.jsonl"))
        if not hits:
            sys.exit(f"No transcript named {session}.jsonl under {PROJECTS}")
        return hits[0]
    sys.exit("Give --transcript PATH or --session ID (the `list` command shows candidates).")


def analyze(t, boundary_line=None):
    if not t.boundaries:
        sys.exit("This transcript has no compaction.")
    if boundary_line:
        cands = [i for i in t.boundaries if t.lineno[i] == boundary_line]
        if not cands:
            sys.exit(f"No compaction at line {boundary_line}; compactions at "
                     f"{[t.lineno[i] for i in t.boundaries]}")
        bi = cands[0]
    else:
        bi = t.boundaries[-1]
    b = t.recs[bi]
    meta = b.get("compactMetadata") or {}
    nxt = next((j for j in t.boundaries if j > bi), len(t.recs))
    buuid = b["uuid"]

    post = None
    for j in range(nxt - 1, bi, -1):
        r = t.recs[j]
        if r.get("type") in ("user", "assistant") and not r.get("isSidechain"):
            ch = t.chain_to(j, stop_uuid=buuid)
            if t.recs[ch[0]].get("uuid") == buuid:
                post = ch
                break
    if not post:
        sys.exit("Could not follow the conversation after the compaction.")
    summ = next((j for j in post if t.recs[j].get("isCompactSummary")), None)
    pre_leaf_uuid = b.get("logicalParentUuid")
    if pre_leaf_uuid not in t.by:
        sys.exit("The compaction record does not point to a message before it.")

    pre_assts = [j for j in range(bi) if t.recs[j].get("type") == "assistant" and not t.recs[j].get("isSidechain")]
    last_pre = t.recs[pre_assts[-1]] if pre_assts else None
    model = (last_pre or {}).get("message", {}).get("model")
    effort = None
    for j in range(bi - 1, -1, -1):
        if t.recs[j].get("type") in ("api-request", "api-request-shape"):
            m = re.search(rb'"effort":\s*"(\w+)"', t.raw[j])
            if m:
                effort = m.group(1).decode()
                break

    # Turns after the compaction. If a message was in flight when the compaction ran, it is turn 1.
    pos_sum = post.index(summ) + 1 if summ is not None else 1

    def first_asst_after(pos):
        return next((k for k in range(pos, len(post)) if t.recs[post[k]].get("type") == "assistant"), None)

    turns, splice_start = [], None
    opener = (meta.get("runningTurnSends") or {}).get("openerUuid")
    if opener in t.by and is_real_user(t.recs[t.by[opener]]):
        fa = first_asst_after(pos_sum)
        cutpos = (fa - 1) if fa is not None else len(post) - 1
        turns.append({"user_idx": t.by[opener], "summary_cut": t.recs[post[cutpos]]["uuid"],
                      "full_cut": pre_leaf_uuid, "splice": [], "reply_pos": fa})
        splice_start = fa
    for k in range(pos_sum, len(post)):
        r = t.recs[post[k]]
        if not is_real_user(r):
            continue
        fa = first_asst_after(k + 1)
        cutpos = (fa - 1) if fa is not None else len(post) - 1
        if splice_start is None:
            splice_start = k
        turns.append({"user_idx": post[k], "summary_cut": t.recs[post[cutpos]]["uuid"],
                      "full_cut": t.recs[post[cutpos]]["uuid"], "splice": post[splice_start:cutpos + 1],
                      "reply_pos": fa})
    for n, tr in enumerate(turns, 1):
        tr["n"] = n
        tr["line"] = t.lineno[tr["user_idx"]]
        tr["text"] = text_of(t.recs[tr["user_idx"]])
        tr["summary_ctx"] = ctx_tokens(t.recs[post[tr["reply_pos"]]]) if tr["reply_pos"] is not None else None

    post_assts = [j for j in post if t.recs[j].get("type") == "assistant"]
    first_post_asst = post_assts[0] if post_assts else None
    first_reply_pos = post.index(first_post_asst) if first_post_asst is not None else len(post)
    incident_msgs = [post[k] for k in range(first_reply_pos + 1, len(post)) if is_real_user(t.recs[post[k]])]

    ts_boundary = parse_ts(b.get("timestamp"))
    ts_last_pre = parse_ts(last_pre.get("timestamp")) if last_pre else None
    ts_summary = parse_ts(t.recs[summ].get("timestamp")) if summ is not None else None
    ts_return = parse_ts(t.recs[turns[0]["user_idx"]].get("timestamp")) if turns else None
    gap_h = (ts_boundary - ts_last_pre).total_seconds() / 3600 if ts_boundary and ts_last_pre else None
    idle = bool(meta.get("precomputed") and ts_summary and ts_return and ts_summary < ts_return
                and gap_h is not None and gap_h > 1)

    summary_text = text_of(t.recs[summ]) if summ is not None else ""
    pre, postt = meta.get("preTokens"), meta.get("postTokens")
    return {
        "bi": bi, "boundary_line": t.lineno[bi], "boundary_uuid": buuid, "summary_idx": summ,
        "summary_text": summary_text, "pre_leaf_uuid": pre_leaf_uuid, "post": post, "turns": turns,
        "incident_msgs": incident_msgs,
        "facts": {
            "trigger": meta.get("trigger"), "preTokens": pre, "postTokens": postt,
            "compression_ratio": round(pre / postt, 2) if pre and postt else None,
            "precomputed": meta.get("precomputed"), "durationMs": meta.get("durationMs"),
            "session_model": model, "session_effort": effort,
            "claude_code_version": b.get("version"),
            "context_before_tokens": ctx_tokens(last_pre) if last_pre else None,
            "context_after_tokens": ctx_tokens(t.recs[first_post_asst]) if first_post_asst is not None else None,
            "boundary_at": b.get("timestamp"), "last_activity_before": (last_pre or {}).get("timestamp"),
            "gap_hours": round(gap_h, 2) if gap_h is not None else None,
            "cache_cold": gap_h > 1 if gap_h is not None else None,
            "idle_signature": idle,
            "turns_available": len(turns),
            "summary_chars": len(summary_text),
        },
    }


def summary_gate(text):
    markers = [m for m in ("primary request", "pending", "current work", "key technical", "files and code",
                           "errors", "problem solving", "user messages", "next step")
               if m in text.lower()]
    ok = len(text) >= 1500 and len(markers) >= 2
    return {"passed": ok, "chars": len(text), "section_markers_found": len(markers)}


def settings_in_force():
    out = {"read_at": now_iso(), "note": "read when the eval ran, not when the compaction ran"}
    p = Path.home() / ".claude" / "settings.json"
    try:
        s = json.loads(p.read_text())
    except Exception:
        s = {}
    keys = ("autoCompactWindow", "idleCompaction", "autoCompactEnabled")
    for k in keys + ("model",):
        if k in s:
            out[k] = s[k]
    # Per-model overrides live under modelSettings.<model id> (e.g. the auto-compact window).
    per_model = {m: {k: v for k, v in (ms or {}).items() if k in keys}
                 for m, ms in (s.get("modelSettings") or {}).items() if isinstance(ms, dict)}
    per_model = {m: v for m, v in per_model.items() if v}
    if per_model:
        out["modelSettings"] = per_model
    env = dict(s.get("env") or {})
    for k in ("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
        if k in os.environ:
            env[k] = os.environ[k]
    if env:
        out["env"] = {k: v for k, v in env.items() if "COMPACT" in k.upper()}
    return out


# ---------------------------------------------------------------- copies and calls

def work_project_dir(cfg):
    wd = Path(cfg["work_dir"]).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    proj = PROJECTS / slug(wd)
    proj.mkdir(parents=True, exist_ok=True)
    return wd, proj


def make_copy(cfg, t, upto_idx, splice=(), reparent_to=None, summary_idx=None, override=None):
    """Write records 0..upto_idx (+ spliced records) as a new session file. Never touches the source."""
    _, proj = work_project_dir(cfg)
    sid = str(uuid.uuid4())
    dest = proj / f"{sid}.jsonl"
    with open(dest, "x") as f:
        for i in range(upto_idx + 1):
            d = json.loads(t.raw[i])
            if "sessionId" in d:
                d["sessionId"] = sid
            if override is not None and i == summary_idx:
                d["message"]["content"] = override
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        for n, i in enumerate(splice):
            d = json.loads(t.raw[i])
            if "sessionId" in d:
                d["sessionId"] = sid
            if n == 0 and reparent_to:
                d["parentUuid"] = reparent_to
                d.pop("logicalParentUuid", None)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return sid, str(dest)


def claude_version(cfg):
    try:
        return subprocess.run([cfg["claude_bin"], "--version"], capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception as e:
        return f"unknown ({e})"


class Caller:
    def __init__(self, cfg, run_dir, prices):
        self.cfg, self.prices = cfg, prices
        self.calls_dir = Path(run_dir) / "calls"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = []

    def __call__(self, label, text, model, effort=None, session=None, cut=None):
        wd, _ = work_project_dir(self.cfg)
        cmd = [self.cfg["claude_bin"], "-p"]
        if session:
            cmd += ["--resume", session]
        if cut:
            cmd += ["--resume-session-at", cut]
        cmd += CLAUDE_FLAGS + ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd.append(text)
        print(f"  · {label} ({model}{', ' + effort if effort else ''})", flush=True)
        started = now_iso()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(wd),
                               timeout=self.cfg["call_timeout_s"])
            out, err, rc = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, err, rc = e.stdout or "", f"timeout after {self.cfg['call_timeout_s']}s", -1
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            d = {"is_error": True, "result": (out or "")[-4000:]}
        usage = d.get("usage") or {}
        rec = {"label": label, "model": model, "effort": effort, "session": session, "cut": cut,
               "started": started, "returncode": rc, "is_error": d.get("is_error", rc != 0),
               "usage": usage, "cost_usd_table": price_usage(self.prices, model, usage),
               "cost_usd_claude_code": d.get("total_cost_usd"), "prompt": text,
               "result": d.get("result"), "stderr": (err or "")[-4000:]}
        write_json(self.calls_dir / f"{label}.json", rec)
        self.ledger.append({k: rec[k] for k in ("label", "model", "usage", "cost_usd_table",
                                                 "cost_usd_claude_code", "is_error")})
        if rec["is_error"]:
            print(f"    ! {label} failed: {str(rec['result'])[:300]} {rec['stderr'][-300:]}", flush=True)
        return rec


# ---------------------------------------------------------------- prices

def load_prices():
    return json.loads(PRICES.read_text())


def rates_for(prices, model, prompt_tokens):
    m = prices["models"].get(model)
    if not m:
        return None
    if "tiers" in m:
        for tier in m["tiers"]:
            if tier["max_prompt_tokens"] is None or prompt_tokens <= tier["max_prompt_tokens"]:
                return tier
    return m


def price_usage(prices, model, usage):
    if not usage:
        return None
    inp = usage.get("input_tokens") or 0
    rd = usage.get("cache_read_input_tokens") or 0
    wr = usage.get("cache_creation_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    r = rates_for(prices, model, inp + rd + wr)
    if not r:
        return None
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5 = max(wr - w1h, 0)
    return round((inp * r["input"] + w5 * r["cache_write_5m"] + w1h * r["cache_write_1h"]
                  + rd * r["cache_read"] + out * r["output"]) / 1e6, 4)


def price_age_days(prices):
    return (dt.date.today() - dt.date.fromisoformat(prices["taken"])).days


def estimate(cfg, a, prices, counterfactual=True, turns=None, noise=None, reuse=False):
    """Priced estimate from the context sizes the transcript itself recorded (upper bounds:
    they include tool definitions that the replay leaves out). Measured 2026-11-06: a resumed
    call reads the cache only when its whole prompt repeats the call before it (the noise
    replay); every other call writes its full context."""
    f = a["facts"]
    sess_model = f["session_model"] or cfg["extractor_model"]
    judge = judge_model(cfg, sess_model)
    full_ctx = f["context_before_tokens"] or f["preTokens"] or 0
    sum_ctx = f["context_after_tokens"] or f["postTokens"] or 0
    calls = []

    def add(label, model, ctx, out, repeat=False):
        write, read = (0, ctx) if repeat else (ctx, 0)
        r = rates_for(prices, model, ctx)
        # Claude Code writes the prompt cache with the 1-hour TTL (measured 2026-11-06:
        # cache_creation.ephemeral_1h_input_tokens carried the whole write).
        cost = None if not r else round((write * r["cache_write_1h"] + read * r["cache_read"]
                                         + out * r["output"]) / 1e6, 4)
        calls.append({"label": label, "model": model, "context": ctx, "output": out, "usd": cost})

    if not reuse:
        add("extract", cfg["extractor_model"], full_ctx, 12000)
        add("probe_full", sess_model, full_ctx, 12000)
        add("probe_null", sess_model, 30000, 3000)
    add("probe_summary", sess_model, sum_ctx, 8000)
    add("judge_probe", judge, 35000, 8000)
    add("note", judge, 30000, 1000)
    if counterfactual:
        n_turns = min(turns or cfg["turns"], len(a["turns"]))
        for tr in a["turns"][:n_turns]:
            grow = max((tr["summary_ctx"] or sum_ctx) - sum_ctx, 0)
            add(f"cf{tr['n']}_full", sess_model, full_ctx + grow, 3000)
            if tr["n"] == 1 or (noise or cfg["noise"]) == "all":
                add(f"cf{tr['n']}_full_noise", sess_model, full_ctx + grow, 3000, repeat=True)
                add(f"judge_cf{tr['n']}_noise", judge, 30000, 1000)
            add(f"cf{tr['n']}_summary", sess_model, tr["summary_ctx"] or sum_ctx, 3000)
            add(f"judge_cf{tr['n']}", judge, 30000, 1000)
    if a["incident_msgs"]:
        add("judge_incidents", judge, 30000, 1000)
    missing = sorted({c["model"] for c in calls if c["usd"] is None})
    total = round(sum(c["usd"] or 0 for c in calls), 2)
    return {"calls": calls, "total_usd": total, "models_without_verified_price": missing,
            "prices_taken": prices["taken"], "prices_source": prices["source"],
            "prices_age_days": price_age_days(prices),
            "prices_stale": price_age_days(prices) > cfg["price_max_age_days"]}


def judge_model(cfg, session_model):
    return cfg["judge_model_alt"] if session_model == cfg["judge_model"] else cfg["judge_model"]


# ---------------------------------------------------------------- scoring

def norm(s):
    s = str(s).lower().strip()
    s = re.sub(r"[`'\"*]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(" .,;:")


LITERAL = re.compile(
    r"https?://[^\s,;)>\]]+"                                # URLs
    r"|[~/]?[\w.@-]+(?:/[\w.@-]+)+"                         # paths
    r"|\b[\w-]+\.(?:jsonl?|md|py|html?|css|js|ts|txt|ya?ml|toml|sh|csv)\b"  # file names
    r"|\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{6,}\b"         # ids mixing letters and digits
)


def literals(text):
    home = str(Path.home())
    out = []
    for m in LITERAL.findall(text or ""):
        tok = m.rstrip(".,;:").replace(home, "~")
        if tok and tok not in out:
            out.append(tok)
    return out


def exact_grade(expected, answer):
    """String check on the literals of the reference: all present = kept, some = distorted.
    Returns None when the reference holds no literal, so the item goes to the judge."""
    toks = literals(expected)
    if not toks:
        return None
    if answer is None or norm(answer) in ("", "unknown"):
        return "lost"
    a = str(answer).replace(str(Path.home()), "~").lower()
    hits = sum(1 for tok in toks if tok.lower() in a)
    return "kept" if hits == len(toks) else ("distorted" if hits else "lost")


def render_items(items, with_answers=True):
    lines = []
    for it in items:
        s = f"- [{it['id']}] ({it['category']}, severity {it['severity']}) {it['question']}"
        if with_answers:
            s += f"\n  Answer: {it['answer']}"
        lines.append(s)
    return "\n".join(lines)


def run_probe_judge(call, items, answers, judge, effort, rng):
    """Blind grading of the judged items; exact items are graded by string match."""
    sides = ["summary", "full", "null"]
    blocks, maps = [], {}
    mechanical = {it["id"] for it in items if it["answer_type"] == "exact" and literals(it["answer"])}
    for it in items:
        if it["id"] in mechanical:
            continue
        order = sides[:]
        rng.shuffle(order)
        letters = dict(zip("ABC", order))
        maps[it["id"]] = letters
        cand = "\n".join(f"  {L}: {answers[s].get(it['id'], 'UNKNOWN')}" for L, s in letters.items())
        blocks.append(f"[{it['id']}] Question: {it['question']}\n  Reference: {it['answer']}\n{cand}")
    grades = {it["id"]: {} for it in items}
    for it in items:
        if it["id"] in mechanical:
            for s in sides:
                grades[it["id"]][s] = exact_grade(it["answer"], answers[s].get(it["id"]))
            grades[it["id"]]["by"] = "string match: " + ", ".join(literals(it["answer"]))
    if blocks:
        rec = call("judge_probe", prompt("judge_probe", ITEMS="\n\n".join(blocks)), judge, effort)
        try:
            for g in parse_json(rec["result"]).get("grades", []):
                letters = maps.get(g.get("id"))
                if not letters:
                    continue
                for L, s in letters.items():
                    v = str(g.get(L, "")).lower()
                    if v in CREDIT:
                        grades[g["id"]][s] = v
                grades[g["id"]]["by"] = "judge"
                grades[g["id"]]["why"] = g.get("why")
        except Exception as e:
            print(f"    ! could not read the judge's grades: {e}")
        write_json(Path(call.calls_dir).parent / "judge_probe_labels.json", maps)
    return grades


def score_probe(items, grades):
    rows, valid = [], []
    for it in items:
        g = grades.get(it["id"], {})
        row = dict(it, summary=g.get("summary"), full=g.get("full"), null=g.get("null"),
                   graded_by=g.get("by"), why=g.get("why"))
        if not all(row[s] in CREDIT for s in ("summary", "full", "null")):
            row["status"] = "ungraded"
        elif row["full"] != "kept":
            row["status"] = "dropped: full-context agent missed it"
        elif row["null"] == "kept":
            row["status"] = "dropped: answerable without the session"
        else:
            row["status"] = "scored"
            valid.append(row)
        rows.append(row)
    wsum = sum(WEIGHT[r["severity"]] for r in valid)
    got = sum(WEIGHT[r["severity"]] * CREDIT[r["summary"]] for r in valid)
    by_cat = {}
    for r in valid:
        c = by_cat.setdefault(r["category"], {"items": 0, "kept": 0, "distorted": 0, "lost": 0})
        c["items"] += 1
        c[r["summary"]] += 1
    critical = [r["id"] for r in valid if r["severity"] == 2 and r["summary"] != "kept"]
    return rows, {
        "items_extracted": len(items), "items_scored": len(valid),
        "items_dropped": sum(1 for r in rows if r["status"].startswith("dropped")),
        "items_ungraded": sum(1 for r in rows if r["status"] == "ungraded"),
        "retention_pct": round(100 * got / wsum, 1) if wsum else None,
        "critical_losses": len(critical), "critical_loss_ids": critical, "by_category": by_cat,
    }


def clean_items(raw):
    items = []
    for n, it in enumerate(raw.get("items", []), 1):
        try:
            sev = int(it.get("severity", 1))
        except (TypeError, ValueError):
            sev = 1
        items.append({
            "id": f"I{n:02d}", "category": str(it.get("category", "fact")),
            "severity": min(max(sev, 0), 2), "question": str(it.get("question", "")).strip(),
            "answer": str(it.get("answer", "")).strip(),
            "answer_type": "exact" if it.get("answer_type") == "exact" else "judged",
            "evidence": str(it.get("evidence", "")).strip(),
        })
    return [i for i in items if i["question"] and i["answer"]]


def ask_answers(call, label, items, model, effort, session=None, cut=None):
    qs = "\n".join(f"[{it['id']}] {it['question']}" for it in items)
    rec = call(label, prompt("probe", QUESTIONS=qs), model, effort, session=session, cut=cut)
    try:
        return {k: str(v) for k, v in parse_json(rec["result"]).get("answers", {}).items()}
    except Exception as e:
        print(f"    ! could not read answers from {label}: {e}")
        return {}


def judge_pair(call, label, items, message, a_text, b_text, judge, effort, rng, a_side, b_side):
    pair = [(a_side, a_text), (b_side, b_text)]
    rng.shuffle(pair)
    rec = call(label, prompt("judge_divergence", ITEMS=render_items(items), MESSAGE=message,
                             A=pair[0][1] or "(no response)", B=pair[1][1] or "(no response)"), judge, effort)
    try:
        v = parse_json(rec["result"])
        better = {"A": pair[0][0], "B": pair[1][0]}.get(str(v.get("better")).strip(), "tie")
        return {"divergence": int(v.get("divergence")), "better": better, "why": v.get("why"),
                "labels": {"A": pair[0][0], "B": pair[1][0]}}
    except Exception as e:
        print(f"    ! could not read the verdict of {label}: {e}")
        return {"divergence": None, "better": None, "why": None}


# ---------------------------------------------------------------- commands

def cmd_list(args, cfg):
    rows = []
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[: args.limit]:
        data = p.read_bytes()
        if b'"compact_boundary"' not in data:
            continue
        n = data.count(b'"subtype":"compact_boundary"') + data.count(b'"subtype": "compact_boundary"')
        rows.append({"session": p.stem, "project": p.parent.name, "compactions": n,
                     "modified": dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    for r in rows:
        print(f"{r['modified']}  {r['session']}  {r['compactions']} compaction(s)  {r['project']}")
    if not rows:
        print(f"No compacted sessions among the {args.limit} most recent transcripts.")


def plan_data(args, cfg):
    path = find_transcript(args.transcript, args.session)
    t = Transcript(path)
    a = analyze(t, args.boundary_line)
    prices = load_prices()
    est = estimate(cfg, a, prices, counterfactual=True, turns=args.turns, noise=args.noise)
    est_probe = estimate(cfg, a, prices, counterfactual=False)
    return path, t, a, prices, est, est_probe


def print_plan(path, t, a, est, est_probe, cfg, args):
    f = a["facts"]
    gate = summary_gate(a["summary_text"])
    n_turns = min(args.turns or cfg["turns"], f["turns_available"])
    print(f"Transcript: {path}")
    print(f"  read {t.prefix_lines} lines, md5 of what was read {t.prefix_md5}")
    print(f"Compaction at line {a['boundary_line']} ({len(t.boundaries)} in this transcript)")
    for k in ("trigger", "preTokens", "postTokens", "compression_ratio", "precomputed", "idle_signature",
              "gap_hours", "cache_cold", "context_before_tokens", "context_after_tokens",
              "session_model", "session_effort", "claude_code_version", "turns_available", "summary_chars"):
        print(f"  {k}: {f[k]}")
    print(f"Summary gate: {'passed' if gate['passed'] else 'FAILED'} "
          f"({gate['chars']} characters, {gate['section_markers_found']} section markers)")
    print(f"Counterfactual window: {n_turns} turn(s) of {f['turns_available']} available")
    print(f"Estimate at API prices taken {est['prices_taken']} ({est['prices_source']}):")
    print(f"  probe only: ${est_probe['total_usd']}")
    print(f"  probe + counterfactual: ${est['total_usd']}")
    if est["prices_stale"]:
        print(f"  ! the price table is {est['prices_age_days']} days old: run check-pricing")
    if est["models_without_verified_price"]:
        print(f"  ! no verified price for {', '.join(est['models_without_verified_price'])}; "
              "those calls are left out of the estimate")
    print("  Upper bound: the context sizes include tool definitions the replay leaves out.")
    print(f"  {SUBSCRIPTION_NOTE}")


def cmd_plan(args, cfg):
    path, t, a, prices, est, est_probe = plan_data(args, cfg)
    if args.json:
        out = {"transcript": str(path), "lines_read": t.prefix_lines, "md5_read": t.prefix_md5,
               "boundary_line": a["boundary_line"], "facts": a["facts"],
               "summary_gate": summary_gate(a["summary_text"]),
               "turns": [{"n": x["n"], "line": x["line"], "text": x["text"][:200]} for x in a["turns"]],
               "estimate_probe_only": est_probe, "estimate_with_counterfactual": est,
               "subscription_note": SUBSCRIPTION_NOTE}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_plan(path, t, a, est, est_probe, cfg, args)


def cmd_plant(args, cfg):
    path = find_transcript(args.transcript, args.session)
    a = analyze(Transcript(path), args.boundary_line)
    text = a["summary_text"]
    if args.mode == "gut":
        planted = ("This session is being continued from a previous conversation. "
                   "The summary of the earlier part is not available.")
    else:
        if not args.drop:
            sys.exit("--mode drop needs at least one --drop PATTERN")
        pats = [re.compile(p, re.I) for p in args.drop]
        kept = [ln for ln in text.split("\n") if not any(p.search(ln) for p in pats)]
        print(f"Dropped {len(text.splitlines()) - len(kept)} of {len(text.splitlines())} summary lines.")
        planted = "\n".join(kept)
    Path(args.out).write_text(planted)
    print(f"Planted summary written to {args.out} ({len(planted)} characters, original {len(text)}).")


def cmd_check_pricing(args, cfg):
    prices = load_prices()
    print(f"Table taken {prices['taken']} ({price_age_days(prices)} days ago) from {prices['source']}")
    try:
        with urllib.request.urlopen(prices["source"], timeout=30) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        sys.exit(f"Could not fetch the pricing page: {e}")
    for model in prices["models"]:
        name = " ".join(w.capitalize() for w in model.replace("claude-", "").split("-")[:1])
        ver = ".".join(model.split("-")[2:])
        rows = [ln.strip() for ln in page.splitlines() if name in ln and ver in ln and "|" in ln]
        print(f"\n{model}  table: {json.dumps(prices['models'][model])}")
        for ln in rows[:4]:
            print(f"  live: {ln}")
        if not rows:
            print("  live: no table row found for this model; check the page by hand")
    print("\nEdit prices.json (values and the 'taken' date) if the live rows differ.")


def cmd_run(args, cfg):
    path = find_transcript(args.transcript, args.session)
    t = Transcript(path)
    a = analyze(t, args.boundary_line)
    f = a["facts"]
    gate = summary_gate(a["summary_text"])
    if not gate["passed"] and not args.force:
        sys.exit("Summary gate failed: the compaction's summary does not look like a summary "
                 f"({gate['chars']} characters, {gate['section_markers_found']} section markers). "
                 "Scoring it would measure the wrong thing. --force runs anyway.")
    prices = load_prices()
    sess_model = args.answer_model or f["session_model"]
    if not sess_model:
        sys.exit("The session model could not be read from the transcript; pass --answer-model.")
    sess_effort = args.answer_effort or f["session_effort"]
    judge = judge_model(cfg, sess_model)
    override = Path(args.summary_override).read_text() if args.summary_override else None
    label = args.label or ("planted" if override is not None else "real")
    do_cf = args.counterfactual == "yes"
    n_turns = min(args.turns or cfg["turns"], len(a["turns"])) if do_cf else 0
    noise_mode = args.noise or cfg["noise"]
    rng = random.Random(args.seed)

    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    run_dir = Path(args.results_dir or cfg["results_dir"]) / "runs" / \
        f"{stamp}_{path.stem[:8]}_L{a['boundary_line']}_{label}"
    run_dir.mkdir(parents=True, exist_ok=False)
    call = Caller(cfg, run_dir, prices)
    est = estimate(cfg, a, prices, counterfactual=do_cf, turns=args.turns, noise=noise_mode,
                   reuse=bool(args.reuse))
    run = {"started": now_iso(), "label": label, "transcript": str(path), "session_id": path.stem,
           "lines_read": t.prefix_lines, "md5_read": t.prefix_md5, "boundary_line": a["boundary_line"],
           "facts": f, "summary_gate": gate, "summary_override": args.summary_override,
           "answer_model": sess_model, "answer_effort": sess_effort,
           "extractor_model": cfg["extractor_model"], "extractor_effort": cfg["extractor_effort"],
           "judge_model": judge, "judge_effort": cfg["judge_effort"],
           "replay_claude_code_version": claude_version(cfg), "settings": settings_in_force(),
           "reuse": args.reuse, "estimate": est, "copies": []}
    write_json(run_dir / "run.json", run)
    print(f"Run folder: {run_dir}")

    # The two copies. Full side: everything before the compaction, plus the real post-compaction
    # turns needed by the counterfactual, re-attached after the last pre-compaction message.
    need = a["turns"][:n_turns]
    splice = need[-1]["splice"] if need else []
    full_sid = sum_sid = None
    try:
        if not args.reuse or n_turns:
            full_sid, p = make_copy(cfg, t, a["bi"] - 1, splice=splice, reparent_to=a["pre_leaf_uuid"])
            run["copies"].append(p)
        cuts = [t.by[x["summary_cut"]] for x in (need or a["turns"][:1])]
        sum_upto = max(cuts + [a["summary_idx"] or 0])
        sum_sid, p = make_copy(cfg, t, sum_upto, summary_idx=a["summary_idx"], override=override)
        run["copies"].append(p)
        write_json(run_dir / "run.json", run)
        sum_cut1 = a["turns"][0]["summary_cut"] if a["turns"] else t.recs[sum_upto]["uuid"]

        # Station 1: probe.
        print("Probe")
        if args.reuse:
            src = Path(args.reuse)
            items = json.loads((src / "items.json").read_text())
            answers = json.loads((src / "answers.json").read_text())
            answers = {"full": answers["full"], "null": answers["null"]}
        else:
            rec = call("extract", prompt("extract"), cfg["extractor_model"], cfg["extractor_effort"],
                       session=full_sid, cut=a["pre_leaf_uuid"])
            try:
                items = clean_items(parse_json(rec["result"]))
            except Exception as e:
                sys.exit(f"The extractor's reply could not be read ({e}); see calls/extract.json")
            answers = {"full": ask_answers(call, "probe_full", items, sess_model, sess_effort,
                                           session=full_sid, cut=a["pre_leaf_uuid"])}
        write_json(run_dir / "items.json", items)

        # Counterfactual, full side, while the full copy is warm in the cache.
        replies = {}
        if do_cf:
            print("Counterfactual (full side)")
            for tr in need:
                replies[(tr["n"], "full")] = call(f"cf{tr['n']}_full", prompt("replay"), sess_model, sess_effort,
                                                  session=full_sid, cut=tr["full_cut"])["result"]
                if tr["n"] == 1 or noise_mode == "all":
                    replies[(tr["n"], "noise")] = call(f"cf{tr['n']}_full_noise", prompt("replay"), sess_model,
                                                       sess_effort, session=full_sid, cut=tr["full_cut"])["result"]

        print("Probe (summary side)")
        answers["summary"] = ask_answers(call, "probe_summary", items, sess_model, sess_effort,
                                         session=sum_sid, cut=sum_cut1)
        if do_cf:
            print("Counterfactual (summary side)")
            for tr in need:
                replies[(tr["n"], "summary")] = call(f"cf{tr['n']}_summary", prompt("replay"), sess_model,
                                                     sess_effort, session=sum_sid, cut=tr["summary_cut"])["result"]
        if "null" not in answers:
            answers["null"] = ask_answers(call, "probe_null", items, sess_model, sess_effort)
        write_json(run_dir / "answers.json", answers)
    finally:
        if not args.keep_copies:
            for p in run["copies"]:
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
            run["copies_deleted"] = True
        write_json(run_dir / "run.json", run)

    # Judging.
    print("Judging")
    grades = run_probe_judge(call, items, answers, judge, cfg["judge_effort"], rng)
    rows, probe = score_probe(items, grades)
    write_json(run_dir / "probe_scores.json", {"summary": probe, "items": rows})
    lost = [r for r in rows if r["status"] == "scored" and r["summary"] != "kept"]
    kept = [r for r in rows if r["status"] == "scored" and r["summary"] == "kept"]
    note = None
    if probe["items_scored"]:
        fmt = lambda rs: "\n".join(f"- ({r['category']}, severity {r['severity']}, {r['summary']}) "
                                   f"{r['question']} -> {r['answer']}" for r in rs) or "(none)"
        note = call("note", prompt("note", LOST=fmt(lost), KEPT=fmt(kept)), judge, cfg["judge_effort"])["result"]

    cf = []
    if do_cf:
        for tr in need:
            msg = tr["text"][:6000]
            v = judge_pair(call, f"judge_cf{tr['n']}", items, msg, replies.get((tr["n"], "full")),
                           replies.get((tr["n"], "summary")), judge, cfg["judge_effort"], rng, "full", "summary")
            entry = {"turn": tr["n"], "line": tr["line"], "gap": v}
            if (tr["n"], "noise") in replies:
                entry["noise"] = judge_pair(call, f"judge_cf{tr['n']}_noise", items, msg,
                                            replies.get((tr["n"], "full")), replies.get((tr["n"], "noise")),
                                            judge, cfg["judge_effort"], rng, "full", "full (second run)")
            cf.append(entry)
        write_json(run_dir / "counterfactual.json",
                   {"turns": cf, "replies": {f"{k[0]}_{k[1]}": v for k, v in replies.items()}})

    incidents = None
    if a["incident_msgs"] and not args.reuse_incidents_skip:
        msgs = a["incident_msgs"][:40]
        block = "\n\n".join(f"[{n}] {text_of(t.recs[i])[:1500]}" for n, i in enumerate(msgs, 1))
        rec = call("judge_incidents", prompt("incidents", ITEMS=render_items(items), MESSAGES=block),
                   judge, cfg["judge_effort"])
        try:
            flags = parse_json(rec["result"]).get("messages", [])
            n_inc = sum(1 for x in flags if x.get("incident") in ("restate", "correct"))
            incidents = {"messages": len(msgs), "incidents": n_inc,
                         "rate": round(n_inc / len(msgs), 3), "flags": flags}
        except Exception as e:
            print(f"    ! could not read the incident count: {e}")

    # Headline.
    gaps = [c["gap"]["divergence"] for c in cf if c["gap"].get("divergence") is not None]
    noises = [c["noise"]["divergence"] for c in cf if c.get("noise", {}).get("divergence") is not None]
    mean_gap = round(sum(gaps) / len(gaps), 2) if gaps else None
    noise_floor = round(sum(noises) / len(noises), 2) if noises else None
    actual_usd = round(sum(c["cost_usd_table"] or 0 for c in call.ledger), 4)
    tokens = {k: sum((c["usage"] or {}).get(k) or 0 for c in call.ledger)
              for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    headline = {
        "retention_pct": probe["retention_pct"], "critical_losses": probe["critical_losses"],
        "items_scored": probe["items_scored"], "items_extracted": probe["items_extracted"],
        "divergence_mean": mean_gap, "noise_floor": noise_floor,
        "divergence_minus_noise": round(mean_gap - noise_floor, 2) if mean_gap is not None and noise_floor is not None else None,
        "turns_scored": len(cf), "incident_rate": incidents["rate"] if incidents else None,
    }
    run.update({"finished": now_iso(), "headline": headline, "probe": probe, "counterfactual": cf,
                "incidents": incidents, "note": note, "calls": call.ledger,
                "cost": {"estimate_usd": est["total_usd"], "actual_usd_table": actual_usd,
                         "actual_usd_claude_code": round(sum(c["cost_usd_claude_code"] or 0 for c in call.ledger), 4),
                         "tokens": tokens, "prices_taken": prices["taken"],
                         "failed_calls": [c["label"] for c in call.ledger if c["is_error"]]}})
    write_json(run_dir / "run.json", run)
    write_report(run_dir, run, rows)

    log_line = {
        "date": run["started"][:10], "label": label, "session_id": path.stem, "boundary_line": a["boundary_line"],
        "trigger": f["trigger"], "idle_signature": f["idle_signature"], "cache_cold": f["cache_cold"],
        "preTokens": f["preTokens"], "postTokens": f["postTokens"], "compression_ratio": f["compression_ratio"],
        "context_before_tokens": f["context_before_tokens"], "context_after_tokens": f["context_after_tokens"],
        "session_model": f["session_model"], "session_effort": f["session_effort"],
        "claude_code_version": f["claude_code_version"], "replay_claude_code_version": run["replay_claude_code_version"],
        "settings": run["settings"], "extractor_model": cfg["extractor_model"], "judge_model": judge,
        **headline, "by_category": probe["by_category"],
        "cost_estimate_usd": est["total_usd"], "cost_actual_usd": actual_usd, "tokens": tokens,
        "prices_taken": prices["taken"], "run_dir": str(run_dir),
    }
    log = Path(args.results_dir or cfg["results_dir"]) / "log.jsonl"
    with open(log, "a") as fh:
        fh.write(json.dumps(log_line, ensure_ascii=False) + "\n")
    print(f"\nReport: {run_dir / 'report.md'}\nLog: {log}")
    print(json.dumps(headline, indent=2))


def write_report(run_dir, run, rows):
    f, h, c = run["facts"], run["headline"], run["cost"]
    L = [f"# Compaction eval: {run['session_id'][:8]}, line {run['boundary_line']} ({run['label']})", ""]
    L += [f"Run {run['started']}. Transcript `{run['transcript']}`, {run['lines_read']} lines read "
          f"(md5 {run['md5_read']}).", ""]
    if run["summary_override"]:
        L += [f"**Self-test run:** the summary was replaced by `{run['summary_override']}`. "
              "This score is expected to be low.", ""]
    L += ["## Headline", "", "| Measure | Value |", "|---|---|",
          f"| Weighted retention | {h['retention_pct']}% of {h['items_scored']} scored items "
          f"({h['items_extracted']} extracted) |",
          f"| Critical losses (severity 2, lost or distorted) | {h['critical_losses']} |",
          f"| Divergence, full vs summary (0–3) | {h['divergence_mean']} over {h['turns_scored']} turn(s) |",
          f"| Noise floor, full vs full (0–3) | {h['noise_floor']} |",
          f"| Divergence minus noise | {h['divergence_minus_noise']} |",
          f"| Incident rate | {h['incident_rate']} |", ""]
    if run.get("note"):
        L += ["## Note", "", str(run["note"]).strip(), ""]
    L += ["## The compaction", "", "| Field | Value |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in f.items()]
    L += ["", "## Items", "",
          "| Id | Category | Sev | Summary side | Full | No context | Status | Question | Reference answer |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        q = r["question"].replace("|", "/").replace("\n", " ")
        ans = r["answer"].replace("|", "/").replace("\n", " ")
        L.append(f"| {r['id']} | {r['category']} | {r['severity']} | {r['summary']} | {r['full']} | "
                 f"{r['null']} | {r['status']} | {q} | {ans} |")
    if run["counterfactual"]:
        L += ["", "## Counterfactual turns", ""]
        for e in run["counterfactual"]:
            g = e["gap"]
            L.append(f"- Turn {e['turn']} (line {e['line']}): divergence {g.get('divergence')}, "
                     f"better fit: {g.get('better')}. {g.get('why') or ''}")
            if e.get("noise"):
                n = e["noise"]
                L.append(f"  - Noise floor: divergence {n.get('divergence')}. {n.get('why') or ''}")
    if run.get("incidents"):
        i = run["incidents"]
        L += ["", "## Incidents", "", f"{i['incidents']} of {i['messages']} later user messages restate or "
              "correct something established before the compaction."]
    L += ["", "## Cost", "",
          f"Estimate ${c['estimate_usd']}; actual ${c['actual_usd_table']} at API prices taken "
          f"{c['prices_taken']} (Claude Code's own figure: ${c['actual_usd_claude_code']}). "
          f"Tokens: {c['tokens']}.", "", SUBSCRIPTION_NOTE]
    if c["failed_calls"]:
        L += ["", f"Failed calls: {', '.join(c['failed_calls'])}. See `calls/`."]
    L += ["", "## Settings", "", f"Answering agents: {run['answer_model']} ({run['answer_effort']}). "
          f"Extractor: {run['extractor_model']} ({run['extractor_effort']}). Judge: {run['judge_model']} "
          f"({run['judge_effort']}). Replay on {run['replay_claude_code_version']}; the session ran on "
          f"{f['claude_code_version']}.", "", f"Settings at run time: `{json.dumps(run['settings'])}`"]
    (Path(run_dir) / "report.md").write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file (default ~/.claude/compaction-evals/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def target(p):
        p.add_argument("--transcript", help="path to a session transcript (.jsonl)")
        p.add_argument("--session", help="session id; its transcript is looked up under ~/.claude/projects")
        p.add_argument("--boundary-line", type=int, help="which compaction (default: the latest)")
        p.add_argument("--turns", type=int, help="counterfactual window (default from config: 3)")
        p.add_argument("--noise", choices=["first", "all"], help="noise floor on the first turn or all turns")

    p = sub.add_parser("list", help="recent sessions that have compactions")
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("plan", help="facts, gate, turns and a priced estimate; spends nothing")
    target(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("run", help="score a compaction")
    target(p)
    p.add_argument("--counterfactual", choices=["yes", "no"], default="no")
    p.add_argument("--summary-override", help="file whose text replaces the summary (self-test)")
    p.add_argument("--reuse", help="earlier run folder: reuse its items and full/no-context answers")
    p.add_argument("--label", help="run label (default real, or planted with --summary-override)")
    p.add_argument("--answer-model")
    p.add_argument("--answer-effort")
    p.add_argument("--results-dir")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--keep-copies", action="store_true")
    p.add_argument("--force", action="store_true", help="run even if the summary gate fails")
    p.add_argument("--reuse-incidents-skip", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("plant", help="write a damaged copy of the summary for the self-test")
    target(p)
    p.add_argument("--mode", choices=["gut", "drop"], default="gut")
    p.add_argument("--drop", action="append", help="regex; summary lines matching it are removed")
    p.add_argument("--out", required=True)
    sub.add_parser("check-pricing", help="compare the price table with the live pricing page")

    args = ap.parse_args()
    cfg = load_config(args.config)
    {"list": cmd_list, "plan": cmd_plan, "run": cmd_run, "plant": cmd_plant,
     "check-pricing": cmd_check_pricing}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
