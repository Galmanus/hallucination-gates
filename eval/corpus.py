#!/usr/bin/env python3
"""
corpus.py -- turn real Claude Code transcripts into (draft, ledger, context) turns.

The output is PRIVATE: it holds raw tool output and conversation text. It is
written outside the repo (default ~/.cache/hg-eval/turns.jsonl) and never
committed. Only aggregate numbers from it go into RESULTS.md.

Turn slicing mirrors the production Stop-hook that runs grounding in front of
the agent (see README "provenance"): a turn starts at a real user prompt (a
user message that is neither a tool_result, nor "Stop hook feedback", nor a
meta/system injection). Inside a turn we keep:

  draft0   assistant text written BEFORE the first "Stop hook feedback" row,
           i.e. exactly what the gate saw the first time it ran on this turn
  ledger0  tool output returned in the same span (tool_result + toolUseResult)
  blocked  whether a grounding "Stop hook feedback" followed in this turn
  context  everything the agent could legitimately have known BEFORE the turn
           started: prior user prompts, prior tool output, prior assistant
           text, injected attachments (dates, hook context). Used only to ask
           "was this flagged token carried from earlier context?".

Stdlib only.
"""
import argparse, glob, json, os, sys

STOP_FB = "Stop hook feedback"


def _rows(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _content(r):
    m = r.get("message")
    return m.get("content") if isinstance(m, dict) else None


def is_real_prompt(r):
    if r.get("type") != "user" or r.get("isMeta"):
        return False
    c = _content(r)
    if isinstance(c, str):
        return not c.startswith(STOP_FB)
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return False
        txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return not txt.startswith(STOP_FB)
    return False


def is_stop_feedback(r):
    if r.get("type") != "user":
        return False
    c = _content(r)
    if isinstance(c, str):
        return c.startswith(STOP_FB) and "GROUNDING" in c
    if isinstance(c, list):
        txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return txt.startswith(STOP_FB) and "GROUNDING" in txt
    return False


def assistant_text(r):
    c = _content(r)
    if r.get("type") != "assistant" or not isinstance(c, list):
        return ""
    return "\n".join(x.get("text", "") for x in c
                     if isinstance(x, dict) and x.get("type") == "text")


def tool_output(r):
    parts = []
    c = _content(r)
    if isinstance(c, list):
        for x in c:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                rc = x.get("content")
                if isinstance(rc, str):
                    parts.append(rc)
                elif isinstance(rc, list):
                    parts += [y.get("text", "") for y in rc
                              if isinstance(y, dict) and y.get("type") == "text"]
    if "toolUseResult" in r:
        t = r["toolUseResult"]
        parts.append(t if isinstance(t, str) else json.dumps(t, ensure_ascii=False))
    return "\n".join(parts)


def other_text(r):
    """user prompts and attachments: context, not ledger."""
    if r.get("type") == "attachment":
        return json.dumps(r.get("attachment", {}), ensure_ascii=False)
    if r.get("type") == "user":
        c = _content(r)
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(x.get("text", "") for x in c
                             if isinstance(x, dict) and x.get("type") == "text")
    return ""


def turns_of(path, max_context=400_000):
    rows = _rows(path)
    starts = [i for i, r in enumerate(rows) if is_real_prompt(r)]
    ctx = []  # rolling prior context
    ctx_tools = []  # prior TOOL output only: sourced, but from an earlier turn
    prev = 0
    for k, s in enumerate(starts):
        for r in rows[prev:s]:
            ctx.append(other_text(r)); ctx.append(tool_output(r)); ctx.append(assistant_text(r))
            ctx_tools.append(tool_output(r))
        prompt_ctx = other_text(rows[s])
        end = starts[k + 1] if k + 1 < len(starts) else len(rows)
        draft, ledger, blocked = [], [], False
        for r in rows[s + 1:end]:
            if is_stop_feedback(r):
                blocked = True
                break
            draft.append(assistant_text(r)); ledger.append(tool_output(r))
        d = "\n".join(x for x in draft if x).strip()
        if d:
            context = "\n".join(x for x in ctx if x)[-max_context:] + "\n" + prompt_ctx
            yield {
                "session": os.path.basename(path)[:-6],
                "project": os.path.basename(os.path.dirname(path)),
                "ts": rows[s].get("timestamp"),
                "draft": d,
                "ledger": "\n".join(x for x in ledger if x),
                "context": context,
                "context_tools": "\n".join(x for x in ctx_tools if x)[-max_context:],
                "blocked_in_prod": blocked,
            }
        prev = s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--out", default=os.path.expanduser("~/.cache/hg-eval/turns.jsonl"))
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    files = glob.glob(os.path.join(a.projects, "*", "*.jsonl"))
    n = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for p in sorted(files):
            try:
                for t in turns_of(p):
                    f.write(json.dumps(t, ensure_ascii=False) + "\n"); n += 1
            except Exception as e:
                print(f"skip {p}: {e!r}", file=sys.stderr)
    print(f"{n} turns from {len(files)} transcripts -> {a.out}")


if __name__ == "__main__":
    main()
