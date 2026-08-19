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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness_quota as hq  # noqa: E402
import harness_usage as hu  # noqa: E402

DS = {"type": "prometheus", "uid": "rig-prom"}
OUT = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"

# Colour steps shared by every saturation gauge, so "red" means the same thing
# on every dashboard.
def steps(*pairs, base="green"):
    # `base="red"` for a figure whose healthy end is the high one — headroom,
    # pump speed, cache hit share — so the pairs climb towards green instead.
    out = [{"color": base, "value": None}]
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
       time_from=None, no_value=None, min_interval=None):
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
    if min_interval:
        panel["interval"] = min_interval
    return L.place(panel, w, h)


def stat(L, title, expr, *, unit="short", w=3, h=4, desc="", thresholds=None,
         decimals=None, graph=True, text_size=32, no_value=None):
    return L.place({
        "type": "stat", "title": title, "description": desc, "datasource": DS,
        "targets": targets([expr]),
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "thresholds": thresholds or steps(),
            "color": {"mode": "thresholds"},
            **({"noValue": no_value} if no_value else {}),
        }, "overrides": []},
        "options": {
            "graphMode": "area" if graph else "none",
            "colorMode": "value", "justifyMode": "auto",
            "textMode": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "text": {"valueSize": text_size},
        },
    }, w, h)


def column(name, *, matcher="byName", **defaults):
    """One field's own unit, thresholds or style, by its displayed name.

    `matcher="byRegexp"` styles every series whose legend matches instead.
    """
    return {"matcher": {"id": matcher, "options": name},
            "properties": [{"id": k.rstrip("_").replace("_", "."), "value": v}
                           for k, v in defaults.items()]}


def table(L, title, queries, *, w=12, h=9, desc="", unit="short", sort_desc=True,
          rename=None, hide=("Time", "__name__", "instance", "job", "host"),
          merge=False, order=None, columns=(), sort_by="Value", no_value=None):
    excludes = {k: True for k in hide}
    # Every key here is the field's name *before* the rename, `indexByName`
    # included — a column ordered by the name it ends up showing is ignored.
    organize = {"excludeByName": excludes, "renameByName": rename or {},
                "indexByName": {name: i for i, name in enumerate(order or [])}}
    # One query per fact, joined on the labels they share: every column of a
    # row comes from its own series, and a missing one leaves a blank rather
    # than dropping the row.
    steps_ = [{"id": "merge", "options": {}}] if merge else []
    return L.place({
        "type": "table", "title": title, "description": desc, "datasource": DS,
        "targets": [dict(t, format="table", instant=True, range=False) for t in targets(queries)],
        "transformations": steps_ + [{"id": "organize", "options": organize}],
        "fieldConfig": {"defaults": {"unit": unit,
                                     "custom": {"align": "auto", "cellOptions": {"type": "auto"}},
                                     **({"noValue": no_value} if no_value else {})},
                        "overrides": list(columns)},
        "options": {"showHeader": True,
                    "sortBy": [{"displayName": sort_by, "desc": sort_desc}],
                    "footer": {"show": False}},
    }, w, h)


def bars(L, title, expr, *, unit="short", w=8, h=9, desc="", decimals=None, no_value=None,
         hide=("Time", "__name__", "instance", "job", "host")):
    """A ranked list. Reads better than a pie for "who spent the most"."""
    # One frame per series comes back in label order, and a bar gauge draws
    # frames in the order given — so ranking needs a table and a sort, not topk.
    organize = {"excludeByName": {k: True for k in hide}, "renameByName": {}, "indexByName": {}}
    return L.place({
        "type": "bargauge", "title": title, "description": desc, "datasource": DS,
        "targets": [dict(t, format="table", instant=True, range=False)
                    for t in targets([(expr, "")])],
        "transformations": [
            {"id": "organize", "options": organize},
            {"id": "sortBy", "options": {"fields": {}, "sort": [{"field": "Value", "desc": True}]}},
        ],
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "color": {"mode": "continuous-BlPu"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
            **({"noValue": no_value} if no_value else {}),
        }, "overrides": []},
        "options": {"displayMode": "gradient", "orientation": "horizontal",
                    "showUnfilled": True, "valueMode": "color",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}},
    }, w, h)


def filter_var(name, label, metric, *, within=()):
    """A dropdown that narrows every panel to one label's values.

    Multi-select with an `All` that is the regex `.*`, so a panel writes
    `label=~"$name"` once and it holds whether one value, several, or all are
    picked. `within` chains a variable to the ones above it, so choosing a
    provider shortens the model list rather than offering models it never sold.
    """
    scope = ",".join(f'{k}=~"${k}"' for k in within)
    query = f"label_values({metric}{{{scope}}},{name})"
    return {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "definition": query, "query": {"qryType": 1, "query": query,
                                       "refId": "PrometheusVariableQueryEditor-VariableQuery"},
        "current": {"text": ["All"], "value": ["$__all"]},
        "includeAll": True, "allValue": ".*", "multi": True,
        "options": [], "refresh": 1, "regex": "", "sort": 1, "hide": 0,
    }


BUILT_IN_ANNOTATION = {
    "builtIn": 1, "name": "Annotations & Alerts", "type": "dashboard",
    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
    "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
}


def peak_windows() -> list[tuple[tuple[str, ...], tuple[str, ...], tuple[int, ...], float]]:
    """Each distinct clock, with the sellers and models billed on it.

    The clock is stated once, in share/prices.tsv. A marker drawn from a second
    copy of it is a marker that will one day disagree with the bill.

    Keyed by the clock rather than by the seller: OpenRouter passes DeepSeek's
    own peak hours through unchanged, so keying by seller drew one band twice
    under two names, neither of which said which model had repriced.
    """
    out: dict[tuple, tuple[set, set]] = {}
    for key, lines in hu.load_overrides().items():
        for value, _, _, name in lines:
            try:
                _, clause = hu.split_clauses(value)
            except ValueError:
                continue
            if not clause["at"]:
                continue
            model = name
            signature = (tuple(sorted(set(clause["at"]))), clause["from"] or 0.0)
            sellers, models = out.get(signature, (set(), set()))
            out[signature] = (sellers | {clause["on"] or model}, models | {model})
    return [(tuple(sorted(sellers)), tuple(sorted(models)), hours, start)
            for (hours, start), (sellers, models) in sorted(out.items())]


def hour_spans(hours) -> list[list[int]]:
    """Contiguous hours collapsed to half-open [start, end) spans."""
    spans: list[list[int]] = []
    for hour in hours:
        if spans and hour == spans[-1][1]:
            spans[-1][1] = hour + 1
        else:
            spans.append([hour, hour + 1])
    return spans


