#!/usr/bin/env python3
"""
run.py -- run every task twice with the same agent (Claude Code, headless):
  arm A  hooks OFF  (--settings '{"disableAllHooks": true}')
  arm B  hooks ON   (the operator's production hooks: grounding Stop hook with
                     revision check, action gate, liveness, selfcheck)
Everything else is identical: model, tools (read-only), turns. Each task runs with
its fixture dir as cwd, so no project CLAUDE.md and no project memory reach the agent
(only ~/.claude/CLAUDE.md, if any): the hooks are the only anti-hallucination layer
that differs between arms.

Records the final answer, and for arm B also the FIRST draft (before any hook
block), read back from the session transcript, so "what the user saw first"
and "what the user got in the end" can both be scored.
Needs the operator's Claude credentials: run it outside any sandbox.
"""
import argparse, concurrent.futures as cf, glob, json, os, subprocess, time

CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_BIN", "~/.local/bin/claude"))
TOOLS = "Read,Glob,Grep,Bash(cat:*),Bash(ls:*)"


def first_draft(session_id):
    """assistant text written before the first Stop-hook feedback in the session."""
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    if not hits:
        return None, 0
    parts, blocks = [], 0
    for line in open(hits[0], encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        c = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
        if r.get("type") == "user":
            txt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))
            if txt.startswith("Stop hook feedback"):
                blocks += 1
                if blocks == 1:
                    first = "\n".join(parts)
        if r.get("type") == "assistant" and isinstance(c, list):
            parts.extend(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return (first if blocks else "\n".join(parts)), blocks


def run_one(task, arm, max_turns, model):
    cmd = [CLAUDE, "-p", task["question"], "--output-format", "json", "--max-turns", str(max_turns),
           "--allowedTools", TOOLS]
    if model:
        cmd += ["--model", model]
    if arm == "A":
        cmd += ["--settings", json.dumps({"disableAllHooks": True})]
    t0 = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    # keep eval blocks out of the operator's production catch ledger (hook v2.6 honours this)
    env = dict(os.environ, GROUNDING_NO_CATCHSPACE="1")
    p = subprocess.run(cmd, cwd=task["dir"], capture_output=True, text=True, timeout=900, env=env)
    dt = time.time() - t0
    try:
        out = json.loads(p.stdout)
    except Exception:
        out = {"result": None, "parse_error": p.stdout[-500:], "stderr": p.stderr[-500:]}
    rec = {"id": task["id"], "arm": arm, "started": started, "final": out.get("result"), "session_id": out.get("session_id"),
           "cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"), "seconds": round(dt, 1),
           "is_error": out.get("is_error"), "exit": p.returncode}
    if arm == "B" and rec["session_id"]:
        rec["first_draft"], rec["hook_blocks"] = first_draft(rec["session_id"])
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=os.path.expanduser("~/.cache/hg-eval/ab/tasks/tasks.json"))
    ap.add_argument("--out", default=os.path.expanduser("~/.cache/hg-eval/ab/runs.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma list of task ids")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--max-turns", type=int, default=12)
    ap.add_argument("--model", default="")
    a = ap.parse_args()
    tasks = json.load(open(a.tasks))
    if a.only:
        keep = set(a.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    if a.limit:
        tasks = tasks[:a.limit]
    done = set()
    if os.path.exists(a.out):
        for line in open(a.out):
            try:
                r = json.loads(line)
                if r.get("final") is not None:
                    done.add((r["id"], r["arm"]))
            except Exception:
                pass
    jobs = [(t, arm) for t in tasks for arm in ("A", "B") if (t["id"], arm) not in done]
    print(f"{len(jobs)} runs to do ({len(done)} already done)", flush=True)
    with cf.ThreadPoolExecutor(a.parallel) as ex, open(a.out, "a", encoding="utf-8") as f:
        futs = {ex.submit(run_one, t, arm, a.max_turns, a.model): (t["id"], arm) for t, arm in jobs}
        for fu in cf.as_completed(futs):
            try:
                rec = fu.result()
            except Exception as e:
                tid, arm = futs[fu]
                rec = {"id": tid, "arm": arm, "final": None, "error": repr(e)}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(rec["id"], rec["arm"], "cost", rec.get("cost_usd"), "blocks", rec.get("hook_blocks"), flush=True)


if __name__ == "__main__":
    main()
