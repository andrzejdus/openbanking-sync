"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import db, link, sync
from .api import ApiError, EnableBanking, max_consent_validity


def _client() -> tuple[EnableBanking, config_module.Config]:
    cfg = config_module.load()
    return EnableBanking(cfg), cfg


def _table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(out)


def _auth_approaches(aspsp: dict) -> str:
    """`auth_methods` is a list of objects, not strings, despite the quick-start docs."""
    approaches = []
    for method in aspsp.get("auth_methods") or []:
        if isinstance(method, dict):
            approach = method.get("approach")
            if approach and approach not in approaches:
                approaches.append(approach)
        elif isinstance(method, str) and method not in approaches:
            approaches.append(method)
    return ", ".join(approaches)


# -- commands --------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    key = Path(args.key).expanduser()
    if not key.exists():
        print(f"Private key not found: {key}", file=sys.stderr)
        return 1
    cfg = config_module.save(
        application_id=args.application_id,
        key_source=key,
        redirect_url=args.redirect_url,
        api_origin=args.api_origin,
    )
    print(f"Wrote {config_module.config_path()}")
    print(f"Private key stored at {cfg.key_path} (mode 600)")
    if args.keep_source:
        print(f"Source key left at {key} — delete it once you have verified the setup.")
    else:
        key.unlink()
        print(f"Removed the downloaded copy at {key}")

    client = EnableBanking(cfg)
    try:
        app = client.application()
    except ApiError as exc:
        print(f"Configuration saved but the API rejected it: {exc}", file=sys.stderr)
        return 1
    print(f"\nAuthenticated as application: {app.get('name')}")
    print(f"  environment:   {app.get('environment', 'unknown')}")
    print(f"  redirect URLs: {', '.join(app.get('redirect_urls', []))}")
    if cfg.redirect_url not in app.get("redirect_urls", []):
        print(
            f"\nWarning: {cfg.redirect_url} is not whitelisted for this application.\n"
            f"Add it in the Enable Banking control panel, or re-run init with one of the above.",
            file=sys.stderr,
        )
    return 0