def _annotation(name, expr, colour, step, text="") -> dict:
    # useValueForTime must stay a JSON boolean: Grafana tests it for truth, and
    # the string "false" would send every marker to 1970.
    return {
        "name": name, "datasource": DS, "enable": True, "hide": False,
        "iconColor": colour, "expr": expr, "step": step,
        "target": {"expr": expr, "refId": "peak", "interval": step, "legendFormat": ""},
        "titleFormat": name, "textFormat": text, "tagKeys": "",
        "useValueForTime": False,
    }


def peak_annotations(sellers, models, hours, start) -> list[dict]:
    """A band over the hours this clock charges peak in, and a line at each flip.

    Both are built from hour(), not from a recording rule: a rule would only
    mark the hours since it was added, and the question is always about a week
    that has already been paid for. Both are toggleable at the top of the board.

    The title names who bills on the clock, and the hover text names what — a
    seller sells many models and only some of them move.
    """
    who = " + ".join(sellers)
    what = ", ".join(models)
    spans = hour_spans(hours)
    began = f"(vector(time()) >= {int(start)})"
    inside = " or ".join(f"(hour() >= {a} and hour() < {b})" for a, b in spans)

    # A line needs exactly one sample; Grafana joins any two under `step` apart
    # into a region. Every expression yields 1, never hour(), because a sample
    # worth 0 — midnight, in a window that reached it — is dropped as inactive.
    flips = sorted({s % 24 for span in spans for s in span})
    at_flip = " or ".join(f"hour() == {h}" for h in flips)
    return [
        _annotation(f"{who} peak hours", f"vector(1) and (({inside}) and {began})",
                    "rgba(255, 152, 0, 0.20)", "300s", text=what),
        _annotation(f"{who} rate change",
                    f"vector(1) and ((({at_flip}) and minute() < 15) and {began})",
                    "rgb(255, 152, 0)", "900s", text=what),
    ]


def dashboard(uid, title, description, L, *, refresh="15s", time_from="now-3h", tags=(),
              variables=(), annotations=()):
    return {
        "uid": uid, "title": title, "description": description,
        "tags": ["rig", *tags], "timezone": "browser", "editable": True,
        "schemaVersion": 39, "version": 1, "refresh": refresh,
        "time": {"from": time_from, "to": "now"},
        "graphTooltip": 1,
        "panels": L.panels,
        "templating": {"list": list(variables)},
        "annotations": {"list": [BUILT_IN_ANNOTATION, *annotations]},
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
    stat(L, "GPU busy", "rig:gpu:busy_ratio", unit="percentunit", w=6,
         desc="Shader time share. Depth is on the Compute dashboard.")
    stat(L, "VRAM used", "rig:gpu:vram_used_ratio", unit="percentunit", w=6,
         desc="Nothing kills a GPU client politely — there is no OOM killer for VRAM. "
              "The card starts refusing allocations near 0.85, not at 1.",
         thresholds=steps(("yellow", 0.7), ("orange", 0.85), ("red", 0.95)))
    stat(L, "VRAM free", "rig:gpu:vram_free_bytes", unit="bytes", w=6,
         desc="What is left for the next model, game or compositor buffer.")
    stat(L, "GPU power", "rig:gpu_watts", unit="watt", w=6)
    stat(L, "Downlink", "rig:net:link:rx_bits_per_sec", unit="bps", w=6,
         desc="The uplink interface alone. The Network dashboard splits it by process group.")
    stat(L, "Uplink", "rig:net:link:tx_bits_per_sec", unit="bps", w=6)
    stat(L, "Link queue", 'max(rig:net:bufferbloat_ratio{kind="internet"})', unit="none", w=6,
         decimals=1,
         desc="Round trip over its own idle value. Above 4, interactive traffic is waiting "
              "behind a bulk transfer and no download speed cap will fix it.",
         thresholds=steps(("yellow", 2), ("orange", 4), ("red", 10)))
    stat(L, "Round trip", 'max(rig:net:rtt_seconds{kind="internet"})', unit="s", w=6,
         thresholds=steps(("yellow", 0.06), ("orange", 0.15), ("red", 0.4)))

    L.row("Is it CPU, GPU, memory, or disk?")
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
    ts(L, "CPU against GPU", [
        ("rig:cpu:busy_ratio", "CPU, all threads"),
        ("rig:cpu:hottest_core_ratio", "CPU, busiest single thread"),
        ("rig:gpu:busy_ratio", "GPU shaders"),
    ], unit="percentunit", max_=1,
       desc="A pegged single thread under an idle GPU is a GPU waiting on the CPU, and neither "
            "figure alone says so. The Compute dashboard breaks both down.")
    ts(L, "RAM against VRAM", [
        ("rig:mem:used_ratio", "system RAM"),
        ("rig:gpu:vram_used_ratio", "VRAM"),
    ], unit="percentunit", max_=1,
       desc="RAM overcommits into swap and gets slow. VRAM does not overcommit: it refuses, and "
            "the client that asked dies.")

    L.row("Who")
    ts(L, "CPU by process group", [("topk(8, rig:proc:cpu_cores)", "{{groupname}}")],
       unit="none", stack=True, desc="Cores held, not percent. 4.0 means four cores solid.")
    ts(L, "Blocked on IO by process group", [("topk(8, rig:proc:blocked)", "{{groupname}}")],
       unit="none", stack=True, desc="The load average, attributed. This is the 'who' answer.")
    ts(L, "Memory by process group", [("topk(8, rig:proc:rss_proportional_bytes)", "{{groupname}}")],
       unit="bytes", stack=True, desc="Proportional set size: pages shared between forks are divided, not double counted.")
    ts(L, "Paging from disk by process group", [("topk(8, rig:proc:major_faults_per_sec)", "{{groupname}}")],
       unit="none", stack=True, desc="Major faults per second. Under swap pressure this names who is paying for it.")
    ts(L, "Internet by process group",
       [("topk(8, rig:net:proc:uplink_bytes_per_sec)", "{{groupname}}")],
       unit="Bps", stack=True,
       desc="Bytes to and from the internet. Traffic to container bridges, VMs and the "
            "tailnet is excluded — none of it touches the line.")
    ts(L, "Round trip against idle", [
        ("rig:net:rtt_seconds", "{{target}} ({{kind}}) now"),
        ("rig:net:rtt_floor_seconds", "{{target}} idle"),
    ], unit="s",
       desc="The two lines apart is a queue outside this machine. That is what a saturated "
            "link feels like from inside it, and no CPU or disk panel will show it.")

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
         thresholds=steps(("orange", 5), ("yellow", 10), ("green", 15), base="red"))
    stat(L, "Pump / loop fan", "rig:thermal:pump_rpm", unit="rotrpm",
         thresholds=steps(("orange", 300), ("green", 400), base="red"))
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
    stat(L, "Worst drive wear", "max(rig:drive:wear_ratio)", unit="percentunit",
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
        ("rig:drive:wear_ratio", ""),
    ], w=8, unit="percentunit", rename={"Value": "wear"})
    ts(L, "Drive temperature", [("rig:drive:temp_celsius", "{{model_name}} {{serial_number}}")],
       unit="celsius", w=8)
    ts(L, "Media errors (cumulative)",
       [("rig:drive:media_errors", "{{model_name}} {{serial_number}}")], unit="none", w=8,
       desc=("Should be a flat line. Any slope is a failing drive. Keyed by serial — smartctl's "
             "`device` is a scan index and moves between drives."))

    return dashboard("rig-storage", "Rig — Storage",
                     "Disk throughput, queue latency, filesystem capacity and SMART endurance.",
                     L, tags=["storage"])


