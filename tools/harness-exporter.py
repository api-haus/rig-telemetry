#!/usr/bin/env python3
"""Serve AI coding harness usage to Prometheus.

A background thread reads what is new in each harness's session files into the
SQLite ledger, then `/metrics` serves the ledger's running totals as counters.
Scanning never happens on the scrape path, so a slow first pass over years of
transcripts cannot time a scrape out.

    tools/harness-exporter.py --port 13360 --interval 60

Counters start from everything already on disk, so the first scrape reports a
lifetime total rather than zero. Prometheus can only draw the part that happens
after it starts watching — `rig ai backfill` imports the rest.
"""

from __future__ import annotations

import argparse
import http.server
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness_usage as hu  # noqa: E402

TOP_PROJECTS = int(os.environ.get("RIG_AI_TOP_PROJECTS", "80"))


def escape(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", " ")


def number(value: float) -> str:
    """Full precision, always.

    A Unix timestamp needs ten significant digits before the seconds survive,
    and a rounded one reads as an exporter that stopped scanning an hour ago.
    """
    if float(value).is_integer() and abs(value) < 2 ** 53:
        return str(int(value))
    return repr(float(value))


class Registry:
    """The metrics text, rebuilt after every scan and served as-is."""

    def __init__(self):
        self.body = "# rig harness exporter starting\n"
        self.lock = threading.Lock()

    def publish(self, text: str):
        with self.lock:
            self.body = text

    def read(self) -> bytes:
        with self.lock:
            return self.body.encode()


def render(db, scan: hu.Scan, prices: hu.Prices) -> str:
    out: list[str] = []

    def emit(name, kind, help_text, samples):
        if not samples:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            tags = ",".join(f'{k}="{escape(str(v))}"' for k, v in labels.items())
            out.append(f"{name}{{{tags}}} {number(value)}" if tags
                       else f"{name} {number(value)}")

    model_key = ("harness", "provider", "model", "kind")
    by_model = hu.totals(db, model_key, prices=prices)
    tokens, requests, reported, unpriced = [], [], [], []
    for row in by_model:
        tags = {k: row[k] for k in model_key}
        for role in hu.TOKEN_ROLES:
            if row[role]:
                tokens.append(({**tags, "role": role}, row[role]))
        requests.append((tags, row["requests"]))
        if row["reported_cost"]:
            reported.append((tags, row["reported_cost"]))
    costs = cost_by_role(by_model, model_key)

    emit("aiusage_tokens_total", "counter",
         "Tokens exchanged with a model. reasoning is already inside output and is never priced.",
         tokens)
    emit("aiusage_cost_usd_total", "counter",
         "What these tokens would cost at published API list rates, in dollars.", costs)
    emit("aiusage_requests_total", "counter", "API responses recorded.", requests)
    emit("aiusage_reported_cost_usd_total", "counter",
         "What the harness itself said the same work cost, where it says anything.", reported)

    limit = TOP_PROJECTS
    by_project = hu.totals(db, ("harness", "project"), prices=prices)
    ranked = sorted(by_project, key=lambda r: -r["cost"])
    keep = {(r["harness"], r["project"]) for r in ranked[:limit]}
    folded: dict[tuple[str, str], list[float]] = {}
    for row in by_project:
        key = (row["harness"], row["project"]) if (row["harness"], row["project"]) in keep \
            else (row["harness"], "other")
        acc = folded.setdefault(key, [0.0, 0.0, 0.0])
        acc[0] += row["cost"]
        acc[1] += sum(row[r] for r in ("input", "output", "cache_read", "cache_write"))
        acc[2] += row["requests"]
    emit("aiusage_project_cost_usd_total", "counter",
         f"API list value per project. Beyond the {limit} dearest, projects fold into `other`.",
         [({"harness": h, "project": p}, v[0]) for (h, p), v in folded.items()])
    emit("aiusage_project_tokens_total", "counter", "Tokens per project, every role summed.",
         [({"harness": h, "project": p}, v[1]) for (h, p), v in folded.items()])
    emit("aiusage_project_requests_total", "counter", "API responses per project.",
         [({"harness": h, "project": p}, v[2]) for (h, p), v in folded.items()])

    for row in hu.unpriced(db, prices):
        unpriced.append(({"harness": row["harness"], "provider": row["provider"],
                          "model": row["model"]}, row["tokens"]))
    emit("aiusage_unpriced_tokens_total", "counter",
         "Tokens whose model publishes no per-token price. Excluded from every cost above.",
         unpriced)

    gauges: dict[str, list] = {}
    for name, tags, value in scan.gauges:
        gauges.setdefault(name, []).append((tags, value))
    emit("aiusage_sessions_live", "gauge", "Harness sessions whose state file moved recently.",
         gauges.get("aiusage_sessions_live", []))
    emit("aiusage_rate_limit_used_ratio", "gauge",
         "Fraction of a subscription window consumed, as the harness last reported it.",
         gauges.get("aiusage_rate_limit_used_ratio", []))
    emit("aiusage_rate_limit_reset_timestamp_seconds", "gauge",
         "When that window resets.", gauges.get("aiusage_rate_limit_reset_timestamp_seconds", []))
    emit("aiusage_rate_limit_seen_timestamp_seconds", "gauge",
         "When the rate-limit figures were last written by the harness.",
         gauges.get("aiusage_rate_limit_seen_timestamp_seconds", []))

    emit("aiusage_source_files", "gauge",
         "Session files found for each harness. Zero means installed but silent.",
         [({"harness": name}, count) for name, count in sorted(scan.files.items())])
    emit("aiusage_source_installed", "gauge",
         "1 when the harness has a home directory on this machine.",
         [({"harness": cls.name}, 1 if cls().installed() else 0) for cls in hu.SOURCES])
    emit("aiusage_scan_duration_seconds", "gauge", "How long the last pass took.",
         [({}, scan.seconds)])
    emit("aiusage_scan_records", "gauge", "Records added by the last pass.",
         [({}, scan.records)])
    emit("aiusage_scan_errors", "gauge", "Files the last pass could not read.",
         [({}, scan.errors)])
    emit("aiusage_scan_timestamp_seconds", "gauge", "When the last pass finished.",
         [({}, time.time())])
    path = hu.catalogue_path()
    emit("aiusage_prices_age_seconds", "gauge",
         "Age of the models.dev price catalogue. Stale prices are still prices.",
         [({}, time.time() - path.stat().st_mtime if path.is_file() else -1)])
    emit("aiusage_prices_models", "gauge", "Providers in the price catalogue.",
         [({}, len(prices.catalogue))])
    emit("aiusage_price_overrides", "gauge",
         "Model names given a rate by hand, because no catalogue entry matches them.",
         [({}, len(prices.overrides))])
    return "\n".join(out) + "\n"


def cost_by_role(rows, key) -> list:
    """Cost split the way the tokens are, so the dear role is visible.

    Only the four billed roles appear: reasoning tokens are already inside
    output and pricing them again would double the bill.
    """
    out = []
    for row in rows:
        tags = {k: row[k] for k in key}
        for role in hu.BILLED_ROLES:
            if row[f"cost_{role}"]:
                out.append(({**tags, "role": role}, row[f"cost_{role}"]))
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    registry: Registry

    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = self.registry.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def loop(registry: Registry, interval: float):
    db = None
    while True:
        started = time.time()
        try:
            # Opened inside the loop: a thread that dies on a locked ledger
            # would leave the last good metrics being served forever, which
            # reads exactly like a machine that has stopped spending money.
            db = db or hu.open_ledger()
            # Prices are rebuilt every pass, so a catalogue that arrives late —
            # or a rate that changes — reprices the whole ledger rather than
            # only what is scanned afterwards.
            prices = hu.Prices()
            scan = hu.scan(db, prices)
            registry.publish(render(db, scan, prices))
        except Exception as e:                                  # noqa: BLE001
            db = None
            registry.publish(f'# scan failed: {escape(str(e))}\n'
                             f'aiusage_scan_errors 1\n'
                             f'aiusage_scan_timestamp_seconds 0\n')
        # The price catalogue refreshes itself on the schedule the library sets.
        if time.time() - started < interval:
            time.sleep(max(1.0, interval - (time.time() - started)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("RIG_AI_PORT", "13360")))
    ap.add_argument("--addr", default=os.environ.get("RIG_AI_ADDR", "127.0.0.1"))
    ap.add_argument("--interval", type=float, default=float(os.environ.get("RIG_AI_INTERVAL", "60")))
    ap.add_argument("--once", action="store_true", help="scan, print the metrics, exit")
    args = ap.parse_args()

    if args.once:
        db = hu.open_ledger()
        prices = hu.Prices()
        print(render(db, hu.scan(db, prices), prices), end="")
        return 0

    registry = Registry()
    Handler.registry = registry
    threading.Thread(target=loop, args=(registry, args.interval), daemon=True).start()
    server = http.server.ThreadingHTTPServer((args.addr, args.port), Handler)
    print(f"harness exporter on http://{args.addr}:{args.port}/metrics "
          f"reading {hu.HOME}, ledger {hu.ledger_path()}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
