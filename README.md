# HighFreq Booking

Log in to a web service with a scripted browser, send a request at the exact
moment something opens, and keep going until it works: retries, fallback
targets, a check that the result holds, and cleanup of duplicates. Runs on
GitHub Actions, on a small server or on your own computer.

> **Public, reduced version.** This repository contains the complete workflow,
> but not the timing refinements of the version I run privately. For more
> information, contact me through GitHub: [@atglauser](https://github.com/atglauser)
> (open an issue or see my profile).

> **Use it responsibly.** Only automate your own accounts, on services whose
> terms allow it. You are responsible for how you use this code.

## What it does

1. **Plan** (`bot.py plan`, no browser): is the next opening close enough, and
   is the date that opens one you want?
2. **Log in** with headless Chromium (Playwright) by following the steps in your
   config: fill forms, click buttons, enter a one-time code (TOTP).
3. **Hand over** the browser's cookies (and CSRF token) to a plain HTTP session,
   and keep it alive until the opening.
4. **Pre-check** shortly before the opening which targets are still free.
5. **Book** at the opening: the first request goes out at T+0, then one per
   interval until it works or the window closes, moving down the target list
   when one is taken.
6. **Verify** that the booking holds; if another booking won the slot, try the
   next targets.
7. **Clean up** duplicates once the result has settled.

Nothing in the code is tied to a particular site: URLs, form fields, request
bodies and response fields all live in `config.yaml`.

## Try it locally

`mock_server.py` is a small fake service (standard library only) with a login
form, a one-time code step and a booking API that opens at a time you choose.
`config.example.yaml` is already set up for it.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Terminal 1:

```bash
python mock_server.py --opens-in 60    # prints OPENING_TS=...
```

Terminal 2 (bash):

```bash
export BOOKER_USERNAME=demo BOOKER_PASSWORD=demo BOOKER_TOTP_SECRET=JBSWY3DPEHPK3PXP
export OPENING_TS=<value from terminal 1>
python bot.py run --config config.example.yaml
```

Terminal 2 (PowerShell):

```powershell
$env:BOOKER_USERNAME="demo"; $env:BOOKER_PASSWORD="demo"; $env:BOOKER_TOTP_SECRET="JBSWY3DPEHPK3PXP"
$env:OPENING_TS="<value from terminal 1>"
python bot.py run --config config.example.yaml
```

Restart the mock with one of these to see the other paths:

| Mock option | What happens |
|---|---|
| `--taken a` | The pre-check drops Target A; Target B is booked. |
| `--rival a` | Target A is booked but `superseded` (someone else's booking won): `LOST`, then Target B. The cleanup cancels the lost booking. |
| `--lose-first-reply` | The first booking is saved but its reply is lost; the retry creates a duplicate, which the cleanup cancels. |

The cleanup runs `cleanup_after_s` (180 s) after the opening; lower it in a copy
of the config to see it sooner. `DRY_RUN=true` only logs in and checks
availability; `TARGET_DATE=YYYY-MM-DD` books an open date right away.

## How a run works

| When (O = opening) | Step |
|---|---|
| Start | `plan`: proceeds only within `gate_minutes` of the next opening, and only if the target date (opening date + `open_days_ahead`) is in `weekdays` and not in `skip_dates`. |
| Right after | Log in. If I already hold a booking for the target date, stop: `already booked`. |
| O − `precheck_before_s` | Availability of every target. Attempt order = config order minus targets reported taken. |
| Waiting | Session check every `keepalive_every_s`; logs in again if the session expired. |
| O | First attempt, sent at T+0. Then one every `retry_every_s` until O + `retry_for_s`. |
| First `retry_grace_s` | Failures are simply retried (services often refuse with the same message just before they open). |
| After that | A failure whose reply matches `taken_pattern`, or a target that availability reports taken, moves on to the next target. My bookings are re-checked regularly (a lost reply may have booked after all) and before every later target, so two targets are never booked for the same day. |
| After a success | My bookings are watched for `verify_for_s`. If all of them are lost (`lost_states`), the next targets are tried. |
| O + `cleanup_after_s` | Lost duplicates are cancelled, only while another booking holds the slot. |

Exit code: `0` booked, already booked or nothing to do; `1` failed (on GitHub,
a failed job sends you an email); `2` missing secrets.

## Configuration

Copy `config.example.yaml` to `config.yaml` and adapt it. To find the values,
do the whole thing once by hand with your browser's DevTools open (Network
tab): note the login pages and fields, then the request that books, the one
that lists your bookings, the one that checks availability and the one that
cancels. "Copy as cURL" shows the method, URL, headers and body.

| Section | What goes there |
|---|---|
| `base_url`, `timezone` | The service's address; the time zone of its opening time. |
| `schedule` | `opens_at`, `open_days_ahead`, `weekdays`, `skip_dates`, `gate_minutes`. |
| `slot` | The time range to book on the target date. |
| `targets` | What to book, in order of preference: `name` for the log, `id` for the requests, any extra keys you need. |
| `vars` | Constants for request bodies, e.g. an account id. |
| `secrets` | Names of the environment variables the run needs. Their values are masked in every log line. |
| `login` | Start URL, steps, `done_url` (regex: logged in), where to find the CSRF token, headers for every request. |
| `requests` | `book` (required); `session_check`, `availability`, `my_bookings`, `cancel` (optional, each enables more of the workflow). |
| `timing` | Retry, check and cleanup intervals (defaults in the example). |

Login steps run in order and stop early once the page URL matches `done_url`:

| Step | Does |
|---|---|
| `goto: URL` | Opens a page. |
| `fill: SELECTOR` + `value` | Types into a field (`value` may be `${SECRET}` or `${totp:SECRET}`). |
| `click: SELECTOR` | Clicks. |
| `press: SELECTOR` + `key` | Presses a key in an element (default Enter). |
| `wait_for: SELECTOR` / `wait_url: REGEX` | Waits for an element or URL. |
| `sleep: SECONDS` | Pauses. |

Add `optional: true` to skip a step whose element doesn't show up (e.g. a code
prompt that only appears sometimes), and `timeout_s` to change the wait.
Placeholders such as `{target.id}`, `{date}`, `{slot_start_utc}` and
`{vars.name}` are listed at the top of `config.example.yaml`.

