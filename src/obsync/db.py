"""SQLite storage.

Every row keeps the bank's original JSON in `raw`. Parsed columns are a
best-effort convenience view: PSD2 field coverage differs per bank, so nothing
is lost when a field this parser expects is missing or named differently.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    aspsp_name     TEXT NOT NULL,
    aspsp_country  TEXT NOT NULL,
    psu_type       TEXT,
    status         TEXT,
    created_at     TEXT NOT NULL,
    valid_until    TEXT,
    raw            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    uid            TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    aspsp_name     TEXT NOT NULL,
    aspsp_country  TEXT NOT NULL,
    iban           TEXT,
    name           TEXT,
    product        TEXT,
    currency       TEXT,
    usage          TEXT,
    cash_account_type TEXT,
    first_seen     TEXT NOT NULL,
    last_synced_at TEXT,
    raw            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
    account_uid    TEXT NOT NULL REFERENCES accounts(uid),
    balance_type   TEXT NOT NULL,
    name           TEXT,
    amount         TEXT,
    currency       TEXT,
    reference_date TEXT,
    fetched_at     TEXT NOT NULL,
    raw            TEXT NOT NULL,
    PRIMARY KEY (account_uid, balance_type, fetched_at)
);

CREATE TABLE IF NOT EXISTS transactions (
    account_uid    TEXT NOT NULL REFERENCES accounts(uid),
    tx_id          TEXT NOT NULL,
    entry_reference TEXT,
    status         TEXT,
    booking_date   TEXT,
    value_date     TEXT,
    transaction_date TEXT,
    amount         TEXT,
    currency       TEXT,
    credit_debit   TEXT,
    balance_after  TEXT,
    balance_after_currency TEXT,
    counterparty   TEXT,
    counterparty_account TEXT,
    remittance     TEXT,
    bank_tx_code   TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    raw            TEXT NOT NULL,
    PRIMARY KEY (account_uid, tx_id)
);

CREATE INDEX IF NOT EXISTS transactions_by_date
    ON transactions (booking_date DESC);
CREATE INDEX IF NOT EXISTS transactions_by_account_date
    ON transactions (account_uid, booking_date DESC);

CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_uid    TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    date_from      TEXT,
    date_to        TEXT,
    fetched        INTEGER DEFAULT 0,
    inserted       INTEGER DEFAULT 0,
    updated        INTEGER DEFAULT 0,
    error          TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    path.chmod(0o600)
    return conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` leaves an
# existing database untouched, so each one is added here and backfilled out of
# the `raw` JSON — no re-sync, and no lost history.
ADDED_COLUMNS = {
    "balance_after": "TEXT",
    "balance_after_currency": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
    added = [c for c in ADDED_COLUMNS if c not in existing]
    for column in added:
        conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {ADDED_COLUMNS[column]}")
    if not added:
        return
    rows = conn.execute(
        "SELECT account_uid, tx_id, raw FROM transactions WHERE balance_after IS NULL"
    ).fetchall()
    for row in rows:
        amount, currency = _amount(json.loads(row["raw"]).get("balance_after_transaction"))
        if amount is None:
            continue
        conn.execute(
            "UPDATE transactions SET balance_after = ?, balance_after_currency = ? "
            "WHERE account_uid = ? AND tx_id = ?",
            (amount, currency, row["account_uid"], row["tx_id"]),
        )
    conn.commit()


# -- parsing helpers -------------------------------------------------------


def _amount(node: Any) -> tuple[str | None, str | None]:
    if isinstance(node, dict):
        return node.get("amount"), node.get("currency")
    return None, None


def _party_name(tx: dict, *keys: str) -> str | None:
    for key in keys:
        node = tx.get(key)
        if isinstance(node, dict) and node.get("name"):
            return node["name"]
    return None


def _account_ref(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    if node.get("iban"):
        return node["iban"]
    other = node.get("other")
    if isinstance(other, dict):
        return other.get("identification")
    return None


def _remittance(tx: dict) -> str | None:
    unstructured = tx.get("remittance_information")
    if isinstance(unstructured, list):
        joined = " ".join(str(part) for part in unstructured if part)
        if joined.strip():
            return joined.strip()
    if isinstance(unstructured, str) and unstructured.strip():
        return unstructured.strip()
    for key in ("note", "additional_information"):
        value = tx.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def transaction_identity(account_uid: str, tx: dict) -> str:
    """Stable per-account transaction id.

    `entry_reference` is the bank's own id and is preferred, but it is optional
    in PSD2 and Polish banks frequently omit it on pending entries. The hash
    fallback keeps re-syncs idempotent instead of duplicating rows every run.
    """
    for key in ("entry_reference", "transaction_id"):
        value = tx.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    amount, currency = _amount(tx.get("transaction_amount"))
    material = "|".join(
        str(part or "")
        for part in (
            account_uid,
            tx.get("booking_date"),
            tx.get("value_date"),
            tx.get("transaction_date"),
            amount,
            currency,
            tx.get("credit_debit_indicator"),
            _party_name(tx, "creditor", "debtor"),
            _remittance(tx),
        )
    )
    return "h:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


# -- writes ----------------------------------------------------------------


def upsert_session(
    conn: sqlite3.Connection,
    session: dict,
    aspsp_name: str,
    aspsp_country: str,
    psu_type: str,
    valid_until: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, aspsp_name, aspsp_country, psu_type, status,
             created_at, valid_until, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            status = excluded.status,
            valid_until = excluded.valid_until,
            raw = excluded.raw
        """,
        (
            session["session_id"],
            aspsp_name,
            aspsp_country,
            psu_type,
            session.get("status"),
            now(),
            valid_until,
            json.dumps(session, ensure_ascii=False),
        ),
    )


