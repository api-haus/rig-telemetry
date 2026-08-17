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
| `rig:cpu:user_ratio`, `:system_ratio`, `:iowait_ratio`, `:nice_ratio`, `:steal_ratio`, `:irq_ratio` | — | By mode. High `iowait` with high `psi:io_full` is a disk bottleneck. `irq` sums hard and soft interrupts. |
| `rig:cpu:core_busy_ratio` | `cpu` | Per hardware thread. The averaged `busy_ratio` cannot tell one saturated thread from every thread half busy. |
| `rig:cpu:hottest_core_ratio` | — | The busiest single thread. **Pinned at 1.0 while the average is low is a single-threaded section, and the usual reason a GPU sits idle.** |
| `rig:cpu:saturated_cores` | — | Threads above 90%. Equal to the thread count means the CPU is genuinely full. |
| `rig:cpu:clock_hz`, `:clock_ratio` | — | Average clock, and its share of single-core boost. No all-core load reaches 1.0 — read the trend. |
| `rig:mem:used_ratio` | — | 1 − available/total. Uses `MemAvailable`, so cache is not counted as used. |
| `rig:mem:swap_used_bytes`, `:swap_used_ratio` | — | Swap occupancy |
| `rig:mem:swap_pages_per_sec` | — | Pages moving through swap. **Sustained thousands is a thrash: the disk is serving memory, not work.** |
| `rig:mem:major_faults_per_sec` | — | Faults served from disk, machine-wide |
| `rig:disk:util_ratio` | `device` | Fraction of time the device had a request in flight |
| `rig:disk:await_seconds` | `device` | Average wait per request. Tens of ms on NVMe means a deep queue, not a slow drive. |
| `rig:disk:read_bytes_per_sec`, `:write_bytes_per_sec` | `device` | Throughput |
| `rig:fs:used_ratio`, `rig:fs:avail_bytes` | `device`, `mountpoint` | Capacity. One device appears under every subvolume and bind mount it carries — aggregate `by (device)`. |

---

## `rig:gpu:` the graphics card — `prometheus/rules/15-gpu.yml`

`rig:gpu_celsius` and `rig:gpu_watts` stay with the sensors above. Everything
derived from the card is here.

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:gpu:busy_ratio` | — | Shader time share. A time share, not an occupancy: one kernel on a single SM reads 100%. |
| `rig:gpu:mem_busy_ratio` | — | Time the memory controller moved data. **Above `busy_ratio` means the card waits on its own VRAM, and more shader clock will not help.** |
| `rig:gpu:encoder_ratio`, `:decoder_ratio` | — | NVENC and NVDEC. A screen recorder lives here and costs almost no shader time. |
| `rig:gpu:vram_used_ratio` | — | Counted from free, not from nvidia-smi's `used`, which excludes the driver reserve. **The card refuses allocations near 0.85, not at 1.** |
| `rig:gpu:vram_used_bytes`, `:vram_free_bytes`, `:vram_total_bytes` | — | The same in bytes. `free` is what the next allocation must fit in. |
| `rig:gpu:sm_clock_hz`, `:mem_clock_hz` | — | Shader clock is continuous; memory clock steps between fixed levels. |
| `rig:gpu:clock_ratio` | — | Shader clock over its maximum. No all-SM load reaches 1.0. |
| `rig:gpu:power_ratio`, `:power_limit_watts` | — | Draw over the **enforced** limit, the one the driver clamps to and `nvidia-smi -pl` changes. At 1.0 the card is power-limited, which is ordinary. |
| `rig:gpu:throttled_ratio` | `reason` | Share of wall time each reason held the clock down, from the card's counters rather than a 15s sample of a flag. `power cap` is ordinary; `thermal` points at [thermals.md](thermals.md); `power brake` is the PSU asserting a hardware line and never is. |
| `rig:gpu:pcie_gen_ratio`, `:pcie_width_ratio` | — | Link state against maximum. **Both downtrain on an idle card, so a low reading only means anything while `busy_ratio` is high.** |
| `rig:igpu:busy_ratio`, `:vram_used_bytes` | — | The integrated GPU, through DRM. Non-zero means something renders on it, which for a desktop session is usually a mistake. |

VRAM has no swap and no OOM killer. System RAM overcommits and the machine gets
slow; VRAM refuses, and the client that asked for it dies — the compositor
included. `tools/vram-guard` caps `app.slice` so an application is refused first;
[vram.md](vram.md) says why the threshold sits at 0.85 and not at 0.95.

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

## `rig:net:` the link and who is on it — `prometheus/rules/25-net.yml`

Method, limits and the sysctl that closes the UDP gap: [network.md](network.md).

Rates here are taken over 1 minute, not the 5 the rest of the stack uses. A
download starts, fills the line and stops well inside five minutes, and a 5m
average of that reads as a half-busy link that never lagged anybody.

### The link

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:net:rx_bytes_per_sec`, `:tx_bytes_per_sec` | `device` | Every interface, bridges and tailnet included |
| `rig:net:errors_per_sec`, `:drops_per_sec` | `device` | Drops on a virtual interface are ordinary; on the default route they are not |
| `rig:net:link:rx_bits_per_sec`, `:tx_bits_per_sec` | — | The default-route interface alone, in the bits a line is sold in |
| `rig:net:link:capacity_rx_bits_per_sec`, `:capacity_tx_bits_per_sec` | — | From `RIG_NET_DOWN_MBIT` / `RIG_NET_UP_MBIT`. Empty when unset |
| `rig:net:link:rx_saturation`, `:tx_saturation` | — | Against that capacity. **Empty until the line speed is set** — an invented capacity would read 100% every time a download beat the last record |
| `rig:net:link:peak_rx_bits_per_sec`, `:peak_tx_bits_per_sec` | — | Fastest seen in 7 days |
| `rig:net:link:rx_share_of_peak` | — | Against that peak. The stand-in while no capacity is configured |

