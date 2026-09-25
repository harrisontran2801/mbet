# mbet

Terminal client for a personal Marathonbet session. It speaks HTTPS only. It does not open Chrome, Firefox, Selenium, or Playwright.

Marathonbet does not publish a customer betting API that this project can honestly ship. Endpoints, payloads, CSRF headers, and response field names stay empty until you copy them from the HTTPS calls your own logged-in session already makes. Until then every account command fails with:

```text
Marathonbet transport adapter is not configured for this operation.
```

mbet will not guess those URLs, and it will not bypass CAPTCHA, MFA, bot protection, rate limits, device checks, or geo restrictions. If the site asks for an interactive check, the CLI stops and tells you to finish it in a normal browser.

## Requirements

- Python 3.12+ recommended. 3.10+ is supported.
- A Marathonbet account you are allowed to use.
- No browser driver.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The `mbet` command is installed, and `python -m mbet` works from this directory.

## Configure

Copy the example and edit it:

```bash
mkdir -p ~/.config/mbet
cp config/config.example.yaml ~/.config/mbet/config.yaml
```

`config/marathonbet.yaml` is the shipped default. It is merged under your user file. Environment variables win over both.

```yaml
base_url: "https://www.marathonbet.com"
currency: "USD"
max_stake: "10.00"
betting_enabled: false
timeout: 20
debug: false
timezone: "Asia/Ho_Chi_Minh"

endpoints:
  login: ""
  logout: ""
  balance: ""
  sports: ""
  events: ""
  event: ""
  odds: ""
  history: ""
  place_bet: ""
  search: ""
```

Fill `endpoints.*` and `request_templates.*.body` only after you have seen the real request. Map JSON fields under `response_maps`. A placeholder shape is in `config/config.example.yaml`. It is not a Marathonbet schema.

| Variable | Overrides |
| --- | --- |
| `MBET_CONFIG` | Path to the YAML file |
| `MBET_HOME` | Directory for the encrypted session (default `~/.config/mbet`) |
| `MBET_BASE_URL` | `base_url` |
| `MBET_CURRENCY` | `currency` |
| `MBET_MAX_STAKE` | `max_stake` |
| `MBET_BETTING_ENABLED` | `betting_enabled` (`true`/`false`) |
| `MBET_TIMEOUT` | `timeout` seconds |
| `MBET_DEBUG` | `debug` (`1` turns on redacted debug logs) |
| `MBET_TIMEZONE` | IANA timezone used to print start times |
| `MBET_ENDPOINT_LOGIN` (and the other endpoint names) | One endpoint path |
| `MBET_DISABLE_KEYRING` | `1` stores the session key in a mode-0600 file instead of the OS keychain |

Leave `betting_enabled` false until you intend to send a bet.

## Login

The password is prompted with hidden input. There is no `--password` flag, so it does not land in shell history. The password is not written to the session file, YAML, or logs.

```bash
python -m mbet login
```

```text
Marathonbet login
Username/email: you@example.com
Password:
Authenticating...
✓ Login successful
```

If the book returns a CAPTCHA, MFA, or device check, login stops. Complete that step in the browser. mbet will not solve it.

Cookies and the CSRF token (when you configure where it comes from) are encrypted at rest. The OS keychain holds the encryption key when it is available. Otherwise the key is a mode-0600 file under `MBET_HOME`.

```bash
python -m mbet logout
python -m mbet status
```

An expired session prints:

```text
✗ Marathonbet session expired.
Run:

mbet login
```

## Read the account

```bash
python -m mbet balance
python -m mbet sports
python -m mbet events --sport football
python -m mbet events --sport tennis
python -m mbet event 381923
python -m mbet odds 381923
python -m mbet search "Team A"
python -m mbet history
python -m mbet config
python -m mbet version
```

Balance is printed as a panel: amount, currency, and session state. Events are a table of id, local time, and match. Odds are grouped by market.

These calls use `GET` (or whatever method you configured) and may retry a failed read. They never place a bet.

## Dry run

Dry-run builds the confirmation and the request body, then stops.

