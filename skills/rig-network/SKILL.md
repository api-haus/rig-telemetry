---
name: rig-network
description: Find out who is using a workstation's internet connection, and whether the link is full, queued, lossy or fine. Use when the user says the internet is slow, laggy or stuttering, asks "who is using all the bandwidth", "what is downloading", "why is my ping high", "why do calls or games lag", complains that a capped download still slows everything, or asks about wifi signal, packet loss, DNS or a speed test. Also use before blaming a download's speed setting for machine-wide lag.
---

# rig-network

```
rig net
```

Prints a verdict, the link's rate against what it is sold as, the queue in
front of every packet, the process groups on it, and the addresses they reach.

`rig` is on PATH whenever this plugin is installed, and needs only the
Prometheus endpoint. If it cannot reach it, the error names the command that
starts the stack.

## A link is never slow. It is full.

Throughput cannot answer "the internet is laggy". A full line and an idle one
deliver the same page at the same rate — until the router's queue fills. Then
every interactive packet waits behind somebody else's bulk transfer, for
hundreds of milliseconds, while the throughput graph shows a healthy busy line.

The measurement is round-trip time against that path's own idle value:

```
rig q 'rig:net:bufferbloat_ratio'   # rig:net:rtt_seconds / rig:net:rtt_floor_seconds
```

| Ratio | Reading |
| --- | --- |
| under 1.5 | Queue is short. Interactive traffic survives a full link. |
| 2 to 4 | Calls and games degrade while anything downloads. |
| above 4 | The lag is real, and the download rate did not cause it. |

**Never answer "the internet is slow" with a bandwidth figure alone.** Quote
the ratio beside it.

A download speed cap only helps while it holds the line below full. Two capped
clients refill the queue. What fixes it at any rate is SQM — `fq_codel` or
`cake` — on the router. Say that instead of proposing a lower cap.

## Name the group, not the program

```
rig net who --since 1h      # process groups ranked by internet traffic
rig net peers               # the addresses it goes to, with reverse DNS
rig net conns --sort rtt    # every live connection and its round trip
```

`rig:net:proc:uplink_bytes_per_sec` counts internet traffic only. Container
bridges, VMs and the tailnet are excluded, because none of them touch the line.
Groups are the same ones every other `rig` answer uses, defined in
`process-exporter/config.yml`.

## State the blind spot whenever you name a top talker

```
rig net doctor
```

UDP carries no byte counter in the kernel's socket layer. QUIC is UDP, so a
browser reading video can be the busiest thing on the link and read as idle
until connection tracking counts it:

```
sudo sysctl -w net.netfilter.nf_conntrack_acct=1
```

Sockets are also sampled every 10 seconds, so a connection shorter than one
pass is never seen. Both gaps are measured against the interface's own
counters:

```
rig q 'rig:net:attributed_ratio'   # share of link bytes with a named owner
```

Below about 0.8, say so in the same sentence as the top talker. A confident
name over a 50% blind spot is a wrong answer with a number on it.

## Radio, line, or ISP

Loss to the gateway and loss past it mean different things, so the probe pings
both and labels them `kind="gateway"` and `kind="internet"`.

| Reading | Means |
| --- | --- |
| `rig:net:wifi_signal_dbm` below −72 | The radio steps down to slow rates. It is the bottleneck before any queue is. |
| `rig:net:wifi_retries_per_sec` rising | Airtime spent saying the same thing again. Rises long before throughput falls. |
| Loss at `kind="gateway"` | The radio or the cable, inside the house. |
| Loss only at `kind="internet"` | Past the router. The ISP. |
| `rig:net:resolver_seconds` high, link quiet | Name resolution, not the line. |

## Measuring the line

```
rig net speedtest              # fills the line both ways, grades the queue A to F
rig net speedtest --down-only
```

It saturates the connection on purpose for a few seconds each way, so say what
it will do before running it on a machine somebody is using. It prints the two
lines to paste into `.env`:

```
RIG_NET_DOWN_MBIT=100
RIG_NET_UP_MBIT=100
```

Until those are set, `rig:net:link:rx_saturation` is empty and nothing can call
the link full. Report that state as "no line speed configured", never as "the
link is fine".

## Asking about the past

Every series is recorded, so "it lagged an hour ago" is answerable:

```
rig q 'topk(5, max_over_time(rig:net:proc:uplink_bytes_per_sec[24h]))'
rig range 'rig:net:bufferbloat_ratio{kind="internet"}' --since 24h --plot
rig net --at 2026-08-17T21:30:00Z
```

Method, limits and the full series list: `docs/network.md` and
`docs/metrics.md`. The dashboard is **Rig — Network** at
<http://localhost:13337>.