### Is it queued?

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:net:rtt_seconds` | `target`, `kind` | ICMP round trip. `kind` is `gateway` or `internet` — loss to the router is the radio, loss past it is the ISP |
| `rig:net:rtt_floor_seconds` | `target`, `kind` | Best seen since the exporter started: the unloaded path |
| `rig:net:bufferbloat_ratio` | `target`, `kind` | Round trip over its own idle value. **The answer to "the download is capped and it still lags".** Above 4 the lag is a queue, not a rate |
| `rig:net:loss_ratio` | `target`, `kind` | Echoes that never came back |
| `rig:net:retransmit_ratio` | — | TCP segments sent again, machine-wide. Rises before ICMP loss: a full queue drops the bulk flow first |
| `rig:net:resolver_seconds` | — | Time for a name to become an address, through the system path |
| `rig:net:tcp_established` | — | Open connections, machine-wide |
| `rig:net:wifi_signal_dbm`, `:wifi_quality` | `device` | Below −72 dBm the radio is the bottleneck before any queue is |
| `rig:net:wifi_retries_per_sec` | `device` | Frames sent again. Airtime spent on nothing |

### Who, and where

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:net:proc:rx_bytes_per_sec`, `:tx_bytes_per_sec` | `groupname`, `scope` | Per process group. `scope` is `internet`, `private` or `local` |
| `rig:net:proc:uplink_bytes_per_sec` | `groupname` | **The "who is using the internet" series.** Bridges, VMs and the tailnet excluded |
| `rig:net:proc:uplink_rx_bytes_per_sec`, `:uplink_tx_bytes_per_sec` | `groupname` | The same, split by direction |
| `rig:net:proc:retransmit_bytes_per_sec` | `groupname` | Bytes the kernel had to send again for this group |
| `rig:net:proc:rtt_seconds` | `groupname` | Worst round trip among that group's established connections |
| `rig:net:proc:connections` | `groupname` | Open connections. Hundreds means peer-to-peer or a download accelerator, and both defeat a single-connection rate cap |
| `rig:net:container:rx_bytes_per_sec`, `:tx_bytes_per_sec`, `:bytes_per_sec` | `name` | Containers with a network namespace of their own, whose sockets the host-side reader cannot see. From cAdvisor. **Host-network containers are excluded** — they report the host's interfaces, and their traffic is already named by process |
| `rig:net:stack:bytes_per_sec` | `project` | The same, per compose project |
| `rig:net:peer:rx_bytes_per_sec`, `:tx_bytes_per_sec`, `:bytes_per_sec` | `peer`, `service`, `scope` | Per remote address. Beyond the busiest, addresses fold into `other` |
| `rig:net:service_bytes_per_sec` | `service` | Named from the remote port; a number means a port nobody has agreed on |

### How much of this is guesswork

| Series | Meaning |
| --- | --- |
| `rig:net:owned_bytes_per_sec` | Sockets on the uplink's addresses plus every container namespace cAdvisor watches |
| `rig:net:attributed_ratio` | That, over the interface's own counters. The interface counts headers and retransmissions and a socket counts payload, so a fully named host-side transfer reads about 0.95 and a quiet link reads far less. **Judge it under load** |
| `rig:net:unattributed_bytes_per_sec` | The rest, in bytes |
| `rig:net:conntrack_available` | 0 means every UDP byte — QUIC included — is unattributed. `sysctl net.netfilter.nf_conntrack_acct=1` |
| `rig:net:scrape_age_seconds` | Since the exporter's last pass |