```bash
python -m mbet bet --event 381923 --selection home --stake 1 --odds 1.85 \
  --event-name "Team A vs Team B" --selection-name "Team A" --dry-run
```

Nothing is posted. CSRF values are shown as `***`. If the place-bet endpoint or body template is still empty, the CLI says so instead of inventing a payload.

## Betting

Live submission is off unless both the config and the environment allow it:

```bash
# config.yaml
betting_enabled: true

# shell
export MBET_BETTING_ENABLED=true
export MBET_MAX_STAKE=10
```

A real bet always prints the slip and waits:

```text
Event:
Team A vs Team B
Selection:
Team A
Odds:
1.85
Stake:
$1.00
Potential return:
$1.85
Confirm bet? [y/N]:
```

The bet is sent only after you answer `y`. `--yes` skips that prompt. It is never the default.

```bash
python -m mbet bet --event 381923 --selection home --stake 1.00 --yes
```

Rules:

- Stake above `MBET_MAX_STAKE` / `max_stake` is rejected before any HTTP call.
- The place-bet request is sent once. It is never retried.
- HTTP 500, a timeout, or a body that does not clearly say accepted becomes:

```text
⚠ Bet status could not be confirmed.
No automatic retry was performed.
```

- The same event, selection, odds, and stake is then blocked until you check the account. `--acknowledge-unknown` allows one new submit only after you have verified the earlier one was not accepted.
- A clear rejection is `Bet rejected` and is not treated as unknown.

## Logging

Default level is INFO. `MBET_DEBUG=1` prints one JSON line per request and response. Passwords, cookies, authorization headers, session tokens, and CSRF tokens are redacted. Debug mode does not disable redaction.

## Tests

Tests use scripted HTTP responses. They do not call Marathonbet.

```bash
pytest
```

Covered: login state, session expiry, cookie persistence, CSRF, event and odds and balance parsing, malformed responses, rate limiting, dry-run, max stake, betting disabled, and no duplicate bet submission.

## Security

- Do not put passwords in source, YAML, `.env`, logs, or exception text.
- `.gitignore` excludes `.env`, session files, cookie files, token files, and virtualenvs.
- The User-Agent identifies mbet. It is not a fake browser fingerprint.
- Redirects are not followed.
- Non-HTTPS endpoints are refused except loopback, for tests.
- If a response is HTML or a challenge page, mbet stops. It does not scrape the site and it does not try to defeat the control.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Transport adapter is not configured | Set the endpoint and the request body from traffic you observed. |
| Login request body is not configured | Add `request_templates.login.body` with `{username}` and `{password}`. |
| Authentication failed | Check the account details. The password was not saved. |
| Session expired | Run `mbet login`. |
| Interactive verification required | Finish CAPTCHA, MFA, or the device check in the browser. |
| CSRF token is required but missing | Set `csrf.header` and `csrf.response_json` or `csrf.cookie`, then log in again. |
| Response was not valid JSON / HTML was returned | Point the endpoint at the JSON call, not the HTML page, and fix `response_maps`. |
| Stake exceeds maximum | Lower the stake or raise `MBET_MAX_STAKE` on purpose. |
| Bet submission is disabled | Reads still work. Set `betting_enabled` and `MBET_BETTING_ENABLED=true` only when you mean to send a bet. |
| Bet status could not be confirmed | Do not resubmit blindly. Check history on the site. |
| Stored session could not be decrypted | `mbet logout`, then `mbet login`. |

## Layout

```text
mbet/
  cli.py            commands
  client.py         MarathonClient
  adapter.py        MarathonAdapter and the configurable HTTP adapter
  auth.py           login state
  transport.py      HTTPS, retries for reads, cookies, CSRF
  bets.py           confirmation, limits, single submit
  markets.py        sports, events, odds parsing
  history.py        history parsing
  config.py         YAML and environment
  storage.py        encrypted session
  models.py         typed records
  errors.py
  logging_config.py
config/marathonbet.yaml
config/config.example.yaml
tests/
```

The CLI depends on `MarathonAdapter`. Swapping in a new transport does not require rewriting the commands.
