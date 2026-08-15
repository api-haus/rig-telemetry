# AI coding harness usage

What every AI coding harness on this machine used, priced at published API list
rates, with a running total per model, per project and per day.

```
rig ai
```

**Dashboard: Rig — AI Spend.**

---

## The number this answers

A subscription hides the cost of a request. You cannot tell a $0.02 turn from a
$3.00 turn, so you cannot tell which habits are expensive. This stack recovers
the figure by counting the tokens each harness records and multiplying by the
rate the provider publishes for the same model on its API.

That figure is **API list value**: what the work would have cost billed through
the API. It is not money that left the account. Under a subscription the money
that left is the plan fee, and the two answer different questions:

- *What did this month cost me?* — the plan fee.
- *What was this session worth, is this project expensive, is delegating to
  subagents cheap?* — API list value. It is the only figure that varies with
  what you did.

Both are true at once. This stack reports the second one and never pretends it
is the first.

---

## Where the money actually goes

On a long agent session the answer is almost never "output".

```
-- where the money goes --------------------------------------------------
  role         list value  tokens   share
  cache_read   $22,165.89  45.11B   76%
  cache_write  $4,538.53   742.36M  16%
  output       $2,283.20   94.37M    8%
  input        $230.44     87.80M    1%
```

A running context is re-sent on every request. Cost is context size times
request count, and both grow as a session runs. Writing tokens is the small
part; remembering them is the bill.

`cache_write` is the other half of the story. A prompt cache that lapses, or a
prefix that changes because an MCP server connected, makes the next request pay
to rewrite the whole window at up to twenty times the read rate.

---

## What is read

One reader per harness, over the files the harness already writes. Nothing is
installed into any harness and no configuration is changed.

| Harness | Source | Convention |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | exclusive; message id deduplicated |
| Codex | `~/.codex/sessions/**/*.jsonl` | OpenAI: cached is inside input |
| Kimi Code | `~/.kimi-code/sessions/**/wire.jsonl` | exclusive, one wire per agent |
| OpenCode | `storage/message/**/*.json` and `opencode.db` | exclusive; reports its own cost |
| pi | `~/.pi/agent/sessions/**/*.jsonl` | exclusive; reports its own cost |
| Qwen Code | `~/.qwen/usage_record.jsonl` | Gemini: cached inside prompt, thoughts inside output |
| dsh | `~/.dsh/sessions/**/session.jsonl.zstd` | exclusive; compressed, one zstd frame per append |
| Gemini CLI | `~/.gemini/tmp/*/chats/*.json` | Gemini; only saved checkpoints carry usage |
| Goose | `~/.local/share/goose/sessions/*.jsonl` | exclusive |
| Crush | `~/.local/share/crush/crush.db` | exclusive |
| Copilot CLI | `~/.copilot/history-session-state/*.json` | OpenAI |
| droid | `~/.factory/sessions/**/*.jsonl` | exclusive |

`rig ai doctor` says which of these are on this machine, which have written
anything, and which are installed but silent. A harness that is installed and
has produced no file this reader understands reports zero files rather than
zero dollars — the two are different facts.

### Token conventions

Every reader converts to one **exclusive** form before anything else happens:

- `input` never includes cached tokens.
- `cache_read` and `cache_write` are separate roles with separate rates.
- `reasoning` is always already inside `output`. It is reported so you can see
  how much of a reply was thinking, and it is never priced, because pricing it
  again would bill the same tokens twice.

Anthropic already reports this shape. OpenAI and Google nest the cached count
inside the prompt count, so the reader subtracts it out.

DeepSeek publishes no cache-write rate because it charges a cache miss at the
plain input rate, so `dsh` records no such role and none is invented for it.

---

## Prices

