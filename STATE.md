# Email Ops state

Updated: 2026-09-25

## Current baseline

Personal project with Issue #1's deterministic Gmail baseline and Issue #3's untrusted-content boundary accepted. PR #8 merged at `2d2765f` as benchmark evidence and Issue #7 is closed. PR #10 merged at `4a60b34` as rejected mitigation evidence and Issue #9 is closed. Issue #11 is closed after private false-confidence analysis. PR #15 merged at `f4083a9` as rejected Issue #13 calibration evidence, and Issue #13 is closed. Issue #16's Stage A only cheap-model comparison completed after a provider-control diagnosis; all three benchmarked models are **REJECT**. This file is the sole repository implementation baseline; the Personal board owns workflow fields. The project is not production-ready.

## Completed

- Personal Hub intake #50 is registered once on board #1; the Personal Agent Skills allowlist is registered. The Gmail Desktop OAuth client and token remain private, and the granted scope is exactly `gmail.readonly`.
- The merged v0.1 adapter proved bounded real Gmail ingestion, canonical normalization, source provenance, structured evidence, and repeat reconciliation without duplicate records. The original 20-thread validation database is a pre-remediation snapshot.
- Issue #3 bounded read-only searches found one explicit live action case and one four-message waiting case. The repository path created one open action. The waiting thread moved NEEDS_JUDGMENT → WAITING_ON_OTHER → NEEDS_JUDGMENT while retaining one open waiting record. Exact repeat processing kept action counts at 1 thread / 1 action / 1 decision and waiting counts at 1 thread / 1 waiting record / 4 decisions. Source provenance matched. Detailed source IDs remain in a private local receipt outside Git.
- PR #4 implements an untrusted-email model-input envelope, source-linked suspicious instruction signals, strict source-bound model-result validation, and trusted reconciliation. No model is invoked. Twenty-one adversarial/boundary tests and the eight existing synthetic tests pass; the 29-test suite exits cleanly.
- Issue #5's merged shadow baseline compared four low-cost OpenRouter configurations without Gmail or authoritative SQLite changes. The nine original real cases had no defensible expected routes; no model qualified for promotion. Detailed cases and results remain private.
- Issue #7 privately labeled 11 unresolved real cases: 5 NO_ACTION, 1 REFERENCE, 1 OPERATIONAL_EVIDENCE, 1 STEPHEN_ACTION, and 3 HUMAN_JUDGMENT_REQUIRED. Gemini 3.1 Flash Lite was correct on 7/11, made 2/11 false-confident errors, and safely abstained on 2/11; only 1/3 human-judgment cases was safely unresolved. Event extraction was correct on 1/1 labeled event. Six paired clean/injected controls were repeated three times each: 8/18 route shifts went toward the injected target, including two patterns at 3/3. Gemini is rejected for promotion in this configuration. All evaluation remained shadow-only and private.
- Issue #9 tested source segmentation and full-versus-business-only dual-view agreement in shadow mode using the same 11 private labels and six paired controls repeated three times. The corrected payload yielded 0/18 injection-target shifts; 15 paired outputs abstained on dual-view disagreement. Real-case accuracy stayed 7/11, but false-confident routes rose to 4/11, safe unresolved fell to 0/11, and 0/3 human-judgment cases safely abstained. Paired clean-control accuracy fell from 18/18 to 15/18 because the encoded clean control abstained in 3/3 repeats. The configuration is **REJECT**, with no promotion trial justified. The 45-test suite exits cleanly; no authoritative model state was changed.
- Issue #11 privately reviewed all four false-confident real cases. Primary causes: 2 PERSONAL_CONTEXT_REQUIRED, 1 ROUTE_TAXONOMY_GAP in the operational event vocabulary, and 1 MODEL_CAPABILITY_ERROR. Three cases should have abstained; one had sufficient evidence for REFERENCE. No useful content was removed by segmentation in these four cases. A source alternative may contain extra wording in one case, but a material extraction defect was not established. No deterministic code change was justified.
- Issue #13 froze the existing 11 private cases before implementation or model calls: 8 SUFFICIENT, 2 INSUFFICIENT_PERSONAL_CONTEXT, and 1 INSUFFICIENT_EVENT_VOCABULARY; labels were unchanged. The three-repeat shadow run caught all 9 known-insufficient attempts before Stage B, but subtype accuracy was 3/9. Only 1/8 sufficient cases passed Stage A in all three repeats, and only 1/8 produced a correct final route on a majority of attempts. Real-case false-confident routes were 0/33, largely because Stage A over-abstained. Paired controls yielded 0/18 injection-target shifts but only 3/18 correct clean routes, below the prespecified 15/18 gate. The frozen dual-view Stage B remained unchanged. The run used 87 model calls and $0.03876525 in reported cost; one malformed provider response had no reported cost. The 56-test suite exits cleanly. This configuration is **REJECT** for promotion; no promotion trial is justified.
- Issue #16 selected `openai/gpt-6-luna-pro`, `qwen/qwen3-30b-a3b-instruct-2507`, and `google/gemini-3.1-flash-lite` for a common-contract Stage A comparison. The exact frozen 11-case corpus and read-only Gmail scope were reverified. Each model had a listed ZDR endpoint supporting structured output. The first request to Luna Pro with required privacy controls returned HTTP 404; the global stop fired after one call, zero case results, and $0 reported cost. No Stage A gate was evaluated and no candidate qualified. The added Stage A only benchmark path and aggregate evaluator passed 63 synthetic tests with clean exit. No Stage B call or authoritative persistence occurred.
- Issue #16 diagnosis: the Stage A transport now retains only HTTP status, provider error code, a redacted message capped at 160 characters, and provider name, reading at most 4 KB of error body. A non-private synthetic probe through the exact transport showed the original 404 was OpenRouter's "no endpoints can handle the requested parameters": no Luna Pro endpoint supports `temperature`, and its ZDR endpoints also lack `max_tokens`. Luna Pro is REJECT_REQUIRED_CONTROLS_UNAVAILABLE and was replaced before the benchmark by `mistralai/mistral-small-3.2-24b-instruct` ($0.09375 / $0.25 per 1M tokens), which passed the same probe with Qwen and Gemini. The frozen prompt, schema, temperature 0, 256 max tokens, and privacy controls were unchanged.
- Issue #16 results (hashes reverified; 33 attempts per model; all schema-valid): Mistral Small 3.2 caught 3/9 known-insufficient attempts, 0/9 exact subtypes, 2/8 sufficient cases all three runs, 1 inconsistent case. Qwen3 30B caught 6/9, 0/9 subtypes, 1/8 sufficient, 3 inconsistent cases. Gemini 3.1 Flash Lite caught 9/9, 3/9 subtypes, 1/8 sufficient, 0 inconsistent, reproducing its Issue #13 over-abstention. Every model is **REJECT**; no STAGE_A_CANDIDATE exists. Diagnostics and benchmark used 104 calls and $0.02338889 reported cost. The 94-test suite exits cleanly. No Stage B call, Gmail mutation, or authoritative persistence occurred.

