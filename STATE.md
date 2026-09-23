# Email Ops state

Updated: 2026-09-22

## Current baseline

Personal project with source Issue #1 as the v0.1 contract. This file is the sole repository implementation baseline; the Personal board owns workflow fields. The v0.1 implementation remains in draft PR #2, unmerged and unaccepted. It is not production-ready.

## Completed

- Standard four-file scaffold and Personal Agent Skills allowlist registration; allowlist PR #25 merged at `0c65b3b`.
- Personal Hub intake #50 is registered once on board #1. Verified after reload: Project=Email Ops, Workflow=In Progress, Effort=3, Risk=Medium, Priority=P1.
- Draft PR #2 proposes a read-only Gmail adapter, canonical threads, deterministic routing, SQLite reconciliation, and normalized event output. Five synthetic tests pass with writable temporary storage and pytest cache disabled.
- A separate connected Gmail review previously inspected 18 threads and 19 messages. It did not exercise repository OAuth or pipeline.
- Dedicated Google Cloud project `email-ops-personal` has Gmail API enabled and a Desktop OAuth client created. The consent configuration selected only `gmail.readonly`.

## Pending

- Resolve the Google OAuth audience boundary, replace the current Desktop client, securely download the new client JSON outside Git, then run the repository OAuth flow and the same bounded real-mail sample twice.
- Review draft PR #2 and compare real-mail outcomes with the proposed rules.

## Deferred

- Gmail mutation and historical bulk cleanup require later evaluation evidence and Stephen's explicit approval.
- JD Delivery changes require a separate cross-project contract.

## Blocked

- Google reports that OAuth access for the new client is restricted to users within the project's organization unless the consent screen is published and verified. The run stopped before consent. No client JSON was saved locally, no token exists, and the repository Gmail pipeline has not run against real mail.

## Next safe action

Resolve the Google project OAuth audience for personal Gmail without broadening scope, then create a fresh Desktop client, download its JSON to a private local path, and run the bounded read-only validation twice.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/stephen-project-hub/issues/50
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/stephen-agent-skills/pull/25