def upsert_account(
    conn: sqlite3.Connection,
    account: dict,
    session_id: str,
    aspsp_name: str,
    aspsp_country: str,
) -> str:
    uid = account["uid"]
    account_id = account.get("account_id") or {}
    conn.execute(
        """
        INSERT INTO accounts
            (uid, session_id, aspsp_name, aspsp_country, iban, name, product,
             currency, usage, cash_account_type, first_seen, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            session_id = excluded.session_id,
            iban = COALESCE(excluded.iban, accounts.iban),
            name = COALESCE(excluded.name, accounts.name),
            product = COALESCE(excluded.product, accounts.product),
            currency = COALESCE(excluded.currency, accounts.currency),
            usage = COALESCE(excluded.usage, accounts.usage),
            cash_account_type = COALESCE(excluded.cash_account_type, accounts.cash_account_type),
            raw = excluded.raw
        """,
        (
            uid,
            session_id,
            aspsp_name,
            aspsp_country,
            account_id.get("iban") if isinstance(account_id, dict) else None,
            account.get("name") or account.get("details"),
            account.get("product"),
            account.get("currency"),
            account.get("usage"),
            account.get("cash_account_type"),
            now(),
            json.dumps(account, ensure_ascii=False),
        ),
    )
    return uid


def insert_balances(conn: sqlite3.Connection, account_uid: str, balances: Iterable[dict]) -> int:
    stamp = now()
    rows = []
    for balance in balances:
        amount, currency = _amount(balance.get("balance_amount"))
        rows.append(
            (
                account_uid,
                balance.get("balance_type") or "UNKNOWN",
                balance.get("name"),
                amount,
                currency,
                balance.get("reference_date") or balance.get("last_change_date_time"),
                stamp,
                json.dumps(balance, ensure_ascii=False),
            )
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO balances
            (account_uid, balance_type, name, amount, currency,
             reference_date, fetched_at, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def upsert_transactions(
    conn: sqlite3.Connection, account_uid: str, transactions: Iterable[dict]
) -> tuple[int, int]:
    """Return (inserted, updated)."""
    stamp = now()
    inserted = updated = 0
    for tx in transactions:
        tx_id = transaction_identity(account_uid, tx)
        amount, currency = _amount(tx.get("transaction_amount"))
        existing = conn.execute(
            "SELECT 1 FROM transactions WHERE account_uid = ? AND tx_id = ?",
            (account_uid, tx_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO transactions
                (account_uid, tx_id, entry_reference, status, booking_date,
                 value_date, transaction_date, amount, currency, credit_debit,
                 balance_after, balance_after_currency,
                 counterparty, counterparty_account, remittance, bank_tx_code,
                 first_seen, last_seen, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_uid, tx_id) DO UPDATE SET
                status = excluded.status,
                booking_date = COALESCE(excluded.booking_date, transactions.booking_date),
                value_date = COALESCE(excluded.value_date, transactions.value_date),
                amount = excluded.amount,
                currency = excluded.currency,
                credit_debit = excluded.credit_debit,
                balance_after = COALESCE(excluded.balance_after, transactions.balance_after),
                balance_after_currency = COALESCE(
                    excluded.balance_after_currency, transactions.balance_after_currency),
                counterparty = COALESCE(excluded.counterparty, transactions.counterparty),
                remittance = COALESCE(excluded.remittance, transactions.remittance),
                last_seen = excluded.last_seen,
                raw = excluded.raw
            """,
            (
                account_uid,
                tx_id,
                tx.get("entry_reference"),
                tx.get("status"),
                tx.get("booking_date"),
                tx.get("value_date"),
                tx.get("transaction_date"),
                amount,
                currency,
                tx.get("credit_debit_indicator"),
                *_amount(tx.get("balance_after_transaction")),
                _party_name(tx, "creditor", "debtor"),
                _account_ref(tx.get("creditor_account") or tx.get("debtor_account")),
                _remittance(tx),
                (tx.get("bank_transaction_code") or {}).get("description")
                if isinstance(tx.get("bank_transaction_code"), dict)
                else None,
                stamp,
                stamp,
                json.dumps(tx, ensure_ascii=False),
            ),
        )
        if existing:
            updated += 1
        else:
            inserted += 1
    return inserted, updated


def start_run(conn: sqlite3.Connection, account_uid: str, date_from: str, date_to: str) -> int:
    cursor = conn.execute(
        "INSERT INTO sync_runs (account_uid, started_at, date_from, date_to) VALUES (?, ?, ?, ?)",
        (account_uid, now(), date_from, date_to),
    )
    return cursor.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sync_runs
        SET finished_at = ?, fetched = ?, inserted = ?, updated = ?, error = ?
        WHERE id = ?
        """,
        (now(), fetched, inserted, updated, error, run_id),
    )
    if error is None:
        # A failed run must not look like a successful sync in `obsync accounts`.
        conn.execute(
            """
            UPDATE accounts SET last_synced_at = ?
            WHERE uid = (SELECT account_uid FROM sync_runs WHERE id = ?)
            """,
            (now(), run_id),
        )
