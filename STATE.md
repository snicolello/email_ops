# Email Ops state

Updated: 2026-09-23

## Current baseline

Personal project with source Issue #1 closed as the bounded deterministic Gmail baseline. PR #2 merged at `eea88a5`; `main` was at `4dd7afb` when Issue #3 began. This file is the sole repository implementation baseline; the Personal board owns workflow fields. Issue #3's untrusted-content boundary was accepted by Stephen in PR #4 for the deterministic baseline. The project is not production-ready.

## Completed

- Personal Hub intake #50 is registered once on board #1; the Personal Agent Skills allowlist is registered. The Gmail Desktop OAuth client and token remain private, and the granted scope is exactly `gmail.readonly`.
- The merged v0.1 adapter proved bounded real Gmail ingestion, canonical normalization, source provenance, structured evidence, and repeat reconciliation without duplicate records. The original 20-thread validation database is a pre-remediation snapshot.
- Issue #3 bounded read-only searches found one explicit live action case and one four-message waiting case. The repository path created one open action. The waiting thread moved NEEDS_JUDGMENT → WAITING_ON_OTHER → NEEDS_JUDGMENT while retaining one open waiting record. Exact repeat processing kept action counts at 1 thread / 1 action / 1 decision and waiting counts at 1 thread / 1 waiting record / 4 decisions. Source provenance matched. Detailed source IDs remain in a private local receipt outside Git.
- PR #4 implements an untrusted-email model-input envelope, source-linked suspicious instruction signals, strict source-bound model-result validation, and trusted reconciliation. No model is invoked. Twenty-one adversarial/boundary tests and the eight existing synthetic tests pass; the 29-test suite exits cleanly.
- Issue #5's bounded shadow tournament ran four low-cost OpenRouter configurations against nine privately reviewed unresolved Gmail cases, eight adversarial fixtures, and four labeled synthetic controls. Six adversarial fixtures were stopped before model invocation by the accepted boundary. Shadow outcomes did not update Gmail or authoritative SQLite state. Results and source material remain private; only aggregate results belong in the PR.
- The nine real cases have no defensible expected route in the private review, so no correct real-case resolution rate or safe automation gain is established. Under no-data-collection and zero-retention provider routing, the candidates had schema-valid rates of 10/15 (GPT-OSS 20B), 14/15 (Qwen3.5-9B), 10/15 (Solar Mini 4), and 14/15 (Gemini 3.1 Flash Lite) on invoked cases. Every candidate made at least one false-confident error on labeled synthetic cases; none qualifies for promotion. GPT-OSS, Qwen, and Solar are rejected for this configuration; Gemini is retained only for further testing.

## Pending

- A live waiting resolution was not found in the bounded search; only waiting creation and persistence through later replies were proven. Pattern-based suspicion signals are incomplete, and schema validation cannot prove a model route is semantically correct.
- Private human adjudication of a usable real-case subset and stronger handling of obfuscated instruction-like material are needed before any promotion trial. The shadow experiment does not validate model-driven routing.

## Deferred

- OpenRouter implementation and benchmarking were outside Issue #3. A separate bounded cheap-model experiment is now cleared to begin; Gmail mutation, historical bulk cleanup, and JD Delivery changes remain deferred.
- Gmail mutation, large historical runs, and cross-project changes retain Stephen's approval boundary.

## Blocked

- No dependency gate remains for bounded, read-only OpenRouter experiments. Model promotion is blocked by absent real-case ground truth and observed synthetic false-confidence; production use and unattended model routing remain unapproved.

## Next safe action

Privately adjudicate a small, representative real unresolved set, then rerun only a narrowed shadow comparison with obfuscated-instruction controls. Keep deterministic routing authoritative.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/email_ops/issues/3
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/email_ops/pull/4
- https://github.com/snicolello/email_ops/issues/5
- https://github.com/snicolello/stephen-project-hub/issues/50