## Ways to run it

| Setup | Start | Distance to the service | Cost | Suited to |
|---|---|---|---|---|
| GitHub Actions, GitHub's scheduler | Can start late or skip a run at busy times | Wherever GitHub's runners are | Free on public repos; minutes on private ones | Trying it out, openings where minutes don't matter |
| GitHub Actions, external trigger | Within seconds | Same | Same, plus a free scheduler account | Dependable daily runs |
| Self-hosted runner near the service | Within seconds (external trigger) | Short, stable round trip; your own clock sync | Small VM (free tiers exist), some upkeep | When being early by milliseconds matters |
| Your own computer | As reliable as your computer | Your connection | Free | Development and tests |

A sensible path: run it against the mock, then do a dry run on GitHub Actions,
then use an external trigger for daily use. Move the job closer to the service
only if the measurements say it matters, and only in a private copy.

### GitHub Actions

1. **Copy the repository** ("Use this template", or fork). For real use, a
   **private** copy is better: your config shows what you automate. Private
   repositories have a monthly quota of Actions minutes, and a run that waits
   for the opening uses about as many minutes as it waits.
2. **Add `config.yaml`** (from `config.example.yaml`) and commit it.
3. **Add the secrets** under Settings → Secrets and variables → Actions:
   `BOOKER_USERNAME`, `BOOKER_PASSWORD`, `BOOKER_TOTP_SECRET` (the base32 key
   behind an authenticator app, usually shown via "can't scan the code?" when
   you add one). Other names: change `secrets:` in the config and the `env:`
   block in `run.yml`.
4. **Dry run:** Actions → *Run booker* → *Run workflow* with `dry_run` ticked.
   The log should show `Logged in; HTTP session ready` and the pre-check.
5. **Test booking:** run it with `target_date` set to a date that is already
   open, then cancel by hand if it was only a test.
6. **Schedule:** uncomment `schedule:` in `run.yml`. Cron times are UTC; start
   about 45 minutes before the opening, well within `gate_minutes`. With
   daylight saving, list both UTC hours: the gate lets only the right one
   proceed. GitHub pauses scheduled workflows in public repositories after 60
   days without activity.