Rates come from [models.dev](https://models.dev), cached at
`~/.cache/rig-telemetry/models.dev.json` and refetched when older than seven
days. A failed fetch keeps the last good copy.

A model id is carried by dozens of gateways, and the first entry in the file is
not the number "what would this cost on the API" is asking for. The lookup
prefers the first-party provider, then any entry that publishes a non-zero
price. A gateway listing $0 is a subscription plan, which answers a different
question.

Where a published cache rate is missing, the plain input rate stands in — the
honest floor, not an invented discount.

### When a name reaches nothing

A harness names its models however it likes, and a plan name is not a model id.
Kimi Code sends `kimi-code/kimi-for-coding` on the wire; models.dev has no such
entry, and no transformation of that string reaches one. Its `config.toml` says
what it is — `display_name = "K2.7 Coding"` — which is `moonshotai/kimi-k2.7-code`.

So a name is stated once rather than guessed, in order of precedence:

| Source | |
| --- | --- |
| `RIG_AI_PRICE_kimi_code_kimi_for_coding` | environment; non-alphanumerics become `_` |
| `~/.config/rig-telemetry/prices.tsv` | yours |
| `share/prices.tsv` | shipped with the repo |
| models.dev, matched directly | the usual case |

Each line is a model name, a tab, and either an alias or four rates:

```
kimi-code/kimi-for-coding	moonshotai/kimi-k2.7-code
some-private-model	0.95 0.19 0.95 4
```

**Prefer the alias.** It says what the model *is*, so the rates stay current
instead of ageing in a file here. The four-number form — input, cache read,
cache write, output, dollars per million — is for a model nobody publishes.

### When the price moves on a clock

DeepSeek bills peak and off-peak. From 16:00 UTC on 2026-08-16 the same
million tokens cost twice as much between 01:00 and 04:00 UTC, and again
between 06:00 and 10:00, as they do the rest of the day. models.dev publishes
one number per model and has nowhere to say which, so a rate line can carry
its own clock:

```
deepseek-v4-pro	0.66 0.022 0.66 1.98 from 2026-08-16T16:00Z on deepseek
deepseek-v4-pro	1.32 0.044 1.32 3.96 from 2026-08-16T16:00Z at 01-04,06-10 on deepseek
```

| Clause | |
| --- | --- |
| `from <UTC instant>` | the rate starts here, and nothing before it is repriced |
| `at <UTC hours>` | only inside these hours; half-open, so `01-04` is 01:00 up to 04:00 |
| `on <provider>` | only when this provider served it |

Several lines may name one model. Of those that fit the moment being priced,
the nearest source wins, then the latest `from`, then the one that names
hours. **Nothing fits means the catalogue answers** — which is why the two
lines above leave every DeepSeek response before the cutover priced at the
rate that was actually charged.

`on` matters because a model is sold by more than one seller. The same
`deepseek-v4-pro` through OpenRouter is billed at OpenRouter's own published
rate, on no clock, and `on deepseek` is what keeps DeepSeek's clock off it.

The ledger keeps the UTC hour of every response beside the local day for this
reason — the day you lived answers "what did I spend yesterday", and the
seller's hour is the grain a peak rate is billed at. `rig ai doctor` prints
every rate that moves, and when.

#### What the clock actually costs you

```
rig ai clock                       # at 8h a day, 30 days
rig ai clock --hours 5 --days 21
```

A headline rate change says nothing on its own. What decides the bill is
**which role's rate moved** against **how much of your traffic is that role**.
DeepSeek's input rate went up 1.5x off-peak; its cache-read rate went up 6.1x,
and on this machine 99.6% of the tokens sent to it are cache reads. The blended
effect is therefore 4.5x off-peak and 9.0x at peak, not the 2x the peak/off-peak
split suggests.

Which hours you work then decides the mix, so `rig ai clock` measures that too
rather than assuming a flat day: it takes the share of this machine's recorded
spend that falls inside the peak window, from the ledger's own UTC hours. Peak
06:00-10:00 UTC is a quiet morning here, which is why the exposure comes out at
21% rather than the 29% a flat day would give.

The window is fixed in UTC, so on a wall clock that keeps daylight saving it
moves twice a year. `rig ai clock` renders it at the offset in force on the day
it is run and names where it goes on the other side of the year, rather than
storing one offset and being an hour wrong for six months.

The "Rig — AI Spend" dashboard carries two annotations for this, generated from
the same `share/prices.tsv` lines and toggleable at the top of the board:

| Annotation | |
| --- | --- |
| `<seller> peak hours` | a band over every hour billed at the peak rate |
| `<seller> rate change` | a vertical line at each flip, four a day here |

Both are built from PromQL's `hour()` rather than from a recording rule, so
they mark a week that has already been paid for — a rule would only know the
hours since it was added — and both start at the `from` instant, so nothing
before the cutover is marked.

Both are the same kind of object. Grafana's Prometheus annotation support
(`packages/grafana-prometheus/src/annotations.ts`) drops every sample whose
value is not above zero, then walks what is left: *"Instead of creating
singular annotation for each active event we group events into region if they
are less or equal to `step` apart."* An event that never gets extended keeps
`timeEnd == time` and draws as a vertical line; one that does gets a `timeEnd`
later than its `time` and draws as a shaded region.

So the difference between the band and the line is only how sparse the query
is against its own step:

- The band runs at a 300s step and matches every peak hour, so its samples are
  exactly one step apart and merge into one region per window.
- The line runs at a 900s step and matches only the first quarter of an hour a
  flip happens in, so it returns one sample per flip, hours from its
  neighbours, and each stays a point.

Two consequences worth keeping. Every expression is wrapped in `vector(1) and
(…)` rather than returning `hour()`, because a window that reached midnight
would return the value 0 there and be silently dropped as inactive. And
`useValueForTime` must be a JSON boolean: Grafana tests it for truth, so the
string `"false"` is true and sends every marker to 1970.

**Nothing is guessed.** A model that no tier reaches is counted in tokens and
excluded from every dollar figure, and `rig ai doctor` prints the exact line
that fixes it. Half a money figure is worse than none.

`rig ai models` has a `rate from` column saying whether each figure came from
models.dev or from an override.

**Money is not stored.** The ledger holds tokens; dollars are computed on
every read. A price catalogue that arrives late, or a rate that changes, then
applies to the whole history rather than only to what is scanned afterwards.

---

## Why this disagrees with the harness's own counter

Claude Code writes one transcript line per content block — the thinking, the
text, and each tool call — and every one of those lines repeats the same
`usage` block for the one API response that produced them. Measured on one day
of real sessions: 3,928 lines carrying usage, 1,751 distinct responses.

`~/.claude/stats-cache.json` adds all the lines. Its total for that day matches
the undeduplicated sum to the token, and is 2.0x what was billed.

This reader deduplicates on the message id, because the response was billed
once. Expect roughly half of what Claude Code's own statistics show, and treat
the difference as the artifact rather than the finding.

Where a harness computes its own cost — OpenCode and pi do — `rig ai doctor`
prints both figures side by side. They agree to within 4%, which is the
independent check on the arithmetic here.

---

## Metrics

Raw series come from `tools/harness-exporter.py` on `127.0.0.1:13360`, scraped
every 60 seconds. Spend moves in turns rather than in seconds, and the series
carry a project label; at 15s this one exporter would cost more disk than the
whole machine's hardware telemetry.

| Series | Labels | Meaning |
| --- | --- | --- |
| `aiusage_tokens_total` | `harness`, `provider`, `model`, `kind`, `role` | Tokens, by role |
| `aiusage_cost_usd_total` | `harness`, `provider`, `model`, `kind`, `role` | API list value |
| `aiusage_requests_total` | `harness`, `provider`, `model`, `kind` | API responses |
| `aiusage_reported_cost_usd_total` | `harness`, `provider`, `model`, `kind` | What the harness itself claims |
| `aiusage_project_cost_usd_total` | `harness`, `project`, `provider`, `model` | List value per project |
| `aiusage_unpriced_tokens_total` | `harness`, `provider`, `model` | Tokens with no published rate |
| `aiusage_sessions_live` | `harness`, `project`, `status` | Sessions whose state file moved recently |
| `aiusage_rate_limit_used_ratio` | `harness`, `window`, `plan` | Subscription window consumed |
| `aiusage_source_files` / `aiusage_source_installed` | `harness` | Coverage |

`kind` is `main` or `subagent`. Delegate enough work and most of the spend
stops being the session you are watching: on this machine 37% of all list value
was spent by subagents.

The project series carry `provider` and `model` too, so one dashboard filter
narrows the project panels the same way it narrows the rest. The 80-project cut
is applied twice over, and both halves matter:

- **On the whole project, not on each of its models.** A project that used five
  models would otherwise compete with itself for five of the kept slots.
- **Inside each provider separately.** Ranked globally, a provider you have
  spent a dollar with keeps no named project at all — it disappears into
  `other` beneath a year of the largest one, and isolating it on the dashboard
  then answers nothing. Per provider, every seller keeps its own top projects.

On this ledger that is 190 series where the bare `harness`/`project` key was 87.
Ranked globally it would be 160, and every provider below Anthropic, Moonshot,
OpenAI and Alibaba would have no named project at all.

The "Rig — AI Spend" dashboard puts `Harness`, `Provider` and `Model` dropdowns
above every panel, chained left to right: pick a provider and the model list
shortens to what that provider sold. Selecting `deepseek` isolates DeepSeek
direct; the same model bought through OpenRouter stays under `openrouter`,
because the provider is the seller, not the model's author. Every panel reads
the raw counter rather than a `rig:ai:*` rule, since a rule has already summed
those labels away.

The `rig:ai:*` recording rules in `prometheus/rules/50-ai.yml` are the stable
vocabulary. `rig:ai:cost_usd` is the running total, `rig:ai:burn_usd_per_hour`
the rate, `rig:ai:cache_read_share` the fraction of input-side tokens that are
the window being re-read.

Only Codex publishes a subscription window. `rig ai limits` shows it and says
so when nothing does.

---

## History, and the one thing Prometheus cannot do

A counter exists from the moment Prometheus starts watching it. The session
files reach back much further — months, in the usual case — so a fresh stack
draws a flat line over real spend that already happened.

The ledger holds all of it. `rig ai daily --since 90d` reads the ledger, not
Prometheus, and works the moment the first scan finishes.

To put that history on the dashboard as well:

```
rig ai backfill --dry-run           # write the OpenMetrics file, print the commands
rig ai backfill                     # write it, import it, restart Prometheus
rig ai backfill --since 90d         # further back; the file grows with the window
rig ai backfill --undo --since 30d  # delete it all again and start over
```

The ledger's granularity is a day, but **one sample a day is not a series
Prometheus can read**. A sample is only visible for the five minutes after it
is written, so a query between two daily points finds nothing, and `sum()`
across sparse series silently drops every one not stamped at that exact
instant. The first attempt here read $2,628 → $11,905 → $4,058 on a counter
that only goes up.

So each day's running total is repeated every `--step` (5 minutes by default)
through the day: exactly what the exporter would have written had it been
running. 31 days comes to 583k samples and a 77 MB temporary file, which
compresses to a few megabytes of blocks. Within a day the line is flat and
steps at midnight — that is the ledger's real resolution, not a smoothing.

The samples carry the scrape's own `job` and `instance` labels, read back from
a live sample. Without them they would be different series and the two ranges
would double each other where they meet.

**The import stops before Prometheus's head, and this is not a nicety.** Blocks
are the persisted past; the head is the present, and it lives in memory until
Prometheus cuts a block from it. A backfilled block that reaches into the
head's range asserts that window is already on disk, so the next restart
truncates the head down to the newest block and discards live telemetry that
had not been written yet.

An earlier version of this command wrote samples right up to the moment it ran,
to make the boundary look continuous. Prometheus restarted, set its head
minimum to the newest backfilled block, and two and a half hours of every
exporter's data — CPU, memory, disks, sensors, the lot — were gone. Data that
exists only in the head cannot be recovered.

So the ceiling is `prometheus_tsdb_head_min_time_seconds`, measured rather than
assumed, and the command prints where it stopped. Everything after that is the
exporter's own to record.

`promtool tsdb create-blocks-from openmetrics` does the import. Prometheus
needs a restart to notice new blocks, which the command does.

`--undo` deletes every `aiusage_*` sample in the window through the admin API.
It takes the live exporter's samples in that window with it, because they are
the same series — that is the point. Current totals survive: a counter carries
its whole history in its value, so only the drawn curve inside the window goes.

---

## The ledger

`~/.cache/rig-telemetry/ai-usage.db`, SQLite, shared by the exporter container
and `rig` on the host — which is why the compose service runs as your uid.

It is a cache, not a record: every row is derived from a file that is still on
disk, so it can be deleted at any time and rebuilt with `rig ai scan`. A schema
change rebuilds it automatically.

**A row is one hour of one thing.** `day` is your local day, because a
spending day is the one you lived. `utc_slot` is the same moment on the
seller's clock, because that is the grain a peak/off-peak rate is billed at.
Both are in the key; everything coarser is folded up at read time.

**Files are keyed relative to `$HOME`, not by absolute path.** The container
reaches the same file at `/host/home/…` that the host reaches at `/home/you/…`.
Keyed absolutely, each scanner read it as a file the other had never seen, and
every harness with no per-record id was counted exactly twice. That doubling
stays perfectly consistent, so it reads as real usage rather than as a fault —
Claude Code and OpenCode were unaffected only because their records carry an id
that is deduplicated.

Reading a file's offset and appending the rows it produced are also one
transaction (`BEGIN IMMEDIATE`), so two scanners running at once cannot both
claim the same bytes.

```
rig ai doctor --verify
```

recounts every session file from scratch and checks the ledger against it. A
ledger *behind* the files is normal — sessions append while it counts. A ledger
*ahead* of them can only mean something was added twice, and it says so and
exits 1. Run it after touching a reader.

Scanning is incremental by byte offset. Measured here: 2,157 files and 6.7 GB
of transcripts in 14.4 seconds on the first pass, and under 0.1 seconds after
that.

A compressed transcript is read the same way. dsh writes one zstd frame per
append, and each frame ends on a line, so a frame boundary is a byte offset
like any other and a frame still being written is left for the next pass. That
needs `compression.zstd`, which is standard library from Python 3.14 — hence
the exporter image. An older interpreter reports the file as unreadable rather
than as empty.

Files that get rewritten rather than appended to — the small per-session
summaries — are re-read whole and their previous contribution deleted first, so
a rewrite cannot count twice.

`$HOME` is bind-mounted read-only into the exporter container. The set of
directories worth watching grows every time a new harness ships, and listing
them individually breaks the moment one of them does not exist yet.

---

## Honest gaps

**A day is a local day.** Buckets follow the machine's timezone, which is what
"what did I spend yesterday" means. Prometheus series are absolute time and are
unaffected.

**Deleted transcripts are gone.** Claude Code cleans up old sessions on its own
schedule. Whatever it removed cannot be read, and the ledger keeps what it
already scanned rather than pretending the gap is zero.

**Only tokens that a harness records can be counted.** A harness that writes no
usage anywhere is invisible here, however much it spent. `rig ai doctor` lists
every reader so the blank is visible.

**Rates change.** The figure is today's list price applied to old tokens, not
the price on the day. That is the right choice for "is this project expensive"
and the wrong one for reconstructing an invoice. A rate line carrying `from`
is the exception: it reprices nothing before its own start date, so a
published price change is followed rather than backdated.
