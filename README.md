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
docker compose up -d
tools/rig health
```

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

Scrape every 15s, 2 year retention capped at 30 GB.

## Dashboards

| Dashboard | For |
| --- | --- |
| **Rig — Overview** | Is the bottleneck CPU, memory or disk, and who owns it |
| **Rig — Who** | Every resource attributed to a named process group |
| **Rig — Thermals** | Temperatures, and cooling efficiency against a month-old baseline |
| **Rig — Storage** | Throughput, queue latency, capacity, drive endurance |

They are generated, not hand-edited: `tools/gen-dashboards.py`.

## The two things this does that a stock stack does not

**It attributes load to a name.** `rig:proc:blocked` splits the load average by
process group, so a load of 500 resolves to `build:rust-link 598` instead of a
shrug. Groups are defined in `process-exporter/config.yml` and survive reboots,
unlike PIDs.

**It knows when the machine needs cleaning.** Dust does not raise a temperature
you can read off a gauge — it raises *thermal resistance*, the degrees needed
per watt removed. The stack records that ratio against case-air temperature at
matched load, averages it over a week, and compares against the same week a
month earlier. A 15% rise is dust, not weather and not workload.
`rig thermals` prints the comparison; `docs/thermals.md` explains the method
and where it can lie. It needs 37 days of history before it says anything.

## For agents

Read [AGENTS.md](AGENTS.md). Short version: run `tools/rig why`.

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
prometheus/rules/             recording rules and alerts, in four layers
process-exporter/config.yml   process group definitions
grafana/                      datasource, dashboard provider, generated dashboards
tools/rig                     the CLI
tools/gen-dashboards.py       dashboard generator
docs/                         metrics reference, thermal method, runbook
```

## Docs

- [AGENTS.md](AGENTS.md) — the query contract and diagnosis procedure
- [docs/metrics.md](docs/metrics.md) — every series, explained
- [docs/thermals.md](docs/thermals.md) — how the dust detection works
- [docs/runbook.md](docs/runbook.md) — operations, upgrades, retention, backup

## Licence

MIT, inherited from dockprom. See [LICENSE](LICENSE).
