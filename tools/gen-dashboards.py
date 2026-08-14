#!/usr/bin/env python3
"""Generate the Grafana dashboards in grafana/dashboards/.

The dashboards are committed JSON, but they are written here, not by hand.
Panels repeat the same half-dozen shapes across four dashboards; editing raw
Grafana JSON drifts them apart within a week.

    tools/gen-dashboards.py         # rewrite grafana/dashboards/*.json
    tools/gen-dashboards.py --check # fail if the committed files are stale

Grafana reloads the folder every 30s, so a regenerate needs no restart.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

DS = {"type": "prometheus", "uid": "rig-prom"}
OUT = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"

# Colour steps shared by every saturation gauge, so "red" means the same thing
# on every dashboard.
def steps(*pairs):
    out = [{"color": "green", "value": None}]
    for colour, value in pairs:
        out.append({"color": colour, "value": value})
    return {"mode": "absolute", "steps": out}


class Layout:
    """Panels are placed left to right, wrapping at 24 grid columns."""

    def __init__(self):
        self.panels: list[dict] = []
        self.x = 0
        self.y = 0
        self.row_h = 0
        self._id = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def place(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x = 0
            self.y += self.row_h
            self.row_h = 0
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        panel["id"] = self.next_id()
        self.x += w
        self.row_h = max(self.row_h, h)
        self.panels.append(panel)
        return panel

    def row(self, title: str):
        self.x = 0
        self.y += self.row_h
        self.row_h = 0
        self.panels.append({
            "type": "row", "title": title, "collapsed": False,
            "id": self.next_id(), "panels": [],
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y},
        })
        self.y += 1


def targets(queries):
    out = []
    for i, q in enumerate(queries):
        expr, legend = q if isinstance(q, tuple) else (q, "")
        out.append({"refId": chr(65 + i), "expr": expr, "legendFormat": legend,
                    "datasource": DS, "editorMode": "code", "range": True})
    return out


def ts(L, title, queries, *, unit="short", w=12, h=8, stack=False, fill=0,
       desc="", min_=None, max_=None, thresholds=None, legend_calcs=("last", "max"),
       time_from=None, no_value=None):
    custom = {
        "drawStyle": "line", "lineWidth": 1, "fillOpacity": fill,
        "gradientMode": "opacity", "showPoints": "never",
        "spanNulls": True, "axisSoftMin": min_, "axisSoftMax": max_,
    }
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
        custom["fillOpacity"] = fill or 40
    if thresholds:
        custom["thresholdsStyle"] = {"mode": "dashed"}
    panel = {
        "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "targets": targets(queries),
        "fieldConfig": {
            "defaults": {
                "unit": unit, "custom": custom,
                "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "min": min_, "max": max_,
                **({"noValue": no_value} if no_value else {}),
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "right",
                       "calcs": list(legend_calcs), "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    if time_from:
        panel["timeFrom"] = time_from
    return L.place(panel, w, h)


def stat(L, title, expr, *, unit="short", w=3, h=4, desc="", thresholds=None,
         decimals=None, graph=True, text_size=32):
    return L.place({
        "type": "stat", "title": title, "description": desc, "datasource": DS,
        "targets": targets([expr]),
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "thresholds": thresholds or steps(),
            "color": {"mode": "thresholds"},
        }, "overrides": []},
        "options": {
            "graphMode": "area" if graph else "none",
            "colorMode": "value", "justifyMode": "auto",
            "textMode": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "text": {"valueSize": text_size},
        },
    }, w, h)


def table(L, title, queries, *, w=12, h=9, desc="", unit="short", sort_desc=True,
          rename=None, hide=("Time", "__name__", "instance", "job", "host")):
    excludes = {k: True for k in hide}
    organize = {"excludeByName": excludes, "renameByName": rename or {}, "indexByName": {}}
    return L.place({
        "type": "table", "title": title, "description": desc, "datasource": DS,
        "targets": [dict(t, format="table", instant=True) for t in targets(queries)],
        "transformations": [{"id": "organize", "options": organize}],
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": []},
        "options": {"showHeader": True,
                    "sortBy": [{"displayName": "Value", "desc": sort_desc}],
                    "footer": {"show": False}},
    }, w, h)


def dashboard(uid, title, description, L, *, refresh="15s", time_from="now-3h", tags=()):
    return {
        "uid": uid, "title": title, "description": description,
        "tags": ["rig", *tags], "timezone": "browser", "editable": True,
        "schemaVersion": 39, "version": 1, "refresh": refresh,
        "time": {"from": time_from, "to": "now"},
        "graphTooltip": 1,
        "panels": L.panels,
        "templating": {"list": []},
    }


# --------------------------------------------------------------------------
# 1. Overview — the dashboard you open when the machine feels wrong
# --------------------------------------------------------------------------

def overview():
    L = Layout()
    L.row("Verdict")
    stat(L, "Load per core", "rig:load:per_cpu", unit="none", decimals=1,
         desc="Load average divided by thread count. Above 1 means a queue exists somewhere.",
         thresholds=steps(("yellow", 1), ("orange", 2), ("red", 4)))
    stat(L, "Blocked on IO", "rig:load:blocked", unit="none",
         desc="Processes in uninterruptible sleep. These count toward load but use no CPU.",
         thresholds=steps(("yellow", 5), ("orange", 25), ("red", 100)))
    stat(L, "Machine stalled on IO", "rig:psi:io_full", unit="percentunit",
         desc="Fraction of time NO task could make progress because of IO. The single most honest number here.",
         thresholds=steps(("yellow", 0.1), ("orange", 0.3), ("red", 0.6)))
    stat(L, "CPU busy", "rig:cpu:busy_ratio", unit="percentunit",
         thresholds=steps(("yellow", 0.7), ("orange", 0.9)))
    stat(L, "Memory used", "rig:mem:used_ratio", unit="percentunit",
         thresholds=steps(("yellow", 0.8), ("orange", 0.9), ("red", 0.95)))
    stat(L, "Swap traffic", "rig:mem:swap_pages_per_sec", unit="none",
         desc="Pages per second moving in and out of swap. Sustained thousands is a thrash.",
         thresholds=steps(("yellow", 500), ("orange", 2000), ("red", 10000)))
    stat(L, "CPU temp", "rig:cpu_celsius", unit="celsius",
         thresholds=steps(("yellow", 75), ("orange", 85), ("red", 92)))
    stat(L, "GPU temp", "rig:gpu_celsius", unit="celsius",
         thresholds=steps(("yellow", 75), ("orange", 83), ("red", 87)))

    L.row("Is it CPU, memory, or disk?")
    ts(L, "Load average, split", [
        ("rig:load:runnable", "runnable (wants CPU)"),
        ("rig:load:blocked", "blocked (waiting on IO)"),
    ], stack=True, unit="none", desc="Linux load counts both. If the blue band dominates, the CPU is innocent.")
    ts(L, "Pressure stall", [
        ("rig:psi:cpu_some", "cpu — some task waiting"),
        ("rig:psi:io_some", "io — some task waiting"),
        ("rig:psi:io_full", "io — EVERY task waiting"),
        ("rig:psi:memory_full", "memory — EVERY task waiting"),
    ], unit="percentunit", max_=1,
       desc="`full` lines are whole-machine stalls. Anything above 0.3 sustained is a stop, not a slowdown.")
    ts(L, "CPU by mode", [
        ("rig:cpu:user_ratio", "user"),
        ("rig:cpu:system_ratio", "system"),
        ("rig:cpu:iowait_ratio", "iowait"),
    ], unit="percentunit", stack=True, max_=1)
    ts(L, "Memory and swap", [
        ("rig:mem:used_ratio", "memory used"),
        ("rig:mem:swap_used_ratio", "swap used"),
    ], unit="percentunit", max_=1)

    L.row("Who")
    ts(L, "CPU by process group", [("topk(8, rig:proc:cpu_cores)", "{{groupname}}")],
       unit="none", stack=True, desc="Cores held, not percent. 4.0 means four cores solid.")
    ts(L, "Blocked on IO by process group", [("topk(8, rig:proc:blocked)", "{{groupname}}")],
       unit="none", stack=True, desc="The load average, attributed. This is the 'who' answer.")
    ts(L, "Memory by process group", [("topk(8, rig:proc:rss_proportional_bytes)", "{{groupname}}")],
       unit="bytes", stack=True, desc="Proportional set size: pages shared between forks are divided, not double counted.")
    ts(L, "Paging from disk by process group", [("topk(8, rig:proc:major_faults_per_sec)", "{{groupname}}")],
       unit="none", stack=True, desc="Major faults per second. Under swap pressure this names who is paying for it.")

    L.row("Machine")
    ts(L, "Disk busy", [("rig:disk:util_ratio", "{{device}}")], unit="percentunit", max_=1, w=8)
    ts(L, "Temperatures", [("rig:temp_celsius", "{{chip_name}} {{label}}")], unit="celsius", w=8)
    table(L, "Firing now", [('ALERTS{alertstate="firing"}', "")], w=8,
          hide=("Time", "__name__", "instance", "job", "host", "alertstate", "Value"),
          desc="Empty is the good state. Each alert carries a `diagnose` annotation in prometheus/rules/40-alerts.yml.")

    return dashboard("rig-overview", "Rig — Overview",
                     "Start here. Says whether the bottleneck is CPU, memory or disk, then names the process group responsible.",
                     L, tags=["overview"])


# --------------------------------------------------------------------------
# 2. Who — attribution only
# --------------------------------------------------------------------------

def who():
    L = Layout()
    L.row("Right now")
    table(L, "Top CPU (cores)", [("topk(15, rig:proc:cpu_cores)", "")], w=8, unit="none",
          rename={"groupname": "group"})
    table(L, "Top memory (proportional)", [("topk(15, rig:proc:rss_proportional_bytes)", "")], w=8, unit="bytes",
          rename={"groupname": "group"})
    table(L, "Top blocked on IO", [("topk(15, rig:proc:blocked)", "")], w=8, unit="none",
          rename={"groupname": "group"})

    L.row("Over time")
    for title, expr, unit, desc in [
        ("CPU (cores held)", "rig:proc:cpu_cores", "none", ""),
        ("Blocked on IO (processes)", "rig:proc:blocked", "none",
         "Uninterruptible sleep. Sums to the load average."),
        ("Resident memory (proportional)", "rig:proc:rss_proportional_bytes", "bytes", ""),
        ("Swapped out", "rig:proc:swap_bytes", "bytes",
         "Memory this group has been pushed out to swap. Large here means it will thrash when it runs again."),
        ("Disk read", "rig:proc:read_bytes_per_sec", "Bps", ""),
        ("Disk write", "rig:proc:write_bytes_per_sec", "Bps", ""),
        ("Major faults (paging from disk)", "rig:proc:major_faults_per_sec", "none", ""),
        ("Process count", "rig:proc:count", "none",
         "A build with unbounded parallelism shows up here first."),
        ("Thread count", "rig:proc:threads", "none", ""),
        ("Context switches", "rig:proc:context_switches_per_sec", "none", ""),
    ]:
        ts(L, title, [(f"topk(10, {expr})", "{{groupname}}")], unit=unit, stack=True, desc=desc)

    L.row("Containers")
    ts(L, "Container CPU", [("topk(10, rig:container:cpu_cores)", "{{name}}")], unit="none", stack=True)
    ts(L, "Container memory", [("topk(10, rig:container:rss_bytes)", "{{name}}")], unit="bytes", stack=True)

    return dashboard("rig-who", "Rig — Who",
                     "Every resource, attributed to a named process group. Groups are defined in process-exporter/config.yml.",
                     L, tags=["attribution"])


# --------------------------------------------------------------------------
# 3. Thermals — including the long-baseline dust detector
# --------------------------------------------------------------------------

def thermals():
    L = Layout()
    L.row("Now")
    stat(L, "CPU", "rig:cpu_celsius", unit="celsius",
         thresholds=steps(("yellow", 75), ("orange", 85), ("red", 92)))
    stat(L, "GPU", "rig:gpu_celsius", unit="celsius",
         thresholds=steps(("yellow", 75), ("orange", 83), ("red", 87)))
    stat(L, "Case air", "rig:ambient_celsius", unit="celsius",
         desc="Motherboard sensor. Every figure below is measured against this, so a hot room does not read as a dirty cooler.",
         thresholds=steps(("yellow", 40), ("orange", 45)))
    stat(L, "GPU throttle headroom", "rig:thermal:gpu_headroom_c", unit="celsius",
         desc="Degrees remaining before the card throttles itself. Small is bad.",
         thresholds={"mode": "absolute", "steps": [
             {"color": "red", "value": None}, {"color": "orange", "value": 5},
             {"color": "yellow", "value": 10}, {"color": "green", "value": 15}]})
    stat(L, "Pump / loop fan", "rig:thermal:pump_rpm", unit="rotrpm",
         thresholds={"mode": "absolute", "steps": [
             {"color": "red", "value": None}, {"color": "orange", "value": 300},
             {"color": "green", "value": 400}]})
    stat(L, "GPU power", "rig:gpu_watts", unit="watt")
    stat(L, "GPU fan", "rig:thermal:gpu_fan_ratio", unit="percentunit")
    stat(L, "Loop delta", "rig:thermal:coolant_delta_c", unit="celsius", decimals=1,
         desc="Temperature across the CPU block. Widens as flow drops.")

    L.row("Does it need cleaning?")
    ts(L, "Cooling efficiency vs. one month ago", [
        ("rig:thermal:gpu_degradation_ratio", "GPU cooler"),
        ("rig:thermal:radiator_degradation_ratio", "radiator"),
        ("rig:thermal:cpu_degradation_ratio", "CPU path overall"),
        ("rig:thermal:mount_degradation_ratio", "cold plate / paste"),
    ], unit="none", w=12, min_=0.8, max_=1.5, time_from="90d",
       no_value="No baseline yet — needs 37 days of history (7-day window vs. the same week 30 days earlier).",
       desc=("1.0 = same cooling as a month ago. 1.15+ on the GPU or radiator line is dust: the same watts "
             "now need more degrees to move. The cold plate line separates dried paste from a dirty radiator. "
             "This panel always shows 90 days regardless of the dashboard range."))
    ts(L, "Heat path, split", [
        ("rig:thermal:die_to_coolant_c", "die -> coolant (mount + paste)"),
        ("rig:thermal:coolant_rise_c", "coolant -> case air (radiator)"),
    ], unit="celsius", w=12, stack=True,
       desc="Where the temperature is actually lost. A growing bottom band is the radiator; a growing top band is the contact.")

    L.row("Detail")
    ts(L, "All sensors", [("rig:temp_celsius", "{{chip_name}} / {{label}}")], unit="celsius", w=24, h=10)
    ts(L, "GPU: temperature vs. power", [
        ("rig:gpu_celsius", "temp (C)"),
        ("rig:gpu_watts", "power (W)"),
        ("rig:thermal:gpu_fan_ratio * 100", "fan (%)"),
    ], unit="none", w=12,
       desc="Read these together. Rising temperature at equal power and equal fan is the cooler getting worse.")
    ts(L, "GPU thermal resistance", [
        ("rig:thermal:gpu_resistance_c_per_w", "instant"),
        ("rig:thermal:gpu_resistance_c_per_w:avg1h", "1h average"),
        ("rig:thermal:gpu_resistance_c_per_w:avg7d", "7d average"),
    ], unit="none", w=12, time_from="30d",
       no_value="No sample yet — the GPU must draw at least 60 W for this to mean anything.",
       desc=("Degrees above case air per watt dissipated. A hardware property, so it is comparable across "
             "months. Only sampled above 60 W, so an idle GPU leaves this empty. Always shows 30 days."))
    ts(L, "Fans and pump", [("rig:fan_rpm", "{{chip_name}} {{label}}")], unit="rotrpm", w=12)
    ts(L, "Coolant", [
        ("rig:coolant_in_celsius", "loop in"),
        ("rig:coolant_out_celsius", "loop out"),
        ("rig:ambient_celsius", "case air"),
    ], unit="celsius", w=12)

    return dashboard("rig-thermals", "Rig — Thermals",
                     "Temperatures, and the month-over-month cooling efficiency trend that tells you when the machine needs cleaning.",
                     L, time_from="now-6h", refresh="30s", tags=["thermal"])


# --------------------------------------------------------------------------
# 4. Storage
# --------------------------------------------------------------------------

def storage():
    L = Layout()
    L.row("Now")
    stat(L, "Busiest disk", "max(rig:disk:util_ratio)", unit="percentunit",
         thresholds=steps(("yellow", 0.6), ("orange", 0.85), ("red", 0.95)))
    stat(L, "Worst write latency", "max(rig:disk:write_await_seconds)", unit="s",
         desc="Discards excluded — btrfs trims are large and asynchronous, and drag a blended figure up on a healthy drive.",
         thresholds=steps(("yellow", 0.01), ("orange", 0.05), ("red", 0.2)))
    stat(L, "Fullest filesystem", "max(rig:fs:used_ratio)", unit="percentunit",
         thresholds=steps(("yellow", 0.8), ("orange", 0.9), ("red", 0.95)))
    stat(L, "Worst drive wear", "max(smartctl_device_percentage_used) / 100", unit="percentunit",
         desc="Fraction of rated write endurance consumed.",
         thresholds=steps(("yellow", 0.7), ("orange", 0.85), ("red", 0.95)))
    stat(L, "Swap in use", "rig:mem:swap_used_bytes", unit="bytes", w=6)
    stat(L, "Swap traffic", "rig:mem:swap_pages_per_sec", unit="none", w=6,
         thresholds=steps(("yellow", 500), ("orange", 2000), ("red", 10000)))

    L.row("IO")
    ts(L, "Disk busy", [("rig:disk:util_ratio", "{{device}}")], unit="percentunit", max_=1)
    ts(L, "Latency by operation", [
        ("rig:disk:read_await_seconds", "{{device}} read"),
        ("rig:disk:write_await_seconds", "{{device}} write"),
        ("rig:disk:discard_await_seconds", "{{device}} trim"),
    ], unit="s",
       desc=("Split the way iostat reports it. A blended figure hides a drive with fast reads and slow writes. "
             "btrfs issues very large asynchronous discards, so the trim line sits seconds high on a healthy drive — "
             "read and write are the ones to judge."))
    ts(L, "Read throughput", [("rig:disk:read_bytes_per_sec", "{{device}}")], unit="Bps")
    ts(L, "Write throughput", [("rig:disk:write_bytes_per_sec", "{{device}}")], unit="Bps")
    ts(L, "Swap traffic", [("rig:mem:swap_pages_per_sec", "pages/s")], unit="none",
       desc="Disk bandwidth the machine is spending on its own memory instead of on work.")
    ts(L, "IO by process group", [("topk(10, rig:proc:io_bytes_per_sec)", "{{groupname}}")],
       unit="Bps", stack=True)

    L.row("Capacity and health")
    ts(L, "Filesystem used", [("max by (device) (rig:fs:used_ratio)", "{{device}}")],
       unit="percentunit", max_=1, w=12,
       desc="One line per device. Subvolumes and bind mounts of the same device are collapsed.")
    ts(L, "Free space", [("max by (device) (rig:fs:avail_bytes)", "{{device}}")], unit="bytes", w=12)
    table(L, "SMART", [
        ("smartctl_device_percentage_used", ""),
    ], w=8, unit="percent", rename={"Value": "wear %"})
    ts(L, "Drive temperature", [("smartctl_device_temperature", "{{device}}")], unit="celsius", w=8)
    ts(L, "Media errors (cumulative)", [("smartctl_device_media_errors", "{{device}}")], unit="none", w=8,
       desc="Should be a flat line. Any slope is a failing drive.")

    return dashboard("rig-storage", "Rig — Storage",
                     "Disk throughput, queue latency, filesystem capacity and SMART endurance.",
                     L, tags=["storage"])


# --------------------------------------------------------------------------

BOARDS = {
    "rig-overview.json": overview,
    "rig-who.json": who,
    "rig-thermals.json": thermals,
    "rig-storage.json": storage,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed JSON is out of date")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, build in BOARDS.items():
        text = json.dumps(build(), indent=2, sort_keys=False) + "\n"
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
            print(f"wrote {path.relative_to(OUT.parent.parent)}")

    if args.check:
        if stale:
            print("stale dashboards (run tools/gen-dashboards.py): " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"{len(BOARDS)} dashboards up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
