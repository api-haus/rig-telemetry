# Metrics reference

Everything this stack derives lives in the `rig:` namespace, defined in
`prometheus/rules/`. Raw exporter series are still queryable; prefer `rig:`,
which is labelled by name rather than by `temp5` and carries no
`instance`/`job` noise on host-level figures.

`tools/rig metrics` lists the live names. This file says what they mean.

Recording rules are not retroactive: a rule added today produces data from
today. To ask about a period before a rule existed, query the raw exporter
series it is built from.

---

## `rig:` sensors — `prometheus/rules/00-telemetry.yml`

hwmon reports `temp5 = 34` in one series and the word `Water_In` in another.
These rules fold the name in.

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:temp_celsius` | `chip_name`, `label`, `chip`, `sensor` | Every temperature sensor, named. `chip_name` is `k10temp`, `asusec`, `nvme`, `spd5118`. |
| `rig:fan_rpm` | `chip_name`, `sensor` | Fan and pump speeds |
| `rig:ambient_celsius` | — | Motherboard sensor, standing in for case air. The reference for every thermal figure. |
| `rig:cpu_celsius` | — | k10temp Tctl |
| `rig:coolant_in_celsius` / `rig:coolant_out_celsius` | — | AIO loop temperatures |
| `rig:gpu_celsius` / `rig:gpu_watts` | — | GPU die temperature and power draw |

---

## `rig:` machine state — `prometheus/rules/10-load.yml`

### Load

| Series | Meaning |
| --- | --- |
| `rig:load:per_cpu` | Load average divided by thread count. Above 1 means a queue exists. |
| `rig:load:runnable` | Processes wanting CPU |
| `rig:load:blocked` | Processes in uninterruptible sleep, waiting on IO |
| `rig:load:io_bound_ratio` | `blocked / load1`. Near 1 means the load average is entirely IO. |

Linux load counts `runnable + blocked`. Split it before concluding anything.

### Pressure stall information

The most honest saturation signal the kernel offers. `some` = at least one task
stalled; `full` = every task stalled, meaning the machine did no work at all
for that fraction of the interval.

| Series | Meaning |
| --- | --- |
| `rig:psi:cpu_some` | Time with a task queued for CPU |
| `rig:psi:io_some` / `rig:psi:io_full` | Time stalled on IO. **`io_full` above 0.3 sustained is a stopped machine.** |
| `rig:psi:memory_some` / `rig:psi:memory_full` | Time stalled in memory reclaim |

### CPU, memory, disk

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:cpu:busy_ratio` | — | 1 − idle, averaged over all threads |
| `rig:cpu:user_ratio`, `:system_ratio`, `:iowait_ratio` | — | By mode. High `iowait` with high `psi:io_full` is a disk bottleneck. |
| `rig:mem:used_ratio` | — | 1 − available/total. Uses `MemAvailable`, so cache is not counted as used. |
| `rig:mem:swap_used_bytes`, `:swap_used_ratio` | — | Swap occupancy |
| `rig:mem:swap_pages_per_sec` | — | Pages moving through swap. **Sustained thousands is a thrash: the disk is serving memory, not work.** |
| `rig:mem:major_faults_per_sec` | — | Faults served from disk, machine-wide |
| `rig:disk:util_ratio` | `device` | Fraction of time the device had a request in flight |
| `rig:disk:await_seconds` | `device` | Average wait per request. Tens of ms on NVMe means a deep queue, not a slow drive. |
| `rig:disk:read_bytes_per_sec`, `:write_bytes_per_sec` | `device` | Throughput |
| `rig:fs:used_ratio`, `rig:fs:avail_bytes` | `device`, `mountpoint` | Capacity. One device appears under every subvolume and bind mount it carries — aggregate `by (device)`. |

---

## `rig:proc:` attribution — `prometheus/rules/20-who.yml`

All keyed by `groupname`, a named binary or family defined in
`process-exporter/config.yml`. Groups outlive PIDs, so a finding stays
meaningful next month.