# --------------------------------------------------------------------------
# 5. Network — who is on the link, and why a capped download still lags
# --------------------------------------------------------------------------

QUEUE = ("A link is never slow, it is full. When it is full the bytes still arrive at the same "
         "rate and every interactive packet waits behind them, so the honest measure of "
         "'the internet is laggy' is round-trip time against its own idle value, not throughput.")


def network():
    L = Layout()
    L.row("Verdict")
    stat(L, "Down", "rig:net:link:rx_bits_per_sec", unit="bps", w=3,
         desc="The default-route interface alone. Container bridges, VMs and the tailnet "
              "carry bytes that never reach the ISP.")
    stat(L, "Up", "rig:net:link:tx_bits_per_sec", unit="bps", w=3)
    stat(L, "Of the line", "rig:net:link:rx_saturation", unit="percentunit", w=3,
         no_value="No line speed set. RIG_NET_DOWN_MBIT in .env, then this reads against "
                  "what you pay for instead of against the busiest minute so far.",
         desc="Downlink against RIG_NET_DOWN_MBIT.",
         thresholds=steps(("yellow", 0.6), ("orange", 0.85), ("red", 0.95)))
    stat(L, "Of its own best", "rig:net:link:rx_share_of_peak", unit="percentunit", w=3,
         desc="Against the fastest this line has been seen to go in 7 days. The stand-in "
              "while nobody has typed in what it was sold as.",
         thresholds=steps(("yellow", 0.6), ("orange", 0.85)))
    stat(L, "Queue", 'max(rig:net:bufferbloat_ratio{kind="internet"})', unit="none", w=3,
         decimals=1, desc=QUEUE + " 1.0 is an idle path. Above 4 the lag is real and it is "
                                  "not the download rate that caused it.",
         thresholds=steps(("yellow", 2), ("orange", 4), ("red", 10)))
    stat(L, "Round trip", 'max(rig:net:rtt_seconds{kind="internet"})', unit="s", w=3,
         thresholds=steps(("yellow", 0.06), ("orange", 0.15), ("red", 0.4)))
    stat(L, "Loss", "max(rig:net:loss_ratio)", unit="percentunit", w=3,
         desc="Echoes that never came back. To the gateway it is the radio or the cable; "
              "only past it is it the ISP.",
         thresholds=steps(("yellow", 0.01), ("orange", 0.05)))
    stat(L, "Named owner", "rig:net:attributed_ratio", unit="percentunit", w=3,
         desc="Share of the link's bytes this stack can attribute to a process group. "
              "The rest is UDP without conntrack accounting, and connections too short to "
              "be sampled. `rig net doctor` says which.",
         thresholds=steps(("orange", 0.5), ("yellow", 0.8), ("green", 0.9), base="red"))

    L.row("Who is using it")
    ts(L, "Down, by process group",
       [("topk(8, rig:net:proc:uplink_rx_bytes_per_sec)", "{{groupname}}")],
       unit="Bps", stack=True, w=12,
       desc="Internet only, and named by the same groups as every other dashboard. "
            "Defined in process-exporter/config.yml.")
    ts(L, "Up, by process group",
       [("topk(8, rig:net:proc:uplink_tx_bytes_per_sec)", "{{groupname}}")],
       unit="Bps", stack=True, w=12,
       desc="The uplink is usually the smaller pipe, and filling it queues the "
            "acknowledgements every download depends on. A backup or a sync client here "
            "slows a machine that is downloading nothing.")
    ts(L, "By container", [
        ("topk(8, rig:net:container:rx_bytes_per_sec)", "{{name}} down"),
        ("topk(8, rig:net:container:tx_bytes_per_sec)", "{{name}} up"),
    ], unit="Bps", w=12,
       no_value="No container has a network namespace of its own — every one of them is on "
                "the host's stack, and its traffic is already named by process above.",
       desc="A container with its own network namespace keeps its sockets there, where the "
            "socket reader cannot see them, so these come from cAdvisor instead. Containers "
            "on the host's network are excluded: their traffic is the host's and is already "
            "counted by process.")
    ts(L, "Link against what has an owner", [
        ("rate(rignet_link_bytes_total[5m])", "the interface counted"),
        ("rate(rignet_attributed_bytes_total[5m])", "attributed to a group"),
    ], unit="Bps", w=12,
       desc="The gap is UDP with no conntrack accounting plus connections that opened and "
            "closed between two samples. Measured, not assumed.")
    bars(L, "By compose stack", "topk(8, rig:net:stack:bytes_per_sec)", unit="Bps", w=12,
         no_value="No compose stack has its own network namespace.",
         desc="One `docker compose up` is one unit of intent, and a download client usually "
              "sits inside one.")
    bars(L, "Total by process group",
         'topk(12, sum by (groupname) (rignet_proc_received_bytes_total{scope="internet"})'
         ' + sum by (groupname) (rignet_proc_sent_bytes_total{scope="internet"}))',
         unit="bytes", w=12, decimals=0,
         desc="Everything recorded, per group. A cumulative counter, so this is the whole "
              "history the exporter has watched.")

    L.row("Is it queued?")
    ts(L, "Round trip time against its own idle value", [
        ("rig:net:rtt_seconds", "{{target}} ({{kind}}) now"),
        ("rig:net:rtt_floor_seconds", "{{target}} idle"),
    ], unit="s", w=12,
       desc=QUEUE + " The floor line is the best this path has answered in since the exporter "
                    "started. Distance between the two lines is somebody else's queue.")
    ts(L, "Queue factor", [("rig:net:bufferbloat_ratio", "{{target}} ({{kind}})")],
       unit="none", w=12, min_=0, thresholds=steps(("orange", 4)),
       desc="Round trip over idle round trip. This is the panel to open when a download is "
            "capped and the machine still feels slow — the cap limits the rate, not the "
            "queue the router builds.")
    ts(L, "Loss and retransmits", [
        ("rig:net:loss_ratio", "{{target}} icmp loss"),
        ("rig:net:retransmit_ratio", "tcp segments sent again"),
    ], unit="percentunit", w=12,
       desc="TCP retransmits rise before ICMP loss does: a full queue drops the bulk flow's "
            "packets first, which is exactly the traffic nobody notices.")
    ts(L, "Worst round trip per process group",
       [("topk(8, rig:net:proc:rtt_seconds)", "{{groupname}}")], unit="s", w=12,
       desc="Taken from each group's own established connections, so a game and a download "
            "on the same link can be told apart. A game climbing here is the complaint, "
            "whatever the download is doing.")

    L.row("Where it goes")
    bars(L, "Down, by remote address",
         'topk(12, rig:net:peer:rx_bytes_per_sec{scope="internet"})', unit="Bps", w=8,
         desc="Addresses beyond the busiest fold into `other`. `rig net peers` resolves names.")
    bars(L, "Up, by remote address",
         'topk(12, rig:net:peer:tx_bytes_per_sec{scope="internet"})', unit="Bps", w=8)
    bars(L, "By service", "topk(10, rig:net:service_bytes_per_sec)", unit="Bps", w=8,
         desc="Named from the remote port. A number instead of a name means a port nobody "
              "has agreed on, which is normal for peer-to-peer and for games.")
    ts(L, "Traffic by service", [("topk(8, rig:net:service_bytes_per_sec)", "{{service}}")],
       unit="Bps", stack=True, w=12)
    table(L, "Busiest peers now",
          [('topk(15, rig:net:peer:bytes_per_sec{scope="internet"})', "")],
          w=12, unit="Bps", rename={"Value": "bytes/s"},
          desc="Instant rate to each address. `rig net conns` has the per-connection detail, "
               "which is deliberately never a metric — one series per socket would cost more "
               "than the rest of this stack together.")

    L.row("The link itself")
    ts(L, "Every interface, down", [("rig:net:rx_bytes_per_sec", "{{device}}")], unit="Bps", w=12,
       desc="Bridges and virtual interfaces included. Docker gives one bridge per compose "
            "project, so a busy line here that is not the default route never left the machine.")
    ts(L, "Every interface, up", [("rig:net:tx_bytes_per_sec", "{{device}}")], unit="Bps", w=12)
    ts(L, "Wifi signal", [("rig:net:wifi_signal_dbm", "{{device}}")], unit="dBm", w=8,
       no_value="No wireless interface — this machine is on a cable.",
       desc="Above -60 dBm is strong, below -72 the radio steps down to slow rates and the "
            "link is the bottleneck before any queue is.")
    ts(L, "Wifi retries", [("rig:net:wifi_retries_per_sec", "{{device}}")], unit="none", w=8,
       no_value="No wireless interface.",
       desc="Frames the radio had to send again. Airtime spent on nothing, and it rises long "
            "before throughput falls.")
    ts(L, "Errors and drops", [
        ("rig:net:errors_per_sec", "{{device}} errors"),
        ("rig:net:drops_per_sec", "{{device}} drops"),
    ], unit="none", w=8,
       desc="Drops on a virtual interface are ordinary. Drops on the default route are not.")
    ts(L, "Established connections", [
        ("rig:net:tcp_established", "machine total"),
        ("topk(6, rig:net:proc:connections)", "{{groupname}}"),
    ], unit="none", w=12,
       desc="A group holding hundreds of connections is either a peer-to-peer client or a "
            "download accelerator, and both defeat any single-connection rate cap.")
    ts(L, "Name resolution", [("rig:net:resolver_seconds", "system resolver")], unit="s", w=12,
       desc="Time for a name to become an address, through the same path an application "
            "uses. Slow here with a quiet link is the resolver, not the line.")

    return dashboard("rig-network", "Rig — Network",
                     "Who is using the link, where it goes, and whether it is full. The queue "
                     "panels answer the question throughput cannot: why everything lags while "
                     "a download sits under its own speed cap.",
                     L, tags=["network"])


