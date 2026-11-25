#!/usr/bin/env python3
"""HighFreq Booking: log in with a scripted browser, then claim a slot the
moment it opens, with retries, fallback targets, verification and cleanup.

PUBLIC, REDUCED VERSION. This file contains the complete workflow but not the
timing refinements of the version the author runs. For more information,
contact the repository owner on GitHub: https://github.com/atglauser

Commands:
  plan  Decide whether this run should do anything (fast, no browser).
        Writes `proceed` and `opening_ts` to $GITHUB_OUTPUT.
  run   Log in, wait for the opening and book.

Environment:
  <secrets>    the variables listed under `secrets:` in the config (run only)
  DRY_RUN      "true": log in and check availability, never book
  TARGET_DATE  YYYY-MM-DD: book that date right away (testing)
  OPENING_TS   epoch seconds of the opening to use (set by `plan`, or by hand
               when testing against mock_server.py)

Secrets, cookies, tokens and one-time codes are masked and never logged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

UTC = dt.timezone.utc
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
REQUEST_NAMES = ("session_check", "book", "availability", "my_bookings", "cancel")
LOGIN_ACTIONS = ("goto", "fill", "click", "press", "wait_for", "wait_url", "sleep")

TIMING_DEFAULTS = {
    "precheck_before_s": 120,   # availability of every target this long before the opening
    "retry_every_s": 1.0,       # one attempt per interval after the opening...
    "retry_for_s": 300,         # ...for this long
    "retry_grace_s": 3,         # failures in the first seconds are simply retried
    "check_every_s": 5,         # "did an earlier attempt book after all?" while retrying
    "request_timeout_s": 10,
    "keepalive_every_s": 300,   # session check while waiting for the opening
    "verify_for_s": 30,         # watch my bookings this long after booking
    "cleanup_after_s": 180,     # cancel duplicates this long after the opening
    "login_timeout_s": 180,
}

log = logging.getLogger("booker")


# --------------------------------------------------------------------------- #
# Logging and secret redaction
# --------------------------------------------------------------------------- #

class Redactor(logging.Filter):
    """Replaces every registered secret with *** in log output."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def add(self, value: str | None) -> None:
        if not value or len(value) < 6 or value in self._secrets:
            return
        self._secrets.add(value)
        if os.getenv("GITHUB_ACTIONS") == "true":
            # Tells the runner to mask this value in all further log output.
            print(f"::add-mask::{value}", flush=True)

    def __call__(self, text: str) -> str:
        for s in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self(record.getMessage())
        record.args = None
        if record.exc_info:
            record.msg += "\n" + self("".join(traceback.format_exception(*record.exc_info))).rstrip()
            record.exc_info = None
        return True


redactor = Redactor()


class LocalFormatter(logging.Formatter):
    """Timestamps in the configured time zone, with milliseconds."""
    tz: dt.tzinfo = UTC

    def formatTime(self, record, datefmt=None):
        t = dt.datetime.fromtimestamp(record.created, self.tz)
        return f"{t:%Y-%m-%d %H:%M:%S}.{int(record.msecs):03d} {t.tzname()}"


_formatter = LocalFormatter("%(asctime)s %(levelname)-7s %(message)s")


def setup_logging() -> None:
    if log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_formatter)
    handler.addFilter(redactor)
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class Target:
    name: str
    id: str
    extra: dict = field(default_factory=dict)   # any other keys, usable as {target.<key>}

    def ctx(self) -> dict:
        return {**self.extra, "name": self.name, "id": self.id}


@dataclass
class Config:
    base_url: str
    tz: ZoneInfo
    opens_at: dt.time
    open_days_ahead: int
    weekdays: set[int]
    skip: list[tuple[dt.date, dt.date]]
    gate_minutes: float
    slot_start: dt.time
    slot_end: dt.time
    targets: list[Target]
    secrets: list[str]
    login: dict
    requests: dict
    timing: dict
    vars: dict


class ConfigError(ValueError):
    pass


