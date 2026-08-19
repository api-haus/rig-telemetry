# AGENTS.md

You are looking at a always-on telemetry stack for one Linux workstation. It
records CPU, memory, swap, disk, network, per-process attribution, every hwmon
sensor, GPU and NVMe SMART data every 15 seconds, and keeps 2 years of it.

**Start here. Do not write PromQL first.**

```
tools/rig why
```

That prints a verdict, the numbers behind it, and the process groups
responsible. It answers "what is wrong with this machine" in one call. Almost
every question a human asks is answered by it or by one more command below.

## The tools

| Command | Answers |
| --- | --- |
| `rig why` | What is wrong right now, and who caused it |
| `rig who --by <dimension>` | Who is using a resource |
| `rig net` | Who is using the internet, and whether the link is full or queued |
| `rig timeline --since 24h` | When load spiked, and which group owned each spike |
| `rig thermals` | Temperatures, and whether cooling has degraded |
| `rig ai` | What the AI coding harnesses used, at API list prices |
| `rig containers` | Docker cost per compose stack (`--each` for containers) |
| `rig disk` | IO, latency, capacity, drive wear |
| `tools/rig-reclaim <path>` | What to delete when a filesystem fills, ranked by restore cost |
| `rig alerts` | What is firing |
| `rig health` | Whether this stack itself is working |
| `rig q '<promql>'` | Arbitrary instant query |
| `rig range '<promql>' --since 7d --plot` | Arbitrary range query, summarised |
| `rig metrics [pattern]` | List the series that exist |

Every command takes `--json` for parsing and `--at <RFC3339>` to evaluate at a
past instant. `rig who` and `rig timeline` take `--since` to aggregate over a
window.

One tool sits outside `rig` because it acts instead of reporting:
`tools/vram-guard` caps GPU memory per cgroup, so a client that fills the card
is refused instead of the compositor. `docs/vram.md`.

Dimensions for `--by`: `cpu`, `blocked`, `running`, `mem`, `rss`, `swap`, `io`,
`read`, `write`, `faults`, `procs`, `threads`, `fds`, `switches`, `net`, `down`,
`up`, `conns`.

## Read this before you diagnose a load average

Linux load average counts runnable tasks **and** tasks in uninterruptible sleep
(D state, waiting on IO). A load of 500 with an idle CPU is not a CPU problem.

Always split it before concluding anything:

```
rig q 'rig:load:runnable'   # wants CPU
rig q 'rig:load:blocked'    # waiting on a disk
```

`rig:psi:io_full` is the fraction of time during which **no task on the
machine** could make progress because of IO. Above 0.3 it *may* mean the
machine is stopped rather than slow — but corroborate it before you believe it.

PSI io pressure is derived from the kernel's `nr_iowait` tally, and that tally
drifts: on this machine `/proc/stat` reports 2 blocked tasks while a scan of
every task finds 0, and `io_full` sits near 0.4 with every disk under 6% busy.
Check a device and a real D-state count before naming an IO stall:

```
rig q 'max(rig:disk:util_ratio)'                  # is any disk actually busy
ps -eo stat | awk '$1 ~ /D/' | wc -l              # are any tasks really blocked
```

`rig why` and `RigIOStall` both apply that corroboration. A high `io_full` with
idle disks is a miscount, not a finding.

## Decision procedure

1. `rig why`. Take the verdict.
2. If it says **MEMORY THRASH**: the disk is busy serving swap, not work.
   `rig who --by faults` names who is paging. Look for a build with unbounded
   parallelism before blaming the disk.
3. If it says **IO STALL**: `rig who --by blocked`, then `rig who --by io`.
4. If it says **CPU SATURATED**: `rig who --by cpu`.
5. If it says **LINK QUEUED** or **LINK FULL**: the machine is idle and the wait
   is outside it. `rig net`, then `rig net who`.
6. If it says **HEALTHY** but the human disagrees, they are describing the past.
   Use `rig timeline --since 24h --min-load 2` to find the spike, then
   `rig why --at <that timestamp>`. If they are describing the internet rather
   than the machine, `rig net` is the command, not `rig why`.

## Read this before you blame the internet on bandwidth

A link is never slow, it is full — and a full link delivers its bytes at the
same rate while every interactive packet waits behind them. Throughput cannot
show that. Round-trip time against its own idle value can:

