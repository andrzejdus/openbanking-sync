# AGENTS.md

Guidance for coding agents working in this repository.

Claude Code compatibility path: `CLAUDE.md` is a symlink to this file. Edit
`AGENTS.md` as the single source of truth.

## What this is

Read-only PSD2 account sync (mBank, Bank Millennium) into local SQLite, through
Enable Banking as the licensed TPP. It is the data layer for a personal finance
app that does not exist yet; treat the SQLite schema as the interface other
code will depend on.

## Hard rules

- **Never commit or print secrets.** The application id and the RSA private key
  live in `~/.config/openbanking-sync/`, never in the tree. Do not echo the
  contents of `config.json` or any `.pem`.
- **Never print real account data into a transcript.** Real IBANs, balances and
  transaction descriptions are the user's financial history. Use aggregate
  counts when reporting, and the seeded fixtures in `tests/` for examples.
- **Account information only.** Enable Banking also exposes payment initiation;
  this app must not gain a payment path without an explicit request.
- The user always authenticates at the bank themselves. Never automate a bank
  login, never ask for banking credentials, never store them.

## Design decisions worth keeping

- `db.py` stores the bank's original JSON in every `raw` column and treats
  parsed columns as best-effort. PSD2 field coverage varies per bank, so a
  parser miss must degrade to a null column, never to a dropped row or a crash.
- Transaction identity (`db.transaction_identity`) prefers `entry_reference`
  and falls back to a content hash. Changing the hash inputs re-keys every
  historical row — treat it as a migration, not an edit.
- Syncs overlap by `sync.OVERLAP_DAYS` because banks amend and late-book
  entries. Upserts must stay idempotent; `tests/test_db.py` guards this.
- A failed `sync_run` must not stamp `accounts.last_synced_at`.

## Testing

`uv run pytest`. Tests use fixture data only and touch no network. Anything
that would need a live bank belongs behind a manual command, not a test.
