# openbanking-sync

Read-only PSD2 sync of Polish bank accounts (mBank, Bank Millennium, others)
into a local SQLite database, via [Enable Banking](https://enablebanking.com).

Built as the data layer for a personal finance app: it owns the bank
connection and the raw history, and everything downstream reads SQLite.

## Why an aggregator

mBank and Millennium both publish real PSD2 XS2A APIs, but using them directly
requires a KNF-issued AISP licence and qualified eIDAS certificates (QWAC +
QSealC) registered as a TPP. There is no personal-account API key at either
bank.

Enable Banking acts as the licensed TPP; this app rides on their licence. In
*restricted production* mode an application can only ever read accounts that
were explicitly linked to it — which is exactly the shape of a personal tool.

## What still needs a browser

Exactly one thing, and only twice a year: **SCA consent**. PSD2 requires the
account holder to authenticate at their own bank, and no provider can offer a
headless first consent. After that the API session works without a browser
until the consent expires — banks cap this between 90 and 180 days.

`obsync link` reduces that to one click: it opens the bank's authorisation
page, catches the redirect on a local listener, and stores the session.
`obsync status` tells you when re-authorisation is due.

Access is **account information only** (AIS). This app never initiates
payments.

## Setup

### 1. Register an application

At <https://enablebanking.com/cp/applications>:

- name the application (the name is shown to you during bank authorisation)
- whitelist redirect URL `http://localhost:8788/callback`
- the browser generates and downloads an RSA private key `<application-id>.pem`

### 2. Point the CLI at it

```sh
uv sync
uv run obsync init --application-id <uuid> --key ~/Downloads/<uuid>.pem
```

The key is copied to `~/.config/openbanking-sync/keys/` with mode 600 and the
downloaded copy is deleted. Nothing secret ever enters the working tree —
`config.json` and `*.pem` are gitignored, but they are not written here anyway.

### 3. Link the banks

```sh
uv run obsync banks -c PL              # exact ASPSP names
uv run obsync link --bank mBank -c PL
uv run obsync link --bank Millennium -c PL
```

Each opens a browser once. You log in at the bank yourself; no credential ever
passes through this app.

### 4. Sync

```sh
uv run obsync sync --full   # longest history the bank allows
uv run obsync sync          # incremental, from 7 days before the newest row
```

## Commands

| Command | Purpose |
| --- | --- |
| `obsync init` | store application id + private key |
| `obsync banks -c PL` | list supported banks and their consent limits |
| `obsync link --bank <name>` | authorise a bank (browser, once per consent) |
| `obsync accounts` | linked accounts, last sync, consent expiry |
| `obsync sync [--full]` | fetch balances and transactions |
| `obsync status` | totals, consent countdown, recent errors |
| `obsync tx [-s text] [-f csv\|json]` | query stored transactions |

## Data

Default location `~/.local/share/openbanking-sync/bank.sqlite3` (mode 600);
override with `OBSYNC_DB`.

Tables: `sessions`, `accounts`, `balances`, `transactions`, `sync_runs`.

Every row keeps the bank's original JSON in a `raw` column. The parsed columns
are a convenience view — PSD2 field coverage differs per bank, so nothing is
lost when a field is missing or named differently. Downstream code that needs a
field this parser does not surface can read it out of `raw` without a re-sync.

Transaction identity is `entry_reference` where the bank supplies one, and a
content hash otherwise (Polish banks routinely omit it on pending entries).
Re-syncing overlapping windows updates rows instead of duplicating them.

## Development

```sh
uv sync --all-groups
uv run pytest
```

## Scheduling

Sync is safe to run repeatedly. A user timer is enough:

```sh
systemd-run --user --on-calendar='*-*-* 07,19:00' \
  --unit=openbanking-sync ~/local/openbanking-sync/.venv/bin/obsync sync
```

Note that it will start failing once a consent expires — `obsync status`
surfaces that, and re-linking needs you at a browser.
