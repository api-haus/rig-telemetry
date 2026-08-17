#!/usr/bin/env python3
"""Serve per-process network attribution and link quality to Prometheus.

Who is using the link is not in /proc/net/dev: the kernel counts bytes per
interface, never per process. Four readings answer it here.

    inet_diag over netlink  every TCP socket, with its byte counters, round
                            trip time and retransmissions, and the inode that
                            names its owner
    /proc/<pid>/fd          inode -> process, the way `ss -p` does it
    nf_conntrack            UDP flow bytes, which no socket counter reports.
                            Needs net.netfilter.nf_conntrack_acct=1
    ICMP echo               a saturated link is not slow, it is queued, and
                            only round trip time under load says so

    tools/net-exporter.py --port 13370 --interval 10
    tools/net-exporter.py --once      # print the metrics and exit

Group names come from process-exporter/config.yml, so `rig:net:proc:*` joins
`rig:proc:*` on the same `groupname`.

Everything is sampled, so a connection that opens and closes between two
passes is missed. `rignet_attribution_gap_ratio` measures that against the
interface counters instead of hiding it. docs/network.md.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import re
import socket
import struct
import sys
import threading
import time

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLM_F_REQUEST, NLM_F_ROOT, NLM_F_MATCH = 0x001, 0x100, 0x200
NLMSG_ERROR, NLMSG_DONE = 2, 3
INET_DIAG_INFO = 2
TCP_STATES = {
    1: "established", 2: "syn_sent", 3: "syn_recv", 4: "fin_wait1", 5: "fin_wait2",
    6: "time_wait", 7: "close", 8: "close_wait", 9: "last_ack", 10: "listen", 11: "closing",
}

# tcp_info is appended to, never reordered, so a fixed offset survives a kernel change.
TCP_INFO = {
    "rtt_us": ("=I", 68), "rttvar_us": ("=I", 72), "total_retrans": ("=I", 100),
    "bytes_acked": ("=Q", 120), "bytes_received": ("=Q", 128),
    "min_rtt_us": ("=I", 148), "bytes_sent": ("=Q", 200), "bytes_retrans": ("=Q", 208),
}

PROC = pathlib.Path(os.environ.get("RIG_NET_PROCFS", "/proc"))
HERE = pathlib.Path(__file__).resolve().parent.parent


def group_config() -> pathlib.Path:
    """process-exporter's config, wherever this runs from.

    The container mounts it at /config; a checkout has it beside this file.
    """
    named = os.environ.get("RIG_NET_GROUPS", "")
    for path in (named, "/config/config.yml", HERE / "process-exporter" / "config.yml"):
        if path and pathlib.Path(path).is_file():
            return pathlib.Path(path)
    return pathlib.Path(named or "/config/config.yml")

# Ports worth a name. A guess on a random high port is a lie with a label on it.
SERVICES = {
    20: "ftp", 21: "ftp", 22: "ssh", 25: "smtp", 53: "dns", 80: "http", 123: "ntp",
    143: "imap", 443: "https", 465: "smtp", 587: "smtp", 853: "dns-tls", 993: "imap",
    995: "pop3", 1194: "vpn", 3074: "xbox", 3478: "turn", 3479: "turn", 4070: "spotify",
    5222: "xmpp", 5223: "xmpp", 5228: "gcm", 5349: "turn", 6881: "bittorrent",
    6882: "bittorrent", 8080: "http-alt", 8443: "https-alt", 27015: "steam-game",
    27016: "steam-game", 27017: "steam-game", 27031: "steam-remote", 27036: "steam-remote",
    41641: "tailscale", 51820: "wireguard",
}
STEAM_PORTS = range(27000, 27100)


def envint(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# sockets, straight from the kernel
# --------------------------------------------------------------------------

class Socket:
    __slots__ = ("cookie", "family", "proto", "state", "laddr", "lport", "raddr",
                 "rport", "uid", "inode", "tx", "rx", "retrans_bytes", "retrans",
                 "rtt", "min_rtt", "group", "pid")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.group = "unattributed"
        self.pid = 0


def _dump(family: int, proto: int, timeout: float = 5.0) -> list[bytes]:
    """One inet_diag dump. Returns each socket's message body."""
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
    sock.settimeout(timeout)
    try:
        # inet_diag_req_v2, then an all-zero sockid meaning "every socket".
        req = struct.pack("=BBBBI", family, proto, 1 << (INET_DIAG_INFO - 1), 0, 0xFFF)
        head = struct.pack("=IHHII", 16 + len(req) + 48, SOCK_DIAG_BY_FAMILY,
                           NLM_F_REQUEST | NLM_F_ROOT | NLM_F_MATCH, 1, 0)
        sock.send(head + req + b"\0" * 48)
        out: list[bytes] = []
        while True:
            buf = sock.recv(1 << 21)
            off = 0
            while off + 16 <= len(buf):
                length, kind, _flags, _seq, _pid = struct.unpack_from("=IHHII", buf, off)
                if kind == NLMSG_DONE or length < 16:
                    return out
                if kind == NLMSG_ERROR:
                    (err,) = struct.unpack_from("=i", buf, off + 16)
                    raise OSError(-err, os.strerror(-err) if err else "netlink error")
                out.append(buf[off + 16:off + length])
                off += (length + 3) & ~3
    finally:
        sock.close()


