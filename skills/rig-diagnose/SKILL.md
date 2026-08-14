---
name: rig-diagnose
description: Diagnose a loaded, slow or stalled Linux workstation from recorded telemetry, and name the process responsible. Use when the user says the machine is slow, laggy, loaded, thrashing, freezing, unresponsive, "what is eating my CPU/RAM/disk", "why is my load average so high", "what is udisks/this process doing", "who is using resources", or asks what happened at some earlier time. Also use before blaming any single program for machine-wide slowness, and to check whether a change actually helped.
---

# rig-diagnose

Run this first, always:

```
rig why
```

It prints a verdict, the numbers behind it, and the process groups responsible.
Most questions end there.

`rig` is on PATH whenever this plugin is installed. It needs only the
Prometheus HTTP endpoint, so it works from any directory.

If it reports it cannot reach Prometheus, the stack is not running — the error
names the exact command to start it. Never substitute `top` or `ps` for a
question about the past; say the telemetry is not running.

## The one fact that decides everything

**Linux load average counts runnable tasks AND tasks in uninterruptible sleep.**
A load of 500 on a 24-thread box with an idle CPU is not a CPU problem — it is
500 processes queued behind a disk.

Never report a load average without splitting it:

```
rig q 'rig:load:runnable'    # wants CPU
rig q 'rig:load:blocked'     # waiting on a disk
rig q 'rig:psi:io_full'      # fraction of time NO task could progress at all
```

`rig:psi:io_full` above 0.3 sustained means the machine is stopped, not slow.

## Follow the verdict

| Verdict | Next command | What you are looking for |
| --- | --- | --- |
| MEMORY THRASH | `rig who --by faults` | The disk is serving swap, not work. Find who is paging. |
| IO STALL | `rig who --by blocked` then `--by io` | Who is in the disk queue |
| MEMORY PRESSURE | `rig who --by mem` | Who holds the memory |
| CPU SATURATED | `rig who --by cpu` | Who holds the cores |
| IO BOUND | `rig who --by blocked` | Load is high, CPU idle — do not look at CPU |
| HEALTHY | `rig timeline --since 24h --min-load 2` | The user is describing the past. Find the spike, then `rig why --at <timestamp>`. |

Then, if the culprit is a docker stack or a development environment, load the
**rig-devenv** skill. If the question is about heat, fans or cleaning, load
**rig-thermals**.

## Asking about the past

The stack keeps 2 years. Every command takes `--at` and most take `--since`:

```
rig why --at 2026-08-14T03:00:00Z
rig who --by cpu --since 24h --agg max
rig timeline --since 7d --by blocked --min-load 2
rig range 'rig:psi:io_full' --since 30d --plot
```

`rig timeline` is the one to reach for when the user says "it was slow last
night" — it prints load per bucket alongside the group that owned it.

## Other views

```
rig containers            # cost per docker compose stack; --each per container
rig disk                  # throughput, latency, capacity, SMART wear
rig alerts                # what is firing
rig health                # is the telemetry itself working
rig metrics               # every series name; docs/metrics.md explains them
```

## Reporting rules

- **Quote the series and the number.** `rig:psi:io_full = 0.83` beats "the disk
  seems busy". A claim with no series name behind it is a guess.
- **Name the process group, not a PID.** PIDs are gone by the time anyone reads
  the answer; `build:rust-link` still means something next month.
- **Say which memory number you used.** `rig:proc:rss_bytes` counts pages shared
  between forks once per fork, so 48 linkers can appear to hold more than the
  machine has. `rig:proc:rss_proportional_bytes` divides them. Prefer the
  proportional one and name it.
- **`rig health` first when something looks impossibly calm.** A dead exporter
  reads exactly like a quiet machine.
- **Do not report a metric you have not sanity-checked.** Disk latency in
  particular is a ratio of rates over 5 minutes; a two-second stall inside the
  window dominates the average. Cross-check a surprising figure with
  `iostat -x 2 2` before calling it a hardware fault.
- **A recording rule is not retroactive.** A rule added today cannot answer a
  question about last week. Query the raw exporter series for that period, and
  say that is what you did.

## Where things are

```
rig where           # stack root, and how it was resolved
rig where -q        # the path alone, for scripting
```

Never hardcode a path — the stack root differs per machine. `rig where` prefers
the checkout a running stack was started from, falling back to this plugin's own
copy. `$RIG_TELEMETRY_HOME` overrides both.

Read `$(rig where -q)/AGENTS.md` before doing anything beyond the commands
above; `docs/metrics.md` there defines every series.

Grafana <http://localhost:13337>, Prometheus <http://localhost:13390>, both
loopback only. `rig q` and `rig range` take arbitrary PromQL when a question
needs something the subcommands do not cover.
