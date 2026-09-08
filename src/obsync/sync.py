"""Fetching account data into the local database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import db
from .api import ApiError, EnableBanking

# Re-fetch a few days behind the newest stored transaction: banks routinely
# amend or book entries late, and pending entries change identity when booked.
OVERLAP_DAYS = 7

# Most Polish banks expose at most 90 days of history over PSD2 without an
# explicit longer request; ask for two years and let the bank cut it short.
DEFAULT_FULL_HISTORY_DAYS = 730


@dataclass
class SyncResult:
    account_uid: str
    label: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    balances: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _account_label(row: sqlite3.Row) -> str:
    parts = [row["aspsp_name"]]
    if row["name"]:
        parts.append(row["name"])
    if row["iban"]:
        parts.append(f"…{row['iban'][-4:]}")
    elif row["currency"]:
        parts.append(row["currency"])
    return " / ".join(parts)


def default_date_from(conn: sqlite3.Connection, account_uid: str, full: bool) -> str:
    if full:
        return (date.today() - timedelta(days=DEFAULT_FULL_HISTORY_DAYS)).isoformat()
    row = conn.execute(
        "SELECT MAX(booking_date) AS newest FROM transactions WHERE account_uid = ?",
        (account_uid,),
    ).fetchone()
    newest = row["newest"] if row else None
    if not newest:
        return (date.today() - timedelta(days=90)).isoformat()
    try:
        anchor = date.fromisoformat(newest[:10])
    except ValueError:
        return (date.today() - timedelta(days=90)).isoformat()
    return (anchor - timedelta(days=OVERLAP_DAYS)).isoformat()


def sync_account(
    conn: sqlite3.Connection,
    client: EnableBanking,
    row: sqlite3.Row,
    date_from: str | None = None,
    date_to: str | None = None,
    full: bool = False,
) -> SyncResult:
    uid = row["uid"]
    result = SyncResult(account_uid=uid, label=_account_label(row))
    date_from = date_from or default_date_from(conn, uid, full)
    date_to = date_to or date.today().isoformat()

    run_id = db.start_run(conn, uid, date_from, date_to)
    conn.commit()

    try:
        result.balances = db.insert_balances(conn, uid, client.balances(uid))

        batch: list[dict] = []
        for transaction in client.transactions(uid, date_from=date_from, date_to=date_to):
            batch.append(transaction)
            if len(batch) >= 200:
                inserted, updated = db.upsert_transactions(conn, uid, batch)
                result.inserted += inserted
                result.updated += updated
                result.fetched += len(batch)
                conn.commit()
                batch = []
        if batch:
            inserted, updated = db.upsert_transactions(conn, uid, batch)
            result.inserted += inserted
            result.updated += updated
            result.fetched += len(batch)

        db.finish_run(conn, run_id, result.fetched, result.inserted, result.updated)
        conn.commit()
    except ApiError as exc:
        result.error = _explain(exc)
        db.finish_run(conn, run_id, result.fetched, result.inserted, result.updated, result.error)
        conn.commit()
    return result


def _explain(exc: ApiError) -> str:
    if exc.status in (401, 403):
        return (
            f"HTTP {exc.status} — the consent has most likely expired or was revoked. "
            f"Re-run `obsync link` for this bank. ({exc.body[:200]})"
        )
    if exc.status == 429:
        return f"HTTP 429 — bank rate limit hit; retry later. ({exc.body[:200]})"
    return f"HTTP {exc.status}: {exc.body[:300]}"


def accounts_to_sync(
    conn: sqlite3.Connection, only: list[str] | None = None
) -> list[sqlite3.Row]:
    if only:
        placeholders = ",".join("?" for _ in only)
        return list(
            conn.execute(
                f"""
                SELECT * FROM accounts
                WHERE uid IN ({placeholders})
                   OR iban IN ({placeholders})
                   OR aspsp_name IN ({placeholders})
                ORDER BY aspsp_name, name
                """,
                only * 3,
            )
        )
    return list(conn.execute("SELECT * FROM accounts ORDER BY aspsp_name, name"))


def expiring_sessions(conn: sqlite3.Connection, within_days: int = 14) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) + timedelta(days=within_days)).isoformat()
    return list(
        conn.execute(
            """
            SELECT * FROM sessions
            WHERE valid_until IS NOT NULL AND valid_until <= ?
            ORDER BY valid_until
            """,
            (cutoff,),
        )
    )