def _ip(family: int, raw: bytes) -> str:
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[:4])
    return socket.inet_ntop(socket.AF_INET6, raw[:16])


def _sock(body: bytes, family: int, proto: str) -> Socket:
    fam, state = struct.unpack_from("=BB", body, 0)
    sport, dport = struct.unpack_from("!HH", body, 4)
    hi, lo = struct.unpack_from("=II", body, 44)
    _expires, _rq, _wq, uid, inode = struct.unpack_from("=IIIII", body, 52)

    attrs, off = {}, 72
    while off + 4 <= len(body):
        alen, atype = struct.unpack_from("=HH", body, off)
        if alen < 4:
            break
        attrs[atype] = body[off + 4:off + alen]
        off += (alen + 3) & ~3

    info = attrs.get(INET_DIAG_INFO, b"")
    read = {}
    for name, (fmt, at) in TCP_INFO.items():
        read[name] = (struct.unpack_from(fmt, info, at)[0]
                      if at + struct.calcsize(fmt) <= len(info) else 0)
    return Socket(
        cookie=(hi << 32) | lo, family=fam, proto=proto,
        state=TCP_STATES.get(state, str(state)),
        laddr=_ip(fam, body[8:24]), lport=sport,
        raddr=_ip(fam, body[24:40]), rport=dport,
        uid=uid, inode=inode,
        # bytes_acked is what the peer confirmed; bytes_sent bills a lossy link twice.
        tx=read["bytes_acked"], rx=read["bytes_received"],
        retrans_bytes=read["bytes_retrans"], retrans=read["total_retrans"],
        rtt=read["rtt_us"] / 1e6, min_rtt=read["min_rtt_us"] / 1e6,
    )


def sockets() -> tuple[list[Socket], list[Socket]]:
    """Every TCP socket with counters, and every UDP socket without them.

    UDP carries no byte counters anywhere in the kernel's socket layer. The
    UDP list is here to name the owner of a conntrack flow by its local port.
    """
    tcp, udp = [], []
    for family in (socket.AF_INET, socket.AF_INET6):
        for body in _dump(family, socket.IPPROTO_TCP):
            tcp.append(_sock(body, family, "tcp"))
        for body in _dump(family, socket.IPPROTO_UDP):
            udp.append(_sock(body, family, "udp"))
    return tcp, udp


def inode_owners(wanted: set[int]) -> dict[int, int]:
    """socket inode -> pid, by walking every process's file descriptors."""
    found: dict[int, int] = {}
    for entry in os.scandir(PROC):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with os.scandir(f"{entry.path}/fd") as fds:
                for fd in fds:
                    try:
                        target = os.readlink(fd.path)
                    except OSError:
                        continue
                    if target.startswith("socket:["):
                        inode = int(target[8:-1])
                        if inode in wanted and inode not in found:
                            found[inode] = pid
        except OSError:
            continue
    return found


# --------------------------------------------------------------------------
# process groups — the same names process-exporter gives
# --------------------------------------------------------------------------

class Groups:
    """process-exporter/config.yml, read for its `comm` lists and name templates.

    The file is the single definition of a group name. Reading it here rather
    than repeating it means `rig:net:proc:*` and `rig:proc:*` cannot drift into
    two vocabularies for the same process.

    Only the subset that file uses is understood: `comm` lists, `cmdline`
    regexes and the `{{.Comm}}` template. Anything else falls back to the
    process's own name, which is what the catch-all rule would have produced.
    """

    ENTRY = re.compile(r"^\s*-\s+name:\s*(.+?)\s*$")
    FIELD = re.compile(r"^\s+(comm|cmdline|exe):\s*\[(.*)\]\s*$")

    def __init__(self, path: pathlib.Path | None = None):
        path = path or group_config()
        self.rules: list[tuple[str, set[str], list[re.Pattern]]] = []
        self.source = str(path)
        self.error = ""
        try:
            self._parse(path.read_text())
        except OSError as e:
            self.error = str(e)

    def _parse(self, text: str):
        name, comms, cmdlines = "", set(), []
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            head = self.ENTRY.match(line)
            if head:
                if name:
                    self.rules.append((name, comms, cmdlines))
                name, comms, cmdlines = head.group(1).strip('"\' '), set(), []
                continue
            field = self.FIELD.match(line)
            if not field or not name:
                continue
            items = [i.strip().strip('"\'') for i in field.group(2).split(",") if i.strip()]
            if field.group(1) == "comm":
                comms |= set(items)
            else:
                for item in items:
                    try:
                        cmdlines.append(re.compile(item))
                    except re.error:
                        continue
        if name:
            self.rules.append((name, comms, cmdlines))

    def of(self, pid: int) -> str:
        try:
            comm = (PROC / str(pid) / "comm").read_text().strip()
        except OSError:
            return "unattributed"
        cmdline = None
        for name, comms, cmdlines in self.rules:
            if comm in comms:
                return name.replace("{{.Comm}}", comm)
            if cmdlines:
                if cmdline is None:
                    try:
                        raw = (PROC / str(pid) / "cmdline").read_bytes()
                    except OSError:
                        raw = b""
                    cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
                if any(p.search(cmdline) for p in cmdlines):
                    return name.replace("{{.Comm}}", comm)
        return comm or "unattributed"