```
rig q 'rig:net:bufferbloat_ratio'   # above 4, the lag is a queue, not a rate
rig q 'rig:net:link:rx_saturation'  # empty until RIG_NET_DOWN_MBIT is set
```

So a download capped below the line speed still lags everything, and raising or
lowering that cap is not the fix. Say so before proposing one.

`rig net` prints the verdict, the queue and the process groups on the link.
`rig net speedtest` fills the line on purpose and grades the queue it makes.

Two blind spots, both measured rather than assumed. UDP carries no byte counter
in the kernel's socket layer, so QUIC is unattributed until
`net.netfilter.nf_conntrack_acct=1` is on; and sockets are sampled, so a
connection shorter than one pass is never seen. `rig:net:attributed_ratio` is
the size of both. Quote it whenever you name a top talker, and run
`rig net doctor` before concluding that something is idle.

## Read this before you quote an AI cost

`rig ai` prices tokens at published API list rates. Under a subscription that
is **value received, not money spent** — the plan fee is what left the account.
Say which one you mean; never call the list figure a bill.

```
rig ai                    # everything, split by harness, role, model, project
rig ai daily --since 30d  # per day, reaches back further than Prometheus does
rig ai limits             # what each subscription has left, from the seller
rig ai clock              # what a seller's peak window costs at your own hours
rig ai doctor             # what is read, what is priced, what is missing
rig ai doctor --verify    # recount the files, prove the ledger against them
```

`rig ai` prices tokens; `rig ai limits` reads plans. They answer different
questions and neither derives the other — a plan window is metered by the
seller against every device the account is signed in on, so no count made here
can reproduce it. `docs/ai-usage.md`.

Never answer "how much will this price change cost me" by multiplying a
headline rate. Nearly every token an agent sends is a cache read, so the
cache-read rate is the whole bill. `rig ai clock` reprices this machine's own
token mix and weights it by the hours it actually works.

Answer "why is it expensive" from the role split, not the model. A running
context is re-sent on every request, so `cache_read` is normally 70-80% of the
figure and `output` under 10%.

Expect about half of what Claude Code's own statistics report: it writes one
transcript line per content block and each repeats the same usage block, while
this deduplicates on the message id. `docs/ai-usage.md` has the measurement.

## Metric vocabulary

Everything this stack computes lives in the `rig:` namespace. Raw exporter
series (`node_*`, `namedprocess_*`, `nvidia_smi_*`, `smartctl_*`,
`container_*`) are still there, but prefer `rig:` — it is stable, labelled by
name rather than by `temp5`, and carries no `instance`/`job` noise on
host-level figures.

`rig metrics` lists all of them. `docs/metrics.md` explains each one.

The five groups:

- `rig:load:*`, `rig:psi:*`, `rig:cpu:*`, `rig:gpu:*`, `rig:mem:*`, `rig:disk:*`,
  `rig:fs:*` — machine state.
- `rig:proc:*` and `rig:container:*` — attribution, keyed by `groupname` /
  `name`. **This is the "who" namespace.**
- `rig:net:*` — the link, its queue, and who is on it. `rig:net:proc:*` is keyed
  by the same `groupname`.
- `rig:drive:*` — per-drive SMART, keyed by `serial_number`. **Not** by
  smartctl's `device`: that label is the position a drive took in the last scan
  and it moves between drives. `docs/metrics.md`.
- `rig:temp_celsius`, `rig:fan_rpm`, `rig:*_celsius` — sensors, by name.
- `rig:thermal:*` — derived cooling health, including the degradation ratios.
- `rig:ai:*` — harness spend, keyed by `harness`, `model`, `project`, `role`.

## Process groups

`rig:proc:*` is keyed by `groupname`, not by PID. A group is a named binary or
a named family of them — `build:rust-link` covers every linker at once,
`browser:zen-bin` covers the browser. The mapping is
`process-exporter/config.yml`; edit it to add a group, then
`docker compose restart process-exporter`.

Naming convention: `build:*`, `agent:*`, `ide:*`, `browser:*`, `game:*`,
`sys:*`, and a bare binary name for everything unclassified.

## Useful queries

