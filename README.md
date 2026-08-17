# rig-telemetry

Always-on telemetry for one Linux workstation. Records everything the machine
knows about itself every 15 seconds, keeps 2 years of it, draws it in Grafana,
and answers "what is wrong and who is doing it" from a terminal or an agent.

**Dashboards: <http://localhost:13337>**

Built on [stefanprodan/dockprom](https://github.com/stefanprodan/dockprom),
extended with per-process attribution, GPU telemetry, NVMe SMART, and a
cooling-degradation baseline that detects when the machine needs cleaning.

```
$ tools/rig why
VERDICT: MEMORY THRASH — swap is eating the disk
         the whole machine is stalled on io 77% of the time,
         and 69886 pages/s are moving through swap.
         the disk is busy serving memory, not work. cpu is a red herring.

-- load ------------------------------------------------------------------
  load1 580 (24.0x cores)   runnable 2   blocked-on-io 586
  cpu busy 99.4%   iowait 76.7%

-- who is blocked on io (procs) ------------------------------------------
  group            value
  ---------------  -----
  build:rust-link  598
```

## Run it

```
git clone https://github.com/api-haus/rig-telemetry
cd rig-telemetry
cp .env.example .env      # optional; every value has a working default
docker compose up -d
tools/rig health
```

On hardware other than the author's, read
[docs/adapting.md](docs/adapting.md) first — sensor names and the NVIDIA
library path are the only machine-specific parts.

Grafana <http://localhost:13337> — anonymous viewing is on, `admin` / `admin` to
edit. Prometheus <http://localhost:13390>. Both bind to loopback only.

Ports sit in the 133xx block on purpose: nothing common lives there, so the
dashboard URL stays free and stable across reboots and whatever else you
install. Full list in [docs/runbook.md](docs/runbook.md).

Set `GF_ADMIN_USER` / `GF_ADMIN_PASSWORD` in a `.env` beside the compose file
to change the login.

## What it records

| Source | Covers |
| --- | --- |
| node-exporter | CPU per mode, load, memory, swap, disks, filesystems, network, every hwmon sensor, PSI, systemd units |
| process-exporter | CPU, memory, swap, IO, faults, thread and process counts, **process state** — per named process group |
| nvidia_gpu_exporter | GPU utilisation, temperature, power, fan, clocks, VRAM, PCIe, throttle headroom |
| smartctl-exporter | NVMe wear, media errors, unsafe shutdowns, per-drive temperature |
| cAdvisor | Per-container CPU, memory and IO |
| net-exporter | Bytes, round-trip time and retransmissions **per process group**, per remote address, plus link queue, packet loss and wifi signal |
| harness-exporter | Tokens and API-list dollars per AI coding harness, model, project and subagent |

Scrape every 15s, 2 year retention capped at 30 GB. The harness exporter is
scraped every 60s — spend moves in turns, not in seconds.

## Dashboards

| Dashboard | For |
| --- | --- |
| **Rig — Overview** | Is the bottleneck CPU, GPU, memory or disk, and who owns it |
| **Rig — Who** | Every resource attributed to a named process group |
| **Rig — Network** | Who is on the link, where it goes, and whether it is full or merely queued |
| **Rig — Compute** | Which end the work is stuck at, then CPU and GPU in depth |
| **Rig — Thermals** | Temperatures, and cooling efficiency against a month-old baseline |
| **Rig — Storage** | Throughput, queue latency, capacity, drive endurance |
| **Rig — AI Spend** | What every coding harness used, at API list prices |

They are generated, not hand-edited: `tools/gen-dashboards.py`.

## What this does that a stock stack does not

**It attributes load to a name.** `rig:proc:blocked` splits the load average by
process group, so a load of 500 resolves to `build:rust-link 598` instead of a
shrug. Groups are defined in `process-exporter/config.yml` and survive reboots,
unlike PIDs.

**It names who is using the internet, and separates full from queued.** The
kernel counts bytes per interface and never per process, so this reads every
TCP socket's own counters over netlink and maps them back through
`/proc/<pid>/fd` to the same process groups everything else uses. Then it says
the thing throughput cannot: a link is never slow, it is full, and a full link
delivers its bytes while every interactive packet waits behind them. That wait
is measured against the path's own idle round-trip time, so a download sitting
under its speed cap and a machine that lags are one fact instead of two.

```
$ rig net
VERDICT: QUEUED — the line is full and everything waits behind it
         round trip to 1.1.1.1 is 214ms against 17ms idle — 12.6x.
         this is a queue, not a shortage of bandwidth.

-- who is on the internet ------------------------------------------------
  group            down      up       conns  worst rtt
  game:steam       5.4M/s    18K/s    39     210ms
  browser:zen-bin  509.3K/s  1.2K/s   13     35ms
```

`rig net speedtest` fills the line on purpose and grades the queue it makes.
`docs/network.md` explains the method, the UDP blind spot, and the one sysctl
that closes it.

**It knows when the machine needs cleaning.** Dust does not raise a temperature
you can read off a gauge — it raises *thermal resistance*, the degrees needed
per watt removed. The stack records that ratio against case-air temperature at
matched load, averages it over a week, and compares against the same week a
month earlier. A 15% rise is dust, not weather and not workload.
`rig thermals` prints the comparison; `docs/thermals.md` explains the method
and where it can lie. It needs 37 days of history before it says anything.

**It knows what the AI cost.** A subscription hides the price of a request, so
you cannot tell a two-cent turn from a three-dollar one. This reads the session
files twelve coding harnesses already write — Claude Code, Codex, Kimi Code,
OpenCode, pi, Qwen Code, dsh, Gemini CLI, Goose, Crush, Copilot CLI, droid — and
prices the tokens at the rates the providers publish for the same models on
their APIs.

```
$ rig ai
-- total -----------------------------------------------------------------
  $29,218.07 across 175,523 API responses and 46.04B billable tokens

-- where the money goes --------------------------------------------------
  role         list value  tokens   share
  cache_read   $22,165.89  45.11B   76%
  cache_write  $4,538.53   742.36M  16%
  output       $2,283.20   94.37M   8%
  input        $230.44     87.80M   1%
```

That is API list value — the work a subscription covered, not money that left
the account. It is the only figure that varies with what you did, so it is the
one that ranks projects, models and habits. The role split is the finding: a
running context is re-sent on every request, so three quarters of the cost of
an agent session is re-reading, not writing.

Per project, per model, per day, and split between your own turns and the
subagents you sent out. `docs/ai-usage.md` explains the conventions, the price
lookup, and why this reports about half of what Claude Code's own statistics do.

## Claude Code plugin

This repo is also a Claude Code plugin. Installing it puts `rig` on PATH and
adds the skills below, so an agent can answer "why is this machine slow"
without being told how.

```
/plugin marketplace add api-haus/rig-telemetry
/plugin install rig-telemetry@rig-telemetry
```

| Skill | Fires on |
| --- | --- |
| `rig-diagnose` | machine slow, loaded, thrashing, "what is using my CPU", "what happened last night" |
| `rig-network` | slow internet, "who is using all the bandwidth", laggy calls or games, a capped download that still lags |
| `rig-devenv` | a project's containers, brokers, watchers or toolchains are loading the machine |
| `rig-thermals` | heat, fans, throttling, "does my PC need cleaning" |
| `rig-aicost` | AI spend, token burn, "what did this project cost", cache reads, subscription windows |

The plugin ships the whole stack, so the installed copy can run it directly.
`rig where` resolves the root on any machine — it prefers the checkout a
running stack was started from, then this plugin's copy;
`$RIG_TELEMETRY_HOME` overrides both. Nothing hardcodes a path.

`rig` itself only needs the Prometheus endpoint, so it works from any
directory, and against a remote host via `RIG_PROMETHEUS=http://host:13390`.

## For agents

Read [AGENTS.md](AGENTS.md). Short version: run `rig why`.

## Durability

Every container is `restart: unless-stopped` and `docker.service` is enabled,
so the stack returns after a reboot on its own. For a hard guarantee that also
survives someone running `docker compose down`, install the systemd unit:

```
sudo tools/install-systemd.sh
```

## Layout

```
docker-compose.yml            the stack
prometheus/prometheus.yml     scrape config and retention
prometheus/rules/             recording rules and alerts, in five layers
process-exporter/config.yml   process group definitions
grafana/                      datasource, dashboard provider, generated dashboards
tools/rig                     the CLI
tools/gen-dashboards.py       dashboard generator
tools/net-exporter.py         per-process network attribution and link quality
tools/harness_usage.py        one reader per AI harness, and the price lookup
tools/harness-exporter.py     serves that to Prometheus
share/prices.tsv              model names that reach no models.dev entry
bin/rig                       PATH shim, added automatically by the plugin
.claude-plugin/               plugin and marketplace manifests
skills/                       rig-diagnose, rig-network, rig-devenv, rig-thermals, rig-aicost
docs/                         metrics, network, thermal method, runbook, adapting
```

## Docs

- [AGENTS.md](AGENTS.md) — the query contract and diagnosis procedure
- [docs/metrics.md](docs/metrics.md) — every series, explained
- [docs/network.md](docs/network.md) — how a byte gets an owner, and why a capped download still lags
- [docs/ai-usage.md](docs/ai-usage.md) — harness readers, token conventions, pricing
- [docs/thermals.md](docs/thermals.md) — how the dust detection works
- [docs/runbook.md](docs/runbook.md) — operations, upgrades, retention, backup
- [docs/adapting.md](docs/adapting.md) — running this on different hardware

## Licence

MIT, inherited from dockprom. See [LICENSE](LICENSE).
