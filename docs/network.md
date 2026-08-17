# Network

Who is using the link, and why the machine lags while nothing is maxed out.

```
rig net
```

That prints a verdict, the link's rate against what it is sold as, the queue in
front of every packet, the process groups on it, and the addresses they talk
to.

## A link is never slow. It is full.

Throughput cannot answer "the internet is laggy". A full 100 Mb/s line and an
idle one deliver the same web page at the same rate — until the queue in front
of it fills. Then every interactive packet waits behind somebody else's bulk
transfer, and the wait is measured in hundreds of milliseconds while the
throughput graph shows a healthy, busy line.

That queue is **bufferbloat**: a router with far more buffer than the line
needs, holding packets instead of dropping them. TCP only learns to slow down
when packets are dropped, so a large buffer teaches it nothing and the queue
stays full.

The measurement is round-trip time against its own idle value:

```
rig:net:bufferbloat_ratio = rig:net:rtt_seconds / rig:net:rtt_floor_seconds
```

| Ratio | Reading |
| --- | --- |
| under 1.5 | The queue is short. Interactive traffic survives a full link. |
| 2 to 4 | Noticeable. Calls and games degrade while anything downloads. |
| above 4 | The lag is real, and it is not the download rate that caused it. |

**A download speed cap only helps while it holds the line below full.** Capping
a client at half the line and then running two of them puts the queue back.
What fixes it at any rate is SQM on the router — `fq_codel` or `cake` — which
drops or marks early instead of hoarding.

`rig net speedtest` measures both halves at once: it fills the line and reports
the round-trip time it caused, graded A to F.

## How a byte gets a name

The kernel counts bytes per interface. It never counts them per process, so
"who is using the internet" has no single source. `tools/net-exporter.py` joins
four readings:

| Reading | Gives | Limit |
| --- | --- | --- |
| `inet_diag` over netlink | Every TCP socket's bytes, round-trip time, retransmissions, and the inode that owns it | TCP only |
| `/proc/<pid>/fd` | inode → process, the way `ss -p` does it | Needs root |
| `nf_conntrack` | UDP flow bytes | Needs `nf_conntrack_acct=1` |
| ICMP echo | Round-trip time and loss, to the gateway and past it | — |

Process groups come from `process-exporter/config.yml`, the same file that
names every other `rig:proc:*` series. One vocabulary: `rig:net:proc:*` and
`rig:proc:*` join on `groupname`.

Traffic is split by **scope**, because most interfaces on a development machine
never reach the ISP:

| Scope | Addresses | Counted against the line |
| --- | --- | --- |
| `internet` | Everything public | Yes |
| `private` | RFC1918, link-local, and the 100.64/10 range tailscale uses | No |
| `local` | Loopback | No |

`rig:net:proc:uplink_bytes_per_sec` is the internet-only series. It is the
answer to the question, and the one `rig who --by net` ranks.

## What this cannot see, and how large that is

**UDP has no byte counter in the socket layer.** Not a limitation of this
stack — the kernel does not keep one. QUIC is UDP, so a browser reading video
can be the busiest thing on the link and appear idle here. Connection tracking
does count it, once accounting is on:

```
sudo sysctl -w net.netfilter.nf_conntrack_acct=1
echo net.netfilter.nf_conntrack_acct=1 | sudo tee /etc/sysctl.d/99-rig-net.conf
```

It costs 16 bytes per tracked flow and nothing else. `rig net doctor` says
whether it is on.

**Sockets are sampled**, every 10 seconds by default. A connection that opens
and closes between two passes is never seen. That mostly means short HTTP
requests, which move little.

**A container with its own network namespace keeps its sockets there**, and the
reader runs in the host's namespace, so it cannot see them at all. cAdvisor
reads each namespace's own counters instead:

```
rig:net:container:rx_bytes_per_sec   # by container
rig:net:stack:bytes_per_sec          # by compose project
```

`rig net` prints them in their own table, because they are not in the process
groups above and their peers are not in the peer table. Containers on the
host's network are excluded from these series — every interface they report is
the host's own, so nine of them would each claim the whole machine's traffic,
and their sockets are already named by process.

Both gaps are measured rather than argued away. The exporter reads the
interface's own counters over the same period and reports the difference:

```
rig:net:attributed_ratio          # share of link bytes with a named owner
rig:net:unattributed_bytes_per_sec
```

The numerator counts sockets bound to the uplink's own addresses, plus what
cAdvisor saw each container's namespace move. A container bridge cannot pad it.

**Read it under load only.** The interface counts packet headers and
retransmissions; a socket counts payload. A host-side bulk transfer with every
owner known reads about 0.95, and a container-side one closer to 1.0, because
cAdvisor counts frames as well. On a quiet link the traffic is acknowledgements
and keepalives, whose headers outweigh their payload, and the same healthy
machine reads 0.4. Judge it while the line is busy — which is what
`RigNetBlindToUdp` does before it fires.

Below 0.6 with conntrack accounting off, `RigNetBlindToUdp` fires and names the
sysctl above.

## Telling the radio from the line

On wireless, the link to the access point fails long before the ISP does.

| Series | Reading |
| --- | --- |
| `rig:net:wifi_signal_dbm` | Above −60 is strong. Below −72 the radio steps down to slow rates and *it* is the bottleneck. |
| `rig:net:wifi_retries_per_sec` | Frames sent again. Airtime spent on nothing, and it rises long before throughput falls. |
| `rig:net:loss_ratio{kind="gateway"}` | Loss to the router is the radio or the cable. Loss only past it is the ISP. |

The ICMP probe pings both the default gateway and one address past it, labelled
`kind="gateway"` and `kind="internet"`, so the two questions never get one
answer.

## What the line is sold as

Nothing can call a link full without knowing its size. Set it once:

```
RIG_NET_DOWN_MBIT=100
RIG_NET_UP_MBIT=100
```

in `.env`, then `docker compose up -d net-exporter`. Until then
`rig:net:link:rx_saturation` is empty — deliberately. The stand-in is
`rig:net:link:rx_share_of_peak`, measured against the fastest this line has been
seen to go in 7 days, and it reads 100% every time a download beats the last
record, which is why it is not the figure any alert uses.

`rig net speedtest` prints the two lines to paste.

## The commands

| Command | Answers |
| --- | --- |
| `rig net` | Is the link full, is it queued, and who is on it |
| `rig net who --since 24h` | Process groups ranked by internet traffic |
| `rig net peers` | The addresses it goes to, with reverse DNS |
| `rig net conns --sort rtt` | Every live connection, its round-trip time and retransmissions |
| `rig net speedtest` | Fill the line, measure the queue that makes, grade it |
| `rig net doctor` | What is measured, what is missing, and the line that fixes it |

Per-connection detail is deliberately never a metric. One series per socket
would cost more storage than every other reading on this machine together, so
`rig net conns` reads the exporter's own `/flows` endpoint at
<http://127.0.0.1:13370/flows> instead of Prometheus.

Series are listed in [metrics.md](metrics.md). The dashboard is **Rig —
Network**.