# --------------------------------------------------------------------------
# 6. Compute — the two processors, together first and then each in depth
# --------------------------------------------------------------------------

def compute():
    L = Layout()
    L.row("Together")
    stat(L, "CPU busy", "rig:cpu:busy_ratio", unit="percentunit", w=4,
         desc="Averaged over every thread. On its own it cannot tell one saturated core from "
              "every core half busy.",
         thresholds=steps(("yellow", 0.7), ("orange", 0.9)))
    stat(L, "Busiest thread", "rig:cpu:hottest_core_ratio", unit="percentunit", w=4,
         desc="The hottest single thread. Pinned at 1.0 while the average sits low is a "
              "single-threaded section, and it is what usually starves a GPU.",
         thresholds=steps(("yellow", 0.8), ("orange", 0.95)))
    stat(L, "GPU shaders", "rig:gpu:busy_ratio", unit="percentunit", w=4,
         thresholds=steps(("yellow", 0.7), ("orange", 0.9)))
    stat(L, "GPU memory bus", "rig:gpu:mem_busy_ratio", unit="percentunit", w=4,
         desc="Time the memory controller moved data. Above the shader figure means the card is "
              "waiting on its own VRAM, and more shader clock will not help.",
         thresholds=steps(("yellow", 0.7), ("orange", 0.9)))
    stat(L, "VRAM used", "rig:gpu:vram_used_ratio", unit="percentunit", w=4,
         thresholds=steps(("yellow", 0.7), ("orange", 0.85), ("red", 0.95)))
    stat(L, "RAM used", "rig:mem:used_ratio", unit="percentunit", w=4,
         thresholds=steps(("yellow", 0.8), ("orange", 0.9), ("red", 0.95)))

    ts(L, "Busy: CPU against GPU", [
        ("rig:cpu:busy_ratio", "CPU, all threads"),
        ("rig:cpu:hottest_core_ratio", "CPU, busiest thread"),
        ("rig:gpu:busy_ratio", "GPU shaders"),
        ("rig:gpu:mem_busy_ratio", "GPU memory bus"),
    ], unit="percentunit", max_=1,
       desc=("The four readings that decide which end the work is stuck at. Both high: the "
             "machine is working. GPU high, CPU low: GPU bound, and the CPU has nothing to do. "
             "GPU low with the busiest thread pinned: the GPU is starved by one thread feeding "
             "it. Both low with the load average up: neither processor is the problem — look "
             "at Storage."))
    ts(L, "Memory: RAM against VRAM", [
        ("rig:mem:used_ratio", "system RAM"),
        ("rig:gpu:vram_used_ratio", "VRAM"),
        ("rig:mem:swap_used_ratio", "swap"),
    ], unit="percentunit", max_=1,
       desc=("The two pools behave differently under pressure. RAM overcommits into swap and the "
             "machine gets slow. VRAM has no swap and no OOM killer: the driver refuses the "
             "allocation and the client that asked for it dies, compositor included."))
    ts(L, "Clock, as a share of maximum", [
        ("rig:cpu:clock_ratio", "CPU vs. single-core boost"),
        ("rig:gpu:clock_ratio", "GPU vs. maximum SM clock"),
    ], unit="percentunit", max_=1,
       desc=("Neither line reaches 1.0 under an all-core or all-SM load, so read the trend, not "
             "the value. Both falling together is one power or thermal envelope holding both "
             "parts down."))
    ts(L, "Power and heat", [
        ("rig:gpu_watts", "GPU watts"),
        ("rig:gpu:power_limit_watts", "GPU limit (W)"),
        ("rig:gpu_celsius", "GPU (C)"),
        ("rig:cpu_celsius", "CPU (C)"),
    ], unit="none",
       desc="Mixed units on one axis on purpose: the question is whether power and temperature "
            "move together, not what either is worth. Thermals has the calibrated version.")
    ts(L, "CPU cores held, by process group", [("topk(8, rig:proc:cpu_cores)", "{{groupname}}")],
       unit="none", stack=True,
       desc="Pair this with the GPU panel beside it — the crosshair is shared across the board, "
            "so hovering names the group that was running when the GPU rose or fell.")
    ts(L, "GPU engines", [
        ("rig:gpu:busy_ratio", "shaders"),
        ("rig:gpu:mem_busy_ratio", "memory bus"),
        ("rig:gpu:encoder_ratio", "encoder (NVENC)"),
        ("rig:gpu:decoder_ratio", "decoder (NVDEC)"),
    ], unit="percentunit", max_=1,
       desc="A screen recorder or a video call lives on the encoder line and costs almost no "
            "shader time.")

    L.row("CPU")
    ts(L, "Every thread", [("rig:cpu:core_busy_ratio", "cpu{{cpu}}")],
       unit="percentunit", max_=1, w=16, h=10, legend_calcs=("last", "max"),
       desc="One line per hardware thread. A flat band of lines at the same height is a "
            "parallel build; one line alone at the top is a section that cannot be split.")
    ts(L, "Threads above 90%", [("rig:cpu:saturated_cores", "saturated threads")],
       unit="none", w=8, h=10, fill=30,
       desc="The same picture as one number. Equal to the thread count means the CPU is "
            "genuinely full; 1 means one thread is, and the other twenty-three are waiting "
            "for it.")
    ts(L, "Time by mode", [
        ("rig:cpu:user_ratio", "user"),
        ("rig:cpu:system_ratio", "system (kernel)"),
        ("rig:cpu:nice_ratio", "nice (background)"),
        ("rig:cpu:iowait_ratio", "iowait"),
        ("rig:cpu:irq_ratio", "interrupts"),
        ("rig:cpu:steal_ratio", "steal"),
    ], unit="percentunit", stack=True, max_=1,
       desc=("The full split, not the three the Overview shows. A large system band is syscall "
             "or page-fault cost rather than the program's own work; a large interrupt band is "
             "a device, usually the network."))
    ts(L, "Load average, split", [
        ("rig:load:runnable", "runnable (wants CPU)"),
        ("rig:load:blocked", "blocked (waiting on IO)"),
    ], stack=True, unit="none",
       desc="Only the runnable half is a CPU problem. The blocked half belongs to Storage.")
    ts(L, "Clock", [("rig:cpu:clock_hz", "average across threads")], unit="hertz",
       desc="Averaged over every thread, so a boosting core and a parked one blend. Falls under "
            "an all-core load by design, and again under heat.")
    ts(L, "Pressure: CPU", [("rig:psi:cpu_some", "some task queued for CPU")],
       unit="percentunit", max_=1,
       desc="The share of time at least one runnable task was waiting for a thread. Rises before "
            "busy_ratio saturates, which makes it the earlier warning.")
    ts(L, "Context switches by process group",
       [("topk(8, rig:proc:context_switches_per_sec)", "{{groupname}}")], unit="none", stack=True,
       desc="Scheduling churn. A group high here and low on cores held is spending its time "
            "being descheduled, not computing.")
    ts(L, "Threads by process group", [("topk(8, rig:proc:threads)", "{{groupname}}")],
       unit="none", stack=True,
       desc="Thread counts far above the CPU's own is where the context switches come from.")
    table(L, "Top CPU now (cores held)", [("topk(15, rig:proc:cpu_cores)", "")],
          w=12, unit="none", rename={"groupname": "group"},
          desc="Cores, not percent. 4.0 is four threads solid.")
    table(L, "Scaling governor", [
        ("count by (governor) (node_cpu_scaling_governor == 1)", ""),
    ], w=12, unit="none", rename={"Value": "threads"},
        desc="Threads under each governor. `schedutil` ramps on demand and lags a short burst; "
             "`performance` holds the clock up and costs idle watts. A split between two of "
             "them is a machine that was reconfigured half way.")

    L.row("GPU")
    stat(L, "Shaders", "rig:gpu:busy_ratio", unit="percentunit", w=3,
         thresholds=steps(("yellow", 0.7), ("orange", 0.9)))
    stat(L, "Memory bus", "rig:gpu:mem_busy_ratio", unit="percentunit", w=3,
         thresholds=steps(("yellow", 0.7), ("orange", 0.9)))
    stat(L, "VRAM free", "rig:gpu:vram_free_bytes", unit="bytes", w=3,
         desc="What the next allocation has to fit in.",
         thresholds=steps(("red", 512 << 20), ("orange", 1 << 30), ("yellow", 2 << 30),
                          base="green"))
    stat(L, "VRAM used", "rig:gpu:vram_used_ratio", unit="percentunit", w=3,
         thresholds=steps(("yellow", 0.7), ("orange", 0.85), ("red", 0.95)))
    stat(L, "Power against limit", "rig:gpu:power_ratio", unit="percentunit", w=3,
         desc="At 1.0 the card is clock-limited by its power budget, which is normal under load "
              "and is the commonest reason the clock sits below maximum.",
         thresholds=steps(("yellow", 0.9)))
    stat(L, "SM clock", "rig:gpu:sm_clock_hz", unit="hertz", w=3)
    stat(L, "Throttle headroom", "rig:thermal:gpu_headroom_c", unit="celsius", w=3,
         desc="Degrees before the card slows itself down.",
         thresholds=steps(("orange", 5), ("yellow", 10), ("green", 15), base="red"))
    stat(L, "Throttled", "sum(rig:gpu:throttled_ratio)", unit="percentunit", w=3,
         desc="Total share of time any reason held the clock down. The panel below says which.",
         thresholds=steps(("yellow", 0.1), ("orange", 0.5)))

    ts(L, "VRAM", [
        ("rig:gpu:vram_used_bytes", "used"),
        ("rig:gpu:vram_total_bytes", "installed"),
    ], unit="bytes", fill=20,
       desc=("Used is counted as installed minus free, which is the number the driver allocates "
             "against. nvidia-smi's own 'used' excludes its reserve, so it stops short of the "
             "total by a few hundred MiB even on a card that is refusing allocations."))
    ts(L, "Why the clock is not at maximum",
       [("rig:gpu:throttled_ratio", "{{reason}}")], unit="percentunit", stack=True, max_=1,
       desc=("Share of wall time each reason held the clock down, from the card's own counters "
             "rather than a 15-second sample of a flag. `power cap` is ordinary under load. "
             "`thermal` means the cooler, and Thermals says whether that is dust. `power brake` "
             "is the PSU asserting a hardware line and is never ordinary."))
    ts(L, "Clocks", [
        ("rig:gpu:sm_clock_hz", "shader"),
        ("rig:gpu:mem_clock_hz", "memory"),
    ], unit="hertz",
       desc="Memory clock steps between a handful of fixed levels; shader clock is continuous. "
            "The memory clock dropping to its lowest step is the card deciding it is idle.")
    ts(L, "Power", [
        ("rig:gpu_watts", "draw"),
        ("rig:gpu:power_limit_watts", "enforced limit"),
    ], unit="watt", thresholds=True,
       desc="Draw pressed flat against the limit for minutes is a power-limited card, not a "
            "broken one. `nvidia-smi -pl` moves the limit line.")
    ts(L, "Temperature against power", [
        ("rig:gpu_celsius", "temp (C)"),
        ("rig:gpu_watts", "power (W)"),
        ("rig:thermal:gpu_fan_ratio * 100", "fan (%)"),
    ], unit="none",
       desc="Rising temperature at equal power and equal fan is the cooler getting worse. "
            "Thermals turns this into a month-over-month ratio.")
    ts(L, "Link to the host", [
        ("rig:gpu:pcie_gen_ratio", "generation, share of maximum"),
        ("rig:gpu:pcie_width_ratio", "lane width, share of maximum"),
    ], unit="percentunit", max_=1,
       desc=("Both drop on an idle card to save power, so only a low reading while the GPU is "
             "busy means anything. A card stuck at half width under load is seated wrong or "
             "sharing lanes with an NVMe drive."))
    ts(L, "Encoder sessions", [
        ("max(nvidia_smi_encoder_stats_session_count)", "sessions"),
        ("max(nvidia_smi_encoder_stats_average_fps)", "average fps"),
        ("max(nvidia_smi_encoder_stats_average_latency)", "average latency (us)"),
    ], unit="none",
       desc="Screen recording, streaming and video calls. Latency climbing while the session "
            "count holds is the encoder queue backing up.")
    ts(L, "Integrated GPU", [
        ("rig:igpu:busy_ratio", "busy"),
        ("rig:igpu:vram_used_bytes / 1e9", "VRAM used (GB)"),
    ], unit="none",
       no_value="The integrated GPU is idle — the desktop is running on the discrete card.",
       desc="Read through DRM rather than nvidia-smi. Non-zero here means something is rendering "
            "on the integrated GPU, which for a desktop session is usually a mistake.")

    return dashboard("rig-compute", "Rig — Compute",
                     "CPU and GPU, together first: which end the work is stuck at, then each "
                     "processor in depth. Per-thread CPU occupancy, the full mode split, GPU "
                     "engines, VRAM, clocks, power and the card's own throttle reasons.",
                     L, tags=["cpu", "gpu"])