def _as_date(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def _as_time(v, where: str) -> dt.time:
    if not isinstance(v, str):
        raise ConfigError(f"{where}: write times as quoted strings, e.g. \"09:00\"")
    return dt.time.fromisoformat(v)


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"{path} not found: copy config.example.yaml to {path} and edit it")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not raw.get("base_url"):
        raise ConfigError(f"{path}: base_url is required")
    sched = raw.get("schedule") or {}
    slot = raw.get("slot") or {}

    targets = []
    for t in raw.get("targets") or []:
        if not isinstance(t, dict) or "name" not in t or "id" not in t:
            raise ConfigError(f"{path}: every target needs a name and an id")
        extra = {k: v for k, v in t.items() if k not in ("name", "id")}
        targets.append(Target(str(t["name"]), str(t["id"]), extra))
    if not targets:
        raise ConfigError(f"{path}: list at least one target under `targets`")

    skip = []
    for item in sched.get("skip_dates") or []:
        if isinstance(item, dict):
            skip.append((_as_date(item["from"]), _as_date(item["to"])))
        else:
            d = _as_date(item)
            skip.append((d, d))

    reqs = raw.get("requests") or {}
    for name, spec in reqs.items():
        if name not in REQUEST_NAMES:
            raise ConfigError(f"{path}: unknown request '{name}' (known: {', '.join(REQUEST_NAMES)})")
        if not isinstance(spec, dict) or not spec.get("url"):
            raise ConfigError(f"{path}: requests.{name} needs a url")
    if "book" not in reqs:
        raise ConfigError(f"{path}: requests.book is required")

    timing = dict(TIMING_DEFAULTS)
    for k, v in (raw.get("timing") or {}).items():
        if k not in TIMING_DEFAULTS:
            raise ConfigError(f"{path}: unknown timing setting '{k}'")
        timing[k] = float(v)

    secrets = [str(s) for s in raw.get("secrets") or []]
    bad = [s for s in secrets if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", s)]
    if bad:
        raise ConfigError(f"{path}: secrets must be environment variable names (A-Z, 0-9, _): {bad}")

    login = raw.get("login") or {}
    if not login.get("url"):
        raise ConfigError(f"{path}: login.url is required")
    for i, step in enumerate(login.get("steps") or [], 1):
        if not isinstance(step, dict) or not any(a in step for a in LOGIN_ACTIONS):
            raise ConfigError(f"{path}: login step {i} needs one of: {', '.join(LOGIN_ACTIONS)}")

    return Config(
        base_url=str(raw["base_url"]).rstrip("/"),
        tz=ZoneInfo(str(raw.get("timezone", "UTC"))),
        opens_at=_as_time(sched.get("opens_at", "00:00"), "schedule.opens_at"),
        open_days_ahead=int(sched.get("open_days_ahead", 0)),
        weekdays={WEEKDAYS[str(d).lower()[:3]] for d in (sched.get("weekdays") or WEEKDAYS)},
        skip=skip,
        gate_minutes=float(sched.get("gate_minutes", 100)),
        slot_start=_as_time(slot.get("start", "00:00"), "slot.start"),
        slot_end=_as_time(slot.get("end", "23:59"), "slot.end"),
        targets=targets,
        secrets=secrets,
        login=login,
        requests=reqs,
        timing=timing,
        vars=raw.get("vars") or {},
    )


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #

def skip_reason(cfg: Config, day: dt.date) -> str | None:
    if day.weekday() not in cfg.weekdays:
        return f"{day:%A} is not in schedule.weekdays"
    for a, b in cfg.skip:
        if a <= day <= b:
            return f"in skip_dates ({a} to {b})"
    return None


def next_opening(cfg: Config, now: dt.datetime) -> dt.datetime:
    local = now.astimezone(cfg.tz)
    opening = dt.datetime.combine(local.date(), cfg.opens_at, cfg.tz)
    if opening <= local:
        opening = dt.datetime.combine(local.date() + dt.timedelta(days=1), cfg.opens_at, cfg.tz)
    return opening


def fmt_ts(cfg: Config, ts: float) -> str:
    t = dt.datetime.fromtimestamp(ts, cfg.tz)
    return f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d} {t.tzname()}"


@dataclass
class Plan:
    proceed: bool
    mode: str = ""                       # scheduled | immediate | dry_run
    target: dt.date | None = None
    opening: dt.datetime | None = None
    would_skip: str | None = None


