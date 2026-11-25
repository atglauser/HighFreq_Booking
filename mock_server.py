#!/usr/bin/env python3
"""Local test target for bot.py and measure.py (standard library only).

Imitates a small booking service: a login form with a one-time code step, a
CSRF token on the app page, and a JSON API whose next day opens at a set
moment. Nothing leaves your machine.

  python mock_server.py --opens-in 90
  # prints OPENING_TS=...; then, in a second terminal (see README):
  OPENING_TS=<value> python bot.py run --config config.example.yaml

Scenarios:
  --taken a           target a is already taken (the pre-check skips it)
  --rival a           someone else books a at the opening and wins: our booking
                      of a is "superseded", so the bot falls back to the next target
  --lose-first-reply  the first successful booking is saved but its reply is lost
                      (HTTP 504): the retry creates a duplicate for the cleanup

Endpoints (JSON unless noted):
  GET/POST /login       username + password form (HTML)
  GET/POST /login/code  one-time code form (HTML; skipped with --totp-secret "")
  GET  /app             app page with <meta name="csrf-token"> (HTML)
  GET  /api/me          200 when logged in, else 401
  GET  /api/availability?target=<id>&date=<YYYY-MM-DD>
  GET  /api/bookings?date=<YYYY-MM-DD>
  POST /api/bookings    {target, date, start, end}; needs X-CSRF-Token
  DELETE /api/bookings/<id>                         needs X-CSRF-Token
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import math
import secrets
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

RIVAL_WINDOW_S = 1.0   # bookings of a --rival target this soon after the opening lose

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>{head}</head>
<body style="font-family:sans-serif;max-width:28rem;margin:3rem auto">{body}</body></html>"""

LOGIN_FORM = """<h1>Sign in</h1>{error}
<form method="post" action="/login">
  <p><label>Username <input name="username" autocomplete="username"></label></p>
  <p><label>Password <input name="password" type="password" autocomplete="current-password"></label></p>
  <p><button type="submit">Sign in</button></p>
</form>"""

CODE_FORM = """<h1>One-time code</h1>{error}
<form method="post" action="/login/code">
  <p><label>Code <input name="code" inputmode="numeric" autocomplete="one-time-code"></label></p>
  <p><button type="submit">Verify</button></p>
</form>"""


