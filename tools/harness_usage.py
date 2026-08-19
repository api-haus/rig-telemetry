#!/usr/bin/env python3
"""Read AI coding harness usage from the files each harness writes to disk.

Every harness records what it sent and what came back. The formats differ, the
token conventions differ, and only some of them say what it cost. This module
reads all of them, converts to one exclusive-token convention, prices the
tokens at published API list rates, and keeps the result in a SQLite ledger.

Two consumers: `tools/harness-exporter.py` serves the ledger to Prometheus,
`rig ai` reads it for history that predates the exporter. Standard library
only, like the rest of this stack.

Read docs/ai-usage.md for the conventions and the honest gaps.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # dsh compresses its transcript; the decoder is stdlib from 3.14 on.
    from compression.zstd import ZstdDecompressor, ZstdError
except ImportError:
    ZstdDecompressor = ZstdError = None

HOME = pathlib.Path(os.environ.get("RIG_AI_HOME") or pathlib.Path.home())
CACHE = pathlib.Path(os.environ.get("RIG_AI_CACHE") or
                     (pathlib.Path.home() / ".cache" / "rig-telemetry"))
CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or
                      (pathlib.Path.home() / ".config")) / "rig-telemetry"
SEED = pathlib.Path(__file__).resolve().parent.parent / "share" / "prices.tsv"
CATALOGUE_URL = "https://models.dev/api.json"
CATALOGUE_MAX_AGE = float(os.environ.get("RIG_AI_PRICES_MAX_AGE_DAYS", "7")) * 86400

# A model id is carried by dozens of gateways and the first match is not the
# number "what would this cost on the API" asks for. First party wins.
FIRST_PARTY = ("anthropic", "openai", "moonshotai", "moonshotai-cn", "google",
               "alibaba", "deepseek", "xai", "mistral", "meta", "zai", "minimax")

TOKEN_ROLES = ("input", "output", "cache_read", "cache_write", "reasoning")


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass
class Rec:
    """One API response, in the exclusive convention.

    `input` never includes cached tokens; `reasoning` is always already inside
    `output`, so it is reported but never priced.
    """
    ts: float
    harness: str
    model: str
    project: str
    kind: str = "main"
    provider: str = ""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    requests: int = 1
    reported_cost: float | None = None
    uid: str | None = None          # set when the record can appear twice

    def day(self) -> str:
        """The local day. "What did I spend yesterday" means the one you lived."""
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d")

    def utc_slot(self) -> str:
        """The UTC hour. A seller that moves its price on a clock moves it on
        its own, so the ledger keeps the hour the seller was billing in."""
        return datetime.fromtimestamp(self.ts, timezone.utc).strftime("%Y-%m-%dT%H")


@dataclass
class Scan:
    """What one pass over the disk found."""
    records: int = 0
    files: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    problems: list[str] = field(default_factory=list)
    gauges: list[tuple[str, dict[str, str], float]] = field(default_factory=list)
    seconds: float = 0.0

    def failed(self, what: str, why: BaseException):
        self.errors += 1
        if len(self.problems) < 10:
            self.problems.append(f"{what}: {type(why).__name__} {why}")


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

def price_key(model: str) -> str:
    """The name an override is filed under: one that survives an env var."""
    return "".join(c if c.isalnum() else "_" for c in model)


def load_overrides() -> dict[str, list[tuple[str, str, int]]]:
    """Model name -> the lines filed under it, as (value, source, rank).

    A harness names its models however it likes and some of those names reach
    no catalogue entry by any transformation, so this file states what a name
    is instead of the lookup guessing. Read every pass: fixing a price should
    not need a restart.

    One name can carry several lines, because one model can carry several
    prices — a rate that starts on a date, or only inside certain hours. The
    rank is how near the source is; `lookup` decides which line answers.
    """
    out: dict[str, list[tuple[str, str, int]]] = {}
    for rank, path in enumerate((SEED, CONFIG / "prices.tsv")):
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            name, _, value = line.partition("\t")
            if not value.strip():
                name, _, value = line.partition(" ")
            if value.strip():
                out.setdefault(price_key(name.strip()), []).append(
                    (value.strip(), str(path), rank))
    for key, value in os.environ.items():
        if key.startswith("RIG_AI_PRICE_") and value.strip():
            out.setdefault(key[len("RIG_AI_PRICE_"):], []).append(
                (value.strip(), "environment", 2))
    return out


# A rate line ends in any of these, each two words. `from` and `at` are read on
# the seller's clock, which is UTC on every price page that publishes one.
CLAUSES = ("from", "at", "on")


def _instant(text: str) -> float:
    """A UTC instant, as a price page writes it: 2026-08-16T16:00Z."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _hours(text: str) -> tuple[int, ...]:
    """UTC hour ranges, half-open, as a clock reads them: `01-04,06-10` is
    01:00 up to 04:00 and 06:00 up to 10:00, so hours 1-3 and 6-9."""
    out: set[int] = set()
    for span in text.split(","):
        first, _, last = span.partition("-")
        out.update(range(int(first), int(last) if last else int(first) + 1))
    return tuple(sorted(out))


_READ_CLAUSE = {"from": _instant, "at": _hours, "on": str}
_NO_CLAUSE = {"from": None, "at": None, "on": None}


def split_clauses(value: str) -> tuple[str, dict]:
    """Peel the trailing clauses off a price line, leaving the rates."""
    words, clauses = value.split(), dict(_NO_CLAUSE)
    while len(words) >= 2 and words[-2] in CLAUSES and clauses[words[-2]] is None:
        clauses[words[-2]] = _READ_CLAUSE[words[-2]](words[-1])
        words = words[:-2]
    return " ".join(words), clauses


