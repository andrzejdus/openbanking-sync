# Terms of Use

Last updated: 2026-09-08

`openbanking-sync` is a personal tool published as source code. These terms
cover its use by anyone who chooses to run their own copy.

## What it does

It reads the account balances and transaction history of bank accounts that
you own and have explicitly authorised, through Enable Banking's Account
Information Service, and stores them in a SQLite database on your own machine.

## What it does not do

It cannot initiate payments or move money — no such capability exists in the
code. It cannot access accounts you have not authorised. It does not transmit
your data anywhere: there is no hosted component, no analytics and no
telemetry.

## Your responsibilities

- Run it only against accounts you own or are lawfully entitled to access.
- Register your own Enable Banking application; do not share private keys.
- Protect the resulting database — it contains your full financial history in
  readable form.
- Comply with your bank's own terms of service.

## Authentication

You authenticate directly with your bank. This software never asks for, sees,
or stores your banking credentials. Consent is granted through your bank's own
strong customer authentication and can be withdrawn there at any time.

## Third-party service

Bank access is provided by Enable Banking Oy, a registered Account Information
Service Provider supervised by the Finnish Financial Supervisory Authority.
Your use of their API is additionally governed by their terms at
<https://auth.enablebanking.com/terms>.

## No warranty

This software is provided "as is", without warranty of any kind, express or
implied. Bank data may be incomplete, delayed, or amended by the bank after
retrieval. Do not rely on it as an authoritative record of your finances —
your bank's own statements are authoritative.

## Limitation of liability

To the maximum extent permitted by law, the author is not liable for any loss
or damage arising from use of this software, including decisions made on the
basis of data it retrieves.

## Licence

See [LICENSE](LICENSE).