def totp_ok(secret: str, code: str, now: float) -> bool:
    """RFC 6238 check (SHA-1, 6 digits, 30 s), accepting one step of drift."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    for drift in (-1, 0, 1):
        counter = int(now // 30) + drift
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        o = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if hmac.compare_digest(f"{value:06d}", code.strip()):
            return True
    return False


class State:
    def __init__(self, args) -> None:
        self.args = args
        self.tz = ZoneInfo(args.timezone)
        self.targets = [t.strip() for t in args.targets.split(",") if t.strip()]
        # A whole second, so OPENING_TS can be passed on exactly.
        self.opening = args.opening_ts if args.opening_ts else math.ceil(time.time() + args.opens_in)
        self.opening_day = (dt.datetime.fromtimestamp(self.opening, self.tz).date()
                            + dt.timedelta(days=args.open_days_ahead))
        self.sessions: dict[str, dict] = {}       # sid -> {stage, csrf}
        self.bookings: list[dict] = []
        self.next_id = 1001
        self.lose_reply = args.lose_first_reply
        self.lock = threading.Lock()

    def is_open(self, day: dt.date, now: float) -> str | None:
        """None if `day` can be booked now, else the reason."""
        today = dt.datetime.fromtimestamp(now, self.tz).date()
        if day < today:
            return "date is in the past"
        if day > self.opening_day or (day == self.opening_day and now < self.opening):
            return "date is not open for booking yet"
        return None

    def rival_holds(self, target: str, day: dt.date, now: float) -> bool:
        return (target in self.args.rival and day == self.opening_day
                and now >= self.opening + RIVAL_WINDOW_S)

    def available(self, target: str, day: dt.date, now: float) -> bool:
        if target in self.args.taken or self.rival_holds(target, day, now):
            return False
        return not any(b["target"] == target and b["date"] == day.isoformat()
                       and b["state"] == "confirmed" for b in self.bookings)


class Handler(BaseHTTPRequestHandler):
    state: State
    server_version = "MockService/1.0"
    protocol_version = "HTTP/1.1"     # keep-alive, like a real service

    # ----- helpers ---------------------------------------------------------- #

    def log_message(self, fmt, *args) -> None:   # quieter than the default
        pass

    def note(self, text: str) -> None:
        t = dt.datetime.now(self.state.tz)
        print(f"{t:%H:%M:%S}.{t.microsecond // 1000:03d}  {text}", flush=True)

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        if self.state.args.latency_ms:
            time.sleep(self.state.args.latency_ms / 1000)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def page(self, title: str, body: str, head: str = "") -> None:
        self._send(200, PAGE.format(title=title, head=head, body=body).encode(),
                   "text/html; charset=utf-8")

    def redirect(self, location: str, cookie: str | None = None) -> None:
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = f"sid={cookie}; Path=/; HttpOnly; SameSite=Lax"
        self._send(303, b"", "text/plain", headers)

    def session(self) -> tuple[str | None, dict | None]:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sid" and value in self.state.sessions:
                return value, self.state.sessions[value]
        return None, None

    def form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def body_json(self) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def api_session(self, write: bool = False) -> dict | None:
        """The logged-in session, or None after answering 401/403."""
        _, s = self.session()
        if not s or s["stage"] != "ok":
            self.json(401, {"error": "not logged in"})
            return None
        if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), s["csrf"]):
            self.json(403, {"error": "invalid CSRF token"})
            return None
        return s

    # ----- routes ----------------------------------------------------------- #

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        _, s = self.session()
        if u.path == "/":
            return self.redirect("/app")
        if u.path == "/login":
            return self.page("Sign in", LOGIN_FORM.format(error=""))
        if u.path == "/login/code":
            if not s or s["stage"] != "code":
                return self.redirect("/login")
            return self.page("One-time code", CODE_FORM.format(error=""))
        if u.path == "/app":
            if not s or s["stage"] != "ok":
                return self.redirect("/login")
            st = self.state
            body = (f"<h1>Bookings</h1><p>Signed in. Next day to open: {st.opening_day}, at "
                    f"{dt.datetime.fromtimestamp(st.opening, st.tz):%Y-%m-%d %H:%M:%S %Z}.</p>")
            head = f'<meta name="csrf-token" content="{html.escape(s["csrf"])}">'
            return self.page("Bookings", body, head)
        if u.path == "/api/me":
            if self.api_session():
                self.json(200, {"user": self.state.args.username})
            return
        if u.path == "/api/availability":
            if not self.api_session():
                return
            try:
                day = dt.date.fromisoformat(q.get("date", ""))
            except ValueError:
                return self.json(400, {"error": "date must be YYYY-MM-DD"})
            target = q.get("target", "")
            if target not in self.state.targets:
                return self.json(404, {"error": f"unknown target {target!r}"})
            with self.state.lock:
                free = self.state.available(target, day, time.time())
            return self.json(200, {"target": target, "date": day.isoformat(), "available": free})
        if u.path == "/api/bookings":
            if not self.api_session():
                return
            with self.state.lock:
                rows = [b for b in self.state.bookings if b["date"] == q.get("date")]
            return self.json(200, {"bookings": rows})
        self.json(404, {"error": "not found"})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        st = self.state
        if u.path == "/login":
            f = self.form()
            if f.get("username") != st.args.username or f.get("password") != st.args.password:
                self.note("login: wrong username or password")
                return self.page("Sign in", LOGIN_FORM.format(error="<p>Wrong username or password.</p>"))
            sid = secrets.token_urlsafe(24)
            stage = "code" if st.args.totp_secret else "ok"
            st.sessions[sid] = {"stage": stage, "csrf": secrets.token_urlsafe(24)}
            self.note(f"login: password OK{', asking for the one-time code' if stage == 'code' else ''}")
            return self.redirect("/login/code" if stage == "code" else "/app", cookie=sid)
        if u.path == "/login/code":
            _, s = self.session()
            if not s or s["stage"] != "code":
                return self.redirect("/login")
            if not totp_ok(st.args.totp_secret, self.form().get("code", ""), time.time()):
                self.note("login: wrong one-time code")
                return self.page("One-time code", CODE_FORM.format(error="<p>Wrong code.</p>"))
            s["stage"] = "ok"
            self.note("login: one-time code OK")
            return self.redirect("/app")
        if u.path == "/api/bookings":
            if not self.api_session(write=True):
                return
            return self.create_booking()
        self.json(404, {"error": "not found"})

    def create_booking(self) -> None:
        st = self.state
        data = self.body_json()
        if data is None:
            return self.json(400, {"error": "body must be a JSON object"})
        target = str(data.get("target", ""))
        try:
            day = dt.date.fromisoformat(str(data.get("date", "")))
        except ValueError:
            return self.json(400, {"error": "date must be YYYY-MM-DD"})
        if target not in st.targets:
            return self.json(404, {"error": f"unknown target {target!r}"})
        now = time.time()
        rel = f"T{now - st.opening:+.3f} s"
        with st.lock:
            reason = st.is_open(day, now)
            if reason:
                self.note(f"POST {target} {day} ({rel}) -> 400 {reason}")
                return self.json(400, {"error": reason})
            if target in st.args.taken or st.rival_holds(target, day, now):
                self.note(f"POST {target} {day} ({rel}) -> 409 unavailable")
                return self.json(409, {"error": "target unavailable"})
            mine = [b for b in st.bookings if b["date"] == day.isoformat() and b["state"] == "confirmed"]
            if any(b["target"] != target for b in mine):
                self.note(f"POST {target} {day} ({rel}) -> 400 overlap")
                return self.json(400, {"error": "you already hold another booking for this slot"})
            if mine:
                state = "superseded"        # duplicate of a booking we already hold
            elif target in st.args.rival and day == st.opening_day:
                state = "superseded"        # the rival's booking was saved first
            else:
                state = "confirmed"
            booking = {"id": f"BK{st.next_id}", "target": target, "date": day.isoformat(),
                       "start": data.get("start"), "end": data.get("end"), "state": state}
            st.next_id += 1
            st.bookings.append(booking)
            lose = st.lose_reply and state == "confirmed"
            if lose:
                st.lose_reply = False
        if lose:
            self.note(f"POST {target} {day} ({rel}) -> saved {booking['id']} ({state}), reply lost: 504")
            return self.json(504, {"error": "gateway timeout"})
        self.note(f"POST {target} {day} ({rel}) -> 201 {booking['id']} ({state})")
        self.json(201, {"booking": booking})

    def do_DELETE(self) -> None:
        u = urlparse(self.path)
        if not u.path.startswith("/api/bookings/"):
            return self.json(404, {"error": "not found"})
        if not self.api_session(write=True):
            return
        bid = u.path.rsplit("/", 1)[-1]
        with self.state.lock:
            booking = next((b for b in self.state.bookings if b["id"] == bid), None)
            if booking:
                booking["state"] = "cancelled"
        if not booking:
            return self.json(404, {"error": f"no booking {bid}"})
        self.note(f"DELETE {bid} -> cancelled")
        self.json(200, {"booking": booking})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--username", default="demo")
    ap.add_argument("--password", default="demo")
    ap.add_argument("--totp-secret", default="JBSWY3DPEHPK3PXP",
                    help='base32 secret for the one-time code step ("" = no code step)')
    ap.add_argument("--timezone", default="UTC")
    ap.add_argument("--open-days-ahead", type=int, default=7)
    ap.add_argument("--opens-in", type=float, default=120,
                    help="seconds from now until the next day opens (default 120)")
    ap.add_argument("--opening-ts", type=float, default=0,
                    help="exact opening as epoch seconds (instead of --opens-in)")
    ap.add_argument("--targets", default="a,b,c,d", help="comma-separated target ids")
    ap.add_argument("--taken", action="append", default=[], metavar="ID")
    ap.add_argument("--rival", action="append", default=[], metavar="ID")
    ap.add_argument("--lose-first-reply", action="store_true")
    ap.add_argument("--latency-ms", type=float, default=0, help="extra delay per response")
    args = ap.parse_args()

    Handler.state = State(args)
    st = Handler.state
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    opening = dt.datetime.fromtimestamp(st.opening, st.tz)
    print(f"Mock service on http://{args.host}:{args.port}  "
          f"(user {args.username!r}, password {args.password!r}"
          f"{', one-time code secret ' + args.totp_secret if args.totp_secret else ''})")
    print(f"Targets {', '.join(st.targets)}; {st.opening_day} opens at "
          f"{opening:%Y-%m-%d %H:%M:%S %Z}  ->  OPENING_TS={f'{st.opening:.3f}'.rstrip('0').rstrip('.')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