```promql
# who owned the load average, worst 5 minutes of the last day
topk(5, max_over_time(rig:proc:blocked[24h]))

# a group's memory over a month
rig:proc:rss_proportional_bytes{groupname="build:rust-link"}

# what the blocked threads are parked in, kernel-side
topk(10, rig:proc:wchan_threads)

# how long an alert has been true
count_over_time(ALERTS{alertname="RigIOStall", alertstate="firing"}[30d]) * 15

# when will this filesystem fill
predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[7d], 30*86400)

# who is on the internet, worst minute of the last day
topk(5, max_over_time(rig:net:proc:uplink_bytes_per_sec[24h]))

# how queued the link was while that happened
max_over_time(rig:net:bufferbloat_ratio{kind="internet"}[24h])

# which end the work is stuck at: all four readings in one answer
{__name__=~"rig:(cpu:busy_ratio|cpu:hottest_core_ratio|gpu:busy_ratio|gpu:mem_busy_ratio)"}

# what held the GPU clock down, by reason
topk(3, rig:gpu:throttled_ratio)

# cooling efficiency now vs a month ago; >1.15 means dust
rig:thermal:gpu_degradation_ratio
```

## Rules for reporting a finding

- Quote the number and the series it came from. `rig:psi:io_full = 0.83` beats
  "the disk seems busy".
- Name the process **group**, not a PID. PIDs are gone by the time anyone reads
  your answer; `build:rust-link` is still meaningful next month.
- Distinguish `rig:proc:rss_bytes` from `rig:proc:rss_proportional_bytes`. The
  first counts pages shared between forks once per fork, so 48 linkers can
  appear to hold more memory than the machine has. Use the proportional series
  for "how much is really theirs" and say which one you used.
- If a `rig:thermal:*_degradation_ratio` is empty, the stack does not have 37
  days of history yet. Say that. Do not substitute a raw temperature and call
  it a degradation finding.
- `rig health` first if anything looks impossibly quiet. A dead exporter reads
  exactly like a calm machine.

## Where things are

```
docker-compose.yml            the stack
prometheus/prometheus.yml     scrape config, retention
prometheus/rules/*.yml        recording rules (the rig: namespace) and alerts
process-exporter/config.yml   process group definitions
grafana/dashboards/*.json     generated — edit tools/gen-dashboards.py instead
tools/rig                     the CLI above
tools/net-exporter.py         per-process network attribution, ICMP probe, radio
tools/gen-dashboards.py       dashboard generator; --check verifies freshness
tools/harness_usage.py        one reader per AI harness, and the price lookup
tools/harness_quota.py        one reader per subscription: what the plan has left
tools/harness-exporter.py     serves both to Prometheus on :13360
tools/vram-guard              the VRAM cap: install, apply, status, verify
tools/vram-probe.py           allocates VRAM until refused; used by verify
share/prices.tsv              model names that reach no models.dev entry
tools/rig-reclaim             what to delete when a filesystem fills
docs/metrics.md               every series, explained
docs/reclaim.md               reclaim categories, restore costs, the two traps
docs/network.md               how a byte gets a name, and what bufferbloat is
docs/ai-usage.md              harness readers, token conventions, pricing rules
docs/thermals.md              how dust detection works and when it lies
docs/vram.md                  why the GPU had no arbitration, and where the cap sits
docs/session.md               why one dead D-Bus broker logs the whole desktop out
docs/runbook.md               operations
```

To teach the stack a new harness, add a `Source` subclass in
`tools/harness_usage.py` and list it in `SOURCES`. It declares the files it
owns and yields records in the exclusive token convention; pricing, dedupe,
incremental reads and every metric come for free.

To teach it a new **subscription**, add a `Subscription` subclass in
`tools/harness_quota.py` and list it in `SUBSCRIPTIONS`. It reads the
credential its harness already keeps — never writing one back — and returns the
windows the seller reports; caching, staleness, the metrics and `rig ai limits`
come for free.

Grafana is at <http://localhost:13337>, Prometheus at <http://localhost:13390>.
Both bind to loopback only.

## Changing rules

After editing anything under `prometheus/`:

```
docker exec rig-prometheus promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://localhost:13390/-/reload
tools/rig health          # 0 failing
```

After editing `tools/gen-dashboards.py`:

```
tools/gen-dashboards.py   # Grafana picks the folder up within 30s
```

Recording rules are not retroactive. A new rule starts producing data from the
moment it is added, so a rule added today cannot answer a question about last
week — query the raw exporter series for that.
