#!/usr/bin/env python3
"""Timing measurements around an opening. Read-only: never books or cancels.

PUBLIC, REDUCED VERSION (see bot.py). For more information, contact the
repository owner on GitHub: https://github.com/atglauser

Phases (O = the opening):
  start       local clock vs NTP; 5 cold connections (DNS / TCP / TLS / first byte)
  O-15 min    log in (O-3 min in test mode)
  then        idle test: does a pooled connection survive 30/60/120/240 s idle?
  O-90 s      local clock vs NTP again
  O-75..O-50  server clock vs local clock, from the HTTP Date header
  O-5..O+20   availability of the first target every 250 ms: when does it change?
  O+25        availability of every target, and my bookings for the target date

Uses requests.session_check (idle test, server clock), requests.availability
(polling) and requests.my_bookings from the config; phases whose request is
not configured are skipped. It logs in once and never again: if the service
rejects the session, it stops instead of competing with a booking run.

Results: measure-results.json and, on GitHub Actions, the job summary.

Environment: the config's secrets, plus OPENING_TS (from `bot.py plan`) or
TEST_LEAD_MIN (pretend the opening is that many minutes away).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import bot
from bot import log, redactor

RESULTS: dict = {"events": []}
_lock = threading.Lock()
_new_conns: list[dict] = []
stop = threading.Event()
cfg: bot.Config
opening = 0.0


class SessionLost(Exception):
    pass


def no_relogin(*_a, **_kw):
    stop.set()
    raise SessionLost("session rejected; not logging in again")


def check_auth(r) -> None:
    if r.status_code == 401 or 300 <= r.status_code < 400 or (
            "json" not in r.headers.get("Content-Type", "") and "<html" in r.text[:1000].lower()):
        stop.set()
        raise SessionLost(f"HTTP {r.status_code}")


def stamp(t: float | None = None) -> str:
    d = dt.datetime.fromtimestamp(time.time() if t is None else t, cfg.tz)
    return f"{d:%H:%M:%S}.{d.microsecond // 1000:03d}"


def record(kind: str, **data) -> None:
    with _lock:
        RESULTS["events"].append({"kind": kind, **data})


def sleep_until(t: float) -> None:
    while (left := t - time.time()) > 0:
        time.sleep(min(left, 5.0))


# --------------------------------------------------------------------------- #
# New TCP/TLS connections opened by requests (urllib3), with their duration
# --------------------------------------------------------------------------- #

def patch_connection_counter() -> None:
    import urllib3.connection as uc

    for cls in (uc.HTTPConnection, uc.HTTPSConnection):
        orig = cls.connect

        def connect(self, *a, _orig=orig, **kw):
            t0 = time.perf_counter()
            try:
                return _orig(self, *a, **kw)
            finally:
                with _lock:
                    _new_conns.append({"at": time.time(),
                                       "ms": round((time.perf_counter() - t0) * 1000, 1)})

        cls.connect = connect


def conns_since(t: float) -> list[float]:
    with _lock:
        return [c["ms"] for c in _new_conns if c["at"] >= t]


# --------------------------------------------------------------------------- #
# Clocks and cold connections
# --------------------------------------------------------------------------- #

def ntp_offset(server: str = "pool.ntp.org") -> None:
    """Local clock error vs NTP (positive = local clock behind). Best of 4 by delay."""
    best = None
    for _ in range(4):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(2)
                t1 = time.time()
                s.sendto(b"\x1b" + 47 * b"\0", (server, 123))
                data, _ = s.recvfrom(48)
                t4 = time.time()

            def ts(off):
                sec, frac = struct.unpack("!II", data[off:off + 8])
                return sec - 2208988800 + frac / 2**32
            t2, t3 = ts(32), ts(40)
            offset = ((t2 - t1) + (t3 - t4)) / 2
            delay = (t4 - t1) - (t3 - t2)
            if best is None or delay < best[1]:
                best = (offset, delay)
        except Exception as e:
            log.warning("NTP query failed: %s", type(e).__name__)
        time.sleep(0.2)
    if best:
        log.info("NTP (%s): local clock offset %+.1f ms (delay %.1f ms)",
                 server, best[0] * 1000, best[1] * 1000)
        record("ntp", at=stamp(), offset_ms=round(best[0] * 1000, 1), delay_ms=round(best[1] * 1000, 1))


def cold_probe() -> None:
    """One fresh connection to base_url, timed step by step (cumulative ms)."""
    u = urlparse(cfg.base_url)
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    t = {}
    p0 = time.perf_counter()
    try:
        ip = socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)[0][4][0]
        t["dns"] = time.perf_counter() - p0
        sock = socket.create_connection((ip, port), timeout=10)
        t["tcp"] = time.perf_counter() - p0
        if https:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        t["tls"] = time.perf_counter() - p0
        sock.sendall(f"GET / HTTP/1.1\r\nHost: {u.netloc}\r\nConnection: close\r\n\r\n".encode())
        first = sock.recv(4096)
        t["first_byte"] = time.perf_counter() - p0
        sock.close()
        status = first.split(b"\r\n", 1)[0].decode(errors="replace")
        if not https:
            del t["tls"]
        ms = {k: round(v * 1000, 1) for k, v in t.items()}
        label = {"dns": "DNS", "tcp": "TCP", "tls": "TLS", "first_byte": "first byte"}
        steps = ", ".join(f"{label[k]} {v:.0f}" for k, v in ms.items())
        log.info("Cold connection to %s: %s ms (cumulative; %s)", ip, steps, status)
        record("cold", at=stamp(), ip=ip, status=status, **ms)
    except Exception as e:
        log.warning("Cold probe failed: %s", type(e).__name__)
        record("cold", at=stamp(), error=type(e).__name__)


# --------------------------------------------------------------------------- #
# Authenticated measurements
# --------------------------------------------------------------------------- #

_tls = threading.local()


def session_copy(client: bot.Client):
    """A per-thread copy of the logged-in session (requests is not thread-safe)."""
    import requests

    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(client.http.headers)
        s.cookies.update(client.http.cookies)
        _tls.s = s
    return s


def timed(session, method: str, url: str, **kw):
    t0 = time.time()
    p0 = time.perf_counter()
    r = session.request(method, url, allow_redirects=False,
                        timeout=cfg.timing["request_timeout_s"], **kw)
    rtt = (time.perf_counter() - p0) * 1000
    check_auth(r)
    return t0, t0 + rtt / 1000, rtt, r


def probe(client: bot.Client) -> tuple[float, int, list[float]]:
    """session_check; returns (ms, status, new connections opened)."""
    since = time.time()
    method, url, kw = client.build("session_check")
    _, _, rtt, r = timed(client.http, method, url, **kw)
    return rtt, r.status_code, conns_since(since)


def idle_test(client: bot.Client, idles: list[int]) -> None:
    rtt, st, new = probe(client)
    log.info("Idle test baseline: %.0f ms (HTTP %d), new connections: %s", rtt, st, new)
    for idle in idles:
        time.sleep(idle)
        rtt, st, new = probe(client)
        log.info("After %3d s idle: %.0f ms (HTTP %d), new connections: %s",
                 idle, rtt, st, new or "none (reused)")
        record("idle", idle_s=idle, ms=round(rtt, 1), status=st, new_conn_ms=new)


def clock_offset(client: bot.Client, start: float, end: float) -> None:
    """Server clock vs local clock from Date header second boundaries. A reply
    whose Date shows s+1 was stamped before it arrived, one that shows s after
    its request was sent, so the local moment the server reaches s+1 lies in
    (send of the last 's' request, arrival of the first 's+1' reply]."""
    sleep_until(start)
    method, url, kw = client.build("session_check")
    samples = []
    while time.time() < end:
        t0, t1, _, r = timed(client.http, method, url, **kw)
        d = r.headers.get("Date")
        if d:
            samples.append((t0, t1, parsedate_to_datetime(d).timestamp()))
        time.sleep(max(0.0, 0.1 - (t1 - t0)))   # at most ~10 requests/s
    lo, hi = -1e18, 1e18
    for (a0, _a1, sa), (_b0, b1, sb) in zip(samples, samples[1:]):
        if sb == sa + 1:
            lo = max(lo, sb - b1)
            hi = min(hi, sb - a0)
    rtts = sorted((s[1] - s[0]) * 1000 for s in samples)
    res = {"samples": len(samples),
           "rtt_ms_min": round(rtts[0], 1) if rtts else None,
           "rtt_ms_median": round(rtts[len(rtts) // 2], 1) if rtts else None}
    if lo <= hi:
        res.update(offset_ms_low=round(lo * 1000, 1), offset_ms_high=round(hi * 1000, 1),
                   offset_ms=round((lo + hi) / 2 * 1000, 1))
        log.info("Server clock vs local: %+.0f ms (between %+.0f and %+.0f; positive = server "
                 "ahead); %d samples, round trip min %.0f / median %.0f ms",
                 res["offset_ms"], res["offset_ms_low"], res["offset_ms_high"],
                 len(samples), res["rtt_ms_min"], res["rtt_ms_median"])
    else:
        log.warning("Server clock offset: no consistent bounds from %d samples", len(samples))
    RESULTS["server_clock"] = res


def poll_availability(client: bot.Client, target: bot.Target, day: dt.date,
                      start: float, end: float, every: float = 0.25) -> None:
    sleep_until(start)
    method, url, kw = client.build("availability", target=target, day=day)
    field = cfg.requests["availability"].get("available_field", "available")
    results = []

    def one(tick: float):
        try:
            t0, t1, rtt, r = timed(session_copy(client), method, url, **kw)
            if r.status_code == 200:
                v = bot.get_path(r.json(), field)
                state = {True: "available", False: "TAKEN"}.get(v, "unknown")
            else:
                state = f"error {bot.describe(r)[:80]}"
            item = {"sent": t0, "recv": t1, "ms": round(rtt, 1), "status": r.status_code,
                    "state": state, "server_date": r.headers.get("Date")}
        except Exception as e:      # SessionLost has already set `stop`
            item = {"sent": tick, "state": f"error {type(e).__name__}"}
        with _lock:
            results.append(item)

    with ThreadPoolExecutor(max_workers=6) as ex:
        tick = start
        while tick < end and not stop.is_set():
            sleep_until(tick)
            ex.submit(one, tick)
            tick += every
    results.sort(key=lambda x: x["sent"])
    # Every change of state, with the window in which it happened: after the
    # previous request was sent and before this one's reply arrived.
    changes = []
    for i, x in enumerate(results):
        if i == 0:
            line = f"{x['state']} at start (O{x['sent'] - opening:+.3f} s)"
        elif x["state"] != results[i - 1]["state"]:
            a = results[i - 1]
            b_end = x.get("recv", x["sent"])
            line = (f"{a['state']} -> {x['state']} between {stamp(a['sent'])} and {stamp(b_end)} "
                    f"(O{a['sent'] - opening:+.3f} s .. O{b_end - opening:+.3f} s)")
        else:
            continue
        changes.append(line)
        log.info("%s: %s", target.name, line)
    RESULTS["first_target_changes"] = changes
    for x in results:
        record("availability", target=target.name, sent=stamp(x["sent"]),
               recv=stamp(x["recv"]) if "recv" in x else None,
               rel_opening_s=round(x["sent"] - opening, 3),
               **{k: v for k, v in x.items() if k not in ("sent", "recv")})


# --------------------------------------------------------------------------- #

def write_outputs() -> None:
    with open("measure-results.json", "w", encoding="utf-8") as f:
        f.write(redactor(json.dumps(RESULTS, indent=1, default=str)))
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        lines = ["## Timing measurements"]
        lines += [f"- {e}" for e in RESULTS["events"] if e["kind"] in ("ntp", "cold", "idle")]
        lines.append(f"- Server clock: {RESULTS.get('server_clock')}")
        lines.append("- First target, state changes:")
        lines += [f"  - {c}" for c in RESULTS.get("first_target_changes", [])]
        if RESULTS.get("session_lost"):
            lines.append(f"- SESSION LOST: {RESULTS['session_lost']}")
        lines.append(f"- After: {RESULTS.get('after')}")
        with open(path, "a", encoding="utf-8") as f:
            f.write(redactor("\n".join(lines)) + "\n")


def main() -> int:
    global cfg, opening
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    bot.setup_logging()
    patch_connection_counter()

    try:
        cfg = bot.load_config(args.config)
    except bot.ConfigError as e:
        log.error("Config error: %s", e)
        return 1
    bot._formatter.tz = cfg.tz
    lead = os.getenv("TEST_LEAD_MIN", "").strip()
    test = bool(lead)
    if test:
        opening = time.time() + float(lead) * 60
    else:
        ts = os.getenv("OPENING_TS", "").strip()
        if not ts:
            log.error("OPENING_TS missing (or set TEST_LEAD_MIN)")
            return 1
        opening = float(ts)
    day = dt.datetime.fromtimestamp(opening, cfg.tz).date() + dt.timedelta(days=cfg.open_days_ahead)
    RESULTS.update(opening=bot.fmt_ts(cfg, opening), target_date=str(day), test_mode=test)
    log.info("Measuring around %s (target date %s)%s", bot.fmt_ts(cfg, opening), day,
             " [TEST MODE]" if test else "")

    try:
        secrets = bot.Secrets(cfg.secrets)
    except ValueError as e:
        log.error("%s", e)
        return 2

    has = cfg.requests.__contains__
    try:
        ntp_offset()
        for _ in range(5):
            th = threading.Thread(target=cold_probe, daemon=True)  # a probe can hang
            th.start()
            th.join(timeout=30)
            if th.is_alive():
                log.warning("Cold probe still running after 30 s; moving on")
                record("cold", at=stamp(), error="hung >30 s")
            time.sleep(1)

        sleep_until(opening - (3 * 60 if test else 15 * 60))
        client = bot.Client(cfg, secrets)
        try:
            client.login()
            client.login = no_relogin       # Client.call would otherwise log in again
            if has("session_check"):
                idle_test(client, [10, 30, 60] if test else [30, 60, 120, 240])
            else:
                log.info("No session_check request: idle test and server clock skipped")
            sleep_until(opening - 90)
            ntp_offset()
            if has("session_check"):
                clock_offset(client, opening - 75, opening - 50)
            if has("availability"):
                poll_availability(client, cfg.targets[0], day, opening - 5, opening + 20)
                if stop.is_set():
                    raise SessionLost("session rejected during availability polling")
            else:
                log.info("No availability request: polling skipped")
            sleep_until(opening + 25)
            after = {}
            if has("availability"):
                after["availability"] = {t.name: bot.safe_availability(client, t, day)
                                         for t in cfg.targets}
            if has("my_bookings"):
                mine = client.my_bookings(day)
                after["my_bookings"] = [{k: m[k] for k in ("target", "state")} for m in (mine or [])]
            RESULTS["after"] = after
            log.info("After the opening: %s", after)
        finally:
            client.close()
    except SessionLost as e:
        log.warning("Measurement session lost (%s); stopped without logging in again", e)
        RESULTS["session_lost"] = f"{stamp()}: {e}"
    except Exception:
        log.error("Measurement error", exc_info=True)
    finally:
        write_outputs()
    return 0


if __name__ == "__main__":
    sys.exit(main())
