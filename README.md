# hallucination-gates

[![selftest](https://github.com/Galmanus/hallucination-gates/actions/workflows/selftest.yml/badge.svg)](https://github.com/Galmanus/hallucination-gates/actions/workflows/selftest.yml)

**Checks that stop an AI agent from stating numbers it never saw, and from acting on values it made up.**

## The problem

AI agents sometimes make things up. Not often, but always with total confidence.

This project started with one case. An AI agent was asked for the last bus of the day between two cities. It answered with a bus company, a departure time, an arrival time, a price and the number of free seats. It sounded like it had checked. It had not. None of the searches it ran returned any of those details. It invented all five.

A better model will not make this go away. Language models are trained to give a likely-sounding answer, and the tests they are graded on reward a lucky guess over "I don't know" ([Kalai et al., 2025](https://arxiv.org/abs/2509.04664)). You cannot make the model stop guessing. You can check what it says and does before it reaches you.

## What this does

It puts two checkpoints between the agent and the world.

**1. Before the agent answers.** Every price, percentage, date, time, count like "3 of 5", IP address and transaction ID in the answer must appear in what the agent actually looked up in that turn: a web page, a file, an API response. If one does not, the answer goes back to the agent before you see it. The agent has three ways out: find a real source, say clearly that the number is an estimate, or remove it.

**2. Before the agent acts.** Before it pays, sends a message, calls an API or deploys, every amount, address and ID in the request must come from you or from something a tool returned. An invented value is blocked. A value that only came from a web page or an email written by someone else makes the agent stop and ask you first. That is the classic scam: "our bank details have changed, pay this account instead".

Think of a teacher asking "where did you read that?". The checker does not know whether a number is true. It only knows whether the agent can point to where the number came from. No source, no answer.

## A small example

The agent looked up an invoice. The API returned:

```text
invoice 1042 · subtotal 72.19 · fee 9.86 · paid 2026-09-28 10:55 · receipt 0x9f3c2a71e4b05d88
```

This is the agent's draft answer, line by line:

| the agent wrote | what the checker does |
|---|---|
| Paid on 28/09 at 10:55. | **passes**: same date and time, written a different way |
| Charged $72.19 + $9.86 = $82.05. | **passes**: both amounts were returned, and it redoes the sum |
| Receipt: 0x9f3c2a71e4b05d8a | **blocked**: the last character differs from the real receipt |
| Chargeback rate this month: 0.4%. | **blocked**: no tool said that |

The agent fixes the receipt and writes "~0.4% (estimate)". Now the answer passes.

## Does it work?

It was tested on 2,711 real answers from an AI agent, and on 709 real actions the agent took.

| what was tried | what happened |
|---|---|
| 19,110 made-up numbers, checked against real lookup results | **2** got through |
| 4,746 made-up transaction IDs | **0** got through |
| A made-up percentage checked against a long lookup result (about 100,000 characters), where the same number already appears somewhere by chance | accepted **71 times in 100** by the previous version, **10 in 100** now. Better, not solved. |
| 2,926 real numbers copied correctly from the lookup | **22** blocked by mistake. The previous version blocked 3. The extra ones come from the stricter check on long lookups, which distrusts a number copied without its label. |
| 709 real actions the agent took (deploys, messages, blockchain transactions) | **685** went through, **23** asked a human first, **1** was blocked |
| 329 approved actions with one character changed in an amount or address | **329** caught |

These numbers come from one person's AI agent over one month, so treat them as a first measurement, not a guarantee. Every test, with its method and margins of error, is in [eval/RESULTS.md](eval/RESULTS.md).

## What it cannot do

- **It only checks numbers, dates, times and IDs.** A made-up name, or a false "I ran the tests and they passed", goes through.
- **"Blocked" means "no source", not "false".** Often the number was right but came from earlier in the conversation. The agent still has to show where it came from.
- **It checks values, not intentions.** The right amount sent to the right person for the wrong reason passes.
- **It sometimes blocks correct answers.** Math it does not recognize, or a correct number copied from a very long lookup without its label. Each case costs the agent one rewrite.
- **How much it reduces made-up answers overall is not measured yet.** In a first test, the same agent answered 36 invented tasks with the checks on and with them off. It made nothing up in either case, so there was nothing to reduce. A harder test is next.

## Try it

You need Python 3.9 or newer. There is nothing to install.

```bash
git clone https://github.com/Galmanus/hallucination-gates && cd hallucination-gates
./run_selftests.sh     # runs all 321 built-in tests, offline
```

To check your own text, put the answer in `draft.txt` and what the tools returned in `ledger.txt`:

```bash
python3 grounding.py check --draft draft.txt --ledger ledger.txt
```

Exit code 1 means blocked. The output lists every number that has no source.

## The six checks

| file | in plain words |
|---|---|
| `grounding.py` | Every number in the answer must come from something the agent looked up. |
| `action_gate.py` | Every value in an action (a payment, a message, an API call) must come from you or from a real lookup. |
| `selfcheck.py` | Ask the model the same question several times. If the answers disagree in meaning, trust none of them. |
| `corroborate.py` | A fact needs more than one independent source. Two copies of the same story count as one. |
| `repro.py` | "This is a security bug" needs a reproduction that matches a prediction written down before running it. |
| `claim.py` | Every claim in a report must point to its evidence, and cannot claim more than that evidence shows. |

Only the first two have been measured on real traffic. The other four are checked by their built-in tests only.

## For engineers

- [docs/TECHNICAL.md](docs/TECHNICAL.md): how the matching works, every verdict, the Python API, the Claude Code hooks, and every limit in detail.
- [eval/RESULTS.md](eval/RESULTS.md): all measurements, with confidence intervals and the mistakes found along the way.
- [docs/DOCTRINE.md](docs/DOCTRINE.md): why the checks sit at the agent's output instead of inside the model.

## License

MIT. See [LICENSE](LICENSE).