def slot_instant(slot: str) -> float:
    """The start of a ledger row's UTC hour."""
    return datetime.strptime(slot, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp()


class Prices:
    """List rates in dollars per million tokens, from models.dev by default.

    Nothing is guessed. A model with no published price and no override is
    priced at zero and counted as unpriced, so the gap is visible instead of
    silently folded into a total that looks complete.
    """

    def __init__(self, catalogue: dict | None = None):
        self.catalogue = catalogue if catalogue is not None else load_catalogue()
        self.overrides = load_overrides()
        self._cache: dict[tuple[str, str], list[dict]] = {}
        self.unpriced: set[tuple[str, str]] = set()

    def lookup(self, provider: str, model: str, when: float | None = None) -> dict | None:
        """The rate in force at `when`, or now.

        A seller that moves its price on a clock is followed on that clock
        rather than averaged over it, so this needs to know which hour it is
        pricing. Of the rules that fit, the nearest source wins, then the
        latest start date, then the one that names hours.
        """
        rules = self._rules(provider, model)
        if not rules:
            return None
        ts = time.time() if when is None else when
        hour = datetime.fromtimestamp(ts, timezone.utc).hour
        fit = [r for r in rules
               if (r["on"] is None or r["on"] == provider)
               and (r["from"] is None or r["from"] <= ts)
               and (r["at"] is None or hour in r["at"])]
        if not fit:
            return None
        return max(fit, key=lambda r: (r["rank"], r["from"] or 0.0, r["at"] is not None))

    def clock(self, provider: str, model: str) -> tuple[tuple[int, ...], float] | None:
        """The UTC hours this model is dearer in, and when the clock starts.

        None when the price does not move on a clock, which is almost every
        model: only a seller that publishes a peak window answers here.
        """
        moving = [r for r in self._rules(provider, model)
                  if r["at"] and (r["on"] is None or r["on"] == provider)]
        if not moving:
            return None
        return (tuple(sorted({h for r in moving for h in r["at"]})),
                min(r["from"] or 0.0 for r in moving))

    def _rules(self, provider: str, model: str) -> list[dict]:
        key = (provider, model)
        if key not in self._cache:
            self._cache[key] = self._resolve(provider, model)
            if not self._cache[key]:
                self.unpriced.add(key)
        return self._cache[key]

    def _resolve(self, provider: str, model: str) -> list[dict]:
        """Every rate that could apply to this model, most local last resort."""
        if not model:
            return []
        rules = self._override_rules(model)
        return rules + self._catalogue_rules(provider, model)

    def _catalogue_rules(self, provider: str, model: str) -> list[dict]:
        if not self.catalogue:
            return []
        for name in self._variants(model):
            hit = self._in_provider(provider, name) if provider else None
            hit = hit or self._anywhere(name)
            if hit:
                return [dict(hit, rank=-1, **_NO_CLAUSE)]
        return []

    def _override_rules(self, model: str) -> list[dict]:
        """An override is either four rates or an alias into the catalogue.

        The alias form is the one to prefer: it says what the model *is*, and
        the rates then track models.dev instead of ageing in a file here. Only
        the rate form takes clauses — an alias says what a model is, which no
        hour of the day changes.
        """
        out = []
        for value, source, rank in self.overrides.get(price_key(model), ()):
            try:
                head, clauses = split_clauses(value)
            except ValueError:
                continue
            parts = head.split()
            if len(parts) == 4:
                try:
                    inp, cr, cw, tail = (float(p) for p in parts)
                except ValueError:
                    continue
                # Four rates name no catalogue entry, so the only provider such
                # a line knows is the one it scoped itself to.
                rates = {"provider": clauses["on"] or "", "input": inp, "cache_read": cr,
                         "cache_write": cw, "output": tail, "source": f"{model} (rates)"}
            else:
                provider, _, name = head.partition("/")
                hit = self._in_provider(provider, name) if name else None
                hit = hit or self._anywhere(name or provider)
                if not hit:
                    continue
                rates = dict(hit)
            out.append(dict(rates, via=source, rank=rank, **clauses))
        return out

    @staticmethod
    def _variants(model: str):
        """The names a harness uses for one model, cheapest guess first."""
        seen, out = set(), []
        base = model.split("/")[-1]
        for name in (model, base, base.rsplit("-", 1)[0], "kimi-" + base,
                     base.replace("_", "-"), base.removesuffix("-latest")):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def _in_provider(self, provider: str, model: str) -> dict | None:
        entry = (self.catalogue.get(provider) or {}).get("models", {}).get(model)
        cost = (entry or {}).get("cost") or {}
        return self._rates(cost, provider, model) if cost.get("input") else None

    def _anywhere(self, model: str) -> dict | None:
        hits = [(p, (v.get("models") or {})[model]) for p, v in self.catalogue.items()
                if model in (v.get("models") or {})]
        if not hits:
            return None
        for want in FIRST_PARTY:
            for p, entry in hits:
                if p == want and (entry.get("cost") or {}).get("input"):
                    return self._rates(entry["cost"], p, model)
        for p, entry in hits:
            # A subscription gateway lists $0, which answers a different question.
            if (entry.get("cost") or {}).get("input"):
                return self._rates(entry["cost"], p, model)
        return None

    @staticmethod
    def _rates(cost: dict, provider: str, model: str) -> dict:
        inp = float(cost.get("input") or 0)
        return {
            "provider": provider,
            "input": inp,
            "output": float(cost.get("output") or 0),
            # A cache rate nobody publishes is not a reason to stay silent —
            # the plain input rate is the honest floor.
            "cache_read": float(cost["cache_read"]) if cost.get("cache_read") is not None else inp,
            "cache_write": float(cost["cache_write"]) if cost.get("cache_write") is not None else inp,
            "source": f"{provider}/{model}",
            "via": "models.dev",
        }

    def provider_of(self, provider: str, model: str) -> str:
        rates = self.lookup(provider, model)
        return (rates and rates["provider"]) or provider or "unknown"


def catalogue_path() -> pathlib.Path:
    return CACHE / "models.dev.json"


_CATALOGUE: tuple[float, dict] = (0.0, {})


def load_catalogue(refresh: bool = True) -> dict:
    """The price catalogue, refetched when older than RIG_AI_PRICES_MAX_AGE_DAYS.

    A failed fetch keeps the last good copy. No copy and no network means no
    money figures, which `rig ai doctor` reports rather than papering over.
    Parsed once per process — the file is several megabytes and every view of
    the ledger needs it.
    """
    global _CATALOGUE
    path = catalogue_path()
    fresh = path.is_file() and (time.time() - path.stat().st_mtime) < CATALOGUE_MAX_AGE
    if refresh and not fresh:
        try:
            fetch_catalogue()
        except (OSError, urllib.error.URLError, ValueError):
            pass
    if not path.is_file():
        return {}
    stamp = path.stat().st_mtime
    if _CATALOGUE[0] != stamp:
        try:
            _CATALOGUE = (stamp, json.loads(path.read_text()))
        except (OSError, ValueError):
            return {}
    return _CATALOGUE[1]


def fetch_catalogue() -> int:
    """Download the catalogue. Returns the provider count."""
    # models.dev answers 403 to the default urllib agent.
    req = urllib.request.Request(CATALOGUE_URL, headers={"User-Agent": "rig-telemetry"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    data = json.loads(body)
    if not isinstance(data, dict) or not data:
        raise ValueError("models.dev returned no providers")
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = catalogue_path().with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(catalogue_path())
    return len(data)


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

# Tokens are stored, money is not. A price catalogue that arrives late, or a
# rate that changes, then applies to everything already recorded instead of
# only to what is scanned afterwards.
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  -- `day` is local because a spending day is the one you lived; `utc_slot` is
  -- the seller's hour, the grain a peak/off-peak rate is billed at.
  day TEXT, utc_slot TEXT,
  harness TEXT, provider TEXT, model TEXT, project TEXT, kind TEXT,
  path TEXT,
  input INTEGER DEFAULT 0, output INTEGER DEFAULT 0,
  cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
  reasoning INTEGER DEFAULT 0, requests INTEGER DEFAULT 0,
  reported_cost REAL DEFAULT 0,
  PRIMARY KEY (day, utc_slot, harness, provider, model, project, kind, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
CREATE INDEX IF NOT EXISTS usage_path ON usage(path);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER, mtime REAL, offset INTEGER, ctx TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen (uid INTEGER PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
"""


def ledger_path() -> pathlib.Path:
    return CACHE / "ai-usage.db"


SCHEMA_VERSION = "4"


def open_ledger(read_only: bool = False) -> sqlite3.Connection:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = ledger_path()
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        return db
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    # The ledger is derived from files that are all still there, so a shape
    # change rebuilds rather than migrates. It costs one full rescan.
    if _schema_of(db) != SCHEMA_VERSION:
        for table in ("usage", "files", "seen", "meta"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
    db.executescript(SCHEMA)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
    return db


def _schema_of(db) -> str:
    try:
        row = db.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
    except sqlite3.Error:
        return ""
    return row["v"] if row else ""


def _uid(text: str) -> int:
    """A 63-bit key for a record that two files can both contain."""
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big") >> 1


def ledger_key(path: pathlib.Path) -> str:
    """A file's identity in the ledger, independent of where HOME is mounted.

    The exporter container reaches the same file at /host/home/... that the
    host reaches at /home/you/..., and both write this one ledger. Keyed by the
    absolute path, each would read the file as one the other had never seen,
    and every harness with no per-record id to deduplicate on would be counted
    exactly twice — a doubling that stays perfectly consistent, so it reads as
    real usage rather than as a fault.
    """
    try:
        return str(path.relative_to(HOME))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

class Source:
    """One harness. Subclasses yield Rec from the files they own.

    `parse` receives a byte offset and returns the offset it stopped at, so a
    growing log is read once. `rescan` sources are small summary files that are
    rewritten rather than appended to, and are re-read whole every pass.
    """
    name = "?"
    home_hint = ""
    rescan = False
    # Two sources can own one file — opencode and opencode2 both read
    # opencode.db, but different tables in it. The `files` table keys by
    # path, so a source that shares a path with another appends this to the
    # ledger key to keep its own offset.
    ledger_suffix = ""

    def files(self) -> list[pathlib.Path]:
        return []

    def parse(self, path: pathlib.Path, offset: int, ctx: dict):
        raise NotImplementedError

    def gauges(self) -> list[tuple[str, dict[str, str], float]]:
        return []

    def installed(self) -> bool:
        return bool(self.home_hint) and (HOME / self.home_hint).exists()

    # -- helpers shared by the line-oriented sources

    @staticmethod
    def _lines(path: pathlib.Path, offset: int, needle: str):
        """Yield (json, new_offset) for lines past `offset` that hold `needle`.

        A partial trailing line is left unread; the next pass starts at it.
        """
        with path.open("rb") as fh:
            if offset:
                fh.seek(offset)
            pos = offset
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                pos += len(raw)
                if needle and needle.encode() not in raw:
                    continue
                try:
                    yield json.loads(raw), pos
                except (ValueError, UnicodeDecodeError):
                    continue
            yield None, pos

    @staticmethod
    def _frames(path: pathlib.Path, offset: int, needle: str):
        """Yield (json, new_offset) for lines in the whole zstd frames past `offset`.

        A harness that compresses as it appends writes one frame per append, so
        the offset every other source keeps as a byte position is a frame
        boundary here and resumes the same way. A trailing frame still being
        written is left unread; the next pass starts at it.
        """
        if ZstdDecompressor is None:
            raise ValueError("compressed transcript needs Python 3.14 for compression.zstd")
        with path.open("rb") as fh:
            fh.seek(offset)
            buf = fh.read()
        pos = offset
        while buf:
            decoder = ZstdDecompressor()
            try:
                block = decoder.decompress(buf)
            except ZstdError as why:
                raise ValueError(f"zstd frame at byte {pos}: {why}") from why
            if not decoder.eof:
                break
            pos += len(buf) - len(decoder.unused_data)
            buf = decoder.unused_data
            for raw in block.splitlines():
                if needle and needle.encode() not in raw:
                    continue
                try:
                    yield json.loads(raw), pos
                except (ValueError, UnicodeDecodeError):
                    continue
        yield None, pos

    @staticmethod
    def _iso(text: str | None) -> float:
        if not text:
            return time.time()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return time.time()

    @staticmethod
    def _project(cwd: str | None) -> str:
        if not cwd:
            return "unknown"
        return pathlib.PurePath(cwd).name or "unknown"


class ClaudeCode(Source):
    """~/.claude/projects/<slug>/<session>.jsonl, one line per API response.

    Anthropic splits cache reads and writes out of `input_tokens`, so the
    counts are already exclusive. Subagents write beside their parent under
    `subagents/`, and a forked session copies the messages it inherited — hence
    the message id is deduplicated.
    """
    name = "claude-code"
    home_hint = ".claude"

    def files(self):
        return sorted((HOME / ".claude" / "projects").glob("**/*.jsonl"))

    def parse(self, path, offset, ctx):
        sub = "subagents" in path.parts
        for obj, pos in self._lines(path, offset, '"usage"'):
            if obj is None:
                return pos
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            model = msg.get("model") or ""
            if not isinstance(u, dict) or not model or model.startswith("<"):
                continue
            cc = u.get("cache_creation") or {}
            yield Rec(
                ts=self._iso(obj.get("timestamp")),
                harness=self.name,
                model=model,
                project=self._project(obj.get("cwd")),
                kind="subagent" if sub or obj.get("isSidechain") else "main",
                input=int(u.get("input_tokens") or 0),
                output=int(u.get("output_tokens") or 0),
                cache_read=int(u.get("cache_read_input_tokens") or 0),
                cache_write=int(u.get("cache_creation_input_tokens")
                               or (cc.get("ephemeral_1h_input_tokens", 0)
                                   + cc.get("ephemeral_5m_input_tokens", 0))),
                reasoning=int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
                uid=f"{msg.get('id')}:{obj.get('requestId')}",
            )

    def gauges(self):
        return _live_sessions(self.name, (HOME / ".claude" / "sessions").glob("*.json"),
                              lambda d: (d.get("cwd"), d.get("status")))


class Codex(Source):
    """~/.codex/sessions/**/rollout-*.jsonl, OpenAI convention.

    `cached_input_tokens` is a subset of `input_tokens` and reasoning is a
    subset of output, so both are subtracted out to reach the exclusive form.
    The rollout also carries the plan's rate-limit windows; `harness_quota.py`
    reads those, because a plan is metered by the seller rather than counted here.
    """
    name = "codex"
    home_hint = ".codex"

    def files(self):
        return sorted((HOME / ".codex" / "sessions").glob("**/*.jsonl"))

    def parse(self, path, offset, ctx):
        for obj, pos in self._lines(path, offset, ""):
            if obj is None:
                return pos
            payload = obj.get("payload") or {}
            # The rollout tags session and turn records on the envelope but
            # every event_msg on the payload, so both have to be consulted.
            kind = obj.get("type") if obj.get("type") != "event_msg" else payload.get("type")
            if kind == "session_meta":
                ctx["provider"] = payload.get("model_provider") or ""
                ctx["cwd"] = payload.get("cwd") or ctx.get("cwd")
            elif kind == "turn_context":
                ctx["model"] = payload.get("model") or ctx.get("model")
                ctx["cwd"] = payload.get("cwd") or ctx.get("cwd")
            elif kind == "token_count":
                rec = self._usage(obj, payload, ctx)
                if rec:
                    yield rec

    def _usage(self, obj, payload, ctx) -> Rec | None:
        info = payload.get("info") or {}
        last = info.get("last_token_usage")
        if not last:
            # Older rollouts only carry the running total. Difference it.
            total = info.get("total_token_usage") or {}
            prev = ctx.get("total") or {}
            last = {k: int(total.get(k, 0)) - int(prev.get(k, 0)) for k in total}
        ctx["total"] = info.get("total_token_usage") or ctx.get("total")
        cached = int(last.get("cached_input_tokens") or 0)
        fresh = max(0, int(last.get("input_tokens") or 0) - cached)
        out = int(last.get("output_tokens") or 0)
        if not (fresh or cached or out):
            return None
        return Rec(
            ts=self._iso(obj.get("timestamp")),
            harness=self.name,
            model=ctx.get("model") or "",
            project=self._project(ctx.get("cwd")),
            provider=ctx.get("provider") or "",
            input=fresh,
            output=out,
            cache_read=cached,
            reasoning=int(last.get("reasoning_output_tokens") or 0),
        )


class KimiCode(Source):
    """~/.kimi-code/sessions/<wd>/<session>/agents/<agent>/wire.jsonl.

    One `usage.record` per API response, already exclusive. Each subagent gets
    its own wire and the main one carries none of those rows, so the sibling
    directories are read too.
    """
    name = "kimi-code"
    home_hint = ".kimi-code"

    def files(self):
        return sorted((HOME / ".kimi-code" / "sessions").glob("*/*/agents/*/wire.jsonl"))

    def parse(self, path, offset, ctx):
        if "cwd" not in ctx:
            ctx["cwd"] = self._cwd(path)
        agent = path.parent.name
        for obj, pos in self._lines(path, offset, '"usage.record"'):
            if obj is None:
                return pos
            if obj.get("type") != "usage.record":
                continue
            u = obj.get("usage") or {}
            yield Rec(
                ts=float(obj.get("time", 0)) / 1000 or time.time(),
                harness=self.name,
                model=obj.get("model") or "",
                project=self._project(ctx.get("cwd")),
                kind="main" if agent == "main" else "subagent",
                input=int(u.get("inputOther") or 0),
                output=int(u.get("output") or 0),
                cache_read=int(u.get("inputCacheRead") or 0),
                cache_write=int(u.get("inputCacheCreation") or 0),
            )

    @staticmethod
    def _cwd(path: pathlib.Path) -> str:
        state = path.parent.parent.parent / "state.json"
        try:
            cwd = json.loads(state.read_text()).get("cwd")
            if cwd:
                return cwd
        except (OSError, ValueError):
            pass
        # A session with no state file still names its working directory in
        # the `wd_<name>_<hash>` directory it lives in.
        stem = path.parent.parent.parent.parent.name
        return stem[3:].rsplit("_", 1)[0] if stem.startswith("wd_") else ""

    def gauges(self):
        return _live_sessions(self.name, (HOME / ".kimi-code" / "sessions").glob("*/*/state.json"),
                              lambda d: (d.get("cwd"), d.get("lastTurnReason") or "idle"),
                              max_age=3600)


class OpenCode(Source):
    """OpenCode keeps assistant messages twice: JSON files, then SQLite.

    Both are read and the message id deduplicates the overlap. It is one of
    three harnesses here that record what they think a turn cost, which is kept
    beside the recomputed figure rather than instead of it.

    The fork this machine now runs keeps the same messages in a second table,
    `session_message`, keyed by the same message id. Its data shape is the v2
    one — `model` is an object and the fields are flat — so it is read through
    the same path but only the rows that exist in no v1 store.
    """
    name = "opencode"
    home_hint = ".local/share/opencode"

    def files(self):
        root = HOME / ".local" / "share" / "opencode"
        out = sorted((root / "storage" / "message").glob("*/*.json"))
        db = root / "opencode.db"
        if db.is_file():
            out.append(db)
        return out

    def parse(self, path, offset, ctx):
        if path.suffix == ".db":
            return self._from_db(path, offset)
        return self._from_file(path, offset)

    def _from_file(self, path, offset):
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            return path.stat().st_size
        rec = self._rec(obj)
        if rec:
            yield rec
        return path.stat().st_size

    def _from_db(self, path, offset):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
            rows = db.execute(
                "SELECT id, time_created, data FROM message WHERE time_created > ? "
                "ORDER BY time_created", (offset,)).fetchall()
        except sqlite3.Error:
            return offset
        newest = offset
        for mid, created, data in rows:
            newest = max(newest, int(created or 0))
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            obj.setdefault("id", mid)
            rec = self._rec(obj)
            if rec:
                yield rec
        db.close()
        return newest

    @staticmethod
    def _rec(obj) -> Rec | None:
        if obj.get("role") != "assistant":
            return None
        tok = obj.get("tokens") or {}
        cache = tok.get("cache") or {}
        total = sum(int(tok.get(k) or 0) for k in ("input", "output")) + \
            sum(int(cache.get(k) or 0) for k in ("read", "write"))
        if not total:
            return None
        # OpenCode's reasoning count is disjoint from its output count — it
        # prices both together at the output rate, which its own `cost` field
        # confirms. Folding reasoning into output keeps one token priced once
        # and still reported under its own role.
        reasoning = int(tok.get("reasoning") or 0)
        return Rec(
            ts=float((obj.get("time") or {}).get("created", 0)) / 1000 or time.time(),
            harness="opencode",
            model=obj.get("modelID") or "",
            project=Source._project((obj.get("path") or {}).get("root")),
            kind="subagent" if obj.get("parentID") and obj.get("agent") not in (None, "build") else "main",
            provider=obj.get("providerID") or "",
            input=int(tok.get("input") or 0),
            output=int(tok.get("output") or 0) + reasoning,
            cache_read=int(cache.get("read") or 0),
            cache_write=int(cache.get("write") or 0),
            reasoning=reasoning,
            reported_cost=float(obj.get("cost") or 0) or None,
            uid=str(obj.get("id")),
        )


class OpenCode2(Source):
    """The OpenCode 2.0 fork (anomalyco/opencode) reads and writes the same
    `~/.local/share/opencode/opencode.db` as v1, but it is a Go binary with a
    different schema. It keeps messages in `session_message`, keyed by session,
    with the usage in the same exclusive shape as v1 — `tokens.input`,
    `tokens.output`, `tokens.cache.read/write`, `tokens.reasoning` — but with
    `model` as an object and no `role` field. The session's `version` column
    names the fork that wrote it (`0.0.0-*`), so the fork is told apart from
    the id, not guessed. Only assistant messages that no v1 table holds are
    read, so nothing is counted twice.

    The working directory and the delegation kind are session attributes, so
    they are read from `session_v2` rather than from each message.
    """
    name = "opencode2"
    home_hint = ".local/share/opencode"
    # Shares opencode.db with OpenCode, each reading a different table.
    ledger_suffix = "2"

    def files(self):
        db = HOME / ".local" / "share" / "opencode" / "opencode.db"
        return [db] if db.is_file() else []

    def parse(self, path, offset, ctx):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
            # v1 keeps its messages in `message`; the OpenCode source reads those.
            # This fork writes to `session_message`, so only the rows v1 did not
            # also put in `message` are read — otherwise the same response would
            # count once under each harness.
            rows = db.execute(
                "SELECT m.id, m.time_created, m.data, "
                "v.directory, v.agent, (v.parent_id IS NOT NULL AND v.parent_id != '') "
                "FROM session_message m "
                "JOIN session_v2 v ON v.id = m.session_id "
                "WHERE m.type = 'assistant' AND m.time_created > ? "
                "AND NOT EXISTS (SELECT 1 FROM message x WHERE x.id = m.id) "
                "ORDER BY m.time_created", (offset,)).fetchall()
        except sqlite3.Error:
            return offset
        newest = offset
        for mid, created, data, directory, agent, delegated in rows:
            newest = max(newest, int(created or 0))
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            tok = obj.get("tokens")
            if not isinstance(tok, dict):
                continue
            cache = tok.get("cache") or {}
            model = obj.get("model") or {}
            if isinstance(model, str):
                model_id, provider = model, ""
            else:
                model_id, provider = model.get("id") or "", model.get("providerID") or ""
            if not model_id or not isinstance(tok.get("input"), int):
                continue
            reasoning = int(tok.get("reasoning") or 0)
            yield Rec(
                ts=float((obj.get("time") or {}).get("created", 0)) / 1000 or time.time(),
                harness=self.name,
                model=model_id,
                project=Source._project(directory),
                kind="subagent" if delegated and agent not in (None, "build") else "main",
                provider=provider,
                input=int(tok.get("input") or 0),
                output=int(tok.get("output") or 0) + reasoning,
                cache_read=int(cache.get("read") or 0),
                cache_write=int(cache.get("write") or 0),
                reasoning=reasoning,
                reported_cost=float(obj.get("cost") or 0) or None,
                uid=str(mid),
            )
        db.close()
        return newest


class Pi(Source):
    """~/.pi/agent/sessions/<slug>/<stamp>_<id>.jsonl.

    Pi names the provider and the API dialect on every assistant message and
    carries its own cost breakdown, so it needs no model guessing.
    """
    name = "pi"
    home_hint = ".pi"

    def files(self):
        return sorted((HOME / ".pi" / "agent" / "sessions").glob("*/*.jsonl"))

    def parse(self, path, offset, ctx):
        for obj, pos in self._lines(path, offset, ""):
            if obj is None:
                return pos
            if obj.get("type") == "session":
                ctx["cwd"] = obj.get("cwd") or ctx.get("cwd")
                continue
            msg = obj.get("message") or {}
            u = msg.get("usage") or {}
            if msg.get("role") != "assistant" or not u.get("totalTokens"):
                continue
            yield Rec(
                ts=self._iso(obj.get("timestamp")),
                harness=self.name,
                model=msg.get("model") or "",
                project=self._project(ctx.get("cwd")),
                provider=msg.get("provider") or "",
                input=int(u.get("input") or 0),
                output=int(u.get("output") or 0),
                cache_read=int(u.get("cacheRead") or 0),
                cache_write=int(u.get("cacheWrite") or 0),
                reported_cost=float((u.get("cost") or {}).get("total") or 0) or None,
            )


class Qwen(Source):
    """~/.qwen/usage_record.jsonl, one line per session, rewritten as it runs.

    Gemini convention: cached tokens are inside the prompt count and thought
    tokens are inside the candidate count. The file is small and a session is
    written more than once, so it is re-read whole and the last line for a
    session wins.
    """
    name = "qwen-code"
    home_hint = ".qwen"
    rescan = True

    def files(self):
        path = HOME / ".qwen" / "usage_record.jsonl"
        return [path] if path.is_file() else []

    def parse(self, path, offset, ctx):
        sessions: dict[str, list[Rec]] = {}
        for obj, pos in self._lines(path, 0, '"models"'):
            if obj is None:
                break
            sid = obj.get("sessionId") or ""
            ts = float(obj.get("timestamp") or 0) / 1000 or time.time()
            project = self._project(obj.get("project"))
            out = []
            for model, m in (obj.get("models") or {}).items():
                cached = int(m.get("cachedTokens") or 0)
                out.append(Rec(
                    ts=ts, harness=self.name, model=model, project=project,
                    input=max(0, int(m.get("inputTokens") or 0) - cached),
                    output=int(m.get("outputTokens") or 0),
                    cache_read=cached,
                    reasoning=int(m.get("thoughtsTokens") or 0),
                    requests=int(m.get("requests") or 1),
                ))
            if out:
                sessions[sid] = out
        for recs in sessions.values():
            yield from recs
        return path.stat().st_size


class GeminiCli(Source):
    """~/.gemini/tmp/<hash>/chats/*.json checkpoints, when they carry usage.

    Gemini CLI only persists `usageMetadata` on saved checkpoints, so an
    installed CLI with no saved chats reports zero files rather than zero cost.
    """
    name = "gemini-cli"
    home_hint = ".gemini"
    rescan = True

    def files(self):
        root = HOME / ".gemini" / "tmp"
        return sorted(p for p in root.glob("*/chats/*.json")) if root.is_dir() else []

    def parse(self, path, offset, ctx):
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        turns = obj if isinstance(obj, list) else obj.get("history") or []
        model = obj.get("model") if isinstance(obj, dict) else ""
        stamp = path.stat().st_mtime
        for turn in turns:
            u = (turn or {}).get("usageMetadata") if isinstance(turn, dict) else None
            if not u:
                continue
            cached = int(u.get("cachedContentTokenCount") or 0)
            yield Rec(
                ts=stamp, harness=self.name,
                model=turn.get("model") or model or "gemini-2.5-pro",
                project=path.parent.parent.name,
                input=max(0, int(u.get("promptTokenCount") or 0) - cached),
                output=int(u.get("candidatesTokenCount") or 0),
                cache_read=cached,
                reasoning=int(u.get("thoughtsTokenCount") or 0),
            )
        return path.stat().st_size


class Goose(Source):
    """~/.local/share/goose/sessions/*.jsonl, usage on the assistant messages."""
    name = "goose"
    home_hint = ".local/share/goose"

    def files(self):
        root = HOME / ".local" / "share" / "goose" / "sessions"
        return sorted(root.glob("*.jsonl")) if root.is_dir() else []

    def parse(self, path, offset, ctx):
        for obj, pos in self._lines(path, offset, "oken"):
            if obj is None:
                return pos
            if obj.get("working_dir"):
                ctx["cwd"] = obj["working_dir"]
            u = obj.get("usage") or obj.get("token_usage") or {}
            if not isinstance(u, dict):
                continue
            inp = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
            out = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
            if not (inp or out):
                continue
            yield Rec(
                ts=self._iso(obj.get("created") or obj.get("timestamp")),
                harness=self.name,
                model=obj.get("model") or u.get("model") or "",
                project=self._project(ctx.get("cwd")),
                input=inp, output=out,
                cache_read=int(u.get("cache_read_input_tokens") or 0),
                cache_write=int(u.get("cache_creation_input_tokens") or 0),
            )


class Crush(Source):
    """~/.local/share/crush/crush.db, one row per assistant message."""
    name = "crush"
    home_hint = ".local/share/crush"

    def files(self):
        path = HOME / ".local" / "share" / "crush" / "crush.db"
        return [path] if path.is_file() else []

    def parse(self, path, offset, ctx):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM messages WHERE created_at > ? ORDER BY created_at",
                (offset,)).fetchall()
        except sqlite3.Error:
            return offset
        newest = offset
        for row in rows:
            keys = row.keys()
            newest = max(newest, int(row["created_at"] or 0))
            inp = int(row["input_tokens"] or 0) if "input_tokens" in keys else 0
            out = int(row["output_tokens"] or 0) if "output_tokens" in keys else 0
            if not (inp or out):
                continue
            yield Rec(
                ts=float(row["created_at"] or 0) or time.time(),
                harness=self.name,
                model=row["model"] if "model" in keys else "",
                project="unknown",
                provider=row["provider"] if "provider" in keys else "",
                input=inp, output=out,
                cache_read=int(row["cache_read_tokens"] or 0) if "cache_read_tokens" in keys else 0,
                cache_write=int(row["cache_creation_tokens"] or 0) if "cache_creation_tokens" in keys else 0,
            )
        db.close()
        return newest


class CopilotCli(Source):
    """~/.copilot/history-session-state/*.json, usage on the model responses."""
    name = "copilot-cli"
    home_hint = ".copilot"
    rescan = True

    def files(self):
        root = HOME / ".copilot" / "history-session-state"
        return sorted(root.glob("*.json")) if root.is_dir() else []

    def parse(self, path, offset, ctx):
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        stamp = path.stat().st_mtime
        for turn in _walk_usage(obj):
            u, model = turn
            cached = int(u.get("cached_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            inp = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
            yield Rec(
                ts=stamp, harness=self.name, model=model or "",
                project=self._project(obj.get("cwd") if isinstance(obj, dict) else None),
                input=max(0, inp - cached),
                output=int(u.get("completion_tokens") or u.get("output_tokens") or 0),
                cache_read=cached,
            )
        return path.stat().st_size


class Droid(Source):
    """~/.factory/sessions/*.jsonl, Factory's droid CLI."""
    name = "droid"
    home_hint = ".factory"

    def files(self):
        root = HOME / ".factory" / "sessions"
        return sorted(root.glob("**/*.jsonl")) if root.is_dir() else []

    def parse(self, path, offset, ctx):
        for obj, pos in self._lines(path, offset, "oken"):
            if obj is None:
                return pos
            if obj.get("cwd"):
                ctx["cwd"] = obj["cwd"]
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            u = msg.get("usage") or {}
            if not isinstance(u, dict) or not u:
                continue
            inp = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
            out = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
            if not (inp or out):
                continue
            yield Rec(
                ts=self._iso(obj.get("timestamp")),
                harness=self.name,
                model=msg.get("model") or "",
                project=self._project(ctx.get("cwd")),
                input=inp, output=out,
                cache_read=int(u.get("cache_read_input_tokens") or 0),
                cache_write=int(u.get("cache_creation_input_tokens") or 0),
            )


class Dsh(Source):
    """~/.dsh/sessions/<slug>/session-<id>/session.jsonl.zstd, DeepSeek's harness.

    The transcript is compressed as it is appended, one zstd frame per write,
    and every frame ends on a line. `inputTokens` already excludes
    `cacheReadTokens`, so the counts arrive exclusive; DeepSeek bills a cache
    miss at the plain input rate and has no cache-write role to report. The
    same usage figures appear on the streaming chunk and on the assembled
    message, and only the message is read.
    """
    name = "dsh"
    home_hint = ".dsh"

    def files(self):
        root = HOME / ".dsh" / "sessions"
        return sorted(root.glob("*/*/session.jsonl.zstd")) if root.is_dir() else []

    def parse(self, path, offset, ctx):
        if "cwd" not in ctx:
            ctx.update(self._header(path))
        for obj, pos in self._frames(path, offset, '"usage"'):
            if obj is None:
                return pos
            if obj.get("type") != "assistant/message":
                continue
            data = obj.get("data") or {}
            u = data.get("usage") or {}
            source = (data.get("message") or {}).get("source") or {}
            if not isinstance(u, dict) or source.get("kind") != "model":
                continue
            yield Rec(
                ts=float(obj.get("time") or 0) / 1000 or time.time(),
                harness=self.name,
                model=source.get("model") or "",
                project=self._project(ctx.get("cwd")),
                kind="subagent" if ctx.get("depth") else "main",
                provider=source.get("provider") or "",
                input=int(u.get("inputTokens") or 0),
                output=int(u.get("outputTokens") or 0),
                cache_read=int(u.get("cacheReadTokens") or 0),
                reasoning=int(u.get("reasoningTokens") or 0),
            )

    @staticmethod
    def _header(path: pathlib.Path) -> dict:
        """The working directory and delegation depth, from the first record."""
        for obj, _ in Source._frames(path, 0, ""):
            if obj is None or obj.get("type") != "session":
                break
            return {"cwd": obj.get("cwd") or "",
                    "depth": int(obj.get("delegationDepth") or 0)}
        return {"cwd": "", "depth": 0}


def _walk_usage(obj, model=""):
    """Every `usage` dict in a nested structure, with the nearest model name."""
    if isinstance(obj, dict):
        model = obj.get("model") or obj.get("modelId") or model
        u = obj.get("usage")
        if isinstance(u, dict) and any(k for k in u if "token" in k):
            yield u, model
        for v in obj.values():
            yield from _walk_usage(v, model)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_usage(v, model)


def _live_sessions(harness, paths, extract, max_age=900):
    """A gauge of sessions whose state file moved recently, by project."""
    counts: dict[tuple[str, str], int] = {}
    now = time.time()
    for path in paths:
        try:
            if now - path.stat().st_mtime > max_age:
                continue
            cwd, status = extract(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            continue
        key = (pathlib.PurePath(cwd or "unknown").name or "unknown", str(status or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return [("aiusage_sessions_live",
             {"harness": harness, "project": p, "status": s}, float(n))
            for (p, s), n in counts.items()]


SOURCES: list[type[Source]] = [
    ClaudeCode, Codex, KimiCode, OpenCode, OpenCode2, Pi, Qwen, Dsh,
    GeminiCli, Goose, Crush, CopilotCli, Droid,
]


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

def scan(db: sqlite3.Connection, prices: Prices, only: str = "") -> Scan:
    """Read what is new on disk into the ledger. Safe to call repeatedly."""
    started = time.time()
    result = Scan()
    for cls in SOURCES:
        src = cls()
        if only and src.name != only:
            continue
        found = 0
        for path in src.files():
            try:
                found += 1
                result.records += _scan_file(db, prices, src, path)
            except FileNotFoundError:
                # A live session rotated the file between the glob and the
                # open. Nothing failed; there is simply nothing there now.
                found -= 1
            except (OSError, sqlite3.Error, ValueError) as e:
                result.failed(str(path), e)
        result.files[src.name] = found
        try:
            result.gauges.extend(src.gauges())
        except (OSError, ValueError) as e:
            result.failed(f"{src.name} gauges", e)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('scanned', ?)", (str(time.time()),))
    db.commit()
    result.seconds = time.time() - started
    return result


def _scan_file(db, prices, src: Source, path: pathlib.Path) -> int:
    """Read one file's new bytes into the ledger, as one atomic unit.

    The exporter container and `rig ai scan` on the host share one ledger, so
    reading a file's offset and appending the rows it produced have to be one
    transaction: two scanners that both read offset N before either writes
    would both add the same bytes. BEGIN IMMEDIATE takes the write lock up
    front, so the second waits and then finds nothing new.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        count = _scan_locked(db, prices, src, path)
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return count


def _scan_locked(db, prices, src: Source, path: pathlib.Path) -> int:
    key = ledger_key(path) + (src.ledger_suffix or "")
    stat = path.stat()
    row = db.execute("SELECT size, mtime, offset, ctx FROM files WHERE path = ?", (key,)).fetchone()
    offset = int(row["offset"]) if row else 0
    ctx = json.loads(row["ctx"]) if row and row["ctx"] else {}

    if row and stat.st_size == row["size"] and stat.st_mtime == row["mtime"]:
        return 0
    if src.rescan or stat.st_size < (row["size"] if row else 0):
        # Rewritten rather than appended to: drop what this file contributed
        # before reading it again, or the rows would count twice.
        db.execute("DELETE FROM usage WHERE path = ?", (key,))
        offset, ctx = 0, {}

    rows: dict[tuple, list[float]] = {}
    count = 0
    gen = src.parse(path, offset, ctx)
    while True:
        try:
            rec = next(gen)
        except StopIteration as stop:
            offset = stop.value if isinstance(stop.value, (int, float)) else stat.st_size
            break
        if rec.uid is not None:
            uid = _uid(f"{src.name}:{rec.uid}")
            if db.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,)).fetchone():
                continue
            db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (uid,))
        rec.provider = prices.provider_of(rec.provider, rec.model)
        k = (rec.day(), rec.utc_slot(), rec.harness, rec.provider,
             rec.model or "unknown", rec.project, rec.kind)
        acc = rows.setdefault(k, [0.0] * 7)
        for i, name in enumerate(TOKEN_ROLES):
            acc[i] += getattr(rec, name)
        acc[5] += rec.requests
        acc[6] += rec.reported_cost or 0.0
        count += 1

    for k, acc in rows.items():
        db.execute("""
            INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (day, utc_slot, harness, provider, model, project, kind, path)
            DO UPDATE SET
              input = input + excluded.input, output = output + excluded.output,
              cache_read = cache_read + excluded.cache_read,
              cache_write = cache_write + excluded.cache_write,
              reasoning = reasoning + excluded.reasoning,
              requests = requests + excluded.requests,
              reported_cost = reported_cost + excluded.reported_cost
        """, (*k, key, *[int(v) for v in acc[:6]], acc[6]))
    db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
               (key, stat.st_size, stat.st_mtime, int(offset), json.dumps(ctx)))
    return count


# --------------------------------------------------------------------------
# reading the ledger
# --------------------------------------------------------------------------

SUM_COLUMNS = ("input", "output", "cache_read", "cache_write", "reasoning",
               "requests", "reported_cost")
BILLED_ROLES = ("input", "output", "cache_read", "cache_write")
COST_COLUMNS = ("cost", *(f"cost_{role}" for role in BILLED_ROLES))


def totals(db, group: tuple[str, ...] = (), since: str = "", until: str = "",
           where: dict[str, str] | None = None, prices: "Prices | None" = None) -> list[dict]:
    """Sum the ledger, grouped by whichever label columns are asked for.

    The SQL always groups by provider, model and the UTC hour as well, because
    that is what a rate is looked up by; the result is priced and then folded
    down to the grouping that was asked for. Every row carries `cost` and a
    `cost_<role>` per billed role, so nobody downstream has to re-apply a rate
    to a sum whose hours have already been folded together.
    """
    prices = prices if prices is not None else Prices(load_catalogue(refresh=False))
    inner = tuple(dict.fromkeys(group + ("provider", "model", "utc_slot")))
    cols = ", ".join(inner)
    sums = ", ".join(f"SUM({c}) AS {c}" for c in SUM_COLUMNS)
    sql = f"SELECT {cols}, {sums} FROM usage"
    clauses, params = [], []
    if since:
        clauses.append("day >= ?")
        params.append(since)
    if until:
        clauses.append("day <= ?")
        params.append(until)
    for k, v in (where or {}).items():
        clauses.append(f"{k} = ?")
        params.append(v)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" GROUP BY {cols}"

    folded: dict[tuple, dict] = {}
    for raw in db.execute(sql, params):
        row = dict(raw)
        rates = prices.lookup(row["provider"], row["model"], slot_instant(row["utc_slot"]))
        row["cost"] = 0.0
        for role in BILLED_ROLES:
            row[f"cost_{role}"] = row[role] * rates[role] / 1e6 if rates else 0.0
            row["cost"] += row[f"cost_{role}"]
        # The stored provider is what was known when the row was written. An
        # override added since can name it properly, and rows that were split
        # across the two spellings fold back together here.
        row["provider"] = (rates and rates["provider"]) or row["provider"] or "unknown"
        key = tuple(row[k] for k in group)
        acc = folded.get(key)
        if acc is None:
            folded[key] = {**{k: row[k] for k in group},
                           **{c: row[c] for c in COST_COLUMNS + SUM_COLUMNS}}
        else:
            for c in COST_COLUMNS + SUM_COLUMNS:
                acc[c] += row[c]
    return sorted(folded.values(), key=lambda r: -r["cost"]) if group else \
        list(folded.values()) or [dict.fromkeys(COST_COLUMNS + SUM_COLUMNS, 0)]


def day_bounds(db) -> tuple[str, str]:
    row = db.execute("SELECT MIN(day) a, MAX(day) b FROM usage").fetchone()
    return (row["a"] or "", row["b"] or "")


def unpriced(db, prices: "Prices | None" = None) -> list[dict]:
    """Models carrying tokens but no price. The honest gap."""
    prices = prices if prices is not None else Prices(load_catalogue(refresh=False))
    out = []
    for row in totals(db, ("harness", "provider", "model"), prices=prices):
        tokens = sum(row[r] for r in BILLED_ROLES)
        if tokens and not prices.lookup(row["provider"], row["model"]):
            out.append({"harness": row["harness"], "provider": row["provider"],
                        "model": row["model"], "tokens": tokens})
    return sorted(out, key=lambda r: -r["tokens"])