Several accounts: one job per account with a `matrix`, each with its own
config file and secret prefix, e.g.
`BOOKER_USERNAME: ${{ secrets[format('{0}BOOKER_USERNAME', matrix.prefix)] }}`.
Give them different targets so they don't compete with each other.

### External trigger

GitHub's scheduler is best effort. Any service that can send an HTTP POST on a
schedule (cron-job.org, for example) can start the workflow within seconds
through the API:

- Token: Settings → Developer settings → Fine-grained tokens, access to this
  repository only, permission *Actions: Read and write*. Note its expiry date.
- Request: `POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/run.yml/dispatches`
  with headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
  `X-GitHub-Api-Version: 2022-11-28` and body `{"ref":"main"}`. GitHub answers `204`.
- A dispatch without inputs behaves like a scheduled run (same gate), so a
  duplicate start is harmless; you can keep GitHub's schedule as a backup.

### Self-hosted runner near the service

> Only in a **private** repository. On a public one, anyone can open a pull
> request whose workflow then runs on your machine.

- A small Linux VM in a region close to the service, its clock synced (e.g.
  chrony), registered as a runner with its own label (Settings → Actions →
  Runners).
- In `run.yml`: `runs-on: [self-hosted, <label>]`, and a virtual environment
  step instead of `actions/setup-python`. Install Chromium's system libraries
  once on the VM (`playwright install-deps chromium`).
- Compare `measure.py` results before and after. A job for an offline runner
  waits in the queue, so keep the GitHub-hosted line at hand as a fallback.

### Your own computer

As in the local test, with your `config.yaml` and real secrets in the
environment, started by cron or Task Scheduler about 45 minutes before the
opening (`run` applies the same gate as `plan`). Keep the computer awake and
its clock synced.

## Reading the log

| Line | Meaning |
|---|---|
| `Next opening: … target date: …` | What the run is aiming at. |
| `Login step n/N: …`, `skipped` | Each login step; optional ones may be skipped. |
| `Logged in; HTTP session ready` | Login done and the session check passed. |
| `My bookings for …` | What you already hold. `RESULT: already booked` ends the run. |
| `Availability pre-check`, `Attempt order` | Which targets were free, and the order they will be tried in. |
| `Attempt N for X (sent T+0.000 s): …` | Each booking request, its send time relative to the opening, and the reply. |
| `X is taken`, `Switching to Y` | Fallback to the next target. |
| `Booking check: …` | My bookings after a success. `LOST …` means none of them holds the slot. |
| `RESULT: BOOKED …; booking id …` | The id comes from the successful reply. If duplicates were made, it can be a duplicate's id; the cleanup keeps the booking that holds the slot. |
| `Cleanup: …`, `After cleanup: …` | Duplicates cancelled. |
| `RESULT: FAILED …` | Nothing booked; the job fails. |

## Timing measurements

`measure.py` is read-only (it never books or cancels). Around an opening it
measures the local clock against NTP, cold connection costs (DNS, TCP, TLS,
first byte), whether an idle connection survives, the server's clock from the
HTTP `Date` header, and when the first target's availability changes. Results
go to `measure-results.json` and the job summary.

- On GitHub: Actions → *Measure timing* with `test_lead_min` (pretends the
  opening is that many minutes away), or empty for the real next opening.
- Locally, with the mock running: `TEST_LEAD_MIN=4 python measure.py --config config.example.yaml`

It uses the same account as the booking run. Don't run both for the same
opening unless the service allows parallel sessions.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Plan, login, booking, verification, cleanup. |
| `measure.py` | Read-only timing measurements. |
| `mock_server.py` | Local fake service for tests. |
| `config.example.yaml` | Annotated configuration, set up for the mock. |
| `.github/workflows/run.yml` | Booking workflow (manual; schedule ready to uncomment). |
| `.github/workflows/measure.yml` | Measurement workflow (manual). |

## Security notes

- Secrets only come from environment variables or GitHub secrets, and are
  masked in logs together with cookies, tokens and one-time codes. No
  screenshots or browser traces are saved.
- A TOTP secret is a full second factor: store it like a password.
- Many services refuse a one-time code that was already used. Don't start two
  runs for the same account within the same 30 seconds.
- Never commit real secrets or HAR files (`.gitignore` covers HAR files and traces).

## License

MIT, see [LICENSE](LICENSE).
