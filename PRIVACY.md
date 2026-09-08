# Privacy Notice

Last updated: 2026-09-08

`openbanking-sync` is a personal, single-user tool. It is run by one individual
on their own computer, to read their own bank accounts. It is not a service
offered to anyone else, has no users other than its operator, and has no
server-side component.

## Who the operator is

The individual running this software on their own machine. For data protection
matters, contact the address registered with the application in the Enable
Banking control panel.

## What data is processed

When the operator authorises one of their own bank accounts, the following is
retrieved through Enable Banking's Account Information Service:

- account identifiers (IBAN, account name, product, currency)
- account balances
- transaction history: dates, amounts, currency, counterparty names and
  accounts, payment descriptions and bank transaction codes

## Whose data it is

Only the operator's own accounts. The application is registered in *restricted*
mode, which means it is technically limited to accounts the operator has
explicitly linked to it. It cannot read anyone else's accounts.

Transaction data unavoidably includes the names of counterparties the operator
has transacted with. That data is not used to contact, profile or evaluate
those counterparties. It is not shared, sold, published, or transferred to any
third party.

## Where it is stored

In a SQLite database file on the operator's own computer, readable only by the
operator's user account. Nothing is uploaded anywhere. There is no hosted
backend, no analytics, and no telemetry.

## Legal basis and purpose

The operator processes their own financial data for their own personal and
household purposes — reviewing their spending and building personal budgeting
tools. Under Article 2(2)(c) GDPR, purely personal or household processing
falls outside the Regulation's scope; this notice is published for transparency
regardless.

## Data sharing

None. Data flows from the bank, through Enable Banking as the licensed account
information service provider, to the operator's own machine, and stops there.

Enable Banking Oy processes the data in transit as an AISP regulated by the
Finnish Financial Supervisory Authority. Their handling is governed by their
own terms at <https://auth.enablebanking.com/terms>.

## Retention

The operator keeps the data as long as it is useful to them and deletes it by
removing the database file. Bank access itself expires automatically when the
PSD2 consent lapses (90–180 days depending on the bank) unless renewed.

## Access, correction and erasure

The operator holds the only copy and can inspect, export or delete it directly.
Consent to access the accounts can be withdrawn at any time in the bank's own
online banking, which immediately stops any further retrieval.

## Payments

This application requests account information only. It has no payment
initiation capability and cannot move money.