def make_plan(cfg: Config, now: dt.datetime, dry_run: bool,
              target_override: dt.date | None, opening_ts: str | None) -> Plan:
    if target_override:
        reason = skip_reason(cfg, target_override)
        if reason:
            log.warning("Target date %s: %s (ignored for an explicit TARGET_DATE)",
                        target_override, reason)
        mode = "dry_run" if dry_run else "immediate"
        log.info("Target date set by hand: %s (mode: %s)", target_override, mode)
        return Plan(True, mode, target_override)

    if opening_ts:
        opening = dt.datetime.fromtimestamp(float(opening_ts), cfg.tz)
    else:
        opening = next_opening(cfg, now)
    target = opening.date() + dt.timedelta(days=cfg.open_days_ahead)
    minutes = (opening - now).total_seconds() / 60
    log.info("Next opening: %s (%.1f min away); target date: %s",
             opening.isoformat(), minutes, target)

    reason = skip_reason(cfg, target)
    if dry_run:
        if reason:
            log.info("A real run would skip %s: %s", target, reason)
        return Plan(True, "dry_run", target, opening, reason)

    if minutes > cfg.gate_minutes:
        log.info("More than %.0f min before the opening; nothing to do this run.", cfg.gate_minutes)
        return Plan(False)
    if minutes < -1:
        log.info("The opening is already past; nothing to do this run.")
        return Plan(False)
    if reason:
        log.info("Skipping target date %s: %s", target, reason)
        return Plan(False)
    return Plan(True, "scheduled", target, opening)


# --------------------------------------------------------------------------- #
# Secrets and templates
# --------------------------------------------------------------------------- #

