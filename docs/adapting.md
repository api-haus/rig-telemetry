# Adapting this to your machine

Most of the stack is hardware-independent: CPU, memory, swap, disks,
filesystems, PSI, per-process attribution and containers work anywhere Linux
and Docker do. Three things are not.

## 1. Sensor names — `prometheus/rules/00-telemetry.yml`

**This is the only file that encodes your specific hardware.** Everything else
reads the `rig:` names it defines.

hwmon reports `temp5 = 34` and, separately, the label `Water_In`. The rules in
that file join the two and then pin a handful of well-known names:

```yaml
- record: rig:ambient_celsius
  expr: max(rig:temp_celsius{chip_name="asusec", label="Motherboard"})
```

`asusec` is an Asus embedded controller and `Motherboard` is its label for the
board sensor. Your machine will differ. Find yours:

```
sensors                                    # human view
rig q 'rig:temp_celsius'                   # what this stack sees, with names
rig thermals                               # the full sensor table
```

Then edit the four pinned records to match:

| Record | Should be | Typical source |
| --- | --- | --- |
| `rig:ambient_celsius` | Case air, or the coolest board sensor | `Motherboard`, `SYSTIN`, `Systin` |
| `rig:cpu_celsius` | CPU package | `Tctl` (AMD k10temp), `Package id 0` (Intel coretemp) |
| `rig:coolant_in_celsius` / `rig:coolant_out_celsius` | AIO loop | Only on boards that expose them |
| `rig:gpu_celsius` / `rig:gpu_watts` | GPU | Works as-is for any NVIDIA card |

**If you have no coolant sensors**, delete those two records and the rules in
`30-thermal.yml` that use them (`coolant_rise_c`, `coolant_delta_c`,
`die_to_coolant_c`, and their `:avg*` and `*_degradation_ratio` derivatives).
The GPU thermal-resistance chain needs only `rig:gpu_*` and
`rig:ambient_celsius`, so dust detection still works on an air-cooled machine.

`rig:ambient_celsius` is the reference for every thermal-health figure. If it
resolves to a sensor that tracks CPU heat rather than case air, the degradation
ratios compress toward 1.0 and under-report. Pick the coolest, most stable
sensor you have.

## 2. NVIDIA library paths — `.env`

No nvidia-container-toolkit is assumed, so `nvidia-smi` and `libnvidia-ml.so`
are bind-mounted from the host. The default is the Arch layout:

```
NVIDIA_LIB_DIR=/usr/lib                    # Arch
NVIDIA_LIB_DIR=/usr/lib/x86_64-linux-gnu   # Debian, Ubuntu
```

Find yours with `ls $(dirname $(readlink -f $(command -v nvidia-smi)))` or
`find /usr/lib -name 'libnvidia-ml.so.1'`.

**No NVIDIA GPU?** Remove the `nvidia-exporter` service from
`docker-compose.yml` and its `gpu` job from `prometheus/prometheus.yml`. Also
remove the `rig:gpu_*` records in `00-telemetry.yml` and everything in
`30-thermal.yml` that depends on them. Nothing else references them.

AMD and Intel GPUs report through hwmon and DRM, which node-exporter already
collects — `rig q 'rig:temp_celsius'` will show them, and the `--collector.drm`
flag is already on.

## 3. Ports and retention

Everything binds loopback in the `133xx` block, chosen because nothing common
lives there:

| Service | Port |
| --- | --- |
| Grafana | 13337 |
| Prometheus | 13390 |
| node-exporter | 13310 |
| process-exporter | 13320 |
| nvidia | 13330 |
| smartctl | 13340 |
| cAdvisor | 13350 |

Change them in `docker-compose.yml` and `prometheus/prometheus.yml` together.
`instance` is pinned to `rig` by a relabel rule, so a port change does not split
your history.

Retention defaults to 2 years capped at 30 GB. Set `RETENTION_TIME` and
`RETENTION_SIZE` in `.env`; whichever is reached first wins. Check your headroom
before raising the cap — the volume lives wherever Docker's root directory is.

## 4. Process groups — `process-exporter/config.yml`

Groups are matched top to bottom, first match wins, with a catch-all that names
every remaining process after its binary. The shipped groups reflect one
developer's machine: Rust and .NET toolchains, JetBrains IDEs, a Firefox
derivative, Steam.

Prune what you do not run and add what you do. The naming convention is
`build:*`, `agent:*`, `ide:*`, `browser:*`, `game:*`, `sys:*`. Reload with:

```
docker compose restart process-exporter
```

Watch the series count afterwards. If it climbs without bound, something is
matching the catch-all under a name that changes every run — give it an
explicit group.

## Verifying an adaptation

```
docker exec rig-prometheus promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://localhost:13390/-/reload
rig health            # 0 failing rules, every target up
rig thermals          # sensors resolve to real names, not blanks
rig why               # a verdict, not an error
```

A record that resolves to nothing is not an error in Prometheus — it silently
produces no series. `rig thermals` showing blanks where names should be is how
you find a mis-pinned sensor.