# --------------------------------------------------------------------------
# 7. AI — what the coding harnesses spent, at API list prices
# --------------------------------------------------------------------------

LIST_PRICE = ("Every dollar here is API list value: what these tokens would cost billed through "
              "the provider's API. A subscription pays a flat fee instead, so this is the value "
              "received rather than money leaving the account.")

# The harness job scrapes at 60s, but $__rate_interval is built from the
# datasource's 15s default. Zoom in past ~6h and the window holds one sample,
# increase() needs two, and the panel goes empty. Floor the step above a scrape.
HARNESS_STEP = "5m"

# What the dropdowns at the top of the board narrow every panel to.
PICK = 'harness=~"$harness",provider=~"$provider",model=~"$model"'
PICK_HARNESS = 'harness=~"$harness"'


def picked(metric, *extra, pick=PICK):
    """`metric{...}` with the dropdowns folded in.

    A rig:ai: recording rule has already summed provider and model away, so a
    panel that must answer "deepseek only" reads the counter itself.
    """
    return f'{metric}{{{",".join((pick, *extra))}}}'


# A window's line and the darker twin its pace line is drawn in. Hues, not
# shades of one hue: a seller owns several windows and they must not read as
# one another.
QUOTA_HUES = [("#73BF69", "#37872D"), ("#FF9830", "#C15C17"), ("#5794F2", "#1F60C4"),
              ("#B877D9", "#8F3BB8"), ("#F2495C", "#C4162A"), ("#6ED0E0", "#1F8FA8"),
              ("#FADE2A", "#B68F00"), ("#FF85C0", "#C43C8F"), ("#C0C7D4", "#6B7280"),
              ("#2DD4BF", "#0F766E"), ("#A3E635", "#65A30D"), ("#818CF8", "#4338CA"),
              ("#E879F9", "#A21CAF"), ("#38BDF8", "#0369A1"), ("#FB923C", "#C2410C"),
              ("#FCA5A5", "#B91C1C")]

