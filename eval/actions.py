#!/usr/bin/env python3
"""
actions.py -- replay action_gate.py over the REAL tool calls in agent transcripts.

For every tool call in every session, in order, the gate decides with only the
evidence that existed at that moment: user turns and tool outputs BEFORE the
call (the assistant's own text never counts), plus the operator's static
context if --static is given. Three measurements:

  real calls      how many side-effecting calls the gate would deny / ask /
                  allow. Real calls were mostly legitimate, so deny+ask is an
                  upper bound on friction; a sample goes to a PRIVATE file for
                  labeling (some real calls do carry unsourced values).
  mutation        every allowed real call whose values were all GROUNDED gets
                  ONE value mutated (last digit / last character changed) and
                  is re-judged. A gate that allows a mutated value is blind to
                  a mistyped or fabricated amount, address, hash or recipient.
  latency         wall time per side-effecting decision.

Output: aggregate JSON (safe to publish) + private samples.
"""
import argparse, glob, json, os, random, re, sys, time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import action_gate as A   # noqa: E402

B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def mutate(value, kind, rng):
    """change one character so the value is still well-formed but different."""
    if kind == "email":
        local, _, dom = value.partition("@")
        if not local:
            return None
        c = local[-1]
        return local[:-1] + ("x" if c != "x" else "y") + "@" + dom
    if kind == "strkey":
        c = value[-1]
        return value[:-1] + (B32[(B32.index(c) + 1) % 32] if c in B32 else "A")
    idx = [i for i, ch in enumerate(value) if ch.isdigit()]
    if idx:
        i = idx[-1]
        return value[:i] + str((int(value[i]) + 1 + rng.randrange(8)) % 10) + value[i + 1:]
    if kind in ("txhash", "base58") and value:
        c = value[-1]
        return value[:-1] + ("b" if c != "b" else "c")
    return None


def _ts(r):
    import datetime
    t = r.get("timestamp")
    try:
        return datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone().replace(tzinfo=None) if t else None
    except Exception:
        return None


def replay(path, static, stats, samples, rng, lat):
    """walk one session in order; judge every side-effecting call with the evidence
    that existed at that moment (the same SessionLedger the hook uses)."""
    led = A.SessionLedger(static)
    with open(path, encoding="utf-8", errors="replace") as f:
        rows = []
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    for r in rows:
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content")
        if r.get("type") == "assistant" and isinstance(content, list):
            now = _ts(r)
            for c in content:
                if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                    continue
                name, inp = c.get("name"), c.get("input")
                try:
                    side, why = A.classify(name, inp if isinstance(inp, dict) else {})
                except Exception:
                    side, why = True, "classify error"
                if not side:
                    continue
                tl, ul = led.snapshot()
                t0 = time.perf_counter()
                d = A.decide(name, inp, tl, ul, now=now)
                lat.append(time.perf_counter() - t0)
                fam = name if name not in A._SHELL_TOOLS else "Bash:" + re.sub(r"shell: '?", "", why)[:22]
                stats["calls"] += 1
                stats[d["decision"]] += 1
                stats["family:" + fam.split("__")[-1][:32] + ":" + d["decision"]] += 1
                if d["decision"] != "allow" and len(samples["flagged"]) < 300:
                    samples["flagged"].append({"session": os.path.basename(path)[:-6], "tool": name,
                                               "decision": d["decision"], "values": d["values"][:8],
                                               "input": json.dumps(inp, ensure_ascii=False)[:600]})
                grounded = [v for v in d["values"] if v["source"] == "GROUNDED"]
                if d["decision"] == "allow" and grounded:
                    v = rng.choice(grounded)
                    mv = mutate(v["value"], v["kind"], rng)
                    if mv and mv != v["value"]:
                        blob = json.dumps(inp, ensure_ascii=False).replace(v["value"], mv)
                        try:
                            minp = json.loads(blob)
                        except Exception:
                            minp = None
                        if isinstance(minp, dict):
                            md = A.decide(name, minp, tl, ul, now=now)
                            caught = md["decision"] != "allow"
                            stats["mut_n"] += 1
                            stats["mut_caught" if caught else "mut_missed"] += 1
                            stats[f"mut_kind:{v['kind']}:{'caught' if caught else 'missed'}"] += 1
                            if not caught and len(samples["mut_missed"]) < 80:
                                samples["mut_missed"].append({"tool": name, "value": v["value"], "mutated": mv,
                                                              "kind": v["kind"]})
        led.feed(r)


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0, c - h), 4), round(min(1, c + h), 4)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--static", action="append", default=[], help="trusted operator files (glob, repeatable)")
    ap.add_argument("--out", default=os.path.expanduser("~/.cache/hg-eval/actions.json"))
    ap.add_argument("--samples", default=os.path.expanduser("~/.cache/hg-eval/actions_samples.json"))
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    static = A.static_context(a.static)
    stats, lat = Counter(), []
    samples = {"flagged": [], "mut_missed": []}
    files = sorted(glob.glob(os.path.join(a.projects, "*", "*.jsonl")))
    for p in files:
        try:
            replay(p, static, stats, samples, rng, lat)
        except Exception as e:
            print(f"skip {p}: {e!r}", file=sys.stderr)
    n = stats["calls"]
    lat.sort()
    res = {
        "transcripts": len(files),
        "side_effect_calls": n,
        "decisions": {k: {"n": stats[k], "rate": round(stats[k] / n, 4) if n else None, "ci95": wilson(stats[k], n)}
                      for k in ("allow", "ask", "deny")},
        "by_family": {k[7:]: v for k, v in sorted(stats.items()) if k.startswith("family:")},
        "mutation": {"n": stats["mut_n"], "caught": stats["mut_caught"], "missed": stats["mut_missed"],
                     "catch_rate": round(stats["mut_caught"] / stats["mut_n"], 4) if stats["mut_n"] else None,
                     "ci95": wilson(stats["mut_caught"], stats["mut_n"]),
                     "by_kind": {k[9:]: v for k, v in sorted(stats.items()) if k.startswith("mut_kind:")}},
        "latency_ms": {"p50": round(1000 * lat[len(lat) // 2], 2) if lat else None,
                       "p95": round(1000 * lat[int(len(lat) * .95)], 2) if lat else None,
                       "max": round(1000 * lat[-1], 2) if lat else None},
        "static_context_chars": len(static),
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(res, open(a.out, "w"), indent=1)
    json.dump(samples, open(a.samples, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
