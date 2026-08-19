# Runbook

## Daily

```
tools/rig why        # what is wrong now
tools/rig alerts     # what is firing
tools/rig health     # is the stack itself honest
```

`rig health` first if the machine looks impossibly quiet. A dead exporter reads
exactly like a calm machine.

## Start, stop, restart

```
docker compose up -d
docker compose ps
docker compose logs -f prometheus
docker compose restart process-exporter
docker compose down            # keeps the data volumes
```

Containers are `restart: unless-stopped` and `docker.service` is enabled, so
the stack returns after a reboot without help. `sudo tools/install-systemd.sh`
adds a boot unit for the case where someone ran `docker compose down`.

## After editing configuration

Prometheus, including rules — validate, reload, verify. Never skip the check;
a bad rule file makes Prometheus refuse the reload and keep serving the old
config silently.

```
docker exec rig-prometheus promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://localhost:13390/-/reload
tools/rig health          # expect 0 failing
```

Process groups — `process-exporter/config.yml` is read at start only, and the
net exporter reads the same file for the same names:

```
docker compose restart process-exporter net-exporter
```

Dashboards — never edit `grafana/dashboards/*.json` by hand; they are
generated. Edit `tools/gen-dashboards.py`, then:

```
tools/gen-dashboards.py         # Grafana reloads the folder within 30s
tools/gen-dashboards.py --check # exits 1 if the committed JSON is stale
```

Dashboards edited in the Grafana UI are not saved back to the repo. To keep a
UI change, port it into the generator.

## Giving UDP an owner

UDP carries no byte counter in the kernel's socket layer, so QUIC — most
browser traffic — has no named owner until connection tracking counts it:

```
rig net doctor                                          # says whether it is on
sudo sysctl -w net.netfilter.nf_conntrack_acct=1        # now
echo net.netfilter.nf_conntrack_acct=1 | sudo tee /etc/sysctl.d/99-rig-net.conf
```

It costs 16 bytes per tracked flow. `rig:net:attributed_ratio` is the share of
link traffic that has an owner, and it is the figure to check after turning
this on. [network.md](network.md) has the method and the rest of the blind spot.

## The VRAM cap

```
tools/vram-guard status         # exits 1 if app.slice carries no cap
sudo tools/vram-guard verify    # re-prove enforcement after a driver upgrade
```

Check `status` after a kernel or NVIDIA upgrade. The cap depends on the driver
charging the cgroup, which is a driver feature, not a kernel guarantee.
`docs/vram.md` explains the mechanism and its limits.

## When the whole desktop dies

Half the applications die at once, nothing new starts, the rest go over the
next minute. Check the session bus before the GPU — it is the cheaper test and
it is usually the answer:

```
journalctl -b | grep -A4 'Too many open files'      # EMFILE in peer_new_with_fd
journalctl -b | grep 'Got disconnect on API bus'    # the user manager lost the bus
grep 'Max open files' /proc/$(pgrep -x dbus-broker | head -1)/limits
```

A broker that dies logs the session out, because uwsm supervises the compositor
through that bus. Expect `uwsm: PID ... exited with RC 1` and `[PAM] Closing
session` within milliseconds. Log in again at the greeter; nothing is wedged
and a reboot buys nothing.

Do not read `winedbg` process counts as a cause. A Proton game that loses its
display spawns one per crashed process, tens of seconds after the fact.

[session.md](session.md) has the mechanism, the limit, and how the three
lookalike failures differ.

## Storage

2 year retention, capped at 30 GB, whichever comes first.

```
tools/rig q 'prometheus_tsdb_head_series'                                  # active series
tools/rig q 'sum(prometheus_tsdb_storage_blocks_bytes)'                    # on disk
docker system df -v | grep rig-telemetry                                   # volume size
```

Raise or lower either limit in `docker-compose.yml` under the `prometheus`
service, then `docker compose up -d prometheus`. Lowering retention deletes
data on the next compaction.

The dominant cardinality is `namedprocess_namegroup_*`, which grows with the
number of distinct process names seen. If series count climbs without bound,
the catch-all rule at the bottom of `process-exporter/config.yml` is matching
something that names itself differently every run — give it an explicit group.

## Backup

The data is in the `rig-telemetry_prometheus_data` volume; the dashboards and
rules are in git.

```
docker run --rm -v rig-telemetry_prometheus_data:/data -v "$PWD":/out alpine \
  tar czf /out/prometheus-backup.tar.gz -C /data .
```

Grafana's own database holds nothing that is not provisioned from this repo,
except UI-made dashboard edits.

## Upgrading