UDP has no byte counter in the kernel's socket layer, and sockets are sampled
every 10 seconds, so a connection that opens and closes between two passes is
never seen. `attributed_ratio` is the measured size of both gaps.

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

## `rig:ai:` harness spend — `prometheus/rules/50-ai.yml`

Every dollar is **API list value**: what the tokens would cost billed through
the provider's API, not money that left a subscription. `docs/ai-usage.md` has
the readers, the token conventions and the pricing rules.

| Series | Labels | Meaning |
| --- | --- | --- |
| `rig:ai:cost_usd` | — | Running total across every harness |
| `rig:ai:cost_usd:by_harness` | `harness` | Same, per harness |
| `rig:ai:cost_usd:by_model` | `harness`, `model` | Same, per model |
| `rig:ai:cost_usd:by_project` | `harness`, `project` | Same, per project directory |
| `rig:ai:cost_usd:by_role` | `role` | **Where the money goes.** Read this first. |
| `rig:ai:cost_usd:by_kind` | `harness`, `kind` | `main` against `subagent` |
| `rig:ai:burn_usd_per_hour` | — | Dollars of list value per hour, over the last hour |
| `rig:ai:cost_usd:today` / `:week` | — | Increase over 24h / 7d |
| `rig:ai:tokens:by_role` | `role` | `input`, `output`, `cache_read`, `cache_write`, `reasoning` |
| `rig:ai:tokens_per_sec` | `harness`, `role` | Live throughput |
| `rig:ai:requests_per_hour` | `harness` | API responses per hour |
| `rig:ai:cache_read_share` | — | Share of input-side tokens that are the window re-read |
| `rig:ai:usd_per_million_tokens` | — | Blended rate. Steps up when a cache lapses. |
| `rig:ai:reported_cost_usd` / `rig:ai:subsidy_usd` | — | What harnesses claim, and the gap to list |
| `rig:ai:sessions_live` | `harness` | Sessions whose state file moved recently |
| `rig:ai:limit_used_ratio` | `harness`, `window`, `plan` | Subscription window consumed. Codex only. |
| `rig:ai:limit_reset_in_seconds` | `harness`, `window` | Until that window resets |
| `rig:ai:unpriced_tokens` | — | Tokens no rate reaches, excluded from every figure. `share/prices.tsv` names them. |
| `rig:ai:scan_age_seconds` | — | Since the exporter last read the session files |

`reasoning` is already inside `output`. It is reported and never priced.

These counters begin at whatever was already on disk when the exporter first
ran, so `increase()` over a window that predates it reports nothing. Use `rig
ai daily` for that history, or import it once with `rig ai backfill`.

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
`RigMemoryExhausted`, `RigVRAMExhausted`, `RigFilesystemFilling`.
Network, in `25-net.yml`: `RigLinkSaturated`, `RigLinkQueued`, `RigPacketLoss`,
`RigWifiWeak`, `RigNetBlindToUdp`.
Thermal: `RigGPUThrottleImminent`, `RigCPUHot`, `RigPumpStalled`,
`RigCoolingNeedsCleaning`, `RigRadiatorNeedsCleaning`, `RigMountDegraded`.
Hardware: `RigDiskWearHigh`, `RigDiskMediaErrors`, `RigDiskSmartFailing`.
Meta: `RigExporterDown`, `RigDataGap` — check these first if the machine looks
impossibly quiet.
AI, in `50-ai.yml`: `RigAiExporterDown`, `RigAiLedgerStale`, `RigAiPricesStale`,
`RigAiSubscriptionWindowNearlyUsed`, `RigAiBurnRateHigh`.

---

## Raw exporter namespaces

| Prefix | From | Notes |
| --- | --- | --- |
| `node_*` | node-exporter | Also carries network, systemd unit states, interrupts, entropy, timex — not all wrapped in `rig:` |
| `namedprocess_namegroup_*` | process-exporter | The source for `rig:proc:*` |
| `nvidia_smi_*` | nvidia_gpu_exporter | Also PCIe link state, encoder sessions, per-engine utilisation |
| `smartctl_device_*` | smartctl-exporter | Wear, media errors, power-on hours, unsafe shutdowns |
| `container_*` | cAdvisor | Per-container; some high-cardinality families are dropped at scrape time |
| `aiusage_*` | harness-exporter | The source for `rig:ai:*`. Counters are lifetime totals from the session files on disk. |
| `rignet_*` | net-exporter | The source for `rig:net:*`. Per-socket counters over netlink, conntrack flows, ICMP probes and the radio. Per-connection detail is served at `/flows` and is never a metric. |
