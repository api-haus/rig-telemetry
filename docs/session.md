# The session bus

The desktop has a single point of failure that is not the compositor and not
the GPU. When the user D-Bus broker dies, the graphical session is logged out
and every application in it dies with it, whether or not it ever used D-Bus.

This page is the failure mode, how to recognise it in a log, and the limit that
causes it.

## Why one dead broker logs you out

SDDM starts the session as `uwsm start -e -D Hyprland hyprland.desktop`. uwsm
does not run the compositor itself. It forks `systemctl` and waits on it:

```
uwsm[4930]: Starting hyprland.desktop and waiting while it is running...
uwsm[4930]: Forked systemctl, PID 5023.
```

That `systemctl` reaches the user manager over the session bus. So the bus is
not a service the session uses — it is the wire the session is supervised
through. Kill the bus and the supervisor loses its subject, exits non-zero, and
uwsm reports the session finished. SDDM then closes the PAM session and logind
tears the whole thing down.

Measured, 2026-08-18. Ten log lines, twelve milliseconds:

```
04:19:48.295494  dbus-broker-launch: ERROR sockopt_get_peerpidfd: Too many open files
                                     peer_new_with_fd    @ src/bus/peer.c +290
                                     listener_dispatch   @ src/bus/listener.c +54
04:19:48.296145  systemd[1216]: Got disconnect on API bus.
04:19:48.297710  dbus-broker[5277]: Dispatched 6236 messages @ 1(±1)μs / message.
04:19:48.297890  xdg-permission-store.service: Main process exited, status=1/FAILURE
04:19:48.298120  uwsm[4930]: PID 5023 exited with RC 1
04:19:48.300299  wireplumber: DBus connection closed: returned 0 bytes on an async read
04:19:48.302702  dbus-broker[1274]: Dispatched 1117000 messages @ 1(±2)μs / message.
04:19:48.303695  dbus-broker-launch: Caught SIGCHLD of broker.
04:19:48.307156  sddm-helper: [PAM] Closing session
```

`peer_new_with_fd` is the accept path. The broker did not fail on a message; it
failed while taking a **new connection**, and a broker that cannot accept
cannot continue.

## Why it looks like the applications crashed

It does not look like a logout, because nothing announces one. What a human
sees is roughly half the desktop dying at once, the rest staying up but
degraded, no new application starting, and the remainder disappearing over the
next minute or two. That shape is diagnostic, and it maps to three different
mechanisms firing in order:

**At once.** Anything that treats bus loss as fatal. Electron raises `int3` the
moment its connection returns zero bytes, so the kernel logs the abort with the
thread name rather than the application name:

```
traps: ThreadPoolSingl[5490] trap int3 ... in 1password
traps: ThreadPoolSingl[8398] trap int3 ... in orca-ide
```

**Nothing new starts.** There is no compositor left to connect to, and the
session is being torn down, so a launch has nowhere to land.

**Over the next minute.** logind kills the stragglers — `Session 3 logged out.
Waiting for processes to exit.` A pure Wayland client with no bus connection
survives until this reaches it, which is why the desktop dies in two waves
rather than one.

Wine makes the second wave loud. A Proton game that loses its display spawns
one `winedbg` per crashed process; the incident above produced 218 `winedbg`
and 216 `conhost.exe` about 27 seconds **after** the session was already gone.
They are wreckage, not cause. Read the timestamps before believing them.

## The limit

The broker inherits the systemd default soft limit of 1024 open files, while
its own configuration invites far more work than that:

| Where | Setting | Value |
| --- | --- | --- |
| `/usr/share/dbus-1/session.conf` | `max_completed_connections` | 100000 |
| `/usr/share/dbus-1/session.conf` | `max_connections_per_user` | 100000 |
| `/usr/share/dbus-1/session.conf` | `max_incoming_unix_fds` | 250000000 |
| kernel, per process | `RLIMIT_NOFILE` soft | **1024** |

Nothing reconciles the two. The broker never refuses a connection on policy,
because policy allows a hundred thousand of them. It accepts until the kernel
returns `EMFILE`, and then it dies. The system broker does not have this
problem — it runs at 16384.

The fix is a drop-in, `/etc/systemd/user/dbus-broker.service.d/10-nofile.conf`:

```ini
[Service]
LimitNOFILE=524288
```

Applied 2026-08-18. Verify against the running broker, not against the unit:

```
systemctl --user show dbus-broker.service -p LimitNOFILE
grep 'Max open files' /proc/$(pgrep -x dbus-broker | head -1)/limits
```

A `daemon-reload` alone does not move an already-running broker. It takes a
`systemctl --user restart dbus-broker.service`, which drops every client on the
bus — so do it while the graphical session is down, never under one.

## What pushed it over

The broker holds about one socket per connected peer, and peer count follows
the machine's process count. Measured against `node_processes_pids`, it is a
burst and not a leak — every excursion returns to baseline:

| Time | Broker fds | Processes | fds/proc |
| --- | --- | --- | --- |
| 08-16 19:40 | 392 | 995 | 0.39 |
| 08-16 19:50 | 1216 | 1400 | 0.87 |
| 08-16 20:20 | 394 | 1000 | 0.39 |
| 08-17 00:50 | 1118 | 1117 | 1.00 |
| 08-17 01:00 | 326 | 724 | 0.45 |

Baseline is about 400 descriptors at 1000 processes. Four hundred more
processes adds about 800 descriptors and lands near 1200, past a 1024 ceiling.
That is the size of a Wine crash storm, so the two excursions above are the
same event surviving at 0.995 rather than tipping.

Bus connections are opened per process, so anything that spawns processes in
bulk spends this budget. Two things did, together:

**Steam.** Launched 04:19:14, the session died 04:19:48. Steam and Proton open
a burst of connections during startup.

**gamemode, thrashing.** 61 `Entering Game Mode` and 61 `Leaving Game Mode` in
one boot, 10 of them inside the single minute 04:19. Each transition runs
`hook.sh`, which stops and starts about six Docker containers and four systemd
timers. Repeatedly launching and killing one binary under gamemode — an
emulator under debug, here — multiplies that by every restart.

The load generator is worth knowing, but it is not the defect. A desktop must
survive a process burst. The defect is a broker that accepts what it cannot
hold.

## Distinguishing this from the other two

Three failures on this machine look similar from the chair and share no
mechanism. Check in this order, because the cheapest test comes first.

| | Session bus death | [VRAM exhaustion](vram.md) | Compositor GPU fault |
| --- | --- | --- | --- |
| Uptime | unbroken | unbroken | unbroken |
| Tell | `Too many open files` in `peer_new_with_fd` | `Failed to allocate NVKMS memory for GEM object` | `NVRM: Xid ...` on the compositor's channel |
| Session | logged out, PAM closed | survives | restarted by uwsm |
| `rig:gpu:vram_used_ratio` | normal | above 0.85 | normal |
| Recovery | log in again at the greeter | log in again | automatic |

The GPU is innocent unless the kernel log names it. In the 2026-08-18 incident
the card peaked at 11.06 of 15.63 GiB with no Xid and no allocation failure,
and the VRAM sawtooth on the dashboard was an emulator being started and killed
by hand, not memory being exhausted. Read the kernel log before the dashboard.