class Secrets:
    """Values of the environment variables named in the config. Every value
    (and every one-time code made from one) is registered for redaction."""

    def __init__(self, names: list[str]) -> None:
        self._values: dict[str, str] = {}
        self._last_totp: int | None = None
        missing = [n for n in names if not os.getenv(n, "").strip()]
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        for n in names:
            self.get(n)

    def get(self, name: str) -> str:
        if name not in self._values:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"environment variable {name} is not set")
            redactor.add(value)
            self._values[name] = value
        return self._values[name]

    def totp(self, name: str) -> str:
        """Current one-time code for the base32 secret in `name`. Never reuses
        a 30 s window and never hands out a code about to expire."""
        import pyotp

        totp = pyotp.TOTP(re.sub(r"\s+", "", self.get(name)).upper())
        while True:
            now = time.time()
            counter = int(now // 30)
            remaining = 30 - now % 30
            if remaining < 5 or counter == self._last_totp:
                log.info("Waiting %.0f s for a fresh one-time code", remaining + 0.5)
                time.sleep(remaining + 0.5)
                continue
            self._last_totp = counter
            code = totp.at(now)
            redactor.add(code)
            return code


_PLACEHOLDER = re.compile(
    r"\$\{(?P<totp>totp:)?(?P<env>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\{(?P<ctx>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)\}")


def get_path(data, path: str | None):
    """data["a"]["b"][0] for "a.b.0"; None if any part is missing."""
    if not path:
        return data
    for part in str(path).split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.lstrip("-").isdigit():
            i = int(part)
            data = data[i] if -len(data) <= i < len(data) else None
        else:
            return None
        if data is None:
            return None
    return data


def render(value, ctx: dict, secrets: Secrets | None = None):
    """Fills {name} (run context) and ${ENV} / ${totp:ENV} (secrets) in every
    string of `value`. A string that is exactly one placeholder keeps the
    value's type (numbers and booleans from `vars` or targets)."""
    if isinstance(value, dict):
        return {k: render(v, ctx, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx, secrets) for v in value]
    if not isinstance(value, str):
        return value

    def resolve(m: re.Match):
        if m.group("env"):
            if secrets is None:
                raise ValueError(f"{m.group(0)} cannot be used here")
            name = m.group("env")
            return secrets.totp(name) if m.group("totp") else secrets.get(name)
        v = get_path(ctx, m.group("ctx"))
        if v is None:
            raise ValueError(f"unknown placeholder {m.group(0)}")
        return v

    whole = _PLACEHOLDER.fullmatch(value)
    if whole:
        return resolve(whole)
    return _PLACEHOLDER.sub(lambda m: str(resolve(m)), value)


def context(cfg: Config, day: dt.date | None = None, target: Target | None = None,
            booking: dict | None = None) -> dict:
    ctx = {"base_url": cfg.base_url, "timezone": cfg.tz.key, "vars": cfg.vars}
    if day is not None:
        start = dt.datetime.combine(day, cfg.slot_start, cfg.tz)
        end = dt.datetime.combine(day, cfg.slot_end, cfg.tz)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        ctx.update(date=day.isoformat(),
                   slot_start=start.isoformat(), slot_end=end.isoformat(),
                   slot_start_utc=start.astimezone(UTC).strftime(fmt),
                   slot_end_utc=end.astimezone(UTC).strftime(fmt))
    if target is not None:
        ctx["target"] = target.ctx()
    if booking is not None:
        ctx["booking"] = booking
    return ctx


# --------------------------------------------------------------------------- #
# Client: browser login (Playwright), then plain HTTP (requests)
# --------------------------------------------------------------------------- #

class LoginError(Exception):
    pass


class SessionExpired(Exception):
    pass


def _screen(page) -> str:
    """Secret-free description of the current page, for diagnostics."""
    u = urlparse(page.url)
    try:
        title = page.title()
    except Exception:
        title = "?"
    return redactor(f"host={u.hostname} path={u.path} title={title[:80]!r}")


class Client:
    def __init__(self, cfg: Config, secrets: Secrets) -> None:
        from playwright.sync_api import sync_playwright

        self.cfg = cfg
        self.secrets = secrets
        self.http = None
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(locale="en-US", timezone_id=cfg.tz.key)

    def close(self) -> None:
        for fn in (self._context.close, self._browser.close, self._pw.stop):
            try:
                fn()
            except Exception:
                pass

    def has(self, name: str) -> bool:
        return name in self.cfg.requests

    # ----- login ---------------------------------------------------------- #

    def login(self) -> None:
        """Runs login.steps in the browser, then hands its cookies (and the
        CSRF token, if configured) to a requests session."""
        lg = self.cfg.login
        ctx = context(self.cfg)
        steps = lg.get("steps") or []
        done = re.compile(lg["done_url"]) if lg.get("done_url") else None
        deadline = time.monotonic() + self.cfg.timing["login_timeout_s"]
        log.info("Logging in (%d steps)", len(steps))
        page = self._context.new_page()
        try:
            page.goto(render(lg["url"], ctx), wait_until="domcontentloaded", timeout=60_000)
            for i, step in enumerate(steps, 1):
                if done and done.search(page.url):
                    log.info("Login: already at done_url; remaining steps skipped")
                    break
                if time.monotonic() > deadline:
                    raise LoginError(f"Login took longer than login_timeout_s ({_screen(page)})")
                self._step(page, step, i, len(steps), ctx)
            if done:
                left_ms = max(1000.0, (deadline - time.monotonic()) * 1000)
                try:
                    page.wait_for_url(lambda u: bool(done.search(u)), timeout=left_ms)
                except Exception:
                    raise LoginError(f"Login did not reach done_url ({_screen(page)})") from None
            log.info("Browser login finished (%s)", _screen(page))
            self._open_session(page)
        finally:
            page.close()

    def _step(self, page, step: dict, i: int, n: int, ctx: dict) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        action = next(a for a in LOGIN_ACTIONS if a in step)
        what = step[action]
        optional = bool(step.get("optional"))
        timeout_ms = float(step.get("timeout_s", 5 if optional else 30)) * 1000
        # Logs the selector or URL template only, never a value.
        log.info("Login step %d/%d: %s %s%s", i, n, action, what, " (optional)" if optional else "")
        try:
            if action == "goto":
                page.goto(render(what, ctx), wait_until="domcontentloaded", timeout=timeout_ms)
            elif action == "sleep":
                page.wait_for_timeout(float(what) * 1000)
            elif action == "wait_url":
                rx = re.compile(what)
                page.wait_for_url(lambda u: bool(rx.search(u)), timeout=timeout_ms)
            else:
                el = page.locator(what).first
                el.wait_for(state="visible", timeout=timeout_ms)
                if action == "fill":
                    el.fill(str(render(step.get("value", ""), ctx, self.secrets)))
                elif action == "click":
                    el.click(timeout=timeout_ms)
                elif action == "press":
                    el.press(str(step.get("key", "Enter")))
        except PlaywrightTimeout:
            if optional:
                log.info("  skipped (not there within %.0f s)", timeout_ms / 1000)
                return
            raise LoginError(f"Login step {i} ({action} {what}) timed out ({_screen(page)})") from None

    def _open_session(self, page) -> None:
        import requests

        lg = self.cfg.login
        csrf = lg.get("csrf") or {}
        token = None
        if csrf:
            deadline = time.monotonic() + 15
            while not token and time.monotonic() < deadline:
                if csrf.get("js"):
                    try:
                        token = page.evaluate(f"() => ({csrf['js']})") or None
                    except Exception:
                        token = None
                if not token and csrf.get("cookie"):
                    token = next((c["value"] for c in self._context.cookies()
                                  if c["name"] == csrf["cookie"]), None)
                if not token:
                    page.wait_for_timeout(500)
            if not token:
                raise LoginError("CSRF token not found (see login.csrf)")
            redactor.add(str(token))

        session = requests.Session()
        for c in self._context.cookies():
            redactor.add(c["value"])
            session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        session.headers["User-Agent"] = page.evaluate("() => navigator.userAgent")
        session.headers.update({k: str(v) for k, v in (lg.get("headers") or {}).items()})
        if token:
            session.headers[csrf.get("header", "X-CSRF-Token")] = str(token)
        self.http = session

        if self.has("session_check"):
            try:
                self.check_session()
            except SessionExpired as e:
                self.http = None
                raise LoginError(f"Session check failed right after login ({e})") from None
        log.info("Logged in; HTTP session ready")

    # ----- HTTP ------------------------------------------------------------ #

    def build(self, name: str, **ctx_kw) -> tuple[str, str, dict]:
        """(method, url, requests kwargs) for requests.<name> in the config."""
        spec = self.cfg.requests[name]
        ctx = context(self.cfg, **ctx_kw)
        kw = {k: render(spec[k], ctx, self.secrets)
              for k in ("params", "json", "data", "headers") if k in spec}
        return str(spec.get("method", "GET")).upper(), render(spec["url"], ctx, self.secrets), kw

    def _request(self, method: str, url: str, **kw):
        if self.http is None:
            raise SessionExpired("not logged in")
        r = self.http.request(method, url, allow_redirects=False,
                              timeout=self.cfg.timing["request_timeout_s"], **kw)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code == 401 or 300 <= r.status_code < 400 or (
                "json" not in ctype and "<html" in r.text[:1000].lower()):
            raise SessionExpired(f"HTTP {r.status_code}")
        return r

    def call(self, method: str, url: str, **kw):
        """One request; on an expired session, logs in again and retries once."""
        try:
            return self._request(method, url, **kw)
        except SessionExpired as e:
            log.warning("Session expired (%s); logging in again", e)
            self.login()
            return self._request(method, url, **kw)

    def send(self, name: str, relogin: bool = True, **ctx_kw):
        method, url, kw = self.build(name, **ctx_kw)
        return (self.call if relogin else self._request)(method, url, **kw)

    def check_session(self) -> None:
        r = self.send("session_check", relogin=False)
        ok = self.cfg.requests["session_check"].get("ok_status", [200])
        if r.status_code not in ok:
            raise SessionExpired(f"HTTP {r.status_code}")

    def keepalive(self) -> None:
        try:
            self.check_session()
        except SessionExpired as e:
            log.warning("Session expired (%s); logging in again", e)
            self.login()

    def availability(self, target: Target, day: dt.date) -> bool | None:
        """True/False, or None when the answer is unclear."""
        spec = self.cfg.requests["availability"]
        r = self.send("availability", target=target, day=day)
        if r.status_code != 200:
            log.warning("Availability of %s on %s unknown: %s", target.name, day, describe(r))
            return None
        try:
            v = get_path(r.json(), spec.get("available_field", "available"))
        except ValueError:
            return None
        return v if isinstance(v, bool) else None

    def my_bookings(self, day: dt.date) -> list[dict] | None:
        """My bookings for `day` (requests.my_bookings must return only that
        day's), cancelled ones left out; None if they cannot be read. Each one:
        {id, state, target, lost, ctx}. `lost` = its state is in lost_states
        (a booking that exists but does not hold the slot)."""
        spec = self.cfg.requests["my_bookings"]
        try:
            r = self.send("my_bookings", day=day)
        except Exception as e:
            log.warning("My bookings unknown: %s", redactor(f"{type(e).__name__}: {e}")[:200])
            return None
        if r.status_code != 200:
            log.warning("My bookings unknown: %s", describe(r))
            return None
        try:
            rows = get_path(r.json(), spec.get("list_field"))
        except ValueError:
            log.warning("My bookings unknown: non-JSON response")
            return None
        if not isinstance(rows, list):
            log.warning("My bookings unknown: list_field is not a list")
            return None
        ignore = {s.lower() for s in spec.get("ignore_states", ["cancelled", "canceled"])}
        lost = {s.lower() for s in spec.get("lost_states", [])}
        names = {t.id: t.name for t in self.cfg.targets}
        mine = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            state = str(get_path(row, spec.get("state_field", "state")) or "")
            if state.lower() in ignore:
                continue
            bid = get_path(row, spec.get("id_field", "id"))
            tid = str(get_path(row, spec.get("target_field", "target")) or "")
            mine.append({"id": bid, "state": state, "target": names.get(tid, tid),
                         "lost": state.lower() in lost,
                         "ctx": {**row, "id": bid, "state": state}})
        return mine


def describe(r, field_path: str | None = None) -> str:
    """Short, token-free description of an error response."""
    try:
        data = r.json()
    except ValueError:
        return f"HTTP {r.status_code}, non-JSON body ({len(r.content)} bytes)"
    msg = get_path(data, field_path) if field_path else None
    if msg is None and isinstance(data, dict):
        msg = next((data[k] for k in ("error", "message", "detail") if data.get(k)), None)
    if isinstance(msg, dict):
        msg = " / ".join(str(msg[k]) for k in ("message", "detail") if msg.get(k)) or msg
    return f"HTTP {r.status_code}: {redactor(str(data if msg is None else msg))[:300]}"


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #

def active(mine: list[dict] | None) -> list[dict] | None:
    """Bookings that really hold the slot."""
    return None if mine is None else [m for m in mine if not m["lost"]]


def describe_mine(mine: list[dict]) -> str:
    return "; ".join(f"{m['id']} {m['target']} ({m['state']})" for m in mine)


def booking_ids(mine: list[dict]) -> str:
    return ", ".join(str(m["id"]) for m in mine)


def try_book(client: Client, target: Target, day: dt.date) -> tuple[str | None, str]:
    """(booking id or None, error detail)."""
    import requests

    spec = client.cfg.requests["book"]
    try:
        r = client.send("book", target=target, day=day)
    except (requests.RequestException, SessionExpired, LoginError) as e:
        return None, f"{type(e).__name__}: {redactor(str(e))[:200]}"
    if r.status_code in spec.get("ok_status", [200, 201]):
        if not spec.get("id_field"):
            return f"HTTP {r.status_code}", ""
        try:
            bid = get_path(r.json(), spec["id_field"])
        except ValueError:
            bid = None
        if bid not in (None, ""):
            return str(bid), ""
    return None, describe(r, spec.get("error_field"))


def safe_availability(client: Client, target: Target, day: dt.date) -> bool | None:
    if not client.has("availability"):
        return None
    try:
        return client.availability(target, day)
    except Exception as e:
        log.warning("Availability check failed: %s", redactor(f"{type(e).__name__}: {e}")[:200])
        return None


def precheck(client: Client, day: dt.date) -> list[Target]:
    """Attempt order: preference order minus targets reported NOT available
    (unknown results stay in)."""
    targets = client.cfg.targets
    if not client.has("availability"):
        log.info("No availability request configured; attempt order = config order")
        return list(targets)
    results = [(t, safe_availability(client, t, day)) for t in targets]
    label = {True: "available", False: "NOT available", None: "unknown"}
    log.info("Availability pre-check for %s:\n%s", day, "\n".join(
        f"  {i}. {t.name:<16} {label[a]}" for i, (t, a) in enumerate(results, 1)))
    order = [t for t, a in results if a is not False]
    if not order:
        log.warning("Every target reported NOT available; trying all in config order")
        order = list(targets)
    log.info("Attempt order: %s", ", ".join(t.name for t in order))
    return order


def book(client: Client, day: dt.date, order: list[Target], until: float | None,
         opens_at: float | None = None) -> tuple[str, Target | None, str]:
    """Returns ("booked", target, booking id), ("already", None, ids) or
    ("failed", None, reason).

    Targets are tried in `order`, one attempt every retry_every_s until
    `until`. For the first retry_grace_s after the opening, failures are simply
    retried (many services refuse with the same message just before they
    open). After that, a failure moves on to the next target if the reply
    matches book.taken_pattern or availability reports the target taken.
    Before every attempt on a later target my bookings are re-checked, so a
    second target is never booked for the same day. With until=None (target
    date set by hand) each target gets one attempt."""
    t = client.cfg.timing
    spec = client.cfg.requests["book"]
    taken_rx = re.compile(spec["taken_pattern"], re.I) if spec.get("taken_pattern") else None
    can_check = client.has("my_bookings")
    idx = attempt = 0
    next_check = 0.0
    while True:
        target = order[idx]
        if idx > 0 and can_check:
            mine = active(client.my_bookings(day))
            if mine:
                return "already", None, booking_ids(mine)
            if mine is None:
                return "failed", None, "cannot read my bookings; not booking another target"

        attempt += 1
        t0 = time.monotonic()
        sent = time.time()
        bid, detail = try_book(client, target, day)
        rel = f" (sent T{sent - opens_at:+.3f} s)" if opens_at is not None else ""
        if bid:
            log.info("Attempt %d for %s%s: BOOKED", attempt, target.name, rel)
            return "booked", target, bid
        log.info("Attempt %d for %s%s failed: %s", attempt, target.name, rel, detail)

        now = time.time()
        if opens_at is None or now >= opens_at + t["retry_grace_s"]:
            if idx == 0 and can_check and (until is None or now >= next_check):
                # A lost reply to an earlier successful attempt also lands here.
                next_check = now + t["check_every_s"]
                mine = active(client.my_bookings(day))
                if mine:
                    return "already", None, booking_ids(mine)
            if (taken_rx is not None and taken_rx.search(detail)) \
                    or safe_availability(client, target, day) is False:
                log.warning("%s is taken", target.name)
                if idx + 1 >= len(order):
                    return "failed", None, "every target in the attempt order is taken"
                idx += 1
                log.info("Switching to %s", order[idx].name)
                continue

        if until is None:
            return "failed", None, detail
        if time.time() + t["retry_every_s"] > until:
            return "failed", None, f"retry window over after {attempt} attempts; last: {detail}"
        time.sleep(max(0.0, t["retry_every_s"] - (time.monotonic() - t0)))


def verify(client: Client, day: dt.date) -> bool:
    """Watches my bookings for verify_for_s. False as soon as every one of
    them is lost (another booking won the slot); True otherwise, also when they
    cannot be read."""
    if not client.has("my_bookings"):
        return True
    end = time.time() + client.cfg.timing["verify_for_s"]
    last = None
    while True:
        mine = client.my_bookings(day)
        if mine is not None:
            seen = describe_mine(mine) or "none"
            if seen != last:
                log.info("Booking check: %s", seen)
                last = seen
            if mine and not active(mine):
                return False
        if time.time() >= end:
            return True
        time.sleep(0.5)


def cancel_extras(client: Client, day: dt.date, at: float) -> None:
    """At `at`, cancels my lost bookings for `day` (duplicates), but only
    while another booking holds the slot; never touches that one."""
    if not (client.has("my_bookings") and client.has("cancel")):
        log.info("Cleanup: not configured (needs requests.my_bookings and requests.cancel)")
        return
    if not client.cfg.requests["my_bookings"].get("lost_states"):
        log.info("Cleanup: no lost_states configured; nothing to clean up")
        return
    log.info("Cleanup scheduled for %s", fmt_ts(client.cfg, at))
    time.sleep(max(0.0, at - time.time()))
    mine = client.my_bookings(day)
    if mine is None:
        log.warning("Cleanup skipped: cannot read my bookings for %s", day)
        return
    extras = [m for m in mine if m["lost"]]
    kept = active(mine)
    if not extras:
        log.info("Cleanup: no duplicates for %s", day)
        return
    if not kept:
        log.warning("Cleanup skipped: nothing holds the slot for %s (%s)", day, describe_mine(mine))
        return
    log.info("Cleanup: cancelling %d duplicate(s), keeping %s", len(extras), describe_mine(kept))
    for m in extras:
        if m["id"] in (None, ""):
            log.warning("Cleanup: a %s booking has no id; skipped", m["state"])
            continue
        try:
            r = client.send("cancel", booking=m["ctx"])
        except Exception as e:
            log.warning("Cleanup: cancelling %s failed: %s", m["id"],
                        redactor(f"{type(e).__name__}: {e}")[:200])
            continue
        if r.ok:
            log.info("Cleanup: cancelled %s", m["id"])
        else:
            log.warning("Cleanup: cancelling %s failed: %s", m["id"], describe(r))
    after = client.my_bookings(day)
    if after is not None:
        log.info("After cleanup: %s", describe_mine(after) or "none")


def _sleep_precise(t: float) -> None:
    while (remaining := t - time.time()) > 0.002:
        time.sleep(remaining - 0.002 if remaining > 0.02 else 0.0005)
    while time.time() < t:
        pass


def wait_until(client: Client, t: float) -> None:
    """Sleeps until `t`, keeping the session alive."""
    every = client.cfg.timing["keepalive_every_s"]
    can = client.has("session_check")
    next_keepalive = time.time() + every
    while (remaining := t - time.time()) > 1.0:
        if can and time.time() >= next_keepalive and remaining > 30:
            log.info("Keep-alive (%.0f min to go)", remaining / 60)
            try:
                client.keepalive()
                log.info("Session OK")
            except Exception as e:
                log.warning("Keep-alive failed: %s", redactor(f"{type(e).__name__}: {e}")[:200])
            next_keepalive = time.time() + every
            continue
        time.sleep(min(remaining - 1.0, 5.0))
    _sleep_precise(t)


def summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def already_booked(when: str, ids: str) -> int:
    log.info("RESULT: already booked for %s (%s)", when, ids)
    summary([f"Already booked for {when}: {ids}"])
    return 0


def run(cfg: Config, plan: Plan) -> int:
    try:
        secrets = Secrets(cfg.secrets)
    except ValueError as e:
        log.error("%s", e)
        return 2

    day = plan.target
    when = f"{day} {cfg.slot_start:%H:%M}-{cfg.slot_end:%H:%M} ({cfg.tz.key})"
    log.info("Target: %s; targets in order: %s", when, ", ".join(t.name for t in cfg.targets))
    t = cfg.timing

    client = Client(cfg, secrets)
    try:
        client.login()

        if client.has("my_bookings"):
            mine = client.my_bookings(day)
            if mine is None:
                log.warning("Could not read my bookings for %s; continuing", day)
            else:
                log.info("My bookings for %s: %s", day, describe_mine(mine) or "none")
                if active(mine):
                    return already_booked(when, booking_ids(active(mine)))

        if plan.mode == "dry_run":
            order = precheck(client, day)
            if plan.would_skip:
                log.info("DRY RUN: a real run would skip %s (%s)", day, plan.would_skip)
            else:
                log.info("DRY RUN: would try %s for %s", ", ".join(x.name for x in order), when)
            summary([f"**Dry run** for {when}: login OK, nothing booked.",
                     f"Attempt order: {', '.join(x.name for x in order)}"])
            return 0

        if plan.mode == "scheduled":
            opening = plan.opening.timestamp()
            until = opening + t["retry_for_s"]
            pre = opening - t["precheck_before_s"]
            if pre > time.time():
                log.info("Waiting until %s for the availability pre-check", fmt_ts(cfg, pre))
                wait_until(client, pre)
            order = precheck(client, day)
            log.info("Waiting for the opening at %s", fmt_ts(cfg, opening))
            wait_until(client, opening)
            result = book(client, day, order, until=until, opens_at=opening)
            if result[0] == "booked" and not verify(client, day):
                lost = result[1]
                log.warning("LOST %s: our booking %s does not hold the slot (another booking "
                            "won); trying the next targets", lost.name, result[2])
                summary([f"Lost **{lost.name}** to another booking"])
                rest = order[order.index(lost) + 1:]
                result = (book(client, day, rest, until=until, opens_at=opening) if rest
                          else ("failed", None, f"{lost.name} lost, no other targets"))
        else:
            result = book(client, day, precheck(client, day), until=None)

        status, target, info = result
        if status == "booked":
            log.info("RESULT: BOOKED %s for %s; booking id %s", target.name, when, info)
            summary([f"Booked **{target.name}** for {when}", f"Booking id: `{info}`"])
            if plan.mode == "scheduled":
                try:
                    cancel_extras(client, day, opening + t["cleanup_after_s"])
                except Exception as e:  # the booking stands; never fail the job here
                    log.warning("Cleanup failed: %s", redactor(f"{type(e).__name__}: {e}")[:200])
            return 0
        if status == "already":
            return already_booked(when, info)
        log.error("RESULT: FAILED to book any target for %s: %s", when, info)
        summary([f"**Failed** to book for {when}. See the log."])
        return 1
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["plan", "run"])
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args(argv)
    setup_logging()

    try:
        cfg = load_config(args.config)
        _formatter.tz = cfg.tz
        dry_run = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
        raw_target = os.getenv("TARGET_DATE", "").strip()
        target = dt.date.fromisoformat(raw_target) if raw_target else None
        plan = make_plan(cfg, dt.datetime.now(cfg.tz), dry_run, target,
                         os.getenv("OPENING_TS", "").strip() or None)

        if args.command == "plan":
            out = os.getenv("GITHUB_OUTPUT")
            if out:
                with open(out, "a", encoding="utf-8") as f:
                    f.write(f"proceed={'true' if plan.proceed else 'false'}\n")
                    if plan.opening:
                        f.write(f"opening_ts={int(plan.opening.timestamp())}\n")
            return 0
        if not plan.proceed:
            return 0
        return run(cfg, plan)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 1
    except Exception:
        log.error("Fatal error", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
