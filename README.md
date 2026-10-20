# quota-rings

A small self-hosted dashboard for the quota windows of your AI subscriptions:
Claude (Max + Pro, or any mix), OpenRouter, MiniMax, and optionally Codex and
GitHub Copilot. SVG rings, a 60-second refresh, one process, no dependencies
beyond the Python standard library.

It is honest about what it can and cannot show. Where a provider has no
programmatic read path, the card says so instead of drawing a zero ring. Where
the vendor does not publish the number, the number is not guessed.

![rings](quota_rings/web/favicon.svg)

## What "refresh" means, precisely

This is the distinction the whole project is built around, so it is worth
stating plainly:

| Action | What it does | Consumes quota? |
|---|---|---|
| The 60s auto-refresh, the Refresh button, `?force=1` | Re-reads the provider's numbers and redraws the rings | **No** |
| A cache TTL expiring | Same | **No** |
| **Redeeming a Codex banked reset** | Really refills the provider's 5-hour and weekly windows | Yes, and the credit is finite |

Refresh is a *display* operation. quota-rings will never describe it as
resetting anything, and it cannot: no provider endpoint reachable from here
except Codex's banked reset does that, and that one is gated behind three
separate opt-ins plus an explicit confirmation (see below).

Anthropic does offer a real "limit reset", but only in a browser or Claude
Desktop. OpenRouter's `limit_reset` field is a *schedule* (daily/weekly/monthly
UTC), not an action. Neither is exposed as a button here, because neither can be
done from a script.

## Requirements

* Python **3.11 or newer** (for `tomllib`). No other dependency, ever.
* Linux, macOS or WSL. Nothing platform-specific.

Check yours:

```bash
python3 --version
```

## Install

```bash
git clone <your-fork-url> quota-rings
cd quota-rings

# Option A: run it in place (no install needed)
python3 -m quota_rings serve --config config.toml

# Option B: install the CLI
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
quota-rings serve --config config.toml
```

## Configure

```bash
cp config.example.toml config.toml
$EDITOR config.toml
```

Then put your credentials **somewhere else** and point the config at them. A
config file never contains a secret, so it is always safe to commit:

```bash
export CLAUDE_MAX_TOKEN='...'       # token = "env:CLAUDE_MAX_TOKEN"
export OPENROUTER_INFERENCE_KEY='...'  # token = "env:OPENROUTER_INFERENCE_KEY"
```

Three reference schemes are supported:

| Scheme | Reads |
|---|---|
| `env:NAME` | an environment variable |
| `file:/run/secrets/key` | the first line of a file (Docker/K8s/systemd secrets) |
| `command:pass show openrouter/infra` | the first line of a command's stdout |

### Multiple accounts per provider

Every provider entry is a list item, so watching two Claude subscriptions is two
blocks, not a fork:

```toml
[[account]]
id = "claude-max"
provider = "claude"
plan = "Max"
billing = "subscription"
price = 100
token = "env:CLAUDE_MAX_TOKEN"

[[account]]
id = "claude-pro"
provider = "claude"
plan = "Pro"
billing = "subscription"
price = 20
token = "env:CLAUDE_PRO_TOKEN"
```

Two things to know about the Claude plan label: the usage payload does **not**
name the plan, so `plan = "Max"` is your own annotation, and Claude's limits are
shared across Claude and Claude Code, so one account has one allowance no matter
how many tools use it.

### Billing: subscription vs pay-as-you-go

`billing = "subscription"` counts toward the "subscriptions / month" total in
the header. `billing = "payg"` does not: usage-based spend has no monthly price,
and adding one would make the number a lie. Those cards show `usage-based`
instead of a price.

## Run it

```bash
python3 -m quota_rings serve --config config.toml
# quota-rings on http://127.0.0.1:8790
```

| Command | Does |
|---|---|
| `serve` | run the dashboard |
| `once` | collect one snapshot, print JSON, exit (good for cron) |
| `check` | health/debug: which credentials resolve, from where. **Never prints a value** |
| `check --probe` | also fetch the providers once and report per-account health |

```bash
python3 -m quota_rings check --config config.toml --probe
```

### As a systemd user service

```ini
# ~/.config/systemd/user/quota-rings.service
[Unit]
Description=Quota Rings dashboard
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/quota-rings
ExecStart=/usr/bin/python3 -m quota_rings serve --config %h/quota-rings/config.toml
EnvironmentFile=%h/.config/quota-rings/env   # your tokens, chmod 600
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
chmod 600 ~/.config/quota-rings/env
systemctl --user enable --now quota-rings
systemctl --user status quota-rings
```

## Remote access