# Ranked, not merely listed: hues are handed out down this list, so the windows
# every seller has take the ones that separate best.
QUOTA_WINDOWS = [r"session", r"weekly", r"weekly-.+", r".*"]


def quota_colours():
    """One hue per seller and window, its line bright and its pace line dark.

    A pair reads as one window and no two windows share a hue. A seller added
    to SUBSCRIPTIONS takes the next hues with no edit here.
    """
    ranked = [(sub.name, window) for window in QUOTA_WINDOWS for sub in hq.SUBSCRIPTIONS]
    hues = {key: QUOTA_HUES[i % len(QUOTA_HUES)] for i, key in enumerate(ranked)}
    out = []
    # Grafana applies overrides in order and the last match wins, so the
    # catch-all is written before the windows that overrule it.
    for name, window in reversed(ranked):
        for suffix, hue in zip(("actual", "even burn"), hues[(name, window)]):
            out.append(column(rf"^{name} {window} {suffix}$", matcher="byRegexp",
                              color={"mode": "fixed", "fixedColor": hue}))
    return out


def subscriptions(L):
    """One line per plan: what each window has left, and when it resets.

    Six queries rather than one, joined on the labels they share. Each window
    is its own series, so a plan that meters none of them leaves a blank cell
    instead of dropping the plan off the board.
    """
    left = f'1 - aiusage_rate_limit_used_ratio{{{PICK_HARNESS}}}'
    reset = f'rig:ai:limit_reset_in_seconds{{{PICK_HARNESS}}}'
    by = "max by (harness, plan)"
    names = ["session left", "session resets in", "weekly left", "weekly resets in",
             "fable weekly left", "measured"]
    headroom = steps(("yellow", 0.15), ("green", 0.4), base="red")
    table(L, "Every plan on one line", [
        f'{by} ({left[:-1]},window="session"}})',
        f'{by} ({reset[:-1]},window="session"}})',
        f'{by} ({left[:-1]},window="weekly"}})',
        f'{by} ({reset[:-1]},window="weekly"}})',
        f'{by} ({left[:-1]},window="weekly-fable"}})',
        f'time() - {by} (aiusage_rate_limit_seen_timestamp_seconds{{{PICK_HARNESS}}})',
    ], w=24, h=7, merge=True, sort_by="provider", sort_desc=False,
       rename={"harness": "provider",
               **{f"Value #{chr(65 + i)}": name for i, name in enumerate(names)}},
       order=["harness", "plan", *(f"Value #{chr(65 + i)}" for i in range(len(names)))],
       columns=[column(name, unit="percentunit", min=0, max=1, decimals=0,
                       color={"mode": "thresholds"}, thresholds=headroom,
                       custom_cellOptions={"type": "gauge", "mode": "gradient"})
                for name in ("session left", "weekly left", "fable weekly left")]
               + [column(name, unit="s", decimals=0)
                  for name in ("session resets in", "weekly resets in")]
               + [column("measured", unit="s", decimals=0,
                         color={"mode": "thresholds"},
                         thresholds=steps(("yellow", 900), ("red", 3600)))],
       no_value="No plan answered. `rig ai limits` says whether that is a signed-out harness, "
                "an expired token, or an account on no metered plan.",
       desc="Read from the seller that meters each plan, so it falls for every device the "
            "account is signed in on. `measured` is how old the figure is: a plan is asked "
            "every few minutes, not every scrape.")

    # The even burn is the window's own clock: how much of it is left in time.
    # Spend uniformly from the reset and the quota tracks it exactly, so the
    # gap between a window's two lines is the whole finding.
    even = (f'clamp((aiusage_rate_limit_reset_timestamp_seconds{{{PICK_HARNESS}}} - time()) '
            f'/ aiusage_rate_limit_window_seconds{{{PICK_HARNESS}}}, 0, 1)')
    ts(L, "Quota left, against spending it evenly", [
        (f'1 - aiusage_rate_limit_used_ratio{{{PICK_HARNESS}}}', "{{harness}} {{window}} actual"),
        (even, "{{harness}} {{window}} even burn"),
    ], unit="percentunit", min_=0, max_=1, w=24, h=8, legend_calcs=("last",),
       no_value="Nothing to pace. A window needs both a reset time and a declared length, and "
                "not every seller states one.",
       desc="The dashed line is the window's own clock: what would be left had you started at "
            "the reset and spent evenly to the end, 100% down to 0% and back to 100%. The solid "
            "line is what is actually left. Above the dash is ahead of pace — the quota outlasts "
            "the window. Below it, the window outlasts the quota and you run dry early. A window "
            "is paced once it has both a reset and a length; `docs/ai-usage.md` names the two "
            "that take the missing half from what the seller says around them.")
    L.panels[-1]["fieldConfig"]["overrides"] = quota_colours() + [
        column(".*even burn", matcher="byRegexp",
               custom_lineStyle={"fill": "dash", "dash": [10, 10]},
               custom_lineWidth=1, custom_fillOpacity=0),
    ]