# --------------------------------------------------------------------------
# where the traffic goes
# --------------------------------------------------------------------------

def scope_of(addr: str) -> str:
    """internet, private or local — the split the ISP link cares about.

    Traffic to a container bridge, a VM or the tailnet never touches the
    uplink, so counting it against the line would report a saturated link on a
    machine that sent nothing out of the house.
    """
    if ":" in addr:
        low = addr.lower()
        if low in ("::1", "::"):
            return "local"
        if low.startswith(("fe80", "fc", "fd", "ff")):
            return "private"          # link-local, unique-local, and multicast
        return "internet"
    try:
        a, b, *_ = (int(p) for p in addr.split("."))
    except ValueError:
        return "internet"
    if a in (0, 127):
        return "local"
    # mDNS, SSDP and every other discovery protocol lives here and never
    # leaves the house, so it must not read as traffic on the uplink.
    if a >= 224 or addr == "255.255.255.255":
        return "private"
    if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
        return "private"
    if (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127):
        return "private"           # link-local, and the CGNAT range tailscale uses
    return "internet"


def kind_of(addr: str) -> str:
    """A probe target is either this side of the router or the far side."""
    return "gateway" if scope_of(addr) in ("private", "local") else "internet"


def service_of(port: int) -> str:
    if port in SERVICES:
        return SERVICES[port]
    if port in STEAM_PORTS:
        return "steam"
    return str(port)


def uplink_addresses(device: str) -> set[str]:
    """Every address the uplink answers on.

    A socket says which local address it uses, and that is the only honest way
    to decide whether its bytes crossed the uplink or a container bridge.
    """
    found: set[str] = set()
    if not device:
        return found
    try:
        import fcntl
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(probe.fileno(), 0x8915,
                                 struct.pack("256s", device[:15].encode()))
            found.add(socket.inet_ntoa(packed[20:24]))
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for line in (PROC / "net" / "if_inet6").read_text().splitlines():
            cols = line.split()
            if len(cols) >= 6 and cols[-1] == device:
                found.add(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(cols[0])))
    except (OSError, ValueError):
        pass
    return found


def default_route() -> str:
    """The interface the uplink actually leaves by."""
    try:
        for line in (PROC / "net" / "route").read_text().splitlines()[1:]:
            cols = line.split()
            if len(cols) > 2 and cols[1] == "00000000":
                return cols[0]
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------
# UDP bytes, from conntrack
# --------------------------------------------------------------------------

class Conntrack:
    """UDP flow bytes, which the socket layer does not count.

    QUIC is UDP, so a browser reading video is invisible without this. The
    counters only exist when nf_conntrack_acct is on; `available` says which
    world this machine is in, and nothing here invents a figure when it is off.
    """

    FLOW = re.compile(
        r"^(\S+)\s+\d+\s+(\S+)\s+\d+.*?src=(\S+)\s+dst=(\S+)\s+sport=(\d+)\s+dport=(\d+)"
        r"(?:\s+packets=(\d+)\s+bytes=(\d+))?.*?"
        r"src=\S+\s+dst=\S+\s+sport=\d+\s+dport=\d+"
        r"(?:\s+packets=(\d+)\s+bytes=(\d+))?")

    RETRY = 60.0

    def __init__(self):
        self.path = PROC / "net" / "nf_conntrack"
        self.acct = PROC / "sys" / "net" / "netfilter" / "nf_conntrack_acct"
        self.reason = ""
        self.checked = time.monotonic()
        self.available = self._probe()

    def _probe(self) -> bool:
        try:
            if self.acct.read_text().strip() != "1":
                self.reason = ("nf_conntrack_acct is off, so the kernel counts no bytes "
                               "per flow. UDP is unattributed until: "
                               "sysctl -w net.netfilter.nf_conntrack_acct=1")
                return False
        except OSError:
            self.reason = "no nf_conntrack_acct: this kernel tracks no connections"
            return False
        try:
            with self.path.open() as fh:
                fh.readline()
        except OSError as e:
            self.reason = f"cannot read {self.path}: {e}"
            return False
        return True

    def flows(self, limit: int = 40000) -> list[tuple]:
        """(proto, src, sport, dst, dport, bytes_out, bytes_in) per live flow."""
        if not self.available:
            # Someone turning the sysctl on should not have to restart this.
            if time.monotonic() - self.checked > self.RETRY:
                self.checked = time.monotonic()
                self.available = self._probe()
            return []
        out = []
        try:
            with self.path.open() as fh:
                for count, line in enumerate(fh):
                    if count >= limit:
                        break
                    m = self.FLOW.match(line)
                    if not m or m.group(2) not in ("udp", "tcp"):
                        continue
                    sent = int(m.group(8) or 0)
                    back = int(m.group(10) or 0)
                    out.append((m.group(2), m.group(3), int(m.group(5)),
                                m.group(4), int(m.group(6)), sent, back))
        except OSError:
            self.available = False
        return out


