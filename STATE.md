# Email Ops state

Updated: 2026-09-22

## Current baseline

Personal project with source Issue #1 as the v0.1 contract. This file is the sole repository implementation baseline; the Personal board owns workflow fields. The v0.1 implementation remains in draft PR #2, unmerged and unaccepted. It is not production-ready.

## Completed

- Standard four-file scaffold and Personal Agent Skills allowlist registration; allowlist PR #25 merged at `0c65b3b`.
- Personal Hub intake #50 is registered once on board #1. Verified after reload: Project=Email Ops, Workflow=In Progress, Effort=3, Risk=Medium, Priority=P1.
- Draft PR #2 proposes the read-only Gmail adapter, canonical threads, deterministic routing, SQLite reconciliation, and normalized event output. Five synthetic tests pass with writable temporary storage and pytest cache disabled.
- Google Cloud project `email-ops-personal` has Gmail API enabled, External/Testing audience, one personal test user, and only `gmail.readonly` configured. Two earlier clients were revoked after their secrets appeared in task images. A fresh Desktop client was saved outside Git and used for repository-local OAuth; the saved token grants only `gmail.readonly`.
- The repository adapter and core processed one bounded personal Gmail sample of 20 threads and 20 messages: 6 Promotions, 2 Social, 11 Updates, 1 Personal; no multi-message thread. Routes: 9 NEEDS_JUDGMENT, 9 NO_ACTION, 2 OPERATIONAL_EVIDENCE. Two open events (one payment confirmation, one receipt) have complete source provenance.
- The exact same in-memory sample was reconciled a second time. SQLite counts remained 20 threads, 2 records, and 20 decisions; no blind duplication.

## Pending

- Review draft PR #2 and the nine uncertain live cases before accepting or expanding routing rules.
- Live STEPHEN_ACTION, WAITING_ON_OTHER, and thread-state transition behavior remain unproven by this sample; no multi-message thread was present.

## Deferred

- Gmail mutation and historical bulk cleanup require later evaluation evidence and Stephen's explicit approval.
- JD Delivery changes require a separate cross-project contract.

## Blocked

- None for the bounded read-only prototype.

## Next safe action

Review draft PR #2 against the aggregate live results and inspect the nine judgment cases privately before deciding on targeted rule changes or further bounded coverage.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/stephen-project-hub/issues/50
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/stephen-agent-skills/pull/25
