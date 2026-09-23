# Email Ops state

Updated: 2026-09-23

## Current baseline

Personal project with Issue #1's deterministic Gmail baseline and Issue #3's untrusted-content boundary accepted. PR #8 merged at `2d2765f` as benchmark evidence and Issue #7 is closed. PR #10 merged at `4a60b34` as rejected mitigation evidence and Issue #9 is closed. Issue #11 is closed after private false-confidence analysis. This file is the sole repository implementation baseline; the Personal board owns workflow fields. The project is not production-ready.

## Completed

- Personal Hub intake #50 is registered once on board #1; the Personal Agent Skills allowlist is registered. The Gmail Desktop OAuth client and token remain private, and the granted scope is exactly `gmail.readonly`.
- The merged v0.1 adapter proved bounded real Gmail ingestion, canonical normalization, source provenance, structured evidence, and repeat reconciliation without duplicate records. The original 20-thread validation database is a pre-remediation snapshot.
- Issue #3 bounded read-only searches found one explicit live action case and one four-message waiting case. The repository path created one open action. The waiting thread moved NEEDS_JUDGMENT → WAITING_ON_OTHER → NEEDS_JUDGMENT while retaining one open waiting record. Exact repeat processing kept action counts at 1 thread / 1 action / 1 decision and waiting counts at 1 thread / 1 waiting record / 4 decisions. Source provenance matched. Detailed source IDs remain in a private local receipt outside Git.
- PR #4 implements an untrusted-email model-input envelope, source-linked suspicious instruction signals, strict source-bound model-result validation, and trusted reconciliation. No model is invoked. Twenty-one adversarial/boundary tests and the eight existing synthetic tests pass; the 29-test suite exits cleanly.
- Issue #5's merged shadow baseline compared four low-cost OpenRouter configurations without Gmail or authoritative SQLite changes. The nine original real cases had no defensible expected routes; no model qualified for promotion. Detailed cases and results remain private.
- Issue #7 privately labeled 11 unresolved real cases: 5 NO_ACTION, 1 REFERENCE, 1 OPERATIONAL_EVIDENCE, 1 STEPHEN_ACTION, and 3 HUMAN_JUDGMENT_REQUIRED. Gemini 3.1 Flash Lite was correct on 7/11, made 2/11 false-confident errors, and safely abstained on 2/11; only 1/3 human-judgment cases was safely unresolved. Event extraction was correct on 1/1 labeled event. Six paired clean/injected controls were repeated three times each: 8/18 route shifts went toward the injected target, including two patterns at 3/3. Gemini is rejected for promotion in this configuration. All evaluation remained shadow-only and private.
- Issue #9 tested source segmentation and full-versus-business-only dual-view agreement in shadow mode using the same 11 private labels and six paired controls repeated three times. The corrected payload yielded 0/18 injection-target shifts; 15 paired outputs abstained on dual-view disagreement. Real-case accuracy stayed 7/11, but false-confident routes rose to 4/11, safe unresolved fell to 0/11, and 0/3 human-judgment cases safely abstained. Paired clean-control accuracy fell from 18/18 to 15/18 because the encoded clean control abstained in 3/3 repeats. The configuration is **REJECT**, with no promotion trial justified. The 45-test suite exits cleanly; no authoritative model state was changed.
- Issue #11 privately reviewed all four false-confident real cases. Primary causes: 2 PERSONAL_CONTEXT_REQUIRED, 1 ROUTE_TAXONOMY_GAP in the operational event vocabulary, and 1 MODEL_CAPABILITY_ERROR. Three cases should have abstained; one had sufficient evidence for REFERENCE. No useful content was removed by segmentation in these four cases. A source alternative may contain extra wording in one case, but a material extraction defect was not established. No deterministic code change was justified.

## Pending

- A live waiting resolution was not found in the bounded search; only waiting creation and persistence through later replies were proven. Pattern-based suspicion signals are incomplete, and schema validation cannot prove a model route is semantically correct.
- Structural segmentation removed the observed target shifts in this small paired corpus but worsened real-case false confidence. Its security signal is useful, but it is **REVISE_LATER** for future model experiments, not an approved classification path. Segmentation remains heuristic and can miss inline or novel steering. Provider-side zero-retention was requested, but no retention guarantee beyond API acceptance is independently verified. No model-driven routing is authorized.

## Deferred

- Gmail mutation, historical bulk cleanup, JD Delivery changes, and production model routing remain deferred.
- Gmail mutation, large historical runs, and cross-project changes retain Stephen's approval boundary.

## Blocked

- Model promotion is blocked by real-case false confidence and unsafe handling of human-judgment cases. Bounded, read-only shadow experiments remain allowed.

## Next safe action

Design one bounded abstention-calibration experiment around evidence sufficiency and personal-context recognition using the existing private corpus; do not start model routing. Keep deterministic routing authoritative.

## Evidence

- https://github.com/snicolello/email_ops/issues/1
- https://github.com/snicolello/email_ops/issues/3
- https://github.com/snicolello/email_ops/pull/2
- https://github.com/snicolello/email_ops/pull/4
- https://github.com/snicolello/email_ops/issues/5
- https://github.com/snicolello/email_ops/pull/6
- https://github.com/snicolello/email_ops/issues/7
- https://github.com/snicolello/email_ops/pull/8
- https://github.com/snicolello/email_ops/issues/9
- https://github.com/snicolello/email_ops/pull/10
- https://github.com/snicolello/email_ops/issues/11
- https://github.com/snicolello/stephen-project-hub/issues/50

