# Email Ops state

Updated: 2026-09-22

## Current baseline

New Personal project. Source Issue #1 defines the active v0.1 contract. This file is the sole repository implementation baseline; the Personal board owns workflow fields. The v0.1 proposal is in draft PR #2; it is not accepted or production-ready.

## Completed

- Standard four-file scaffold generated using the installed Personal project-onboarding bootstrap procedure.
- Read-only Gmail adapter, canonical threads, deterministic routing, SQLite reconciliation, and normalized event output proposed in draft PR #2. Five synthetic tests pass.
- A separate connected Personal Gmail review fetched 18 bounded threads (19 messages, one multi-message thread). This did not exercise the repository OAuth or pipeline. The sample contained promotional/social mail, job notifications, a payment confirmation, and an ambiguous payment reminder; it did not establish a waiting case or validated action classification.

## Pending

- Review draft PR #2 and reconcile observed real-mail cases against the repository pipeline.
- Verify repository-local OAuth against Stephen's personal Gmail, then run a bounded real-mail sample and record aggregate evidence without publishing content.
- Complete Personal Hub allowlist, intake, and portfolio registration.
- Personal Hub intake #50 exists; the allowlist edit is proposed in draft Agent Skills PR #25. Live board registration remains unverified.

## Deferred

- Gmail mutation and historical bulk cleanup require later evaluation evidence and Stephen's explicit approval.
- JD Delivery changes require a separate cross-project contract.

## Blocked

- Repository-local OAuth needs a Google desktop OAuth client configuration kept outside Git.

## Next safe action

Review draft PR #2, supply a local desktop OAuth client configuration, and run the bounded CLI sample against personal Gmail.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/stephen-project-hub/issues/50
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/stephen-agent-skills/pull/25