# --------------------------------------------------------------------------
# is the link queued? — ICMP, and the resolver
# --------------------------------------------------------------------------

class Pinger(threading.Thread):
    """Round trip time to the gateway and past it, sampled on its own clock.

    Throughput cannot tell a full link from a fast one. A queue can: the same
    bytes arrive, and every interactive packet waits behind them. This is the
    measurement that makes a capped download and a laggy machine the same fact.
    """

    daemon = True

    def __init__(self, targets: list[str], interval: float, window: int = 60):
        super().__init__(name="ping")
        self.targets = targets
        self.interval = interval
        self.window = window
        self.lock = threading.Lock()
        self.results: dict[str, list] = {t: [] for t in targets}
        self.floor: dict[str, float] = {}
        self.mode = "raw"
        self.error = ""

    def _socket(self):
        try:
            return socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        except PermissionError:
            self.mode = "dgram"
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)

    def ping(self, target: str, seq: int, timeout: float = 1.0) -> float | None:
        try:
            sock = self._socket()
        except OSError as e:
            self.error = f"no ICMP socket: {e}"
            return None
        try:
            sock.settimeout(timeout)
            ident = os.getpid() & 0xFFFF
            body = struct.pack("!d", time.time()) + b"rig-telemetry"
            head = struct.pack("!BBHHH", 8, 0, 0, ident, seq & 0xFFFF)
            head = struct.pack("!BBHHH", 8, 0, checksum(head + body), ident, seq & 0xFFFF)
            started = time.monotonic()
            sock.sendto(head + body, (target, 0))
            while time.monotonic() - started < timeout:
                packet, source = sock.recvfrom(2048)
                # A raw socket hands back the IP header, and every echo reply on the machine.
                icmp = packet[20:] if self.mode == "raw" else packet
                if len(icmp) < 8 or source[0] != target:
                    continue
                kind, _code, _sum, _id, got = struct.unpack_from("!BBHHH", icmp, 0)
                if kind == 0 and got == (seq & 0xFFFF):
                    return time.monotonic() - started
            return None
        except TimeoutError:
            return None
        except OSError as e:
            self.error = str(e)
            return None
        finally:
            sock.close()

    def run(self):
        seq = 0
        while True:
            seq += 1
            for target in self.targets:
                rtt = self.ping(target, seq)
                with self.lock:
                    kept = self.results[target]
                    kept.append(rtt)
                    del kept[:-self.window]
                    if rtt is not None and rtt < self.floor.get(target, 1e9):
                        self.floor[target] = rtt
            time.sleep(self.interval)

    def read(self) -> dict[str, dict]:
        with self.lock:
            out = {}
            for target, kept in self.results.items():
                good = [v for v in kept if v is not None]
                out[target] = {
                    "rtt": good[-1] if good and kept[-1] is not None else None,
                    "best": self.floor.get(target),
                    "worst": max(good) if good else None,
                    "loss": 1 - len(good) / len(kept) if kept else None,
                    "samples": len(kept),
                }
            return out


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    return ~(total + (total >> 16)) & 0xFFFF


class Resolver(threading.Thread):
    """How long a name takes to become an address, through the system path."""

    daemon = True
    NAMES = ("cloudflare.com", "github.com", "steamcommunity.com", "youtube.com")

    def __init__(self, interval: float = 30.0):
        super().__init__(name="resolver")
        self.interval = interval
        self.seconds: float | None = None
        self.failures = 0

    def run(self):
        turn = 0
        while True:
            name = self.NAMES[turn % len(self.NAMES)]
            turn += 1
            started = time.monotonic()
            try:
                socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
                self.seconds = time.monotonic() - started
            except OSError:
                self.failures += 1
                self.seconds = None
            time.sleep(self.interval)