def cmd_banks(args: argparse.Namespace) -> int:
    client, _ = _client()
    aspsps = client.aspsps(country=args.country)
    if args.search:
        needle = args.search.lower()
        aspsps = [a for a in aspsps if needle in a.get("name", "").lower()]
    rows = []
    for aspsp in sorted(aspsps, key=lambda a: (a.get("country", ""), a.get("name", ""))):
        validity = aspsp.get("maximum_consent_validity")
        rows.append(
            [
                aspsp.get("name", ""),
                aspsp.get("country", ""),
                ", ".join(aspsp.get("psu_types", [])),
                f"{int(validity) // 86400}d" if validity else "-",
                _auth_approaches(aspsp),
                "beta" if aspsp.get("beta") else "",
            ]
        )
    print(_table(rows, ["BANK", "CC", "PSU TYPES", "CONSENT", "AUTH", "FLAGS"]))
    print(f"\n{len(rows)} bank(s)")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    client, cfg = _client()
    redirect_url = args.redirect_url or cfg.redirect_url

    matches = [
        a
        for a in client.aspsps(country=args.country)
        if a.get("name", "").lower() == args.bank.lower()
    ]
    if not matches:
        matches = [
            a
            for a in client.aspsps(country=args.country)
            if args.bank.lower() in a.get("name", "").lower()
        ]
    if not matches:
        print(
            f"No bank matching {args.bank!r} in {args.country}. Try `obsync banks -c {args.country}`.",
            file=sys.stderr,
        )
        return 1
    if len(matches) > 1:
        print(f"{args.bank!r} matches several banks:", file=sys.stderr)
        for aspsp in matches:
            print(f"  {aspsp['name']}", file=sys.stderr)
        return 1

    aspsp = matches[0]
    valid_until = max_consent_validity(aspsp, args.days)

    use_listener = link.is_local(redirect_url) and not args.manual
    if use_listener and not link.port_is_free(redirect_url):
        print(f"Port for {redirect_url} is already in use; falling back to manual paste.")
        use_listener = False

    auth_url, state = client.start_authorization(
        aspsp_name=aspsp["name"],
        country=aspsp["country"],
        redirect_url=redirect_url,
        valid_until=valid_until,
        psu_type=args.psu_type,
    )

    print(f"\nBank:    {aspsp['name']} ({aspsp['country']})")
    print(f"Consent: valid until {link.describe_window(valid_until)}")
    print(f"\nOpen this URL and log in to authorise read-only access:\n\n  {auth_url}\n")

    if use_listener:
        if not args.no_browser:
            link.open_in_browser(auth_url)
        if link.is_https(redirect_url):
            print(
                "The callback uses a self-signed certificate, so the browser will\n"
                "warn once after the bank redirects. Accept it to finish linking."
            )
        print(f"Waiting for the redirect to {redirect_url} …")
        try:
            result = link.await_redirect(
                redirect_url,
                timeout=args.timeout,
                config_dir=config_module.config_dir(),
            )
        except (TimeoutError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if "error" in result:
            print(
                f"Bank returned an error: {result['error']} "
                f"{result.get('error_description', '')}",
                file=sys.stderr,
            )
            return 1
        if result.get("state") != state:
            print("State mismatch on the redirect — aborting.", file=sys.stderr)
            return 1
        code = result["code"]
    else:
        pasted = input("Paste the URL you were redirected to: ")
        try:
            code = link.code_from_pasted_url(pasted)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    session = client.create_session(code)
    conn = db.connect(config_module.db_path())
    db.upsert_session(
        conn,
        session,
        aspsp_name=aspsp["name"],
        aspsp_country=aspsp["country"],
        psu_type=args.psu_type,
        valid_until=valid_until.isoformat(),
    )
    accounts = session.get("accounts", [])
    for account in accounts:
        db.upsert_account(
            conn,
            account,
            session_id=session["session_id"],
            aspsp_name=aspsp["name"],
            aspsp_country=aspsp["country"],
        )
    conn.commit()

    print(f"\nLinked. Session {session['session_id']} — {len(accounts)} account(s):")
    for account in accounts:
        account_id = account.get("account_id") or {}
        print(
            f"  {account['uid']}  "
            f"{account_id.get('iban', '')}  "
            f"{account.get('name') or account.get('product') or ''} "
            f"{account.get('currency', '')}"
        )
    print("\nNow run `obsync sync --full` to pull history.")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    conn = db.connect(config_module.db_path())
    rows = []
    for row in conn.execute(
        """
        SELECT a.*, s.valid_until
        FROM accounts a LEFT JOIN sessions s ON s.session_id = a.session_id
        ORDER BY a.aspsp_name, a.name
        """
    ):
        rows.append(
            [
                row["uid"][:12] + "…",
                row["aspsp_name"],
                row["iban"] or "",
                (row["name"] or row["product"] or "")[:28],
                row["currency"] or "",
                (row["last_synced_at"] or "never")[:16],
                (row["valid_until"] or "")[:10],
            ]
        )
    print(
        _table(
            rows,
            ["UID", "BANK", "IBAN", "NAME", "CUR", "LAST SYNC", "CONSENT TO"],
        )
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    client, _ = _client()
    conn = db.connect(config_module.db_path())
    targets = sync.accounts_to_sync(conn, args.account)
    if not targets:
        print("No linked accounts. Run `obsync link --bank mBank -c PL` first.", file=sys.stderr)
        return 1

    failures = 0
    for row in targets:
        result = sync.sync_account(
            conn,
            client,
            row,
            date_from=args.date_from,
            date_to=args.date_to,
            full=args.full,
        )
        if result.ok:
            print(
                f"✓ {result.label}: {result.fetched} fetched, "
                f"{result.inserted} new, {result.updated} updated, "
                f"{result.balances} balance(s)"
            )
        else:
            failures += 1
            print(f"✗ {result.label}: {result.error}", file=sys.stderr)
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = db.connect(config_module.db_path())
    print(f"Database: {config_module.db_path()}")

    totals = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM accounts) AS accounts,
               (SELECT COUNT(*) FROM transactions) AS transactions,
               (SELECT MIN(booking_date) FROM transactions) AS oldest,
               (SELECT MAX(booking_date) FROM transactions) AS newest
        """
    ).fetchone()
    print(
        f"Accounts: {totals['accounts']}   Transactions: {totals['transactions']}"
        f"   Range: {totals['oldest'] or '-'} … {totals['newest'] or '-'}\n"
    )

    rows = []
    now = datetime.now(timezone.utc)
    for session in conn.execute("SELECT * FROM sessions ORDER BY valid_until"):
        remaining = ""
        if session["valid_until"]:
            try:
                delta = datetime.fromisoformat(session["valid_until"]) - now
                remaining = f"{delta.days}d" if delta.days >= 0 else "EXPIRED"
            except ValueError:
                remaining = "?"
        rows.append(
            [
                session["aspsp_name"],
                session["session_id"][:12] + "…",
                (session["valid_until"] or "")[:10],
                remaining,
            ]
        )
    print(_table(rows, ["BANK", "SESSION", "CONSENT TO", "LEFT"]))

    soon = sync.expiring_sessions(conn, within_days=14)
    if soon:
        print("\nRe-authorisation needed soon (browser login required):")
        for session in soon:
            print(f"  obsync link --bank {session['aspsp_name']!r} -c {session['aspsp_country']}")

    failed = list(
        conn.execute(
            "SELECT * FROM sync_runs WHERE error IS NOT NULL ORDER BY id DESC LIMIT 3"
        )
    )
    if failed:
        print("\nRecent sync errors:")
        for run in failed:
            print(f"  {run['started_at'][:16]}  {run['account_uid'][:12]}…  {run['error'][:100]}")
    return 0


def cmd_tx(args: argparse.Namespace) -> int:
    conn = db.connect(config_module.db_path())
    where = []
    params: list[object] = []
    if args.since:
        where.append("t.booking_date >= ?")
        params.append(args.since)
    if args.until:
        where.append("t.booking_date <= ?")
        params.append(args.until)
    if args.account:
        where.append("(t.account_uid = ? OR a.iban = ? OR a.aspsp_name = ?)")
        params.extend([args.account, args.account, args.account])
    if args.search:
        where.append("(t.remittance LIKE ? OR t.counterparty LIKE ?)")
        params.extend([f"%{args.search}%", f"%{args.search}%"])
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    query = f"""
        SELECT t.*, a.aspsp_name, a.iban
        FROM transactions t JOIN accounts a ON a.uid = t.account_uid
        {clause}
        ORDER BY t.booking_date DESC, t.tx_id
        LIMIT ?
    """
    params.append(args.limit)
    records = list(conn.execute(query, params))

    if args.format == "json":
        print(json.dumps([dict(r) for r in records], ensure_ascii=False, indent=2, default=str))
        return 0
    if args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(
            ["date", "bank", "amount", "currency", "dir", "counterparty", "remittance", "status"]
        )
        for r in records:
            writer.writerow(
                [
                    r["booking_date"],
                    r["aspsp_name"],
                    r["amount"],
                    r["currency"],
                    r["credit_debit"],
                    r["counterparty"],
                    r["remittance"],
                    r["status"],
                ]
            )
        return 0

    rows = [
        [
            (r["booking_date"] or "")[:10],
            r["aspsp_name"][:10],
            f"{'-' if r['credit_debit'] == 'DBIT' else '+'}{r['amount'] or ''}",
            r["currency"] or "",
            (r["counterparty"] or "")[:24],
            (r["remittance"] or "")[:40],
        ]
        for r in records
    ]
    print(_table(rows, ["DATE", "BANK", "AMOUNT", "CUR", "COUNTERPARTY", "DESCRIPTION"]))
    print(f"\n{len(rows)} transaction(s)")
    return 0


# -- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsync",
        description="Read-only PSD2 account sync into a local SQLite database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="store the Enable Banking application id and private key")
    p_init.add_argument("--application-id", required=True)
    p_init.add_argument("--key", required=True, help="path to the .pem downloaded from the control panel")
    p_init.add_argument("--redirect-url", default=config_module.DEFAULT_REDIRECT_URL)
    p_init.add_argument("--api-origin", default=config_module.DEFAULT_API_ORIGIN)
    p_init.add_argument(
        "--keep-source",
        action="store_true",
        help="do not delete the downloaded key after copying it into the config dir",
    )
    p_init.set_defaults(func=cmd_init)

    p_banks = sub.add_parser("banks", help="list supported banks")
    p_banks.add_argument("-c", "--country", default=None, help="ISO country code, e.g. PL")
    p_banks.add_argument("-s", "--search", default=None, help="filter by name substring")
    p_banks.set_defaults(func=cmd_banks)

    p_link = sub.add_parser("link", help="authorise a bank (opens a browser once)")
    p_link.add_argument("--bank", required=True, help="ASPSP name, e.g. mBank")
    p_link.add_argument("-c", "--country", default="PL")
    p_link.add_argument("--days", type=int, default=180, help="requested consent length (clamped by the bank)")
    p_link.add_argument("--psu-type", default="personal", choices=["personal", "business"])
    p_link.add_argument("--redirect-url", default=None)
    p_link.add_argument("--manual", action="store_true", help="paste the redirect URL instead of listening")
    p_link.add_argument("--no-browser", action="store_true", help="print the URL, do not open it")
    p_link.add_argument("--timeout", type=int, default=300)
    p_link.set_defaults(func=cmd_link)

    p_accounts = sub.add_parser("accounts", help="list linked accounts")
    p_accounts.set_defaults(func=cmd_accounts)

    p_sync = sub.add_parser("sync", help="fetch balances and transactions")
    p_sync.add_argument("-a", "--account", action="append", help="account uid, IBAN or bank name; repeatable")
    p_sync.add_argument("--full", action="store_true", help="ask for the longest history the bank allows")
    p_sync.add_argument("--date-from")
    p_sync.add_argument("--date-to")
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="consent expiry, totals and recent errors")
    p_status.set_defaults(func=cmd_status)

    p_tx = sub.add_parser("tx", help="query stored transactions")
    p_tx.add_argument("-a", "--account")
    p_tx.add_argument("--since")
    p_tx.add_argument("--until")
    p_tx.add_argument("-s", "--search")
    p_tx.add_argument("-n", "--limit", type=int, default=50)
    p_tx.add_argument("-f", "--format", choices=["table", "csv", "json"], default="table")
    p_tx.set_defaults(func=cmd_tx)

    return parser


def main(argv: list[str] | None = None) -> int:
    # `link` prints the authorisation URL and then blocks waiting for the
    # redirect. Under a pipe, block buffering would hold that URL back until
    # the wait is already over, so keep stdout line buffered either way.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config_module.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
