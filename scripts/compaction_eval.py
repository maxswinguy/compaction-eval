#!/usr/bin/env python3
"""compaction-eval: score how well a Claude Code compaction kept what mattered.

Commands: list, plan, run, leak-check, self-test-status, history, tradeoff, overview, plant, check-pricing.

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
    # Self-test verdict. Retention counts only items the no-context agent missed, so the no-context
    # side scores 0 by construction; a fair self-test (every route scrubbed) should land near it.
    # Measured 2026-11-07: four fair self-tests 0.0%; five with routes left open 25.0 to 71.9%.
    # 10 fails all five and leaves room for a stray item or two. Recalibrate as runs accumulate.
    "self_test_max_retention_pct": 10,
    "self_test_min_items": 10,               # fewer scored items: the verdict is not trusted
    "kept_reply_warn_chars": 1000,           # plan warns when the reply kept after the summary is longer
    "replay_short_chars": 40,                # plan warns when the replay is one message at most this long
    "capture_timeout_s": 180,                # the free request capture of the self-test's leak check
}

# Attachment types Claude Code adds after a compaction that describe the setup (instructions, tool and
# skill listings, environment), not the session. Every other attachment after the boundary carries the
# session's own content (a re-attached file, a note naming a file read before, the skills invoked with
# their arguments, a file edited on disk), so the self-test scrubs it. An unknown type is scrubbed too:
# a new attachment type is more likely to leak than to be needed.
GENERIC_ATTACHMENTS = {
    "instructions", "session_context", "date", "deferred_tools_delta", "deferred_tools_record",
    "agent_listing_delta", "skill_listing", "mcp_instructions_delta", "environment", "model",
    "hook_success", "hook_additional_context", "credential_org", "remote_session_change",
    "prompt_snapshot", "thinking_drop",
}
SCRUBBED = "(removed for the compaction-eval self-test)"
GUT_SUMMARY = ("This session is being continued from a previous conversation. "
               "The summary of the earlier part is not available.")

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

    # Files Claude Code re-attaches after the compaction (recently read or written files). The
    # compacted agent really has them, so a real run keeps them; a self-test blanks them, because a
    # session that wrote its own handoff files carries most of its facts there, not in the summary
    # (measured 2026-11-07: a gutted summary still scored 71.9% on a session with 3 such files).
    reattached, session_atts = [], []
    for j in range(bi + 1, nxt):
        att = t.recs[j].get("attachment")
        if t.recs[j].get("type") != "attachment" or not isinstance(att, dict):
            continue
        if att.get("type") == "file":
            body = ((att.get("content") or {}).get("file") or {}).get("content")
            reattached.append({"idx": j, "line": t.lineno[j], "file": att.get("filename"),
                               "chars": len(body) if isinstance(body, str) else None})
        if att.get("type") not in GENERIC_ATTACHMENTS:
            session_atts.append({"idx": j, "line": t.lineno[j], "type": att.get("type")})

    # Messages Claude Code keeps word for word after the summary. They sit BEFORE the boundary in the
    # file and are re-linked after the summary when the session loads (compactMetadata.preservedMessages),
    # so following the records after the boundary never shows them. Measured 2026-11-07 on four
    # compactions: each kept the agent's last reply (1,505 to 5,842 characters, a closing recap three
    # times) and the user's answer to it, and a gutted summary still scored 41 to 51% from them.
    pm = meta.get("preservedMessages") or {}
    pu = list(dict.fromkeys((pm.get("allUuids") or []) + (pm.get("uuids") or [])))
    preserved = sorted(t.by[u] for u in pu if u in t.by)
    kept = [{"idx": j, "line": t.lineno[j], "type": t.recs[j].get("type"), "chars": len(text_of(t.recs[j]))}
            for j in preserved if t.recs[j].get("type") in ("user", "assistant", "attachment")]
    kept_reply_chars = sum(k["chars"] for k in kept if k["type"] == "assistant")
    return {
        "bi": bi, "boundary_line": t.lineno[bi], "boundary_uuid": buuid, "summary_idx": summ,
        "summary_text": summary_text, "pre_leaf_uuid": pre_leaf_uuid, "post": post, "turns": turns,
        "incident_msgs": incident_msgs, "reattached": reattached, "kept": kept,
        "session_attachments": session_atts,
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
            "reattached_files": len(reattached),
            "reattached_chars": sum(r["chars"] or 0 for r in reattached),
            "kept_messages": len(kept),
            "kept_reply_chars": kept_reply_chars,
            "session_attachments": sum(1 for s in session_atts
                                       if first_post_asst is None or s["idx"] < first_post_asst),
        },
    }


def summary_gate(text):
    markers = [m for m in ("primary request", "pending", "current work", "key technical", "files and code",
                           "errors", "problem solving", "user messages", "next step")
               if m in text.lower()]
    ok = len(text) >= 1500 and len(markers) >= 2
    return {"passed": ok, "chars": len(text), "section_markers_found": len(markers)}


IDLE_NOTICE = "written while you were away"   # Claude Code's own message after an idle compaction


def settings_in_force():
    out = {"read_at": now_iso(),
           "note": "read from ~/.claude/settings.json when the eval ran, not when the compaction ran; see at_compaction"}
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


def at_compaction(t, a):
    """What this compaction's own transcript proves about the settings at that moment. Claude Code writes
    no settings snapshot into the transcript, so this holds only facts and bounds read from the records,
    never a value taken from a settings file. A window size cannot be read at all: a session that reached
    N tokens without compacting for size only shows that its window was at least N."""
    f = a["facts"]
    nxt = next((j for j in t.boundaries if j > a["bi"]), len(t.recs))
    notice = any(r.get("type") == "system" and r.get("subtype") == "informational"
                 and IDLE_NOTICE in str(r.get("content")) for r in t.recs[a["bi"] + 1:nxt])
    peak = max((ctx_tokens(r) for r in t.recs[:a["bi"]] if r.get("type") == "assistant"
                and not r.get("isSidechain")), default=0)
    idle = notice or f["idle_signature"]
    out = {"source": "this compaction's transcript records; nothing here comes from a settings file",
           "trigger": f["trigger"], "model": f["session_model"], "effort": f["session_effort"],
           "claude_code_version": f["claude_code_version"],
           "idle_compaction": ("on, recorded in the transcript" if notice
                               else "on, by its signature (no notice found)" if idle else "not shown")}
    if idle:
        out["auto_compact_window_at_least"] = peak   # it had not compacted for size at this context
        out["idle_min_tokens_at_most"] = f["preTokens"]
    elif f["trigger"] == "auto":
        out["auto_compact_window_about"] = f["preTokens"]   # compacted for size near this context
    settings = Path.home() / ".claude" / "settings.json"
    ts = parse_ts(f["boundary_at"])
    if settings.exists() and ts:
        changed = dt.datetime.fromtimestamp(settings.stat().st_mtime, dt.timezone.utc) > ts
        out["settings_file_modified_after_compaction"] = changed
    return out


# ---------------------------------------------------------------- copies and calls

def work_project_dir(cfg):
    wd = Path(cfg["work_dir"]).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    proj = PROJECTS / slug(wd)
    proj.mkdir(parents=True, exist_ok=True)
    return wd, proj


def _scrub_strings(o, removed, keep_keys=("type",)):
    """Replace every string leaf (except the values of `keep_keys`) with SCRUBBED; collect originals."""
    if isinstance(o, dict):
        return {k: (v if k in keep_keys else _scrub_strings(v, removed, keep_keys)) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub_strings(v, removed, keep_keys) for v in o]
    if isinstance(o, str) and o.strip() and o != SCRUBBED:
        removed.append(o)
        return SCRUBBED
    return o


def scrub_record(d):
    """Remove the session's own content from one record, in place (self-test only). Returns the original
    strings removed. An attachment loses every string in `attachment` and in `rendered` (the text the
    model reads), file names included: a file name alone answered an item exactly (2026-11-07). A kept
    message loses its text, thinking, tool inputs and tool results."""
    removed = []
    if d.get("type") == "attachment":
        d["attachment"] = _scrub_strings(d.get("attachment") or {}, removed)
        if isinstance(d.get("rendered"), list):
            for r in d["rendered"]:
                if isinstance(r, dict) and isinstance(r.get("content"), str):
                    removed.append(r["content"])
                    r["content"] = f"<system-reminder>\n{SCRUBBED}\n</system-reminder>"
        return removed
    m = d.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        removed.append(c)
        m["content"] = SCRUBBED
    for x in c if isinstance(c, list) else []:
        if not isinstance(x, dict):
            continue
        ty = x.get("type")
        if ty == "text":
            removed.append(x.get("text", ""))
            x["text"] = SCRUBBED
        elif ty == "tool_use":
            removed.append(json.dumps(x.get("input"), ensure_ascii=False))
            x["input"] = {}
        elif ty == "tool_result":
            cc = x.get("content")
            removed.append(cc if isinstance(cc, str) else json.dumps(cc, ensure_ascii=False))
            x["content"] = SCRUBBED
        elif ty in ("image", "thinking", "redacted_thinking"):
            # A thinking block's text is empty in the transcript, but its signature is sent and carries the
            # thinking itself (seen in a captured request, 2026-11-07). Turn it into placeholder text.
            removed.append(x.get("signature") or x.get("data") or ty)
            x.clear()
            x.update({"type": "text", "text": SCRUBBED})
    if "toolUseResult" in d:
        d["toolUseResult"] = _scrub_strings(d["toolUseResult"], removed)
    return [s for s in removed if s and s.strip()]


def make_copy(cfg, t, upto_idx, splice=(), reparent_to=None, summary_idx=None, override=None, scrub=()):
    """Write records 0..upto_idx (+ spliced records) as a new session file. Never touches the source.
    `scrub`: indices of records whose session content is removed (self-test only, see scrub_record).
    `cost-state` records are always left out: Claude Code restores the session's running cost from the
    last one on resume and adds it to every call's total_cost_usd (measured 2026-11-07: $32.70 added
    to each of three calls, so a $10.47 run reported $108.56).
    In a self-test copy (`scrub` given), the original session id is also replaced everywhere by the
    copy's: Claude Code stores it in the environment block (the scratchpad path), where it answered an
    item asking for the session's id prefix (2026-11-07).
    Returns (session id, path, number of records scrubbed, original strings removed)."""
    _, proj = work_project_dir(cfg)
    sid = str(uuid.uuid4())
    dest = proj / f"{sid}.jsonl"
    scrub, n_scrubbed, removed = set(scrub), 0, []
    orig = t.path.stem.encode()
    with open(dest, "x") as f:
        for i in range(upto_idx + 1):
            d = json.loads(t.raw[i].replace(orig, sid.encode()) if scrub else t.raw[i])
            if d.get("type") == "cost-state":
                continue
            if "sessionId" in d:
                d["sessionId"] = sid
            if override is not None and i == summary_idx:
                d["message"]["content"] = override
            if i in scrub:
                got = scrub_record(d)
                if got:
                    n_scrubbed += 1
                    removed += got
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        for n, i in enumerate(splice):
            d = json.loads(t.raw[i])
            if d.get("type") == "cost-state":
                continue
            if "sessionId" in d:
                d["sessionId"] = sid
            if n == 0 and reparent_to:
                d["parentUuid"] = reparent_to
                d.pop("logicalParentUuid", None)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return sid, str(dest), n_scrubbed, removed


# ---------------------------------------------------------------- the free leak check

def capture_request(cfg, model, session=None, cut=None):
    """Have Claude Code build the request it would send for this copy, and catch it locally instead of
    sending it: `claude -p` runs with ANTHROPIC_BASE_URL pointing at a catcher on 127.0.0.1 that keeps
    the body and answers 400. Nothing reaches the API, so this costs nothing. Request headers are never
    read or kept. Returns the text of the request's messages (all string values, joined), or None."""
    import http.server
    import threading
    bodies = []

    class Catcher(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n)
            if "/messages" in self.path:
                bodies.append(body)
            msg = b'{"type":"error","error":{"type":"invalid_request_error","message":"captured"}}'
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Catcher)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wd, _ = work_project_dir(cfg)
    cmd = [cfg["claude_bin"], "-p"]
    if session:
        cmd += ["--resume", session]
    if cut:
        cmd += ["--resume-session-at", cut]
    cmd += CLAUDE_FLAGS + ["--model", model, "(compaction-eval leak check)"]
    env = dict(os.environ, ANTHROPIC_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        subprocess.run(cmd, capture_output=True, text=True, cwd=str(wd), env=env,
                       stdin=subprocess.DEVNULL, timeout=cfg["capture_timeout_s"])
    except subprocess.TimeoutExpired:
        pass
    finally:
        srv.shutdown()
    for body in reversed(bodies):
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            continue
        if d.get("messages"):
            out = []

            def walk(o):
                if isinstance(o, dict):
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
                elif isinstance(o, str):
                    out.append(o)
            walk(d["messages"])
            return "\n".join(out)
    return None


def leak_check(cfg, t, a, sum_upto, cut, override, scrubbed_sid, removed, model, copies):
    """The self-test's free leak check. Claude Code builds the summary-side request three times and a
    local catcher keeps it: from a copy that is NOT scrubbed (the control: the removed text must be found
    there), from the scrubbed copy (it must be gone), and with no session (text the setup itself carries,
    which is not a leak). Appends the extra copy's path to `copies` so the caller deletes it.
    Returns (check, scrubbed request text, no-session request text)."""
    print("Leak check (free: the requests are caught locally, nothing is sent)")
    cans = canaries(removed)
    raw_sid, raw_p, _, _ = make_copy(cfg, t, sum_upto, summary_idx=a["summary_idx"], override=override)
    copies.append(raw_p)
    req_raw = capture_request(cfg, model, session=raw_sid, cut=cut)
    req_sum = capture_request(cfg, model, session=scrubbed_sid, cut=cut)
    req_null = capture_request(cfg, model)
    if not (req_raw and req_sum and req_null):
        return ({"verdict": "Claude Code did not build one of the requests, so the self-test cannot show it "
                            "is a fair control"}, req_sum, req_null)
    # Text that is not the session's: what a request with no session carries, and what the setup records
    # after the boundary carry (the skill listing, the instructions), which the self-test keeps.
    setup = []
    for j in range(a["bi"] + 1, sum_upto + 1):
        att = t.recs[j].get("attachment")
        if isinstance(att, dict) and att.get("type") in GENERIC_ATTACHMENTS:
            _scrub_strings(t.recs[j], setup)
    not_session = (req_null + "\n" + "\n".join(setup)).lower()
    found_raw = [c for c in cans if c in req_raw and c.lower() not in not_session]
    found_sum = [c for c in cans if c in req_sum and c.lower() not in not_session]
    control = (override or "").strip()[:80]
    check = {"canaries": len(cans), "found_unscrubbed": len(found_raw), "found_scrubbed": len(found_sum),
             "leaks": found_sum[:20], "planted_summary_seen": bool(control) and control in req_sum and control in req_raw,
             "request_chars": {"unscrubbed": len(req_raw), "scrubbed": len(req_sum), "none": len(req_null)}}
    print(f"  {len(cans)} distinctive lines removed; found in the unscrubbed request: {len(found_raw)}; "
          f"in the scrubbed request: {len(found_sum)}")
    if not check["planted_summary_seen"] or (cans and not found_raw):
        check["verdict"] = ("the control failed (the planted summary or the removed text was not found where "
                            "it must be), so the check proves nothing")
    elif found_sum:
        check["verdict"] = "session content still reaches the summary side: " + " | ".join(found_sum[:5])
    else:
        check["verdict"] = "clean"
    return check, req_sum, req_null


def answers_reachable(items, req_sum, req_null):
    """Items whose literal answer (a path, an id, a number) is in the scrubbed request and not in the
    no-session one: a route the scrub does not cover."""
    home = str(Path.home())
    s_low = req_sum.replace(home, "~").lower()
    n_low = req_null.replace(home, "~").lower()
    return [it["id"] for it in items if literals(it["answer"]) and
            all(x.lower() in s_low for x in literals(it["answer"])) and
            not all(x.lower() in n_low for x in literals(it["answer"]))]


def canaries(strings):
    """Distinctive lines of the removed strings: for each, up to 3 of its longest lines (8 to 120
    characters). Lines survive the rendering Claude Code applies (line-number prefixes, escaping)."""
    out = []
    for s in strings:
        lines = sorted({ln.strip() for ln in str(s).splitlines() if 8 <= len(ln.strip()) <= 120},
                       key=len, reverse=True)
        for ln in lines[:3]:
            if ln not in out and SCRUBBED not in ln:
                out.append(ln)
    return out


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


COST_LIMIT = ("This is an estimate, not a bill. It takes the calls that really followed the compaction and adds "
              "back the history the compaction removed, at API list prices. A session that had kept its full history "
              "might have gone differently, and would have reached its auto-compact limit and compacted anyway. On a "
              "Claude subscription nothing is billed per call: read it as how much more of your usage limits the same "
              "work would have used.")


def cost_side(t, a, prices):
    """What the compaction saved, from the token counts the transcript records per API call. Method: take every
    call of the main conversation after the compaction, up to the next compaction or the end of the session,
    once each (a call streamed over several records is one call; a subagent's call is untouched by the compaction).
    Without the compaction, each would have carried the removed history too: read from the prompt cache at the
    cache-read price, or written again at the cache-write price when the gap since the previous call exceeds the
    cache's lifetime. The transcript does not record the call that wrote the summary, so its cost is estimated:
    one cached read of the history, plus the summary's length (4 characters a token) as output. On the one
    compaction where a running-cost record brackets it (2026-11-08, Substack Reader), the unexplained remainder
    was $0.29 for an estimate of $0.23: it ran on the cache, not a cold rewrite."""
    f = a["facts"]
    removed = max((f["context_before_tokens"] or 0) - (f["context_after_tokens"] or 0), 0)
    nxt = next((j for j in t.boundaries if j > a["bi"]), len(t.recs))
    calls, order = {}, []
    for j in range(a["bi"] + 1, nxt):
        r = t.recs[j]
        m = r.get("message") or {}
        if r.get("type") != "assistant" or r.get("isSidechain") or not m.get("usage") or m.get("model") == "<synthetic>":
            continue
        key = m.get("id") or r.get("uuid")
        if key not in calls:
            order.append(key)
        calls[key] = r
    ttl_1h = any(((calls[k]["message"]["usage"].get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0) > 0
                 for k in order)
    ttl = 3600 if ttl_1h else 300
    write_key = "cache_write_1h" if ttl_1h else "cache_write_5m"
    prev = parse_ts(f["last_activity_before"])
    actual = extra = 0.0
    cold, peak, unpriced, first = 0, 0, set(), None
    for k in order:
        r = calls[k]
        m = r["message"]
        ts = parse_ts(r["timestamp"])
        ctx = ctx_tokens(r)
        peak = max(peak, ctx)
        c = price_usage(prices, m["model"], m["usage"])
        rates = rates_for(prices, m["model"], ctx + removed)
        if c is None or rates is None:
            unpriced.add(m["model"])
            prev = ts
            continue
        is_cold = prev is not None and ts is not None and (ts - prev).total_seconds() > ttl
        cold += is_cold
        add = removed * (rates[write_key] if is_cold else rates["cache_read"]) / 1e6
        if first is None:
            first = {"with": c, "without": c + add,
                     "gap_h": round((ts - prev).total_seconds() / 3600, 2) if prev and ts else None}
        actual += c
        extra += add
        prev = ts
    rates = rates_for(prices, f["session_model"], f["context_before_tokens"] or 0) if f["session_model"] else None
    own = (round(((f["context_before_tokens"] or 0) * rates["cache_read"]
                  + (f["summary_chars"] or 0) / 4 * rates["output"]) / 1e6, 4) if rates else None)
    return {"calls": len(order), "cold_calls": cold, "history_removed_tokens": removed,
            "actual_usd": round(actual, 4), "extra_usd_without_compaction": round(extra, 4),
            "actual_plus_extra_usd": round(actual + extra, 4),
            "first_reply_usd_with": round(first["with"], 4) if first else None,
            "first_reply_usd_without": round(first["without"], 4) if first else None,
            "first_reply_gap_hours": first["gap_h"] if first else None,
            "peak_context_with_tokens": peak, "peak_context_without_tokens": peak + removed if order else None,
            "compaction_own_usd_estimate": own,
            "net_saved_usd": round(extra - own, 4) if own is not None else None,
            "cache_ttl_seconds": ttl, "unpriced_models": sorted(unpriced), "prices_taken": prices["taken"]}


def cost_lines(c):
    """The saving in plain language, with its limit on the same screen."""
    if not c["calls"]:
        return ["No call followed this compaction, so there is nothing to compare.", COST_LIMIT]
    L = [f"Without this compaction, the {c['calls']} API call(s) that followed it would have cost about "
         f"${c['actual_plus_extra_usd']:.2f} instead of ${c['actual_usd']:.2f}: about "
         f"${c['extra_usd_without_compaction']:.2f} more. The compaction removed {c['history_removed_tokens']:,} tokens "
         f"of history from every one of them, and {c['cold_calls']} came after a break long enough for that history "
         "to have left the cache.",
         f"First reply after it ({c['first_reply_gap_hours']} hours after the last message): about "
         f"${c['first_reply_usd_with']:.2f} with the compaction, about ${c['first_reply_usd_without']:.2f} without.",
         f"Largest context these calls reached: {c['peak_context_with_tokens']:,} tokens, against about "
         f"{c['peak_context_without_tokens']:,} without the compaction."]
    if c["compaction_own_usd_estimate"] is not None:
        L.append(f"Producing the summary itself cost about ${c['compaction_own_usd_estimate']:.2f} (estimated, the "
                 f"transcript does not record that call), so the net saving is about ${c['net_saved_usd']:.2f}.")
    if c["unpriced_models"]:
        L.append(f"Left out, no price in the table: {', '.join(c['unpriced_models'])}.")
    L.append(COST_LIMIT)
    return L


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
    est_reuse = estimate(cfg, a, prices, counterfactual=True, turns=args.turns, noise=args.noise, reuse=True)
    est_reuse_probe = estimate(cfg, a, prices, counterfactual=False, reuse=True)
    return path, t, a, prices, est, est_probe, est_reuse, est_reuse_probe


def print_plan(path, t, a, prices, est, est_probe, est_reuse, est_reuse_probe, cfg, args):
    f = a["facts"]
    gate = summary_gate(a["summary_text"])
    n_turns = min(args.turns or cfg["turns"], f["turns_available"])
    print(f"Transcript: {path}")
    print(f"  read {t.prefix_lines} lines, md5 of what was read {t.prefix_md5}")
    print(f"Compaction at line {a['boundary_line']} ({len(t.boundaries)} in this transcript)")
    for k in ("trigger", "preTokens", "postTokens", "compression_ratio", "precomputed", "idle_signature",
              "gap_hours", "cache_cold", "context_before_tokens", "context_after_tokens",
              "session_model", "session_effort", "claude_code_version", "turns_available", "summary_chars",
              "reattached_files", "reattached_chars", "kept_messages", "kept_reply_chars", "session_attachments"):
        print(f"  {k}: {f[k]}")
    if f["kept_reply_chars"] > cfg["kept_reply_warn_chars"]:
        print(f"  ! Claude Code kept the agent's last reply word for word after the summary "
              f"({f['kept_reply_chars']} characters). If it is a recap, a real score credits what it carries "
              "to the compaction, not to the summary alone.")
    print(f"Summary gate: {'passed' if gate['passed'] else 'FAILED'} "
          f"({gate['chars']} characters, {gate['section_markers_found']} section markers)")
    print(f"Counterfactual window: {n_turns} turn(s) of {f['turns_available']} available")
    if n_turns == 1 and a["turns"] and len(a["turns"][0]["text"].strip()) <= cfg["replay_short_chars"]:
        msg = a["turns"][0]["text"].strip()
        print(f"  ! The replay has only one short message ({len(msg)} characters: \"{msg}\"). It adds about "
              f"${round(est['total_usd'] - est_probe['total_usd'], 2)} to the probe alone and shows little about "
              "what the agent would do differently.")
    k = at_compaction(t, a)
    print("Known from the transcript about this compaction (nothing else records the settings at that moment):")
    for key, value in k.items():
        if key != "source":
            print(f"  {key}: {value}")
    print(f"Estimate at API prices taken {est['prices_taken']} ({est['prices_source']}):")
    print(f"  probe only: ${est_probe['total_usd']}")
    print(f"  probe + counterfactual: ${est['total_usd']}")
    print(f"  reusing a self-test's questions (--reuse): probe only ${est_reuse_probe['total_usd']}, "
          f"probe + counterfactual ${est_reuse['total_usd']}")
    if est["prices_stale"]:
        print(f"  ! the price table is {est['prices_age_days']} days old: run check-pricing")
    if est["models_without_verified_price"]:
        print(f"  ! no verified price for {', '.join(est['models_without_verified_price'])}; "
              "those calls are left out of the estimate")
    print("  Upper bound: the context sizes include tool definitions the replay leaves out.")
    print(f"  {SUBSCRIPTION_NOTE}")
    print("What the compaction saved (free, from the transcript):")
    for line in cost_lines(cost_side(t, a, prices)):
        print(f"  {line}")


def cmd_plan(args, cfg):
    path, t, a, prices, est, est_probe, est_reuse, est_reuse_probe = plan_data(args, cfg)
    if args.json:
        out = {"transcript": str(path), "lines_read": t.prefix_lines, "md5_read": t.prefix_md5,
               "boundary_line": a["boundary_line"], "facts": a["facts"],
               "summary_gate": summary_gate(a["summary_text"]),
               "turns": [{"n": x["n"], "line": x["line"], "text": x["text"][:200]} for x in a["turns"]],
               "estimate_probe_only": est_probe, "estimate_with_counterfactual": est,
               "estimate_reuse_probe_only": est_reuse_probe, "estimate_reuse_with_counterfactual": est_reuse,
               "at_compaction": at_compaction(t, a), "cost_side": cost_side(t, a, prices),
               "cost_limit": COST_LIMIT, "subscription_note": SUBSCRIPTION_NOTE}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_plan(path, t, a, prices, est, est_probe, est_reuse, est_reuse_probe, cfg, args)


def cmd_plant(args, cfg):
    path = find_transcript(args.transcript, args.session)
    a = analyze(Transcript(path), args.boundary_line)
    text = a["summary_text"]
    if args.mode == "gut":
        planted = GUT_SUMMARY
    else:
        if not args.drop:
            sys.exit("--mode drop needs at least one --drop PATTERN")
        pats = [re.compile(p, re.I) for p in args.drop]
        kept = [ln for ln in text.split("\n") if not any(p.search(ln) for p in pats)]
        print(f"Dropped {len(text.splitlines()) - len(kept)} of {len(text.splitlines())} summary lines.")
        planted = "\n".join(kept)
    Path(args.out).write_text(planted)
    print(f"Planted summary written to {args.out} ({len(planted)} characters, original {len(text)}).")


def self_test_scope(t, a):
    """The summary side's cut for the probe, and the records a self-test scrubs up to it."""
    cuts = [t.by[x["summary_cut"]] for x in a["turns"][:1]]
    sum_upto = max(cuts + [a["summary_idx"] or 0])
    cut = a["turns"][0]["summary_cut"] if a["turns"] else t.recs[sum_upto]["uuid"]
    to_scrub = sorted({r["idx"] for r in a["kept"]} |
                      {r["idx"] for r in a["session_attachments"] if r["idx"] <= sum_upto})
    return sum_upto, cut, to_scrub


def cmd_leak_check(args, cfg):
    """The self-test's leak check alone, free: is a gutted self-test a fair control on this compaction?"""
    path = find_transcript(args.transcript, args.session)
    t = Transcript(path)
    a = analyze(t, args.boundary_line)
    override = Path(args.summary_override).read_text() if args.summary_override else GUT_SUMMARY
    model = args.answer_model or a["facts"]["session_model"]
    sum_upto, cut, to_scrub = self_test_scope(t, a)
    copies, check, n = [], {"verdict": "not run"}, -1
    try:
        sid, p, n, removed = make_copy(cfg, t, sum_upto, summary_idx=a["summary_idx"], override=override,
                                       scrub=to_scrub)
        copies.append(p)
        print(f"Compaction at line {a['boundary_line']}: {len(to_scrub)} records to scrub, {n} scrubbed:")
        for i in to_scrub:
            r = t.recs[i]
            print(f"  line {t.lineno[i]}: {r.get('type')} {(r.get('attachment') or {}).get('type') or ''}")
        check, req_sum, req_null = leak_check(cfg, t, a, sum_upto, cut, override, sid, removed, model, copies)
        if args.items and req_sum and req_null:
            items = json.loads(Path(args.items).read_text())
            check["answers_reachable"] = answers_reachable(items, req_sum, req_null)
            print(f"  item answers still in the scrubbed request: {', '.join(check['answers_reachable']) or 'none'}")
    finally:
        for p in copies:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
    print(f"Verdict: {check['verdict']}")
    if args.json:
        print(json.dumps(check, indent=2, ensure_ascii=False))
    sys.exit(0 if check["verdict"] == "clean" and n == len(to_scrub)
             and not check.get("answers_reachable") else 1)


def load_log(cfg, results_dir=None):
    log = Path(results_dir or cfg["results_dir"]).expanduser() / "log.jsonl"
    lines = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
    return log, lines


def is_self_test(x):
    # The first self-test (2026-11-06) was logged as "planted-gut"; later ones as "planted".
    return str(x.get("label", "")).startswith("planted")


def self_test_reading(x, bar):
    """Whether one self-test's log line counts as a fair pass today, and if not, why."""
    v = x.get("self_test_passed")
    if v is None:
        return "no verdict (older run, does not count)"
    if v is False:
        return "failed"
    # `kept_messages` entered the log with the all-routes scrub and its leak check (2026-11-07):
    # a pass logged before it left the kept reply and other routes open, so it proves nothing.
    if "kept_messages" not in x:
        return "passed before every route was scrubbed (does not count)"
    r = x.get("retention_pct")
    if r is None or r > bar:
        return f"passed under an older bar, above today's {bar}% (does not count)"
    return "passed"


def fair_passes(lines, bar, session_id, boundary_line):
    """Fair self-test passes on one compaction. A pass counts only for the compaction it ran on: whether
    the scrub closes every route depends on that session (its kept reply, re-attached files, skills)."""
    return [x for x in lines if is_self_test(x) and x.get("session_id") == session_id
            and x.get("boundary_line") == boundary_line and self_test_reading(x, bar) == "passed"]


def cmd_self_test_status(args, cfg):
    """Exit 0 when a self-test passed fairly on the compaction named (session, boundary line): every
    route scrubbed, retention at or below the current bar. 1 otherwise, and 1 when no compaction is
    named: a pass on one compaction says nothing about another."""
    log, lines = load_log(cfg, args.results_dir)
    bar = cfg["self_test_max_retention_pct"]
    tests = [x for x in lines if is_self_test(x)]
    if not (args.session or args.transcript):
        print(f"{len(tests)} self-test run(s) in {log}. A self-test counts only for the compaction it ran on: "
              "name one with --session (and --boundary-line).")
        for x in tests:
            print(f"  {x.get('date')} {x.get('session_id', '')[:8]} L{x.get('boundary_line')}: retention "
                  f"{x.get('retention_pct')}%, {self_test_reading(x, bar)}")
        sys.exit(1)
    path = find_transcript(args.transcript, args.session)
    a = analyze(Transcript(path), args.boundary_line)
    sid, bl = path.stem, a["boundary_line"]
    mine = [x for x in tests if x.get("session_id") == sid and x.get("boundary_line") == bl]
    passed = fair_passes(lines, bar, sid, bl)
    print(f"Compaction {sid[:8]} line {bl}: {len(mine)} self-test run(s) in {log}; "
          f"{len(passed)} count as a fair pass.")
    for x in mine:
        print(f"  {x.get('date')}: retention {x.get('retention_pct')}%, {self_test_reading(x, bar)} "
              f"({Path(x.get('run_dir', '')).name})")
    if passed:
        print(f"Real runs on this compaction can reuse its questions: --reuse {passed[-1].get('run_dir')}")
    sys.exit(0 if passed else 1)


def run_root(run_dir):
    """The run whose questions a run used: follow --reuse back to the run that extracted them."""
    seen, d = set(), str(run_dir or "")
    while d and d not in seen:
        seen.add(d)
        try:
            nxt = json.loads((Path(d) / "run.json").read_text()).get("reuse")
        except (OSError, ValueError):
            return Path(d).name
        if not nxt:
            return Path(d).name
        d = str(nxt)
    return Path(d).name


def session_name(session_id):
    """The session's title (Claude Code's `custom-title` records), or None: not every session has one."""
    hits = sorted(PROJECTS.glob(f"*/{session_id}.jsonl"))
    if not hits:
        return None
    for line in reversed(hits[0].read_bytes().split(b"\n")):
        if b'"custom-title"' in line:
            try:
                title = json.loads(line).get("customTitle")
            except json.JSONDecodeError:
                continue
            if title:
                return str(title)
    return None


def session_label(session_id):
    name = session_name(session_id)
    return f"{name} [{session_id[:8]}]" if name else session_id[:8]


def real_reading(x, passes):
    """How a real score reads today: validated on the same questions, on other questions, or not at all."""
    roots = {run_root(p.get("run_dir")) for p in passes}
    if not passes:
        return "real score, NOT validated: no fair self-test on this compaction"
    if run_root(x.get("run_dir")) in roots:
        return "real score, validated by a fair self-test on the same questions"
    return ("real score, the compaction passed a fair self-test but on other questions; "
            "a self-test with --reuse of this run checks these")


def cmd_history(args, cfg):
    """Every logged run, grouped by compaction, with what each result can and cannot be trusted for today.
    The log lines are left as written; this reading is recomputed from them and the run folders."""
    log, lines = load_log(cfg, args.results_dir)
    bar = cfg["self_test_max_retention_pct"]
    groups = {}
    for n, x in enumerate(lines, 1):
        groups.setdefault((x.get("session_id", ""), x.get("boundary_line")), []).append((n, x))
    print(f"{len(lines)} run(s) in {log}, on {len(groups)} compaction(s).")
    for (sid, bl), xs in groups.items():
        passes = fair_passes([x for _, x in xs], bar, sid, bl)
        print(f"\n{session_label(sid)} line {bl}: "
              + (f"fair self-test passed ({len(passes)})" if passes else "NO fair self-test has passed"))
        kept = next((x for _, x in reversed(xs) if "kept_reply_chars" in x), None)
        if kept:
            print(f"  kept messages {kept['kept_messages']}, the agent's kept reply "
                  f"{kept['kept_reply_chars']} characters, re-attached files {kept.get('reattached_files')}")
        else:
            print("  kept reply not recorded for this compaction: `plan` shows it, for free")
        for n, x in xs:
            head = (f"  log line {n}, {x.get('date')}, {x.get('label')}: {x.get('retention_pct')}% of "
                    f"{x.get('items_scored')} items")
            if is_self_test(x):
                reading = "self-test, " + self_test_reading(x, bar)
            else:
                reading = real_reading(x, passes)
            notes = []
            try:
                cost = json.loads((Path(x.get("run_dir", "")) / "run.json").read_text()).get("cost") or {}
                cc, tb = cost.get("actual_usd_claude_code"), cost.get("actual_usd_table")
                if cc and tb and abs(cc - tb) > max(0.05, 0.02 * tb):
                    notes.append(f"its report's Claude Code cost figure ${cc} is wrong; the price-table "
                                 f"figure ${tb} is right")
            except (OSError, ValueError):
                notes.append("run folder not found")
            if not is_self_test(x) and x.get("divergence_mean") is not None:
                notes.append(f"replay divergence {x.get('divergence_mean')}, noise {x.get('noise_floor')}")
            print(f"{head}: {reading}" + ("".join(f"; {m}" for m in notes)) + ".")


def cmd_tradeoff(args, cfg):
    """One compaction, both sides: what it lost (the logged real score) and what it saved (the transcript's token
    counts). Spends nothing."""
    path = find_transcript(args.transcript, args.session)
    t = Transcript(path)
    a = analyze(t, args.boundary_line)
    _, lines = load_log(cfg, args.results_dir)
    bar = cfg["self_test_max_retention_pct"]
    bl = a["boundary_line"]
    passes = fair_passes(lines, bar, path.stem, bl)
    print(f"{session_label(path.stem)}, compaction at line {bl} ({a['facts']['boundary_at']})")
    real = [x for x in lines if not is_self_test(x) and x.get("session_id") == path.stem and x.get("boundary_line") == bl]
    print("\nWhat it gave up:")
    if not real:
        print("  No real score is logged for this compaction, so the loss side is empty. `plan` prices one.")
    else:
        x = real[-1]
        cats = x.get("by_category") or {}
        total = {k: sum(c.get(k, 0) for c in cats.values()) for k in ("items", "kept", "distorted", "lost")}
        print(f"  Weighted retention {x.get('retention_pct')}% of {x.get('items_scored')} scored items "
              f"({real_reading(x, passes)}). Critical losses: {x.get('critical_losses')}.")
        print(f"  Kept {total['kept']}, distorted {total['distorted']}, lost {total['lost']} of {total['items']}.")
        for cat, c in sorted(cats.items()):
            print(f"    {cat}: {c['kept']} kept of {c['items']}")
        try:
            rows = json.loads((Path(x["run_dir"]) / "probe_scores.json").read_text())["items"]
            for r in rows:
                if r["status"] == "scored" and r["severity"] == 2 and r["summary"] != "kept":
                    print(f"  Critical, {r['summary']}: {r['question']}")
        except (OSError, ValueError, KeyError):
            print("  (the run folder with the items is not available)")
        print("  This counts everything the agent had after the compaction: the summary, and the messages and "
              "files Claude Code kept beside it. It does not measure the summary alone.")
    print("\nWhat it saved:")
    for line in cost_lines(cost_side(t, a, load_prices())):
        print(f"  {line}")


NUM_WORDS = ["None", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve"]
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"]
KIND_LABELS = {"ruling": "Rulings", "decision": "Decisions", "id_path": "Exact ids and paths",
               "constraint": "Constraints", "open_task": "Open tasks", "fact": "Facts"}
OVERVIEW_WORD_LIMIT = 900        # at five compactions; each further one adds a table row
OVERVIEW_CRITICAL_SHOWN = 8      # the rest stay in each run's report.md


def plain_name(session_id):
    name = session_name(session_id)
    if not name:
        return session_id[:8]
    # A session title's leading status mark and its " — subtitle" are dropped: the table needs a short name.
    return re.sub(r"^[^\w\[(]+", "", name).split(" — ")[0].strip()


def overview_rows(lines, cfg):
    """Which runs an overview may count: for each compaction, the latest real score a fair self-test vouches
    for on the same questions. Anything else is left out and named, because an unvalidated score could not be
    told from an empty summary. Returns (rows in log order, [(compaction label, why left out)])."""
    bar = cfg["self_test_max_retention_pct"]
    groups = {}
    for n, x in enumerate(lines, 1):
        groups.setdefault((x.get("session_id", ""), x.get("boundary_line")), []).append((n, x))
    rows, left_out = [], []
    for (sid, bl), xs in groups.items():
        passes = fair_passes([x for _, x in xs], bar, sid, bl)
        roots = {run_root(p.get("run_dir")) for p in passes}
        reals = [(n, x) for n, x in xs if not is_self_test(x)]
        good = [(n, x) for n, x in reals if run_root(x.get("run_dir")) in roots]
        if good:
            rows.append({"n": good[-1][0], "x": good[-1][1], "sid": sid, "line": bl})
        else:
            left_out.append((f"{plain_name(sid)}, line {bl} [{sid[:8]}]",
                             "its real score is not validated by a fair self-test on the same questions" if reals
                             else "no real score yet"))
    rows.sort(key=lambda r: r["n"])
    per_session = {}
    for r in rows:
        per_session.setdefault(r["sid"], []).append(r)
    for r in rows:
        same = per_session[r["sid"]]
        r["name"] = plain_name(r["sid"])
        if len(same) > 1:
            r["name"] += f", {ORDINALS[same.index(r)] if same.index(r) < len(ORDINALS) else 'later'} compaction"
    return rows, left_out


def overview_cost(row, prices):
    """The saving for one row: from the log when the run recorded it, else from the transcript, else None."""
    if row["x"].get("cost_side"):
        return row["x"]["cost_side"]
    hits = sorted(PROJECTS.glob(f"*/{row['sid']}.jsonl"))
    if not hits:
        return None
    try:
        t = Transcript(hits[0])
        return cost_side(t, analyze(t, row["line"]), prices)
    except (SystemExit, OSError, ValueError, KeyError):
        return None


def build_overview(lines, cfg):
    """A plain-language report across every logged compaction, in the shape Maxime approved on 2026-11-08
    (trials/2026-11_compaction-evals/overview-draft-2026-11-08.md): conclusion first, session names, what
    each kind of thing kept, what it means per setting, and what the data cannot support."""
    rows, left_out = overview_rows(lines, cfg)
    if not rows:
        L = ["# What your compactions gave up and what they saved", "",
             "No compaction has a validated real score yet, so there is nothing to report across compactions. "
             "A score counts here once a fair self-test has passed on the same questions."]
        if left_out:
            L += ["", "Left out:"] + [f"- {name}: {why}." for name, why in left_out]
        return "\n".join(L) + "\n"

    prices = load_prices()
    for r in rows:
        r["cost"] = overview_cost(r, prices)
        r["cats"] = r["x"].get("by_category") or {}
        r["tot"] = {k: sum(c.get(k, 0) for c in r["cats"].values()) for k in ("items", "kept", "distorted", "lost")}
    tot = {k: sum(r["tot"][k] for r in rows) for k in ("items", "kept", "distorted", "lost")}
    missing = tot["distorted"] + tot["lost"]
    critical = sum(r["x"].get("critical_losses") or 0 for r in rows)
    priced = [r for r in rows if r["cost"] and r["cost"].get("calls") and r["cost"].get("net_saved_usd") is not None]
    net = sum(r["cost"]["net_saved_usd"] for r in priced)
    n = len(rows)
    idle = [r for r in rows if r["x"].get("idle_signature")]

    head = (f"**Across {n} compaction{'s' if n != 1 else ''}, the agent kept {tot['kept']} of the {tot['items']} things "
            f"that mattered, blurred {tot['distorted']} and lost {tot['lost']}, and the compactions saved about "
            f"${net:.0f} of API-priced work on what followed. "
            f"{NUM_WORDS[critical] if critical < len(NUM_WORDS) else critical} of the {missing} blurred or lost items "
            "would have led to a wrong action.**")
    L = ["# What your compactions gave up and what they saved", "",
         f"*Generated {dt.date.today().isoformat()} from the compaction log. Length limit: {OVERVIEW_WORD_LIMIT} words at five "
         f"compactions, one more table row for each further one, and at most {OVERVIEW_CRITICAL_SHOWN} critical items listed.*",
         "", head, ""]
    kind = ("All are idle compactions" if len(idle) == n else f"{len(idle)} of the {n} are idle compactions")
    L += [f"{kind}: Claude Code compacted a long session while you were away from it. The log does not record a setting "
          "being varied between them, so none shows which setting is better.", ""]

    L += ["## The five compactions" if n == 5 else "## The compactions", "",
          "| Session | History removed (tokens) | Kept | Score | Critical | Net saved | First reply, with / without |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        c, x = r["cost"], r["x"]
        have = c and c.get("calls")
        L.append(f"| {r['name']} | {c['history_removed_tokens']:,} | " if have else f"| {r['name']} | not available | ")
        L[-1] += (f"{r['tot']['kept']} of {r['tot']['items']} | {x.get('retention_pct'):g}% | {x.get('critical_losses')} | "
                  + (f"${c['net_saved_usd']:.2f} | ${c['first_reply_usd_with']:.2f} / ${c['first_reply_usd_without']:.2f} |"
                     if have and c.get("net_saved_usd") is not None else "not available | not available |"))
    L += ["", "*Score* counts important items more. *Critical* means forgetting it causes a wrong action. *Net saved* is the "
          "extra cost of the later calls with the full history, minus an estimate of what writing the summary cost.", ""]

    kinds = {}
    for r in rows:
        for cat, c in r["cats"].items():
            d = kinds.setdefault(cat, {"items": 0, "kept": 0})
            d["items"] += c.get("items", 0)
            d["kept"] += c.get("kept", 0)
    ordered = sorted(kinds.items(), key=lambda kv: (-kv[1]["kept"] / max(kv[1]["items"], 1), kv[0]))
    label = lambda cat: KIND_LABELS.get(cat, cat.replace("_", " ").capitalize())
    L += ["## What survives, by kind", "", "| Kind | Kept |", "|---|---|"]
    L += [f"| {label(cat)} | {c['kept']} of {c['items']} ({100 * c['kept'] / max(c['items'], 1):.0f}%) |" for cat, c in ordered]
    best = [label(cat) for cat, c in ordered if c["kept"] == c["items"]]
    worst = label(ordered[-1][0])
    note = (f"{' and '.join(best)} survived every time. " if best else "")
    if len(ordered) > 1:
        note += f"{worst} fared worst."
    L += ["", note.strip(), ""]

    crit_items = []
    for r in rows:
        try:
            items = json.loads((Path(r["x"]["run_dir"]) / "probe_scores.json").read_text())["items"]
        except (OSError, ValueError, KeyError):
            continue
        for it in items:
            if it["status"] == "scored" and it["severity"] == 2 and it["summary"] != "kept":
                crit_items.append((r["name"], it["summary"], it["question"]))
    word = {"lost": "lost", "distorted": "blurred"}
    L += ["## The four critical items" if len(crit_items) == 4 else "## The critical items", ""]
    L += [f"- **{name}** {word.get(g, g)} the answer to: {q}" for name, g, q in crit_items[:OVERVIEW_CRITICAL_SHOWN]]
    if len(crit_items) > OVERVIEW_CRITICAL_SHOWN:
        L.append(f"- And {len(crit_items) - OVERVIEW_CRITICAL_SHOWN} more, in each run's report.")
    if not crit_items:
        L.append("None.")
    L.append("")

    runs = {}
    replays = []
    incidents = {"n": 0, "of": 0}
    for r in rows:
        x = r["x"]
        if x.get("divergence_mean") is not None and x.get("noise_floor") is not None:
            replays.append((x["divergence_mean"], x["noise_floor"]))
        try:
            run_json = json.loads((Path(x["run_dir"]) / "run.json").read_text())
            if run_json.get("incidents"):
                incidents["n"] += run_json["incidents"]["incidents"]
                incidents["of"] += run_json["incidents"]["messages"]
        except (OSError, ValueError, KeyError):
            pass
    L += ["## How the agent behaved afterwards", ""]
    sentence = []
    if replays:
        above = sum(1 for d, nf in replays if d > nf)
        prefix = (f"On the {len(replays)} compactions with a replay, the compacted agent differed" if above == len(replays)
                  else f"On {above} of the {len(replays)} compactions with a replay, the compacted agent differed")
        sentence.append(f"{prefix} from the full-history agent more than two runs of the same agent differ "
                        f"({min(d for d, _ in replays):.1f} to {max(d for d, _ in replays):.1f} on a 0 to 3 scale, against "
                        f"{min(nf for _, nf in replays):g} to {max(nf for _, nf in replays):.1f}).")
    if incidents["of"]:
        sentence.append(f"{incidents['n']} later message{'s' if incidents['n'] != 1 else ''} of {incidents['of']} "
                        "had to restate something.")
    L += [" ".join(sentence) or "No replay or later messages were judged.", ""]

    L += ["## What this means for each setting", ""]
    pidle = [r for r in idle if r in priced]
    if pidle:
        gains = [r["cost"]["first_reply_usd_without"] - r["cost"]["first_reply_usd_with"] for r in pidle]
        nets = [r["cost"]["net_saved_usd"] for r in pidle]
        L += [f"**`idleCompaction`** (compact a long session after a break). The gain: ${min(gains):.2f} to ${max(gains):.2f} "
              f"less on the first reply back, and ${min(nets):.2f} to ${max(nets):.2f} net over what followed. The price: "
              f"about {100 * missing / max(tot['items'], 1):.0f}% of the items blurred or lost ({missing} of {tot['items']}). "
              "Turning it off keeps the full history after a break, at the \"without\" column's price. Nothing here shows the "
              "full-history session would have gone better.", ""]
    else:
        L += ["**`idleCompaction`** (compact a long session after a break). No idle compaction is priced here.", ""]
    if idle:
        L += [f"**`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`** (the size from which idle compaction runs). All {len(idle)} idle "
              f"compactions were at {min(r['x'].get('preTokens') or 0 for r in idle):,} tokens or more, so this says nothing "
              "about smaller sessions.", ""]
    size = [r for r in rows if not r["x"].get("idle_signature") and r["x"].get("trigger") == "auto"]
    if size:
        pts = [r["x"].get("preTokens") or 0 for r in size]
        L += [f"**`autoCompactWindow`** (how large a session grows before compacting for size). {len(size)} of the {n} compacted "
              f"for size, at {min(pts):,} to {max(pts):,} tokens; the log does not compare windows.", ""]
    else:
        L += [f"**`autoCompactWindow`** (how large a session grows before compacting for size). None of the {n} compacted for "
              "size, so there is no evidence here.", ""]

    no_at = sum(1 for x in lines if "at_compaction" not in x)
    L += ["## What the data cannot support", "",
          "- **Which setting to choose.** Nothing was compared under two settings.",
          "- **Other kinds of compaction.** Only idle ones were scored." if len(idle) == n else
          f"- **Other kinds of compaction.** {n - len(idle)} of the {n} are not idle; too few to compare the kinds.",
          "- **The summary alone.** Each score counts everything the agent had: the summary, and the messages and files "
          "Claude Code kept beside it.",
          (f"- **Whether a loss cost anything in practice.** Apart from those {incidents['of']} messages, the eval does not "
           "follow what the session did next." if incidents["of"] else
           "- **Whether a loss cost anything in practice.** The eval does not follow what the session did next."),
          f"- **Precise percentages.** One user, {n} compaction{'s' if n != 1 else ''}, {tot['items']} items, one extraction "
          "choosing the questions, one judge grading once. A kind with few items moves several points on one item.",
          "- **The dollar figures as bills.** They are estimates at API list prices, on the calls that really happened. A session "
          "keeping its full history might have gone differently, and would have reached its auto-compact limit and compacted "
          "anyway. On a subscription nothing is billed per call.",
          "- **The settings written in the log.** They were read when each eval ran, not when the compaction ran"
          + (f"; {no_at} of the {len(lines)} log lines have nothing else, so their settings may be wrong for the compaction." if no_at
             else "; each line also records what its transcript proves."), ""]

    if left_out:
        L += ["## Compactions left out", ""] + [f"- {name}: {why}." for name, why in left_out] + [""]

    tests = [x for x in lines if is_self_test(x)]
    bar = cfg["self_test_max_retention_pct"]
    fair = [x for x in tests if self_test_reading(x, bar) == "passed"]
    failed = [x for x in tests if self_test_reading(x, bar) == "failed"]
    older = len(tests) - len(fair) - len(failed)
    reals = [x for x in lines if not is_self_test(x)]
    rng = (f", between {min(x['retention_pct'] for x in fair):g}% and {max(x['retention_pct'] for x in fair):g}%"
           if fair else "")
    L += [f"## The {len(lines)} runs behind this", "",
          f"{len(reals)} real scores ({n} shown here, the other {len(reals) - n} superseded or not validated) and {len(tests)} "
          f"self-tests. Of those self-tests, {len(fair)} are fair passes{rng}, and each of the {n} scores rests on one. "
          f"{len(failed)} failed and {older} date from before the test was fair; they do not count."]
    return "\n".join(L) + "\n"


def cmd_overview(args, cfg):
    """Plain-language report across every compaction logged so far. Spends nothing."""
    _, lines = load_log(cfg, args.results_dir)
    text = build_overview(lines, cfg)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Overview written to {args.out} ({len(text.split())} words).")
    else:
        print(text, end="")


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
    strip = override is not None and not args.keep_reattached
    do_cf = args.counterfactual == "yes"
    if strip and do_cf:
        sys.exit("The self-test scrubs the user's message that turn 1 replies to, so it runs the probe only: "
                 "use --counterfactual no.")
    validated_by = None
    if override is None:
        # A real score is trusted only once a gutted summary has scored low on the same compaction.
        _, logged = load_log(cfg, args.results_dir)
        passes = fair_passes(logged, cfg["self_test_max_retention_pct"], path.stem, a["boundary_line"])
        if passes:
            validated_by = passes[-1].get("run_dir")
        elif not args.unvalidated:
            sys.exit(f"No fair self-test has passed on this compaction ({path.stem[:8]}, line "
                     f"{a['boundary_line']}), so its real score could not be trusted. Run the self-test first "
                     "(plant --mode gut, then run --summary-override <file> --counterfactual no), then this "
                     "run with --reuse <the self-test's folder>. --unvalidated runs anyway; the log marks it.")
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
           "at_compaction": at_compaction(t, a), "cost_side": cost_side(t, a, prices),
           "reuse": args.reuse, "estimate": est, "copies": [],
           "reattached": [{k: r[k] for k in ("line", "file", "chars")} for r in a["reattached"]],
           "kept": [{k: r[k] for k in ("line", "type", "chars")} for r in a["kept"]],
           "session_attachments": [{k: r[k] for k in ("line", "type")} for r in a["session_attachments"]],
           "reattached_stripped": strip, "validated_by": validated_by}
    write_json(run_dir / "run.json", run)
    print(f"Run folder: {run_dir}")

    # The two copies. Full side: everything before the compaction, plus the real post-compaction
    # turns needed by the counterfactual, re-attached after the last pre-compaction message.
    need = a["turns"][:n_turns]
    splice = need[-1]["splice"] if need else []
    full_sid = sum_sid = None
    try:
        if not args.reuse or n_turns:
            full_sid, p, _, _ = make_copy(cfg, t, a["bi"] - 1, splice=splice, reparent_to=a["pre_leaf_uuid"])
            run["copies"].append(p)
        cuts = [t.by[x["summary_cut"]] for x in (need or a["turns"][:1])]
        sum_upto = max(cuts + [a["summary_idx"] or 0])
        # Self-test: every route by which the session's own content reaches the summary side is scrubbed,
        # not only the summary: the kept messages, and every non-generic attachment up to the cut.
        to_scrub = self_test_scope(t, a)[2] if strip else []
        sum_sid, p, n_scrubbed, removed = make_copy(cfg, t, sum_upto, summary_idx=a["summary_idx"],
                                                    override=override, scrub=to_scrub)
        run["copies"].append(p)
        run["scrubbed"] = [{"line": t.lineno[i], "type": t.recs[i].get("type"),
                            "attachment": (t.recs[i].get("attachment") or {}).get("type")} for i in to_scrub]
        run["reattached_blanked"] = n_scrubbed
        if strip and n_scrubbed != len(to_scrub):
            sys.exit(f"Self-test: scrubbed {n_scrubbed} of {len(to_scrub)} records; a record held no text "
                     "where scrub_record looks, so the format may have changed and the self-test would not "
                     "be a fair control. Fix scrub_record first.")
        write_json(run_dir / "run.json", run)
        sum_cut1 = a["turns"][0]["summary_cut"] if a["turns"] else t.recs[sum_upto]["uuid"]

        # The free leak check, before anything is spent. Claude Code builds the summary-side request three
        # times and a local catcher keeps it: from a copy that is NOT scrubbed (the positive control: the
        # removed text must be found there), from the scrubbed copy (it must be gone), and with no session
        # (text the setup itself carries, which is not a leak).
        if strip:
            check, req_sum, req_null = leak_check(cfg, t, a, sum_upto, sum_cut1, override, sum_sid, removed,
                                                  sess_model, run["copies"])
            run["leak_check"] = check
            write_json(run_dir / "run.json", run)
            if check["verdict"] != "clean":
                sys.exit(f"Leak check: {check['verdict']}. Nothing was spent.")

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
        if strip:
            # A literal answer still in the scrubbed request is a route the scrub does not cover: it is
            # reported, and it fails the verdict when the item is scored.
            reach = answers_reachable(items, req_sum, req_null)
            run["leak_check"]["answers_reachable"] = reach
            write_json(run_dir / "run.json", run)
            print(f"  item answers still in the scrubbed request: {', '.join(reach) or 'none'}")

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
    self_test = None
    if label == "planted":
        r, n = probe["retention_pct"], probe["items_scored"]
        reasons = []
        if not strip:
            reasons.append("the session's other routes were kept (--keep-reattached), so this is not the fair control")
        reach = (run.get("leak_check") or {}).get("answers_reachable") or []
        scored_ids = {x["id"] for x in rows if x["status"] == "scored"}
        if strip and set(reach) & scored_ids:
            reasons.append("answers of scored items still reach the summary side by another route: "
                           + ", ".join(sorted(set(reach) & scored_ids)))
        if n < cfg["self_test_min_items"]:
            reasons.append(f"only {n} scored items (minimum {cfg['self_test_min_items']})")
        if r is None or r > cfg["self_test_max_retention_pct"]:
            reasons.append(f"retention {r}% is above {cfg['self_test_max_retention_pct']}%")
        self_test = {"passed": not reasons, "retention_pct": r, "items_scored": n,
                     "max_retention_pct": cfg["self_test_max_retention_pct"],
                     "reattached_stripped": strip, "records_scrubbed": run.get("reattached_blanked"),
                     "leak_check": run.get("leak_check"), "reasons": reasons}
    run.update({"finished": now_iso(), "headline": headline, "probe": probe, "counterfactual": cf,
                "self_test": self_test,
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
        "settings": run["settings"], "at_compaction": run["at_compaction"], "cost_side": run["cost_side"],
        "extractor_model": cfg["extractor_model"], "judge_model": judge,
        **headline, "by_category": probe["by_category"],
        "cost_estimate_usd": est["total_usd"], "cost_actual_usd": actual_usd, "tokens": tokens,
        "prices_taken": prices["taken"], "run_dir": str(run_dir),
        "reattached_files": f["reattached_files"], "reattached_stripped": strip,
        "kept_messages": f["kept_messages"], "kept_reply_chars": f["kept_reply_chars"],
        "records_scrubbed": run.get("reattached_blanked") if strip else 0,
    }
    if override is None:
        log_line["validated_by"] = validated_by
    if self_test is not None:
        log_line["self_test_passed"] = self_test["passed"]
    log = Path(args.results_dir or cfg["results_dir"]) / "log.jsonl"
    with open(log, "a") as fh:
        fh.write(json.dumps(log_line, ensure_ascii=False) + "\n")
    print(f"\nReport: {run_dir / 'report.md'}\nLog: {log}")
    print(json.dumps(headline, indent=2))
    if self_test is not None:
        print("Self-test: " + ("PASSED" if self_test["passed"] else "FAILED: " + "; ".join(self_test["reasons"])))


def write_report(run_dir, run, rows):
    f, h, c = run["facts"], run["headline"], run["cost"]
    L = [f"# Compaction eval: {run['session_id'][:8]}, line {run['boundary_line']} ({run['label']})", ""]
    L += [f"Run {run['started']}. Transcript `{run['transcript']}`, {run['lines_read']} lines read "
          f"(md5 {run['md5_read']}).", ""]
    if run["summary_override"]:
        st = run.get("self_test") or {}
        blanked = (f"{run.get('reattached_blanked')} records of the session's other routes scrubbed: kept "
                   "messages, re-attached files, file notes, invoked skills" if run.get("reattached_stripped")
                   else "the other routes kept, so this is not the fair control")
        L += [f"**Self-test run:** the summary was replaced by `{run['summary_override']}` ({blanked}). "
              f"Verdict: **{'PASSED' if st.get('passed') else 'FAILED'}**"
              + (f" ({'; '.join(st.get('reasons', []))})" if st.get("reasons") else "")
              + f". It passes at {st.get('max_retention_pct')}% retention or below.", ""]
        lc = run.get("leak_check")
        if lc:
            L += [f"**Leak check (free):** {lc['canaries']} distinctive lines removed; found in the request built "
                  f"from the unscrubbed copy: {lc['found_unscrubbed']} (the control); in the scrubbed request: "
                  f"{lc['found_scrubbed']}. Item answers still reachable: "
                  f"{', '.join(lc.get('answers_reachable') or []) or 'none'}.", ""]
    if not run["summary_override"]:
        L += [(f"**Validated:** a fair self-test passed on this compaction (`{Path(run['validated_by']).name}`)."
               if run.get("validated_by") else "**Not validated:** no fair self-test had passed on this "
               "compaction when this run was made, so its score is not trusted."), ""]
    if run.get("kept"):
        reply = sum(k["chars"] for k in run["kept"] if k["type"] == "assistant")
        L += [f"**Kept messages:** Claude Code kept {len(run['kept'])} record(s) from before the compaction word "
              f"for word after the summary, among them the agent's last reply ({reply} characters). "
              + ("They were scrubbed for this self-test." if run.get("reattached_stripped") else
                 "The compacted agent had them, so retention counts what they carry, not only the summary's."), ""]
    if run.get("reattached"):
        L += [f"**Re-attached files:** Claude Code re-attached {len(run['reattached'])} file(s) after the "
              "compaction. " + ("They were blanked for this self-test." if run.get("reattached_stripped") else
                                "The compacted agent had them, so retention counts facts they carry, not only "
                                "the summary's.")]
        L += [f"- line {r['line']}: `{r['file']}` ({r['chars']} characters)" for r in run["reattached"]]
        L += [""]
    if run.get("cost_side"):
        L += ["## What this compaction lost, and what it saved", "",
              f"**Lost:** retention {h['retention_pct']}% of {h['items_scored']} scored items, "
              f"{h['critical_losses']} critical loss(es).", "",
              "**Saved:** " + " ".join(cost_lines(run["cost_side"])), ""]
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
          f"{f['claude_code_version']}.", "",
          f"Known from the transcript about this compaction: `{json.dumps(run.get('at_compaction'))}`", "",
          f"Settings file when the eval ran (it may differ from the compaction's): `{json.dumps(run['settings'])}`"]
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
    p.add_argument("--unvalidated", action="store_true",
                   help="real run without a fair self-test on this compaction; the log and report mark it")
    p.add_argument("--keep-reattached", action="store_true",
                   help="self-test only: keep the session's other routes (kept messages, re-attached files, "
                        "file notes, invoked skills); not the fair control, never passes")
    p = sub.add_parser("leak-check", help="free: would a gutted self-test still let session content through?")
    target(p)
    p.add_argument("--summary-override", help="planted summary (default: the gutted one)")
    p.add_argument("--items", help="items.json of an earlier run: also list answers still reachable")
    p.add_argument("--answer-model")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("self-test-status", help="has a fair self-test passed on this compaction? exit 0 if yes")
    target(p)
    p.add_argument("--results-dir")
    p = sub.add_parser("history", help="every logged run, with what each result can be trusted for today")
    p.add_argument("--results-dir")
    p = sub.add_parser("tradeoff", help="free: one compaction, what it lost beside what it saved")
    target(p)
    p.add_argument("--results-dir")
    p = sub.add_parser("overview", help="free: a plain-language report across every logged compaction")
    p.add_argument("--results-dir")
    p.add_argument("--out", help="write the report to this file instead of printing it")
    p = sub.add_parser("plant", help="write a damaged copy of the summary for the self-test")
    target(p)
    p.add_argument("--mode", choices=["gut", "drop"], default="gut")
    p.add_argument("--drop", action="append", help="regex; summary lines matching it are removed")
    p.add_argument("--out", required=True)
    sub.add_parser("check-pricing", help="compare the price table with the live pricing page")

    args = ap.parse_args()
    cfg = load_config(args.config)
    {"list": cmd_list, "plan": cmd_plan, "run": cmd_run, "plant": cmd_plant,
     "check-pricing": cmd_check_pricing, "self-test-status": cmd_self_test_status,
     "leak-check": cmd_leak_check, "history": cmd_history, "tradeoff": cmd_tradeoff, "overview": cmd_overview}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
