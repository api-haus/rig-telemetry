#!/usr/bin/env python3
"""What a subscription has left, asked of the seller rather than counted here.

`harness_usage.py` counts tokens out of transcripts and prices them, which
answers "what was this worth". It cannot answer "how much of my plan is gone":
a plan is a timed window metered on the seller's side, against every device the
account is signed in on. Only the seller knows the figure, so this module asks
it, with the credential the harness already holds.

**Read-only, always.** Each credential file is owned by its harness and a
refresh rotates it, so writing one back would sign a live session out. An
expired token is reported as expired and the last good figure is kept.

    tools/rig ai limits

To teach the stack a new subscription, add a `Subscription` subclass and list
it in `SUBSCRIPTIONS`. Caching, staleness, the metrics and the CLI come free.
Read docs/ai-usage.md for what each window means and where the figures stop.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime

from harness_usage import CACHE, HOME

TIMEOUT = float(os.environ.get("RIG_AI_QUOTA_TIMEOUT", "10"))
MAX_AGE = float(os.environ.get("RIG_AI_QUOTA_MAX_AGE", "300"))

SESSION_SECONDS = 5 * 3600
WEEKLY_SECONDS = 7 * 86400


class Unreadable(Exception):
    """Why a plan could not be read, phrased as what to do about it."""


@dataclass
class Window:
    """One metered window of a plan, as the seller reports it."""
    label: str
    used: float                      # 0..1
    resets_at: float | None = None   # epoch seconds, None when the seller says nothing
    seconds: float | None = None     # declared length, None when the seller declares none


@dataclass
class Quota:
    """One subscription at one moment."""
    harness: str
    plan: str = "unknown"
    # `provider` was asked of the seller now; `harness` is what the harness
    # itself last wrote to disk, which is only as fresh as its last run.
    source: str = "provider"
    measured: float = 0.0            # when the figures were true
    fetched: float = 0.0             # when this reading was taken
    windows: list[Window] = field(default_factory=list)
    error: str = ""
    stale: bool = False              # kept from an earlier pass because this one failed


def slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.strip().lower())
    return "-".join(part for part in out.split("-") if part)


def span_name(seconds: float) -> str:
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= div:
            return f"{round(seconds / div, 1):g}{unit}"
    return f"{round(seconds):g}s"


def name_window(seconds: float | None, fallback: str) -> str:
    """Name a window by the length the seller declared for it.

    Codex and Kimi both declare a length and neither names it, so the length is
    the only thing that says which window this is. A minute of tolerance covers
    the drift seen in older Codex buckets without swallowing other durations.
    """
    if seconds is None:
        return fallback
    if abs(seconds - SESSION_SECONDS) <= 60:
        return "session"
    if abs(seconds - WEEKLY_SECONDS) <= 60:
        return "weekly"
    return span_name(seconds)


def epoch(value) -> float | None:
    """Seconds, from an ISO string, a seconds epoch or a milliseconds epoch."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # 1e11 sits past any plausible seconds epoch and short of any ms one.
        return float(value) / 1000 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def number(value) -> float | None:
    """A count the seller may send as a string."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get(url: str, token: str, headers: dict[str, str], what: str) -> dict:
    """One GET with a bearer token, with every failure named in one sentence."""
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": f"Bearer {token}", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as why:
        if why.code in (401, 403):
            raise Unreadable(f"{what} refused the token (HTTP {why.code}); sign in again") from why
        raise Unreadable(f"{what} answered HTTP {why.code}") from why
    except urllib.error.URLError as why:
        raise Unreadable(f"{what} unreachable: {why.reason}") from why
    except (ValueError, TimeoutError) as why:
        raise Unreadable(f"{what} sent no readable usage: {why}") from why


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------

class Subscription:
    """One plan, read through the credential its harness already keeps.

    `name` matches the harness name in `harness_usage.SOURCES`, so a window and
    the tokens spent inside it carry the same label and join on it.
    """
    name = "?"
    home_hint = ""

    def installed(self) -> bool:
        return bool(self.home_hint) and (HOME / self.home_hint).exists()

    def read(self) -> Quota:
        raise NotImplementedError

    def credentials(self, path: pathlib.Path) -> dict:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as why:
            raise Unreadable(f"not signed in; no {path.name}") from why
        except (OSError, ValueError) as why:
            raise Unreadable(f"{path.name} is unreadable: {why}") from why

    def fresh(self, token: str | None, expires_at: float | None, refresh_with: str) -> str:
        """The access token, or why it cannot be used.

        Never refresh it here. The harness owns the token lifecycle and a
        refresh rotates the refresh token with it, which signs out whatever
        session is holding the old one.
        """
        if not token:
            raise Unreadable("not signed in; the credential file holds no access token")
        if expires_at is not None and expires_at - time.time() <= 5:
            gone = span_name(max(0, time.time() - expires_at))
            raise Unreadable(f"token expired {gone} ago; run {refresh_with} to refresh it")
        return token


class ClaudeCode(Subscription):
    """Anthropic's OAuth usage endpoint, the one Claude Code's own /usage reads.

    The response carries a `limits` array of named windows: the 5-hour session,
    the whole week, and a weekly window scoped to one model — Fable has its own
    share of the same week. The label carries the model the seller named, so a
    new scoped model appears on its own rather than falling out of a list here.
    """
    name = "claude-code"
    home_hint = ".claude"
    url = "https://api.anthropic.com/api/oauth/usage"
    # Both headers are what the CLI sends; the endpoint is gated on the beta.
    headers = {"anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"}
    kinds = {"session": ("session", SESSION_SECONDS), "weekly_all": ("weekly", WEEKLY_SECONDS)}

    def read(self) -> Quota:
        creds = self.credentials(HOME / ".claude" / ".credentials.json").get("claudeAiOauth") or {}
        token = self.fresh(creds.get("accessToken"), epoch(creds.get("expiresAt")), "claude")
        data = get(self.url, token, self.headers, "Anthropic")
        windows = self._structured(data) or self._named(data)
        if not windows:
            raise Unreadable("the account has no metered plan window")
        return Quota(harness=self.name, plan=str(creds.get("subscriptionType") or "unknown"),
                     measured=time.time(), windows=windows)

    def _structured(self, data: dict) -> list[Window]:
        out = []
        for limit in data.get("limits") or []:
            percent = number(limit.get("percent"))
            if percent is None:
                continue
            label, seconds = self.kinds.get(limit.get("kind") or "", (None, None))
            if label is None:
                model = ((limit.get("scope") or {}).get("model") or {}).get("display_name") or ""
                group = limit.get("group") or limit.get("kind") or "limit"
                label = f"{slug(group)}-{slug(model)}" if model else slug(group)
                seconds = WEEKLY_SECONDS if str(limit.get("group")) == "weekly" else None
            out.append(Window(label, percent / 100, epoch(limit.get("resets_at")), seconds))
        return out

    def _named(self, data: dict) -> list[Window]:
        """Older responses carry the two windows as named blocks instead."""
        out = []
        for key, label, seconds in (("five_hour", "session", SESSION_SECONDS),
                                    ("seven_day", "weekly", WEEKLY_SECONDS)):
            block = data.get(key) or {}
            percent = number(block.get("utilization"))
            if percent is None:
                percent = number(block.get("used_percentage"))
            if percent is not None:
                out.append(Window(label, percent / 100, epoch(block.get("resets_at")), seconds))
        return out


class KimiCode(Subscription):
    """Moonshot's managed coding plan, at the endpoint Kimi Code's /usage reads.

    The top-level `usage` block is the plan's week — the CLI labels it "Weekly
    limit" — and each entry under `limits` declares its own shorter window.
    Counts arrive as strings.

    The access token lasts fifteen minutes and the CLI rewrites the file when
    it runs, so an idle machine reports an expired token rather than a figure.
    """
    name = "kimi-code"
    home_hint = ".kimi-code"
    base = os.environ.get("KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1")

    def read(self) -> Quota:
        creds = self.credentials(HOME / ".kimi-code" / "credentials" / "kimi-code.json")
        token = self.fresh(creds.get("access_token"), epoch(creds.get("expires_at")), "kimi")
        data = get(f"{self.base.rstrip('/')}/usages", token, {}, "Moonshot")
        windows = []
        # The plan's own block declares no length, so it carries none. Its name
        # is the CLI's ("Weekly limit"), and its reset time is the hard fact.
        weekly = self._window(data.get("usage"), "weekly", None)
        if weekly:
            windows.append(weekly)
        for entry in data.get("limits") or []:
            seconds = self._seconds(entry.get("window") or {})
            window = self._window(entry.get("detail"), name_window(seconds, "rolling"), seconds)
            if window:
                windows.append(window)
        if not windows:
            raise Unreadable("the plan reports no metered window")
        level = ((data.get("user") or {}).get("membership") or {}).get("level") or ""
        return Quota(harness=self.name, plan=slug(level.replace("LEVEL_", "")) or "unknown",
                     measured=time.time(), windows=windows)

    @staticmethod
    def _seconds(window: dict) -> float | None:
        duration = number(window.get("duration"))
        if duration is None:
            return None
        unit = str(window.get("timeUnit") or "").upper()
        for needle, mult in (("DAY", 86400), ("HOUR", 3600), ("MINUTE", 60), ("SECOND", 1)):
            if needle in unit:
                return duration * mult
        return None

    @staticmethod
    def _window(detail, label: str, seconds: float | None) -> Window | None:
        if not isinstance(detail, dict):
            return None
        limit, used = number(detail.get("limit")), number(detail.get("used"))
        if used is None:
            remaining = number(detail.get("remaining"))
            used = None if remaining is None or limit is None else limit - remaining
        if not limit or limit <= 0 or used is None:
            return None
        reset = detail.get("resetTime") or detail.get("resetAt")
        return Window(label, min(1.0, max(0.0, used / limit)), epoch(reset), seconds)


class Codex(Subscription):
    """The ChatGPT plan behind Codex, or what the last rollout recorded of it.

    Two ways to learn the same windows. The backend endpoint answers now and
    needs `auth.json`, which only a ChatGPT login writes — an API-key Codex has
    none. Failing that, every rollout carries the windows the server returned
    with each response, so the newest one that holds a populated block is what
    the plan looked like when Codex last ran.
    """
    name = "codex"
    home_hint = ".codex"
    url = "https://chatgpt.com/backend-api/wham/usage"
    headers = {"User-Agent": "codex-cli", "OpenAI-Beta": "codex-1", "originator": "Codex Desktop"}

    def read(self) -> Quota:
        try:
            return self._backend()
        except Unreadable as why:
            recorded = self._recorded()
            if recorded:
                return recorded
            raise why

    def _backend(self) -> Quota:
        tokens = self.credentials(HOME / ".codex" / "auth.json").get("tokens") or {}
        token = self.fresh(tokens.get("access_token"), None, "codex")
        headers = dict(self.headers)
        if tokens.get("account_id"):
            headers["ChatGPT-Account-Id"] = str(tokens["account_id"])
        data = get(self.url, token, headers, "ChatGPT")
        limits = data.get("rate_limit") or {}
        windows = []
        for key, fallback in (("primary_window", "session"), ("secondary_window", "weekly")):
            block = limits.get(key) or {}
            used = number(block.get("used_percent"))
            if used is None:
                continue
            seconds = number(block.get("limit_window_seconds"))
            windows.append(Window(name_window(seconds, fallback), used / 100,
                                  epoch(block.get("reset_at")), seconds))
        if not windows:
            raise Unreadable("the account has no metered plan window")
        return Quota(harness=self.name, plan=str(data.get("plan_type") or "unknown"),
                     measured=time.time(), windows=windows)

    def _recorded(self) -> Quota | None:
        found = None
        for path in sorted((HOME / ".codex" / "sessions").glob("**/*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
            for line in self._blocks(path):
                found = line
            if found:
                break
        if not found:
            return None
        when, limits = found
        windows = []
        for key, fallback in (("primary", "session"), ("secondary", "weekly")):
            block = limits.get(key) or {}
            used = number(block.get("used_percent"))
            if used is None:
                continue
            minutes = number(block.get("window_minutes"))
            seconds = None if minutes is None else minutes * 60
            windows.append(Window(name_window(seconds, fallback), used / 100,
                                  epoch(block.get("resets_at")), seconds))
        if not windows:
            return None
        return Quota(harness=self.name, plan=str(limits.get("plan_type") or "unknown"),
                     source="harness", measured=when, windows=windows)

    @staticmethod
    def _blocks(path: pathlib.Path):
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if b'"rate_limits"' not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    limits = (obj.get("payload") or {}).get("rate_limits")
                    if limits and (limits.get("primary") or limits.get("secondary")):
                        yield epoch(obj.get("timestamp")) or 0.0, limits
        except OSError:
            return


SUBSCRIPTIONS: list[type[Subscription]] = [ClaudeCode, Codex, KimiCode]


# --------------------------------------------------------------------------
# cache, so the scrape path and the CLI share one reading
# --------------------------------------------------------------------------

def cache_path() -> pathlib.Path:
    return CACHE / "quota.json"


def load_cache() -> dict[str, Quota]:
    try:
        raw = json.loads(cache_path().read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for name, row in (raw.get("quotas") or {}).items():
        try:
            windows = [Window(**w) for w in row.pop("windows", [])]
            out[name] = Quota(windows=windows, **row)
        except TypeError:
            continue
    return out


def save_cache(quotas: dict[str, Quota]):
    body = {"quotas": {name: asdict(q) for name, q in quotas.items()}}
    tmp = cache_path().with_suffix(".tmp")
    try:
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(body, indent=1))
        tmp.replace(cache_path())
    except OSError:
        pass


def read_all(max_age: float = MAX_AGE, only: str = "") -> list[Quota]:
    """Every subscription on this machine, from the cache or from the seller.

    A seller that refuses now has not unsaid what it said an hour ago, so the
    last good figure is kept and marked stale, with the failure beside it.
    """
    now = time.time()
    cached = load_cache()
    out = []
    for cls in SUBSCRIPTIONS:
        sub = cls()
        if (only and sub.name != only) or not sub.installed():
            continue
        was = cached.get(sub.name)
        if was and not was.error and now - was.fetched < max_age:
            out.append(was)
            continue
        try:
            quota = sub.read()
        except Unreadable as why:
            quota = Quota(harness=sub.name, error=str(why))
            if was and was.windows:
                quota.windows, quota.plan = was.windows, was.plan
                quota.measured, quota.source, quota.stale = was.measured, was.source, True
        quota.fetched = now
        out.append(quota)
    for quota in out:
        # A window whose reset has passed says nothing about the one running
        # now: the seller cleared the counter and nobody here saw the new one.
        live = [w for w in quota.windows if w.resets_at is None or w.resets_at > now]
        if quota.windows and not live and not quota.error:
            quota.error = (f"the last figure is from {span_name(now - quota.measured)} ago "
                           "and every window it named has reset since")
        quota.windows = live
    cached.update({q.harness: q for q in out})
    save_cache(cached)
    return out


def gauges(quotas: list[Quota]) -> list[tuple[str, dict[str, str], float]]:
    """The reading as Prometheus samples, in the exporter's own tuple shape."""
    out = []
    for quota in quotas:
        out.append(("aiusage_rate_limit_readable", {"harness": quota.harness},
                    0 if quota.error else 1))
        if not quota.windows:
            continue
        for window in quota.windows:
            tags = {"harness": quota.harness, "window": window.label,
                    "plan": quota.plan, "source": quota.source}
            out.append(("aiusage_rate_limit_used_ratio", tags, window.used))
            if window.resets_at:
                out.append(("aiusage_rate_limit_reset_timestamp_seconds", tags, window.resets_at))
            if window.seconds:
                out.append(("aiusage_rate_limit_window_seconds", tags, window.seconds))
        out.append(("aiusage_rate_limit_seen_timestamp_seconds",
                    {"harness": quota.harness, "source": quota.source}, quota.measured))
    return out
