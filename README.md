# hallucination-gates

[![selftest](https://github.com/Galmanus/hallucination-gates/actions/workflows/selftest.yml/badge.svg)](https://github.com/Galmanus/hallucination-gates/actions/workflows/selftest.yml)

**Checks that catch an AI agent stating numbers it never saw, and stop it from acting on values it made up.**

**Who it is for:** anyone who runs an AI agent, either in Claude Code or one their team built. Setting it up means running commands on a computer. It does not plug into the ChatGPT or Claude chat websites.

## What problem it solves

An AI agent is an AI assistant that does more than chat. It searches the web, reads files, calls other programs, and can take actions such as sending messages or making payments.

AI agents sometimes make things up. Not often, but when they do, they sound just as sure as when they are right.

This project started with one case. An agent was asked for the last bus of the day between two cities. It answered with a bus company, a departure time, an arrival time, a price and a count of free seats. It sounded like it had checked. It had not. None of the searches it ran returned any of those details. It invented all of them.

Switching to a better model does not fix this on its own. Language models are trained to give a likely-sounding answer, and the tests they are graded on reward a lucky guess over "I don't know" ([Kalai et al., 2025](https://arxiv.org/abs/2509.04664)). You cannot count on the model to stop guessing. You can check what it says and does before it matters.

## What you get

- **Numbers without a source get caught.** Before the reply is final, the agent has to find the source, mark the number as an estimate, or remove it. If it still cannot after two tries, the reply goes out with a visible warning.
- **Actions get checked before they run.** Before your agent pays, sends or deploys through a command or tool the checker recognizes, every amount and address is checked. A made-up value is refused. A value that only came from a web page or someone else's email needs your OK first. That is the classic scam: "our bank details have changed, pay this account instead".
- **Free and open source (MIT).** You download a folder of Python files. The two main checks need Python 3.9 or newer and nothing else: no account, no API key, no internet connection, no extra AI model. They work with any model, because they only read text.
- **Fast.** The reply check takes about 10 milliseconds on half of replies, and a few seconds on the longest ones. The action check takes about a quarter of a second.

## How it works

First, one word. A **lookup** is anything the agent fetches while working on a reply: a web search, a web page, a file, or an answer from another program. Engineers call these "tool calls".

The checker adds two checkpoints between the agent and the world.

**1. Before a reply is final.** Every price, percentage, date, time, count like "3 of 5", IP address and transaction hash in the reply must appear in a lookup made for that same reply, or in what you typed. A number from earlier in the conversation does not count: the agent has to show again where it came from. If a number has no source, the reply goes back to the agent with the list of those numbers.

**2. Before the agent acts.** Every amount, address and ID in the action must come from you or from a lookup. An invented value is refused. A value that only came from text someone else wrote makes the agent stop and ask you.

Think of a teacher asking "where did you read that?". The checker does not know whether a number is true. It only knows whether the agent can point to where the number came from. No source, no number, unless it is clearly marked as an estimate.

## A small example

The agent looked up an invoice. The lookup returned:

```text
invoice 1042 · subtotal 72.19 · fee 9.86 · paid 2026-09-28 10:55 · receipt 0x9f3c2a71e4b05d88
```

This is the agent's draft reply, line by line:

| the agent wrote | what the checker does |
|---|---|
| Paid on 28/09 at 10:55. | **passes**: same date and time, written a different way |
| Charged $72.19 + $9.86 = $82.05. | **passes**: both amounts were in the lookup, and it redoes the sum |
| Receipt: 0x9f3c2a71e4b05d8a | **blocked**: the last character differs from the real receipt |
| Chargeback rate this month: 0.4%. | **blocked**: no lookup said that |

The agent fixes the receipt and writes "~0.4% (estimate)". Now the reply passes.

## How to use it

Setting this up means typing commands in a terminal. If you have never done that, send this section to whoever set up your AI agent.

- **Do you use Claude Code?** Use option 1.
- **Did your team build its own agent?** Use option 2 or 3. Both are for your engineer.

First, download the code. You need Python 3.9 or newer.

```bash
git clone https://github.com/Galmanus/hallucination-gates
cd hallucination-gates
./run_selftests.sh     # optional: checks the tool works on your computer (339 built-in tests, no internet needed)
```

### Option 1: in Claude Code, automatically

Claude Code is Anthropic's AI agent that runs on your computer. It can run a command of your choice at set moments, and it calls these commands "hooks". The settings below add two hooks:

- **When the agent finishes a reply** ("Stop"), `grounding.py` checks it. If a number has no source, Claude Code gives the reply back to the agent with the list of those numbers, and the agent has to correct it before the turn ends. It gets two chances. If a number still has no source after that, the reply ends with a visible warning, so it never gets stuck in a loop. In the Claude Code window you still see the first draft, followed by the corrected one.
- **Before the agent runs a command or a tool that changes something** ("PreToolUse"), `action_gate.py` checks the values in it.

Add this to your Claude Code settings file: `~/.claude/settings.json` for every project, or `.claude/settings.json` inside one project. Replace `/path/to` with the folder you downloaded in the step above. If the file already has hooks, add these entries next to them.

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "timeout": 15,
                     "command": "python3 /path/to/hallucination-gates/grounding.py --hook" } ] }
    ],
    "PreToolUse": [
      { "matcher": "Bash|PowerShell|mcp__.*|Artifact|ArtifactData|RemoteTrigger|CronCreate|PushNotification",
        "hooks": [ { "type": "command", "timeout": 15,
                     "command": "python3 /path/to/hallucination-gates/action_gate.py --hook" } ] }
    ]
  }
}
```

To start gently, change the action_gate line to exactly this. In this mode it asks you instead of refusing:

```json
"command": "ACTION_GATE_MODE=ask python3 /path/to/hallucination-gates/action_gate.py --hook"
```

More settings, such as counting your own notes as a trusted source, are in [docs/TECHNICAL.md](docs/TECHNICAL.md#hooks).

### Option 2: with any other AI agent (for your engineer)

Save the agent's reply in one file and everything it looked up in another, then run:

```bash
python3 grounding.py check --draft answer.txt --ledger lookups.txt
```

For the invoice example, the output is:

```text
grounding: HALLUCINATION_RISK
  danger-tokens: 6  |  grounded: 4  |  declared-uncertain: 0  |  derived: 1  |  composed: 0  |  flagged: 1
  --- FLAGGED (no discrete grounding in ledger) ---
  [UNSOURCED] percent  '0.4%'
           line: 'Chargeback rate this month: 0.4%.'
