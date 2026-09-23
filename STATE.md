# Email Ops state

Updated: 2026-09-23

## Current baseline

Personal project with source Issue #1 closed as the bounded deterministic Gmail baseline. PR #2 merged at `eea88a5`; `main` was at `4dd7afb` when Issue #3 began. This file is the sole repository implementation baseline; the Personal board owns workflow fields. Issue #3 work is proposed in draft PR #4, unmerged and unaccepted. The project is not production-ready.

## Completed

- Personal Hub intake #50 is registered once on board #1; the Personal Agent Skills allowlist is registered. The Gmail Desktop OAuth client and token remain private, and the granted scope is exactly `gmail.readonly`.
- The merged v0.1 adapter proved bounded real Gmail ingestion, canonical normalization, source provenance, structured evidence, and repeat reconciliation without duplicate records. The original 20-thread validation database is a pre-remediation snapshot.
- Issue #3 bounded read-only searches found one explicit live action case and one four-message waiting case. The repository path created one open action. The waiting thread moved NEEDS_JUDGMENT → WAITING_ON_OTHER → NEEDS_JUDGMENT while retaining one open waiting record. Exact repeat processing kept action counts at 1 thread / 1 action / 1 decision and waiting counts at 1 thread / 1 waiting record / 4 decisions. Source provenance matched. Detailed source IDs remain in a private local receipt outside Git.
- Draft PR #4 proposes an untrusted-email model-input envelope, source-linked suspicious instruction signals, strict source-bound model-result validation, and trusted reconciliation. No model is invoked. Twenty-one adversarial/boundary tests and the eight existing synthetic tests pass; the 29-test suite exits cleanly.

## Pending

- Review and accept PR #4's boundary before any OpenRouter evaluation. Issue #3 remains open until that review.
- A live waiting resolution was not found in the bounded search; only waiting creation and persistence through later replies were proven. Pattern-based suspicion signals are incomplete, and schema validation cannot prove a model route is semantically correct.

## Deferred

- OpenRouter integration and benchmarking, Gmail mutation, historical bulk cleanup, and JD Delivery changes are outside Issue #3.
- Gmail mutation, large historical runs, and cross-project changes retain Stephen's approval boundary.

## Blocked

- OpenRouter evaluation remains blocked until the untrusted-content boundary is accepted.

## Next safe action

Review draft PR #4 and its adversarial tests against Issue #3, then decide whether to accept the boundary for a separate bounded model experiment.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/email_ops/issues/3
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/email_ops/pull/4
- https://github.com/snicolello/stephen-project-hub/issues/50