def wireless() -> dict[str, dict]:
    """/proc/net/wireless — signal, and the retries a weak link pays."""
    out: dict[str, dict] = {}
    try:
        lines = (PROC / "net" / "wireless").read_text().splitlines()[2:]
    except OSError:
        return out
    for line in lines:
        name, _, rest = line.partition(":")
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            out[name.strip()] = {
                "quality": float(cols[1].rstrip(".")),
                "signal_dbm": float(cols[2].rstrip(".")),
                "noise_dbm": float(cols[3].rstrip(".")),
                "retries": float(cols[7]),
                "missed_beacons": float(cols[9]) if len(cols) > 9 else 0.0,
            }
        except ValueError:
            continue
    return out


def interface_bytes() -> dict[str, tuple[int, int]]:
    """Per-interface rx/tx, to measure what the socket sampler did not see."""
    out: dict[str, tuple[int, int]] = {}
    try:
        lines = (PROC / "net" / "dev").read_text().splitlines()[2:]
    except OSError:
        return out
    for line in lines:
        name, _, rest = line.partition(":")
        cols = rest.split()
        if len(cols) >= 9:
            out[name.strip()] = (int(cols[0]), int(cols[8]))
    return out


# --------------------------------------------------------------------------
# accumulation
# --------------------------------------------------------------------------

class Counters:
    """Per-socket deltas, added into counters Prometheus can rate().

    A socket counter is cumulative for the life of that socket, and sockets
    come and go. Only the growth between two passes is real traffic, so the
    first pass records where everything stood and adds nothing — otherwise a
    restart would report every long-lived connection's whole history as a
    spike in one scrape interval.
    """

    def __init__(self, peers: int = 40):
        self.proc: dict[tuple, float] = {}
        self.peer: dict[tuple, float] = {}
        self.retrans: dict[str, float] = {}
        self.last: dict[int, tuple] = {}
        self.flows: dict[tuple, tuple] = {}
        self.interfaces: dict[str, tuple[int, int]] = {}
        self.attributed = 0.0
        self.link = 0.0
        self.primed = False
        self.top_peers = peers
        self.uplink: set[str] = set()

    def add(self, key: tuple, value: float, into: dict):
        if value > 0:
            into[key] = into.get(key, 0.0) + value

    def sockets(self, live: list[Socket]):
        seen = set()
        for s in live:
            seen.add(s.cookie)
            was = self.last.get(s.cookie)
            self.last[s.cookie] = (s.tx, s.rx, s.retrans_bytes)
            if was is None and self.primed:
                grew = (s.tx, s.rx, s.retrans_bytes)
            elif was is None:
                continue
            else:
                grew = tuple(max(0, now - then) for now, then in
                             zip((s.tx, s.rx, s.retrans_bytes), was))
            scope = scope_of(s.raddr)
            self.add((s.group, "tcp", scope, "tx"), grew[0], self.proc)
            self.add((s.group, "tcp", scope, "rx"), grew[1], self.proc)
            self.add(s.group, grew[2], self.retrans)
            service = service_of(s.rport)
            self.add((s.raddr, service, scope, "tx"), grew[0], self.peer)
            self.add((s.raddr, service, scope, "rx"), grew[1], self.peer)
            if s.laddr in self.uplink:
                self.attributed += grew[0] + grew[1]
        for cookie in set(self.last) - seen:
            del self.last[cookie]

    def conntrack(self, flows: list[tuple], owners: dict[tuple, str]):
        """Flow counters, attributed by the local port that opened them."""
        seen = set()
        for proto, src, sport, dst, dport, out_bytes, in_bytes in flows:
            if proto != "udp":
                continue                       # TCP is counted from its socket
            key = (proto, src, sport, dst, dport)
            seen.add(key)
            was = self.flows.get(key)
            self.flows[key] = (out_bytes, in_bytes)
            if was is None and not self.primed:
                continue
            grew = ((out_bytes, in_bytes) if was is None or out_bytes < was[0]
                    else (out_bytes - was[0], in_bytes - was[1]))
            # conntrack records a flow from whichever end opened it. When that
            # end was not this machine, the local port is the destination one.
            if (proto, sport) not in owners and (proto, dport) in owners:
                src, sport, dst, dport = dst, dport, src, sport
                grew = (grew[1], grew[0])
            group = owners.get((proto, sport), "unattributed")
            scope = scope_of(dst)
            self.add((group, "udp", scope, "tx"), grew[0], self.proc)
            self.add((group, "udp", scope, "rx"), grew[1], self.proc)
            service = service_of(dport)
            self.add((dst, service, scope, "tx"), grew[0], self.peer)
            self.add((dst, service, scope, "rx"), grew[1], self.peer)
            if src in self.uplink:
                self.attributed += grew[0] + grew[1]
        for key in set(self.flows) - seen:
            del self.flows[key]

    def link_traffic(self, device: str, now: dict[str, tuple[int, int]]):
        """What the interface itself moved, for the gap the sampler leaves."""
        was = self.interfaces.get(device)
        self.interfaces = now
        if device in now and was and self.primed:
            rx, tx = now[device]
            self.link += max(0, rx - was[0]) + max(0, tx - was[1])

    def ranked_peers(self) -> dict[tuple, float]:
        """The busiest peers by name, the rest summed into `other`.

        A per-address series for every host a browser touches would cost more
        storage than every hardware metric on this machine put together.
        """
        totals: dict[str, float] = {}
        for (peer, _service, _scope, _dir), value in self.peer.items():
            totals[peer] = totals.get(peer, 0.0) + value
        keep = {p for p, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:self.top_peers]}
        out: dict[tuple, float] = {}
        for (peer, service, scope, direction), value in self.peer.items():
            key = (peer if peer in keep else "other", service if peer in keep else "other",
                   scope, direction)
            out[key] = out.get(key, 0.0) + value
        if len(self.peer) > 20000:
            self.peer = {k: v for k, v in self.peer.items() if k[0] in keep}
        return out


