# Email Ops state

Updated: 2026-09-22

## Current baseline

Personal project with source Issue #1 as the v0.1 contract. This file is the sole repository implementation baseline; the Personal board owns workflow fields. The deterministic read-only Gmail baseline was reviewed and merged in PR #2 at `eea88a5`. Issue #1 remains open, and v0.1 is not production-ready.

## Completed

- Standard four-file scaffold and Personal Agent Skills allowlist registration; allowlist PR #25 merged at `0c65b3b`.
- Personal Hub intake #50 is registered once on board #1. Verified fields: Project=Email Ops, Workflow=In Progress, Effort=3, Risk=Medium, Priority=P1.
- PR #2 implements the read-only Gmail adapter, canonical threads, deterministic routing, SQLite reconciliation, and normalized event output. Eight synthetic tests pass and exit cleanly.
- Google Cloud project `email-ops-personal` has Gmail API enabled, External/Testing audience, one personal test user, and only `gmail.readonly` configured. Two exposed earlier clients were revoked. A fresh Desktop client completed repository-local OAuth; the saved token grants only `gmail.readonly`. Credentials, token, and database remain private and outside Git.
- The repository adapter processed a bounded personal Gmail sample of 20 threads and 20 messages: 6 Promotions, 2 Social, 11 Updates, 1 Personal. Initial routes: 9 NEEDS_JUDGMENT, 9 NO_ACTION, 2 OPERATIONAL_EVIDENCE. Two open events (one payment confirmation, one receipt) have source provenance. The exact sample was reconciled twice without duplicate records.
- Private review of the nine judgment cases found seven HTML-only messages whose bodies were not extracted. PR #2 now falls back to readable HTML when plain text is empty and no longer treats an unsubscribe footer alone as NO_ACTION. All nine exact source threads retained NEEDS_JUDGMENT and provenance after the fix; two in-memory reconciliation passes stayed at 9 threads, 0 records, and 9 decisions. The private case assessment remains outside Git.

## Pending

- Issue #1 remains open. Live STEPHEN_ACTION, WAITING_ON_OTHER, job-event, and multi-message thread-transition behavior remain unproven by this sample; the synthetic transition test passes.
- Evaluate the remaining judgment cases and targeted live coverage before claiming low manual judgment or accepting v0.1.

## Deferred

- Gmail mutation and historical bulk cleanup require later evaluation evidence and Stephen's explicit approval.
- JD Delivery changes require a separate cross-project contract.
- A bounded cheap-model assessment for residual ambiguity may be proposed separately; no model adapter is implemented.

## Blocked

- None for the merged deterministic read-only baseline.

## Next safe action

Select a small, exact read-only sample containing an action, a waiting item, and a multi-message thread where available, then validate those remaining Issue #1 behaviors without broad mailbox scanning.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/stephen-project-hub/issues/50
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/stephen-agent-skills/pull/25
