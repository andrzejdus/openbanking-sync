"""Storage-layer tests: identity, idempotency and defensive parsing."""

from __future__ import annotations

import json

import pytest

from obsync import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.sqlite3")
    db.upsert_session(
        connection,
        {"session_id": "sess-1", "status": "AUTHORIZED", "accounts": []},
        aspsp_name="mBank",
        aspsp_country="PL",
        psu_type="personal",
        valid_until="2026-12-01T00:00:00+00:00",
    )
    db.upsert_account(
        connection,
        {
            "uid": "acc-1",
            "account_id": {"iban": "PL61109010140000071219812874"},
            "name": "eKonto",
            "currency": "PLN",
            "product": "eKonto osobiste",
        },
        session_id="sess-1",
        aspsp_name="mBank",
        aspsp_country="PL",
    )
    connection.commit()
    return connection


def booked(**overrides):
    tx = {
        "entry_reference": "REF-001",
        "status": "BOOK",
        "booking_date": "2026-09-01",
        "value_date": "2026-09-01",
        "transaction_amount": {"amount": "123.45", "currency": "PLN"},
        "credit_debit_indicator": "DBIT",
        "creditor": {"name": "Biedronka"},
        "creditor_account": {"iban": "PL27114020040000300201355387"},
        "remittance_information": ["PLATNOSC KARTA", "12345"],
        "bank_transaction_code": {"description": "Card payment"},
    }
    tx.update(overrides)
    return tx


def test_entry_reference_is_preferred_identity(conn):
    assert db.transaction_identity("acc-1", booked()) == "REF-001"


def test_identity_falls_back_to_hash_when_bank_omits_reference(conn):
    tx = booked()
    del tx["entry_reference"]
    identity = db.transaction_identity("acc-1", tx)
    assert identity.startswith("h:")
    # Stable across calls, and distinct per account.
    assert identity == db.transaction_identity("acc-1", tx)
    assert identity != db.transaction_identity("acc-2", tx)


def test_resync_updates_rather_than_duplicates(conn):
    inserted, updated = db.upsert_transactions(conn, "acc-1", [booked()])
    assert (inserted, updated) == (1, 0)

    inserted, updated = db.upsert_transactions(conn, "acc-1", [booked()])
    assert (inserted, updated) == (0, 1)

    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count == 1


def test_pending_becoming_booked_updates_status(conn):
    db.upsert_transactions(conn, "acc-1", [booked(status="PDNG")])
    db.upsert_transactions(conn, "acc-1", [booked(status="BOOK")])
    row = conn.execute("SELECT status FROM transactions").fetchone()
    assert row["status"] == "BOOK"


def test_parsed_columns(conn):
    db.upsert_transactions(conn, "acc-1", [booked()])
    row = conn.execute("SELECT * FROM transactions").fetchone()
    assert row["amount"] == "123.45"
    assert row["currency"] == "PLN"
    assert row["credit_debit"] == "DBIT"
    assert row["counterparty"] == "Biedronka"
    assert row["counterparty_account"] == "PL27114020040000300201355387"
    assert row["remittance"] == "PLATNOSC KARTA 12345"
    assert row["bank_tx_code"] == "Card payment"


def test_sparse_transaction_survives_and_keeps_raw(conn):
    """A bank returning almost nothing must not break the sync."""
    sparse = {"booking_date": "2026-09-02", "transaction_amount": {"amount": "9.99"}}
    inserted, _ = db.upsert_transactions(conn, "acc-1", [sparse])
    assert inserted == 1
    row = conn.execute(
        "SELECT * FROM transactions WHERE booking_date = '2026-09-02'"
    ).fetchone()
    assert row["currency"] is None
    assert row["counterparty"] is None
    assert json.loads(row["raw"]) == sparse


def test_remittance_falls_back_to_note(conn):
    tx = booked(entry_reference="REF-note")
    del tx["remittance_information"]
    tx["note"] = "Przelew wlasny"
    db.upsert_transactions(conn, "acc-1", [tx])
    row = conn.execute("SELECT remittance FROM transactions WHERE tx_id = 'REF-note'").fetchone()
    assert row["remittance"] == "Przelew wlasny"


def test_debtor_used_when_no_creditor(conn):
    tx = booked(entry_reference="REF-in", credit_debit_indicator="CRDT")
    del tx["creditor"]
    del tx["creditor_account"]
    tx["debtor"] = {"name": "PRACODAWCA SP Z O O"}
    tx["debtor_account"] = {"iban": "PL10105000997603123456789123"}
    db.upsert_transactions(conn, "acc-1", [tx])
    row = conn.execute("SELECT * FROM transactions WHERE tx_id = 'REF-in'").fetchone()
    assert row["counterparty"] == "PRACODAWCA SP Z O O"
    assert row["counterparty_account"] == "PL10105000997603123456789123"


def test_balances_recorded_with_snapshot_time(conn):
    written = db.insert_balances(
        conn,
        "acc-1",
        [
            {
                "name": "Available",
                "balance_type": "ITAV",
                "balance_amount": {"amount": "4200.00", "currency": "PLN"},
                "reference_date": "2026-09-08",
            }
        ],
    )
    assert written == 1
    row = conn.execute("SELECT * FROM balances").fetchone()
    assert row["amount"] == "4200.00"
    assert row["balance_type"] == "ITAV"
    assert row["fetched_at"]


def test_account_upsert_keeps_known_values_when_bank_omits_them(conn):
    db.upsert_account(
        conn,
        {"uid": "acc-1", "account_id": {}, "currency": "PLN"},
        session_id="sess-1",
        aspsp_name="mBank",
        aspsp_country="PL",
    )
    row = conn.execute("SELECT * FROM accounts WHERE uid = 'acc-1'").fetchone()
    assert row["iban"] == "PL61109010140000071219812874"
    assert row["name"] == "eKonto"


def test_sync_run_records_error(conn):
    run_id = db.start_run(conn, "acc-1", "2026-06-01", "2026-09-08")
    db.finish_run(conn, run_id, fetched=0, error="HTTP 403 consent expired")
    row = conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["error"].startswith("HTTP 403")
    assert row["finished_at"]


def test_failed_run_does_not_stamp_last_synced_at(conn):
    run_id = db.start_run(conn, "acc-1", "2026-06-01", "2026-09-08")
    db.finish_run(conn, run_id, error="HTTP 403")
    assert conn.execute("SELECT last_synced_at FROM accounts WHERE uid='acc-1'").fetchone()[0] is None

    run_id = db.start_run(conn, "acc-1", "2026-06-01", "2026-09-08")
    db.finish_run(conn, run_id, fetched=3, inserted=3)
    assert conn.execute("SELECT last_synced_at FROM accounts WHERE uid='acc-1'").fetchone()[0]