## Pending

- A live waiting resolution was not found in the bounded search; only waiting creation and persistence through later replies were proven. Pattern-based suspicion signals are incomplete, and schema validation cannot prove a model route is semantically correct.
- Structural segmentation removed the observed target shifts in this small paired corpus but worsened real-case false confidence. Its security signal is useful, but it is **REVISE_LATER** for future model experiments, not an approved classification path. Segmentation remains heuristic and can miss inline or novel steering. Provider-side zero-retention was requested, but no retention guarantee beyond API acceptance is independently verified. No model-driven routing is authorized.
- The Issue #13 Stage A gate is experimental shadow code only. Its observed over-abstention leaves useful automation unproven. The private frozen labels and detailed results remain outside Git. No authoritative model routing or persistence is enabled.
- Issue #16's gates are measured and failed by every probe-cleared candidate: the non-Gemini models let known-insufficient cases through as SUFFICIENT, and all three over-abstain on sufficient cases. Subtype labeling is near-useless across models. Provider acceptance of ZDR and `data_collection=deny` is API-enforced routing, not an independently verified retention guarantee. No authoritative model routing or persistence is enabled.

## Deferred

- Gmail mutation, historical bulk cleanup, JD Delivery changes, and production model routing remain deferred.
- Gmail mutation, large historical runs, and cross-project changes retain Stephen's approval boundary.

## Blocked

- Model promotion remains blocked: earlier configurations were false-confident or over-abstained, and no Issue #16 model passed Stage A. Stop LLM semantic escalation for now.

## Next safe action

Review and merge PR #17 as REJECT evidence and close Issue #16. Keep deterministic routing authoritative; do not open another model experiment without a new approved issue.

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
- https://github.com/snicolello/email_ops/issues/13
- https://github.com/snicolello/email_ops/pull/15
- https://github.com/snicolello/email_ops/issues/16
- https://github.com/snicolello/email_ops/pull/17
- https://github.com/snicolello/stephen-project-hub/issues/50