```

How to read it: `HALLUCINATION_RISK` means blocked, and `GROUNDED_OK` means every number has a source or is marked as an estimate. Each `[UNSOURCED]` line is one number with no source, followed by the line it came from.

The command finishes with code 0 when every number has a source, code 1 when something was blocked, and code 3 when a file could not be read. Your agent's program can read that code to decide whether to send the reply. Anything that can run a command can use it: an automated check, a small script around your agent, a job queue.

This checks replies only. To check actions outside Claude Code, use `action_gate` from Python, as in option 3.

### Option 3: from Python (for developers)

```python
import sys
sys.path.insert(0, "/path/to/hallucination-gates")   # the folder you downloaded
import grounding, action_gate

reply = "Charged $72.19 + $9.86 = $82.05. Chargeback rate: 0.4%."
lookups = '{"id": 1042, "subtotal": 72.19, "fee": 9.86}'
result = grounding.check(reply, lookups)
print(result["block"])                     # True
for f in result["flagged"]:
    print(f["token"], f["verdict"])        # 0.4% UNSOURCED

decision = action_gate.decide("send_payment", {"to": "acme@client.example", "amount": "1900.00"},
                              trusted="user: pay acme@client.example R$ 1.200,00")
print(decision["decision"])                # deny: you asked to pay 1,200.00 (written R$ 1.200,00), not 1,900.00
```

## Does it work?

It was tested on 2,711 real replies from one AI agent, and on 709 real actions the agent took.

| what was tried | what happened |
|---|---|
| The testers (not the AI) made up 19,110 numbers and checked each one against the real lookups of a real reply | the checker let **2** through |
| 4,746 made-up transaction hashes and wallet addresses | **0** got through |
| A random made-up percentage checked against a very long lookup (about 100,000 characters), where it usually already appears somewhere by coincidence | let through **10 times in 100**. An earlier version of this tool let through 71 in 100. Better, not solved. |
| 2,926 correct numbers copied from a lookup onto a line with no words that say what they are | **22** blocked by mistake (an earlier version: 3). In a very long lookup, the checker now distrusts a number copied without the words next to it, such as "fee" in "fee 9.86". |
| 1,254 correct sums of two plain numbers taken from a long lookup, with no words that say what they are | **655** blocked by mistake (an earlier version: 0). This is the biggest cost of that stricter rule. |
| 709 real actions during normal work (deploys, messages, blockchain transactions), with the author's own notes counted as a trusted source | **685** went through, **23** asked first, **1** was refused. Since this was normal work, these 24 stops are the cost (false alarms), not catches. |
| 329 approved actions with one character changed in a value (295 were server addresses, 1 was an amount) | **329** caught |

These numbers come from one person's AI agent over one month. Treat them as a first measurement, not a guarantee. Every test, with its method and margins of error, is in [eval/RESULTS.md](eval/RESULTS.md).

## What it cannot do

- **It only recognizes some ways of writing a number.** It checks amounts with a currency sign (R$, $, US$, €, USD, BRL, EUR), percentages, times like 14:30, dates like 28/09, 2026-09-28 or Sep 28, counts like "3 of 5", IP addresses, and transaction hashes or wallet addresses. A plain count like "12 free seats" or "350 ms" passes unchecked.
- **Claims without numbers pass.** A made-up name, or a false "I ran the tests and they passed", goes through.
- **"Blocked" means "no source in this reply", not "false".** About 2 in 3 blocked numbers had appeared earlier in the conversation or in the agent's notes. The agent still has to show where they came from.
- **The agent's own output counts as a source.** If the agent prints a number with a command, writes it to a file and reads it back, or gets it from a helper agent, the reply check accepts it.
- **The action check only knows some kinds of actions.** It covers web requests, blockchain command-line tools, messages, database writes, file transfers and connected tools that write. An unknown script such as `./pay.sh` is not checked. A value the agent computes inside a command counts as sourced. It checks values, not intentions: the right amount sent to the right person for the wrong reason passes.
- **It sometimes blocks correct replies.** Examples are math it does not recognize, or correct numbers copied from a very long lookup without the words that say what they are. Each costs the agent a rewrite, at most two, and then the reply goes out with a warning.
- **How much it reduces made-up answers overall is not known yet.** In a first test, the same agent did 36 test tasks twice, once with the checks and once without. It never stated an invented value as fact either time (without the checks it gave 3 guesses clearly labelled as guesses, out of 27), so there was nothing to reduce. A harder test is next.

## The six checks

The Claude Code setup above turns on the first two. The other four are extra checks a developer can call from Python. The Claude Code settings do not turn them on.

| file | in plain words |
|---|---|
| `grounding.py` | Every number in the reply must come from a lookup or from what you typed. |
| `action_gate.py` | Every value in an action (a payment, a message, a web request) must come from you or from a lookup. |
| `selfcheck.py` | Ask the model the same question several times. If the answers disagree in meaning, trust none of them. It needs your own access to a model's API, or answers you collected before. |
| `corroborate.py` | A fact needs more than one independent source. Two copies of the same story count as one. |
| `repro.py` | Before anyone calls something a security bug, they must write down what they expect to happen, then show the bug happening exactly that way. |
| `claim.py` | Every claim in a report must point to its evidence, and cannot claim more than that evidence shows. |

Only the first two have been measured on a real agent's work. The other four are checked by their built-in tests only.

## For engineers

- [docs/TECHNICAL.md](docs/TECHNICAL.md): how the matching works, every verdict, the hooks and their settings, and every limit in detail.
- [eval/RESULTS.md](eval/RESULTS.md): all measurements, with confidence intervals and the mistakes found along the way.
- [docs/DOCTRINE.md](docs/DOCTRINE.md): why the checks sit at the agent's output instead of inside the model.

## License

MIT. See [LICENSE](LICENSE).
