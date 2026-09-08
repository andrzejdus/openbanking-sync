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

Exactly one thing, and only twice a year per bank: **SCA consent**. PSD2 requires the
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

- choose the **Production** environment (a Sandbox application only ever sees
  mock banks — mBank is not in its ASPSP list at all)
- name the application (the name is shown to you during bank authorisation)
- whitelist redirect URL `https://localhost:8788/callback` — production
  applications reject `http://`, so the scheme matters
- production additionally requires a description, a data-protection email, and
  public privacy and terms URLs; this repo's [PRIVACY.md](PRIVACY.md) and
  [TERMS.md](TERMS.md) exist for that
- the browser generates and downloads an RSA private key `<application-id>.pem`

A new production application starts **Inactive**. It activates once you link
your first account to it ("Activate by linking accounts" in the control panel),
and from then on only linked accounts are readable. Until it is active, every
API call returns `403 Application is not active`.

That linking step is **per bank, not once**. A bank you have not linked in the
control panel will still let you authorise successfully — and hand back a
session with zero accounts. `obsync link` detects that case, explains it, and
declines to save the useless session. So each bank costs two logins the first
time: one to whitelist it in the control panel, one for the session this tool
actually reads through. Renewals afterwards are a single login.

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

Because the callback must be HTTPS, the listener serves a self-signed
certificate generated on first use and kept in `~/.config/openbanking-sync/`.
Your browser will warn once when the bank redirects back — accept it and the
link completes. The certificate is deliberately never regenerated, so you only
have to do that once rather than at every consent renewal.

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

Sync is idempotent, so it is safe to run repeatedly. Ready-made units are in
[`contrib/`](contrib):

```sh
cp contrib/openbanking-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now openbanking-sync.timer
```

Twice a day is deliberate. PSD2 caps *unattended* account access — anything not
accompanied by a fresh SCA — at four requests per day per account, and in
practice both banks ignore `date_from` and return their whole history on every
call, so an "incremental" sync costs the same as a full one. Syncing more often
buys nothing and risks `429`s.

It will start failing once a consent expires — `obsync status` surfaces that,
and re-linking needs you at a browser.

## What the banks actually return

Written from real data, because the PSD2 spec is a poor guide to any individual
bank. Both populate `booking_date`, `entry_reference`, amount, currency,
direction and a description on every row; neither ever sends
`bank_transaction_code` or `value_date`. Beyond that they are complementary:

| Field | mBank | Bank Millennium |
| --- | --- | --- |
| `transaction_date` | all rows | never |
| `balance_after_transaction` | all rows | never |
| counterparty name / account | 40% of rows | all rows |

mBank omits the counterparty on card payments, naming the merchant only in the
description. So `booking_date` is the only date field you can rely on across
both, and any consumer should treat every other column as optional — reading
the untouched bank JSON from `raw` when it needs more.