| Series | Meaning |
| --- | --- |
| `rig:proc:cpu_cores` | Cores held, not percent. `4.0` is four cores solid. |
| `rig:proc:blocked` | Processes in uninterruptible sleep. **The load average, attributed.** |
| `rig:proc:running` | Processes on CPU |
| `rig:proc:zombies` | Unreaped children |
| `rig:proc:load_share` | Group's share of the load average |
| `rig:proc:rss_bytes` | Resident memory. Counts pages shared between forks once per fork, so a 48-way build can appear to hold more than the machine has. |
| `rig:proc:rss_proportional_bytes` | Resident memory with shared pages divided. **Use this for "how much is really theirs".** |
| `rig:proc:swap_bytes` | Pushed out to swap. Large here means it will thrash when it runs again. |
| `rig:proc:virtual_bytes` | Address space reserved. Rarely interesting. |
| `rig:proc:read_bytes_per_sec`, `:write_bytes_per_sec`, `:io_bytes_per_sec` | Disk IO |
| `rig:proc:major_faults_per_sec` | Pages fetched from disk. **Under swap pressure this names who is paying for the thrash.** |
| `rig:proc:context_switches_per_sec` | Scheduling churn |
| `rig:proc:count`, `:threads`, `:open_fds` | Cardinality. A build with unbounded parallelism shows up in `count` first. |
| `rig:proc:wchan_threads` | Threads per kernel wait function. Under an IO stall this reads like a stack trace for the whole machine. |
| `rig:container:cpu_cores`, `:rss_bytes`, `:swap_bytes`, `:io_bytes_per_sec` | Same, per container, keyed by `name` |
| `rig:stack:cpu_cores`, `:rss_bytes`, `:swap_bytes`, `:io_bytes_per_sec`, `:containers` | Per compose project, keyed by `project`. One `docker compose up` is one unit of intent; per-container numbers hide the cost of running several worktrees of the same stack at once. |

---

## `rig:thermal:` cooling health — `prometheus/rules/30-thermal.yml`

Method and failure modes: [thermals.md](thermals.md).

| Series | Meaning |
| --- | --- |
| `rig:thermal:gpu_rise_c` | GPU above case air |
| `rig:thermal:gpu_resistance_c_per_w` | Degrees per watt. A hardware property, comparable across months. Sampled only above 60 W. |
| `rig:thermal:gpu_fan_ratio`, `rig:thermal:gpu_headroom_c` | Fan duty, and degrees remaining before the card throttles |
| `rig:thermal:coolant_rise_c` | Coolant above case air — the radiator's job |
| `rig:thermal:coolant_delta_c` | Across the block — tracks flow |
| `rig:thermal:die_to_coolant_c` | Die above coolant — tracks mount and paste |
| `rig:thermal:cpu_resistance_index` | Die-to-ambient normalised by busy ratio. Arbitrary units; only its own trend means anything. |
| `rig:thermal:pump_rpm` | Loop pump/fan |
| `*:avg1h`, `*:avg7d` | Smoothed baselines |
| `rig:thermal:gpu_degradation_ratio` | This week vs. the same week a month ago. **>1.15 is dust.** |
| `rig:thermal:radiator_degradation_ratio` | Radiator, same comparison. >1.20 is dust. |
| `rig:thermal:mount_degradation_ratio` | Cold plate contact. >1.20 is dried paste. |
| `rig:thermal:cpu_degradation_ratio` | CPU path overall |

Degradation ratios are empty until 37 days of history exist. That is honest,
not broken.

---

## Alerts — `prometheus/rules/40-alerts.yml`

There is no Alertmanager and no receiver, by design. A firing alert is a series:

```promql
ALERTS{alertstate="firing"}
count_over_time(ALERTS{alertname="RigIOStall", alertstate="firing"}[30d]) * 15   # seconds true
```

Each alert carries a `diagnose` annotation naming the query that follows it up.
`tools/rig alerts` prints the current set.

Saturation: `RigIOStall`, `RigSwapThrash`, `RigLoadIOBound`, `RigCPUSaturated`,
`RigMemoryExhausted`, `RigFilesystemFilling`.
Thermal: `RigGPUThrottleImminent`, `RigCPUHot`, `RigPumpStalled`,
`RigCoolingNeedsCleaning`, `RigRadiatorNeedsCleaning`, `RigMountDegraded`.
Hardware: `RigDiskWearHigh`, `RigDiskMediaErrors`, `RigDiskSmartFailing`.
Meta: `RigExporterDown`, `RigDataGap` — check these first if the machine looks
impossibly quiet.

---

## Raw exporter namespaces

| Prefix | From | Notes |
| --- | --- | --- |
| `node_*` | node-exporter | Also carries network, systemd unit states, interrupts, entropy, timex — not all wrapped in `rig:` |
| `namedprocess_namegroup_*` | process-exporter | The source for `rig:proc:*` |
| `nvidia_smi_*` | nvidia_gpu_exporter | Also PCIe link state, encoder sessions, per-engine utilisation |
| `smartctl_device_*` | smartctl-exporter | Wear, media errors, power-on hours, unsafe shutdowns |
| `container_*` | cAdvisor | Per-container; some high-cardinality families are dropped at scrape time |