# --------------------------------------------------------------------------
# metrics text
# --------------------------------------------------------------------------

def escape(value: str) -> str:
    return str(value).replace("\\", r"\\").replace('"', r"\"").replace("\n", " ")


def number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 2 ** 53:
        return str(int(value))
    return repr(float(value))


class Registry:
    """The metrics text, rebuilt after every pass and served as it stands."""

    def __init__(self):
        self.body = "# rig net exporter starting\n"
        self.flows = "[]"
        self.lock = threading.Lock()

    def publish(self, text: str, flows: str = ""):
        with self.lock:
            self.body = text
            if flows:
                self.flows = flows

    def read(self, what: str = "metrics") -> bytes:
        with self.lock:
            return (self.body if what == "metrics" else self.flows).encode()


def render(state: dict) -> str:
    out: list[str] = []

    def emit(name, kind, help_text, samples):
        if not samples:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if value is None:
                continue
            tags = ",".join(f'{k}="{escape(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{tags}}} {number(value)}" if tags else f"{name} {number(value)}")

    counters: Counters = state["counters"]
    proc = counters.proc
    emit("rignet_proc_sent_bytes_total", "counter",
         "Bytes a process group sent, by protocol and by where they went.",
         [({"groupname": g, "proto": p, "scope": s}, v)
          for (g, p, s, d), v in sorted(proc.items()) if d == "tx"])
    emit("rignet_proc_received_bytes_total", "counter",
         "Bytes a process group received.",
         [({"groupname": g, "proto": p, "scope": s}, v)
          for (g, p, s, d), v in sorted(proc.items()) if d == "rx"])
    emit("rignet_proc_retransmitted_bytes_total", "counter",
         "Bytes the kernel had to send again for this group. Loss, seen from the sender.",
         [({"groupname": g}, v) for g, v in sorted(counters.retrans.items())])

    live = state["live"]
    conns: dict[tuple, int] = {}
    rtt: dict[str, float] = {}
    for s in live:
        if s.state == "listen":
            continue
        conns[(s.group, s.proto, s.state)] = conns.get((s.group, s.proto, s.state), 0) + 1
        if s.state == "established" and s.rtt > 0:
            rtt[s.group] = max(rtt.get(s.group, 0.0), s.rtt)
    emit("rignet_proc_connections", "gauge", "Open connections per group.",
         [({"groupname": g, "proto": p, "state": st}, v) for (g, p, st), v in sorted(conns.items())])
    emit("rignet_proc_rtt_seconds", "gauge",
         "Worst round trip time among a group's established connections.",
         [({"groupname": g}, v) for g, v in sorted(rtt.items())])

    peers = counters.ranked_peers()
    emit("rignet_peer_sent_bytes_total", "counter",
         "Bytes sent to one remote address. Beyond the busiest, addresses fold into `other`.",
         [({"peer": p, "service": sv, "scope": sc}, v)
          for (p, sv, sc, d), v in sorted(peers.items()) if d == "tx"])
    emit("rignet_peer_received_bytes_total", "counter",
         "Bytes received from one remote address.",
         [({"peer": p, "service": sv, "scope": sc}, v)
          for (p, sv, sc, d), v in sorted(peers.items()) if d == "rx"])

    # `kind` separates the two questions a probe answers: the gateway is your
    # own radio and cable, anything past it is the line you pay for.
    probe = [({"target": t, "kind": kind_of(t)}, r) for t, r in sorted(state["ping"].items())]
    emit("rignet_probe_rtt_seconds", "gauge",
         "Last ICMP round trip time. Against `_floor` this is the queue in front of you.",
         [(tags, r["rtt"]) for tags, r in probe])
    emit("rignet_probe_rtt_floor_seconds", "gauge",
         "Best round trip time seen since this exporter started — the unloaded link.",
         [(tags, r["best"]) for tags, r in probe])
    emit("rignet_probe_rtt_worst_seconds", "gauge",
         "Worst round trip time in the recent window.",
         [(tags, r["worst"]) for tags, r in probe])
    emit("rignet_probe_loss_ratio", "gauge", "Share of recent echoes that never came back.",
         [(tags, r["loss"]) for tags, r in probe])
    emit("rignet_resolver_seconds", "gauge",
         "Time for a name to become an address through the system resolver.",
         [({}, state["resolver"])])
    emit("rignet_resolver_failures_total", "counter", "Name lookups that failed.",
         [({}, state["resolver_failures"])])

    radio = sorted(state["wireless"].items())
    emit("rignet_wifi_signal_dbm", "gauge",
         "Received signal. Below -70 dBm the radio drops to slow rates and everything queues.",
         [({"device": d}, w["signal_dbm"]) for d, w in radio])
    emit("rignet_wifi_link_quality", "gauge", "Driver's own link quality figure.",
         [({"device": d}, w["quality"]) for d, w in radio])
    emit("rignet_wifi_noise_dbm", "gauge", "Noise floor, where the driver reports one.",
         [({"device": d}, w["noise_dbm"]) for d, w in radio if w["noise_dbm"] > -200])
    emit("rignet_wifi_retries_total", "counter",
         "Frames the radio had to send again. Airtime spent on nothing.",
         [({"device": d}, w["retries"]) for d, w in radio])
    emit("rignet_wifi_missed_beacons_total", "counter", "Beacons the radio did not hear.",
         [({"device": d}, w["missed_beacons"]) for d, w in radio])

    emit("rignet_default_route", "gauge",
         "1 on the interface the uplink leaves by. Join on it to pick the link out of a dozen bridges.",
         [({"device": state["device"]}, 1)] if state["device"] else [])
    emit("rignet_link_capacity_bits_per_second", "gauge",
         "What the line is sold as, from RIG_NET_DOWN_MBIT and RIG_NET_UP_MBIT. Unset means unknown.",
         [({"direction": d}, v) for d, v in sorted(state["capacity"].items())])
    emit("rignet_attributed_bytes_total", "counter",
         "Payload bytes on the uplink's own addresses that this exporter named an owner for.",
         [({}, counters.attributed)])
    emit("rignet_link_bytes_total", "counter",
         "Bytes the uplink interface moved over the same period, owner or not. Counts "
         "packet headers and retransmissions, which a socket counter does not, so the two "
         "never meet: a bulk transfer with every owner known reads about 0.95.",
         [({}, counters.link)])

    emit("rignet_conntrack_available", "gauge",
         "1 when nf_conntrack accounting is on. At 0 every UDP byte, QUIC included, is unattributed.",
         [({}, 1 if state["conntrack"] else 0)])
    emit("rignet_sockets", "gauge", "Sockets read from the kernel on the last pass.",
         [({"proto": "tcp"}, state["tcp_sockets"]), ({"proto": "udp"}, state["udp_sockets"])])
    emit("rignet_flows", "gauge", "Conntrack flows read on the last pass.",
         [({}, state["flow_count"])])
    emit("rignet_scrape_duration_seconds", "gauge", "How long the last pass took.",
         [({}, state["seconds"])])
    emit("rignet_scrape_timestamp_seconds", "gauge", "When the last pass finished.",
         [({}, time.time())])
    emit("rignet_scrape_errors", "gauge", "Readings the last pass could not take.",
         [({}, state["errors"])])
    emit("rignet_groups_source", "gauge",
         "1 when process group names were read from process-exporter's own config.",
         [({"path": state["groups_source"]}, 0 if state["groups_error"] else 1)])
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------

def collect(counters: Counters, groups: Groups, ct: Conntrack) -> tuple[dict, list[Socket]]:
    started = time.monotonic()
    errors = 0
    try:
        tcp, udp = sockets()
    except OSError:
        errors += 1
        tcp, udp = [], []

    wanted = {s.inode for s in tcp + udp if s.inode}
    owners = inode_owners(wanted) if wanted else {}
    named: dict[int, str] = {}
    for s in tcp + udp:
        pid = owners.get(s.inode, 0)
        s.pid = pid
        if pid:
            if pid not in named:
                named[pid] = groups.of(pid)
            s.group = named[pid]

    device = default_route()
    counters.uplink = uplink_addresses(device)
    counters.sockets([s for s in tcp if s.state != "listen"])
    flows = ct.flows()
    counters.conntrack(flows, {("udp", s.lport): s.group for s in udp
                               if s.group != "unattributed"})
    counters.link_traffic(device, interface_bytes())
    counters.primed = True

    capacity = {}
    for name, direction in (("RIG_NET_DOWN_MBIT", "down"), ("RIG_NET_UP_MBIT", "up")):
        value = os.environ.get(name, "").strip()
        if value:
            try:
                capacity[direction] = float(value) * 1e6
            except ValueError:
                errors += 1
    return {
        "counters": counters, "live": tcp, "device": device, "capacity": capacity,
        "conntrack": ct.available, "tcp_sockets": len(tcp), "udp_sockets": len(udp),
        "flow_count": len(flows), "wireless": wireless(),
        "seconds": time.monotonic() - started, "errors": errors,
        "groups_source": groups.source, "groups_error": groups.error,
    }, tcp


def flow_report(live: list[Socket], ping: dict, ct: Conntrack) -> str:
    """The live connection table, for `rig net conns`. Never scraped."""
    rows = [{
        "group": s.group, "pid": s.pid, "proto": s.proto, "state": s.state,
        "local_port": s.lport, "peer": s.raddr, "port": s.rport,
        "service": service_of(s.rport), "scope": scope_of(s.raddr),
        "sent": s.tx, "received": s.rx, "rtt": s.rtt, "min_rtt": s.min_rtt,
        "retransmitted": s.retrans_bytes,
    } for s in live if s.state != "listen"]
    rows.sort(key=lambda r: -(r["sent"] + r["received"]))
    return json.dumps({"connections": rows, "ping": ping,
                       "conntrack": {"available": ct.available, "reason": ct.reason}}, indent=1)


class Handler(http.server.BaseHTTPRequestHandler):
    registry: Registry

    def do_GET(self):
        path = self.path.split("?")[0]
        if path not in ("/metrics", "/", "/flows"):
            self.send_error(404)
            return
        body = self.registry.read("flows" if path == "/flows" else "metrics")
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if path == "/flows"
                         else "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def loop(registry: Registry, interval: float, pinger: Pinger, resolver: Resolver):
    counters, groups, ct = Counters(envint("RIG_NET_TOP_PEERS", 40)), Groups(), Conntrack()
    while True:
        started = time.time()
        try:
            state, live = collect(counters, groups, ct)
            state["ping"] = pinger.read()
            state["resolver"] = resolver.seconds
            state["resolver_failures"] = resolver.failures
            registry.publish(render(state), flow_report(live, state["ping"], ct))
        except Exception as e:                                  # noqa: BLE001
            registry.publish(f"# pass failed: {escape(str(e))}\n"
                             f"rignet_scrape_errors 1\n"
                             f"rignet_scrape_timestamp_seconds 0\n")
        spent = time.time() - started
        if spent < interval:
            time.sleep(interval - spent)


def targets() -> list[str]:
    """The gateway, then something past it. Both, because only the pair tells
    a full uplink from a house with a broken router."""
    named = os.environ.get("RIG_NET_PING_TARGETS", "").strip()
    if named:
        return [t.strip() for t in named.split(",") if t.strip()]
    out = []
    try:
        for line in (PROC / "net" / "route").read_text().splitlines()[1:]:
            cols = line.split()
            if len(cols) > 2 and cols[1] == "00000000":
                raw = bytes.fromhex(cols[2])[::-1]
                out.append(socket.inet_ntop(socket.AF_INET, raw))
                break
    except (OSError, ValueError):
        pass
    return out + ["1.1.1.1"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=envint("RIG_NET_PORT", 13370))
    ap.add_argument("--addr", default=os.environ.get("RIG_NET_ADDR", "127.0.0.1"))
    ap.add_argument("--interval", type=float, default=envint("RIG_NET_INTERVAL", 10))
    ap.add_argument("--once", action="store_true", help="one pass, print the metrics, exit")
    ap.add_argument("--flows", action="store_true", help="one pass, print the connection table")
    args = ap.parse_args()

    pinger = Pinger(targets(), float(envint("RIG_NET_PING_INTERVAL", 2)))
    resolver = Resolver()

    if args.once or args.flows:
        ct = Conntrack()
        counters = Counters(envint("RIG_NET_TOP_PEERS", 40))
        state, live = collect(counters, Groups(), ct)
        # One pass has no deltas by definition, so a second one follows it.
        time.sleep(min(2.0, args.interval))
        pinger.ping(pinger.targets[0], 1)
        state, live = collect(counters, Groups(), ct)
        state["ping"] = pinger.read()
        state["resolver"] = resolver.seconds
        state["resolver_failures"] = resolver.failures
        print(flow_report(live, state["ping"], ct) if args.flows else render(state), end="")
        return 0

    registry = Registry()
    Handler.registry = registry
    pinger.start()
    resolver.start()
    threading.Thread(target=loop, args=(registry, args.interval, pinger, resolver),
                     daemon=True).start()
    server = http.server.ThreadingHTTPServer((args.addr, args.port), Handler)
    print(f"net exporter on http://{args.addr}:{args.port}/metrics "
          f"every {args.interval:g}s, pinging {', '.join(pinger.targets)}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