def ai():
    L = Layout()
    money = picked("aiusage_cost_usd_total")
    reads = picked("aiusage_tokens_total", 'role="cache_read"')
    sent = picked("aiusage_tokens_total", 'role=~"input|cache_read|cache_write"')
    billable = picked("aiusage_tokens_total", 'role!="reasoning"')
    cache_share = f"sum({reads}) / clamp_min(sum({sent}), 1)"
    per_million = f"sum({money}) / clamp_min(sum({billable}) / 1e6, 1e-9)"
    L.row("Subscriptions — what is left, and when it comes back")
    subscriptions(L)

    L.row("API list value")
    stat(L, "All recorded", f"sum({money})", unit="currencyUSD", w=4, decimals=0,
         desc=LIST_PRICE + " Counts every session file still on disk.")
    # An offset delta, not increase(): a counter recomputed from the ledger can
    # move down, and increase() reads that as a reset and re-adds the lot.
    stat(L, "Last 24h", f"sum({money}) - sum({money} offset 24h)", unit="currencyUSD", w=3,
         decimals=0, thresholds=steps(("yellow", 50), ("orange", 200), ("red", 500)),
         desc="What the counter has climbed in a day. Never increase(): a backfilled sample "
              "meeting a live one reads as a reset and adds the whole counter back.")
    stat(L, "Last 7 days", f"sum({money}) - sum({money} offset 7d)", unit="currencyUSD", w=3,
         decimals=0,
         desc="What the counter has climbed in a week, measured the same way.")
    stat(L, "Burn rate", f"sum(rate({money}[1h])) * 3600", unit="currencyUSD", w=3, decimals=1,
         desc="Dollars of list value per hour, averaged over the last hour.",
         thresholds=steps(("yellow", 10), ("orange", 40), ("red", 100)))
    stat(L, "Per million tokens", per_million, unit="currencyUSD", w=3,
         decimals=2, desc="Blended over every role. Rises when caches lapse and windows get rewritten.")
    stat(L, "Context re-read", cache_share, unit="percentunit", w=3,
         desc="Share of input-side tokens that are cache reads — the window being re-sent. "
              "A cache read is billed at about a tenth of a fresh input token, so high is cheap: "
              "the red end is a session paying full price to say the same thing again.",
         thresholds=steps(("orange", 0.5), ("yellow", 0.75), ("green", 0.9), base="red"))
    stat(L, "Live sessions", f'sum(aiusage_sessions_live{{{PICK_HARNESS}}})', unit="none", w=2,
         desc="Harness sessions whose state file moved in the last few minutes.")
    stat(L, "Harnesses seen", f'count(aiusage_source_files{{{PICK_HARNESS}}} > 0)',
         unit="none", w=3, graph=False,
         desc="Harnesses with session files on this machine. `rig ai doctor` lists all of them.")

    L.row("Where the money goes")
    ts(L, "Spend by token role", [
        (f"sum by (role) (increase({money}[$__rate_interval]))", "{{role}}"),
    ], unit="currencyUSD", stack=True, w=8, min_interval=HARNESS_STEP,
       desc=("cache_read is normally the largest by far: a running context is re-sent on every "
             "request, so cost is context size times request count. cache_write spikes when a "
             "prompt cache lapses and the whole window is paid for again."))
    ts(L, "Spend by harness", [
        (f"sum by (harness) (increase({money}[$__rate_interval]))", "{{harness}}"),
    ], unit="currencyUSD", stack=True, w=8, min_interval=HARNESS_STEP)
    ts(L, "Spend by model", [
        (f"topk(8, sum by (model) (increase({money}[$__rate_interval])))", "{{model}}"),
    ], unit="currencyUSD", stack=True, w=8, min_interval=HARNESS_STEP,
       desc=("The eight dearest in the window. A model is summed across whichever provider "
             "served it, so the Provider dropdown is what separates the same model bought "
             "from two sellers."))
    # The counters carry a harness label too; leaving it in the `by` draws one
    # bar per harness under the same name.
    bars(L, "Total by project",
         f'topk(15, sum by (project) ({picked("aiusage_project_cost_usd_total")}))',
         unit="currencyUSD", w=8, decimals=0,
         desc="Cumulative list value per project directory, all recorded history.")
    bars(L, "Total by model", f"topk(12, sum by (model) ({money}))",
         unit="currencyUSD", w=8, decimals=0)
    bars(L, "Total by role", f"sum by (role) ({money})",
         unit="currencyUSD", w=8, decimals=0)

    L.row("Who did the work")
    ts(L, "Delegated versus direct", [
        (f"sum by (kind) (increase({money}[$__rate_interval]))", "{{kind}}"),
    ], unit="currencyUSD", stack=True, w=12, min_interval=HARNESS_STEP,
       desc="Send enough work to subagents and most of the money stops being spent by the session "
            "you are watching.")
    ts(L, "Spend by project", [
        (f'topk(8, sum by (project) '
         f'(increase({picked("aiusage_project_cost_usd_total")}[$__rate_interval])))',
         "{{project}}"),
    ], unit="currencyUSD", stack=True, w=12, min_interval=HARNESS_STEP)
    table(L, "Model detail", [(f"sum by (harness, provider, model) ({money})", "")],
          w=12, unit="currencyUSD", rename={"Value": "list value"},
          desc="Rates come from models.dev. `rig ai models` prints the per-million figures used.")
    table(L, "Harnesses on this machine", [(f'aiusage_source_files{{{PICK_HARNESS}}}', "")],
          w=12, unit="none", rename={"Value": "session files"},
          desc="Zero files means the harness is installed but has written nothing this reader "
               "understands. `rig ai doctor` says which.")

    L.row("Tokens")
    ts(L, "Tokens per second by role", [
        (f'sum by (harness, role) (rate({picked("aiusage_tokens_total")}[15m]))',
         "{{harness}} {{role}}"),
    ], unit="none", stack=True, w=12,
       desc="reasoning is already inside output and is never priced twice.")
    ts(L, "API responses per hour", [
        (f'sum by (harness) (rate({picked("aiusage_requests_total")}[1h])) * 3600', "{{harness}}"),
    ], unit="none", stack=True, w=12)
    ts(L, "Context re-read share", [(cache_share, "cache reads / input-side tokens")],
       unit="percentunit", max_=1, w=12,
       desc="Climbs through a long session. The window grows and every request pays for all of it.")
    ts(L, "Dollars per million tokens", [(per_million, "blended")],
       unit="currencyUSD", w=12,
       desc="A step up means either a dearer model or a cache rebuild.")

    L.row("Subscription and coverage")
    ts(L, "Subscription window left", [
        (f'1 - aiusage_rate_limit_used_ratio{{{PICK_HARNESS}}}',
         "{{harness}} {{window}} ({{plan}})"),
    ], unit="percentunit", max_=1, w=12,
       no_value="No subscription here answered. `rig ai limits` says whether that is a signed-out "
                "harness, an expired token, or an account on no metered plan.",
       desc="What the plan has left, asked of the seller that meters it. It is the only figure on "
            "this dashboard that is about the plan rather than about list price, and it falls for "
            "every device the account is signed in on, not only this one.")
    ts(L, "List price against what the harness claims", [
        (f'sum({picked("aiusage_reported_cost_usd_total")})', "harness reported"),
        (f"sum({money})", "API list value"),
    ], unit="currencyUSD", w=12,
       desc="Only some harnesses report a cost at all, so the reported line covers part of the "
            "total. Where both exist the gap is the subsidy.")
    stat(L, "Tokens with no published price",
         f'sum({picked("aiusage_unpriced_tokens_total")})', unit="none", w=6,
         desc="Excluded from every dollar figure here. A model nobody sells by the token gets no "
              "invented number.",
         thresholds=steps(("yellow", 1)))
    stat(L, "Ledger age", "rig:ai:scan_age_seconds", unit="s", w=6,
         desc="Since the exporter last read the session files.",
         thresholds=steps(("yellow", 300), ("orange", 900)))
    stat(L, "Price catalogue age", "aiusage_prices_age_seconds", unit="s", w=6,
         desc="models.dev is refetched by the exporter on its own schedule.",
         thresholds=steps(("yellow", 7 * 86400), ("orange", 30 * 86400)))
    stat(L, "Files tracked", f'sum(aiusage_source_files{{{PICK_HARNESS}}})',
         unit="none", w=6, graph=False)

    return dashboard("rig-ai", "Rig — AI Spend",
                     "What every AI coding harness on this machine used, priced at published API "
                     "list rates. Money, tokens, models, projects, and the delegated share. The "
                     "dropdowns narrow every panel to one harness, provider or model.",
                     L, time_from="now-7d", refresh="1m", tags=["ai", "cost"],
                     annotations=[a for clock in peak_windows()
                                  for a in peak_annotations(*clock)],
                     variables=[
                         filter_var("harness", "Harness", "aiusage_cost_usd_total"),
                         filter_var("provider", "Provider", "aiusage_cost_usd_total",
                                    within=("harness",)),
                         filter_var("model", "Model", "aiusage_cost_usd_total",
                                    within=("harness", "provider")),
                     ])


# --------------------------------------------------------------------------

BOARDS = {
    "rig-overview.json": overview,
    "rig-who.json": who,
    "rig-network.json": network,
    "rig-compute.json": compute,
    "rig-thermals.json": thermals,
    "rig-storage.json": storage,
    "rig-ai.json": ai,
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
