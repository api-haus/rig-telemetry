---
name: rig-aicost
description: Report what AI coding harnesses cost, in tokens and in API-list dollars, across Claude Code, Codex, Kimi Code, OpenCode, pi, Qwen Code, dsh, Gemini CLI, Goose, Crush, Copilot CLI and droid, and what each subscription has left of its session, weekly and model-scoped windows. Use when the user asks how much they spent on AI, what a session or project cost, where the tokens went, which model or subagent is expensive, whether context is being re-read, how much of a plan is used or left, when a limit resets, whether they are about to run out, or asks to compare harnesses, providers or plans. Also use before claiming any AI usage figure, and when the user mentions token burn, cache reads, cost per turn, or "is this worth it on the API".
---

# rig-aicost

```
rig ai
```

Prints the running total at API list prices, split by harness, by token role,
by model, by project, and by direct versus delegated work. One call answers
almost every version of "what is this costing me".

`rig` is on PATH whenever this plugin is installed. `rig ai` reads a local
SQLite ledger, not Prometheus, so it answers even when the stack is down and it
reaches back further than the stack has been recording.

## The one thing to get right before answering

**A subscription pays a flat fee. Every dollar `rig ai` prints is API list
value** — what the same tokens would cost billed through the provider's API.
Never call it "what you paid" or "your bill". Say what it is:

> $2,495 of API list value on `swordgal` — the work a subscription covered.

Both figures are true and they answer different questions. The plan fee answers
"what did this month cost". List value is the only figure that varies with what
was actually done, so it is the one that ranks projects, models and habits.

## Commands

| Command | Answers |
| --- | --- |
| `rig ai` | Everything, at once. Start here. |
| `rig ai --since 7d` | The same, over a window |
| `rig ai daily --since 30d` | Spend per day, with a sparkline |
| `rig ai daily --by-harness` | One column per harness |
| `rig ai projects --top 25` | Which project directory is expensive |
| `rig ai models` | Per model, with the exact $/M rates used |
| `rig ai limits` | What each subscription has left, asked of the seller |
| `rig ai doctor` | What is read, what is priced, what is missing |
| `rig ai doctor --verify` | Recount every file and check the ledger against it |
| `rig ai scan` | Read new session files now, without the container |
| `rig ai backfill` | Put the ledger's history into Prometheus for Grafana |

Every one takes `--json`.

## "How much is left" is a different question from "what did it cost"

`rig ai` counts tokens and prices them. It cannot say what a plan has left, and
neither can any arithmetic on its output. Use the other command:

```
rig ai limits
```

The percentages come from the seller that meters the plan, across **every
device the account is signed in on** — not from this machine's tokens. Claude
Code reports a 5-hour session window, a weekly window, and a weekly window
scoped to Fable; Codex and Kimi Code report session and weekly.

When answering "am I about to run out", quote the window, the percentage left
and the reset time together. A window at 90% that resets in twenty minutes is
not the same finding as one at 90% with six days to run.

Two things the table says that are easy to skip:

- **`measured`** — how old the figure is. A plan read four hours ago is a
  different fact from one read now. `rig ai limits --refresh` asks again.
- **`from`** — `provider` was asked of the seller; `harness` is what the
  harness itself last wrote, which is only as fresh as its last run.

If a plan reports nothing, `rig ai limits` prints why on its own line — signed
out, token expired, or an account on no metered plan. Read that sentence out
rather than reporting the plan as unused. An expired Kimi token means the CLI
has not run for fifteen minutes; nothing is wrong with the plan.

## Read the role split before blaming the model

The first table to look at is **where the money goes**, not which model was
used. On agent sessions the answer is nearly always the same:

| Role | What it is |
| --- | --- |
| `cache_read` | The running context, re-sent on every request. Usually 70–80% of the bill. |
| `cache_write` | A cache that lapsed or a prompt prefix that changed. Up to 20x the read rate. |
| `output` | What the model actually wrote. Usually under 10%. |
| `input` | Genuinely new tokens. Almost nothing. |

So the expensive habits are the ones that grow the context or invalidate the
cache — long sessions without a compaction, an MCP server connecting mid-run,
a tool list that changes. They are not "the model talks too much".

If someone asks how to spend less, answer from this table. Halving output
changes 8% of the bill; halving the context halves it.

## Delegated work is the other blind spot

`rig ai` ends with a **main / subagent** split. Subagent spend is real, is
billed to the session that sent it, and is invisible on any statusline. When it
is a large share, say so — it is usually the surprise in the number.

## Rules for reporting a figure

- Name the window. "All recorded history" is not "this month"; the header line
  says which days the session files on disk actually cover.
- Quote the role split when explaining a total. A number with no cause is not
  an answer.
- `rig ai doctor` before claiming coverage. A harness with zero session files
  is installed and silent, which is not the same as unused.
- If doctor reports a model as counted but not priced, **fix it rather than
  caveat it.** A plan name is not a model id — Kimi Code sends
  `kimi-code/kimi-for-coding`, which is K2.7 Coding, which models.dev publishes
  as `moonshotai/kimi-k2.7-code`. Find what the harness's own config calls it
  (`display_name` in `~/.kimi-code/config.toml`, for instance), add one line to
  `~/.config/rig-telemetry/prices.tsv`, and re-run. Doctor prints the line.
  Prefer the alias form over four hard-coded rates, so the numbers stay current.
- Expect about half of what Claude Code's own statistics report. Claude Code
  writes one transcript line per content block and each repeats the same usage
  block; this deduplicates on the message id because the response was billed
  once. Say that rather than hedging the number.
- Do not convert list value into a subscription verdict without the plan price.
  "$27k of list value" does not by itself mean a plan is good value; it means
  that much work was done.

## When the question is about the live rate

Prometheus carries the series once the exporter has been up. `rig:ai:cost_usd`
is the running total, `rig:ai:burn_usd_per_hour` the current rate,
`rig:ai:cache_read_share` the fraction of input-side tokens that are the window
being re-read. The **Rig — AI Spend** dashboard draws all of them.

A fresh stack has no history in Prometheus even though the ledger does — a
counter only exists from the moment it is watched. Use `rig ai daily` for the
past, or `rig ai backfill` to import it.

`docs/ai-usage.md` has the conventions, the price resolution rules, and the
gaps.