The dashboard binds `127.0.0.1` by default and **refuses** a non-loopback bind
unless you set `allow_remote_bind = true`. There is no authentication in this
program. If you expose it, put your own auth in front of it first — an SSH
tunnel is the easy answer:

```bash
ssh -N -L 8790:127.0.0.1:8790 you@host
```

## What each provider actually reports

`supported` means a documented public contract. `unstable` means it works but
the vendor publishes no schema, so it can change or vanish. `unsupported` means
there is no programmatic read path with that credential — no ring is drawn and
the reason is shown.

| Provider | Tier | Credential | Windows | Notes |
|---|---|---|---|---|
| **OpenRouter** | supported | inference key; optional management key | per-key spend cap (daily/weekly/monthly) | Best documented. More keys ≠ more capacity; limits are per account |
| **Claude** | unstable | Claude Code OAuth token | 5-hour + 7-day | Undocumented endpoint used by Claude Code. A Console API key bills pay-as-you-go and has no window at all |
| **MiniMax** | unstable | Subscription Key | 5-hour + 7-day | Vendor-published in prose, no published schema. Video is weekly-only, so it is text, not a ring |
| **Codex** | unstable, off by default | ChatGPT OAuth token | session (18000s) + weekly (604800s) | Undocumented backend; a plain HTTP client can hit an edge challenge. Holds the only real vendor reset |
| **Copilot** | unstable, off by default | GitHub token | calendar month (00:00 UTC on the 1st) | Internal endpoint, unsupported by GitHub. AI-credit vs legacy premium-request billing is read from the payload and labelled |
| **Mistral** | unsupported for individuals | Admin API key (**Enterprise only**) | none for individuals | The Admin API is Enterprise-only and a standard key cannot read admin endpoints, so subscription and pay-as-you-go usage are simply not visible to the account holder. Enabled Enterprise accounts get rate/spend-limit text, never an invented percentage |

Deliberately not implemented, because the data is not reachable: a vendor plan
name for Claude, any Mistral or MiniMax pay-as-you-go balance, and any automatic
vendor quota reset outside Codex.

## Vendor quota resets (Codex only)

Redeeming a banked Codex reset really does refill the 5-hour and weekly windows.
It is finite, does not replenish, and cannot be undone, so it takes **three**
independent switches:

1. `[server] enable_vendor_actions = true`
2. `allow_vendor_reset = true` on that specific `[[account]]`
3. `{"confirm": "redeem-banked-reset"}` in the POST body

All three default to off. There is no other write in the program, and no GET can
trigger one — `tests/test_server.py` asserts exactly that.

```bash
curl -X POST http://127.0.0.1:8790/api/accounts/codex/vendor-reset \
     -H 'Content-Type: application/json' \
     -d '{"confirm":"redeem-banked-reset"}'
```

The UI refuses to redeem when no window is fully used, because spending a finite
credit early wastes most of its value. Pass `"force": true` to override.

## HTTP API

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | the dashboard |
| `/api/usage` | GET | snapshot. `?force=1` re-reads the providers (still not a reset) |
| `/api/health` | GET | per-account freshness and staleness |
| `/api/check` | GET | credential sources present/absent, never values |
| `/api/meta` | GET | configured providers and their support tier |
| `/api/accounts/<id>/vendor-reset` | POST | the only write. Requires all three gates above |

The API returns percentages, reset instants and your own labels. It never
returns a credential, a vendor's raw payload, or an account identifier, and
every string is passed through a redactor that masks both the credentials this
process resolved and any token-shaped text from any source.

## Tests

```bash
# from the repo root, so the test package resolves
python3 -m unittest discover -s tests -t .
```

The suite is fully offline: every provider response is a fixture in
`tests/fixtures/`. It pins the specific traps that produce plausible-but-wrong
rings — a 1% reading treated as a fraction, MiniMax's HTTP-200 error envelope,
remaining-vs-used percentages, epoch milliseconds, and Codex windows labelled by
position instead of duration.

Before publishing anything:

```bash
./scripts/scan-secrets.sh
```

This scans the working tree **and the full git history** for credentials,
emails, personal paths and literal config secrets. `tests/test_selfscan.py` runs
the same checks inside the suite, so a leak fails the build rather than waiting
for a human to remember.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no Claude OAuth token configured` | `token` unset, or the env var is not visible to the service |
| `HTTP 403 edge challenge` on Codex | Expected sometimes. The endpoint expects the CLI's browser-shaped headers |
| MiniMax card errors with an HTTP 200 body | Correct: MiniMax reports auth failures in the body envelope, and that is treated as a failure |
| A card shows a reason instead of a ring | Working as intended. The provider has no read path for that credential |
| `/api/usage` returns stale numbers | An account failed and kept its last good reading. Check `/api/health` for the age and the error |

## License

MIT. See [LICENSE](LICENSE).