Image tags are pinned in `docker-compose.yml` on purpose. To move one:

```
# edit the tag, then
docker compose pull <service> && docker compose up -d <service>
tools/rig health
```

After a Prometheus major version, re-run `promtool check config`. After a
Grafana major version, open each dashboard once — the schema migrates on read
and a panel that fails to migrate shows as empty rather than as an error.

## Adding a target

1. Add the exporter to `docker-compose.yml` on `network_mode: host`, bound to
   `127.0.0.1`.
2. Add a `scrape_configs` entry in `prometheus/prometheus.yml`.
3. `curl -X POST http://localhost:13390/-/reload`, then `tools/rig health`.
4. Wrap the useful series in `rig:` recording rules — dashboards and
   `tools/rig` read that namespace, not raw exporter names.
5. Document them in `docs/metrics.md`.

## What leaves this machine

Every port binds to loopback. Three things still reach outward, all from the
harness exporter:

| Host | Carries | Why |
| --- | --- | --- |
| `models.dev` | nothing of yours | the price catalogue, refetched weekly |
| `api.anthropic.com`, `chatgpt.com`, `api.kimi.com` | the harness's own OAuth token | what each subscription has left |

The three usage endpoints are the same ones each harness's own `/usage` command
calls, with the same credential, and nothing is sent but that token. The files
are read and never written: a refresh rotates the refresh token with it and
would sign out whatever session holds the old one.

`RIG_AI_QUOTA_INTERVAL=0` stops the exporter asking at all. `rig ai limits`
still asks when you run it, because that is you asking.

## Never write blocks that reach into the head

`promtool tsdb create-blocks-from` writes into the same directory Prometheus is
serving from, and Prometheus trusts what it finds there. Blocks are the
persisted past; the head is the present, held in memory until a block is cut
from it. A block whose range overlaps the head asserts that window is already
on disk, so the next restart truncates the head to the newest block and
discards everything in memory that had not been written yet.

Measured here, the hard way: an import that reached up to the present cost this
stack two and a half hours of every exporter's data. Nothing recovers it.

`rig ai backfill` now stops at `prometheus_tsdb_head_min_time_seconds` and says
where it stopped. Any other backfill needs the same ceiling:

```
tools/rig q 'prometheus_tsdb_head_min_time_seconds'   # write nothing at or after this
```

## Known quirks

**Ports.** Everything binds loopback: Grafana **13337**, Prometheus 13390,
node-exporter 13310, process-exporter 13320, GPU 13330, SMART 13340,
cAdvisor 13350, harness-exporter 13360, net-exporter 13370. The net exporter
also serves `/flows`, a live connection table that is deliberately not a
metric. The 133xx block is deliberate: nothing
common lives there, so the dashboard URL stays free and stable across reboots
and other software.

**The harness exporter runs as you, and can see your home directory.** It reads
the session files AI coding harnesses write, and those live all over `$HOME` in
a set that grows with every new harness — so `$HOME` is mounted read-only
rather than a list of subdirectories that breaks when one does not exist. It
runs as `RIG_UID`/`RIG_GID` (1000 by default) because it shares its SQLite
ledger with `rig ai` on the host. Set both in `.env` if your account differs.

The ledger at `~/.cache/rig-telemetry/ai-usage.db` is a cache: every row comes
from a file still on disk. Delete it and `rig ai scan` rebuilds it, about 15
seconds for 6.7 GB of transcripts. A schema change rebuilds it on its own.

**GPU exporter.** No nvidia-container-toolkit is installed, so `nvidia-smi` and
`libnvidia-ml.so` are bind-mounted from the host. A driver upgrade that changes
those paths breaks the container — `docker compose logs nvidia-exporter` will
say so plainly, and `RigExporterDown` fires.

**Filesystem duplicates.** btrfs subvolumes and bind mounts put one device
under many mountpoints. Aggregate `by (device)`; `tools/rig disk` and the
storage dashboard already do.

**Disk latency is bursty over a 5-minute window.** `rig:disk:*_await_seconds`
is a ratio of rates, so a two-second stall inside the window dominates the
average. Cross-check a surprising figure against `iostat -x 2 2` before acting
on it. The lifetime ratio agrees with iostat exactly:
`node_disk_read_time_seconds_total / node_disk_reads_completed_total`.

**Trim latency is high on healthy drives.** btrfs issues very large
asynchronous discards; hundreds of milliseconds to tens of seconds there is
normal. Judge a drive on read and write await.

**`rig:proc:rss_bytes` can exceed physical memory.** It counts pages shared
between forks once per fork. Use `rig:proc:rss_proportional_bytes`.
