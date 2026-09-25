"""Issue #16 Stage A only comparison; no route classification or persistence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import mean, median

from .core import Thread
from .model_boundary import prepare_model_input
from .sufficiency_shadow import (
    SUFFICIENCY_STATES, SufficiencyClient, SufficiencyOutcome,
    sufficiency_payload, validate_sufficiency_result,
)


CANDIDATE_MODELS = (
    "openai/gpt-6-luna-pro",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "google/gemini-3.1-flash-lite",
)
# Pre-benchmark replacements for provider incompatibility only; each value must
# pass the same synthetic probe before any private case is sent.
PRE_BENCHMARK_REPLACEMENTS: dict[str, str] = {
    # Luna Pro exposes no endpoint accepting temperature under require_parameters.
    "openai/gpt-6-luna-pro": "mistralai/mistral-small-3.2-24b-instruct",
}
MAX_BENCHMARK_MODELS = 3


def stage_a_allowlist() -> tuple[str, ...]:
    return CANDIDATE_MODELS + tuple(PRE_BENCHMARK_REPLACEMENTS.values())


def select_benchmark_models(dispositions: dict[str, str],
                            replacements: dict[str, str] | None = None) -> tuple[str, ...]:
    """Keep probe-cleared originals; swap only control-incompatible ones."""
    replacements = PRE_BENCHMARK_REPLACEMENTS if replacements is None else replacements
    selected = []
    for original in CANDIDATE_MODELS:
        disposition = dispositions.get(original)
        if disposition == "PROBE_PASS":
            selected.append(original)
        elif original in replacements:
            if disposition != "REJECT_REQUIRED_CONTROLS_UNAVAILABLE":
                raise ValueError("replacement is approved only for unavailable controls")
            if dispositions.get(replacements[original]) == "PROBE_PASS":
                selected.append(replacements[original])
    if any(model not in CANDIDATE_MODELS for model in replacements) or len(set(selected)) != len(selected):
        raise ValueError("invalid replacement mapping")
    return tuple(selected[:MAX_BENCHMARK_MODELS])


def assess_candidate(thread: Thread, owner: str, model_id: str,
                     client: SufficiencyClient) -> SufficiencyOutcome:
    """Use the frozen Stage A prompt/payload/validator, never Stage B."""
    if model_id not in stage_a_allowlist() or model_id not in client.allowed_model_ids:
        raise ValueError("unapproved Stage A benchmark model")
    envelope = prepare_model_input(thread, owner)
    if envelope is None:
        return SufficiencyOutcome(model_id, None, None, None, False, None,
                                  "NEEDS_JUDGMENT", "not_eligible_or_missing_source",
                                  0, None, None, 0, None)
    identity = (envelope.source_provider, envelope.source_thread_id,
                envelope.source_message_id)
    if envelope.suspicious_signals:
        return SufficiencyOutcome(model_id, *identity, False, None,
                                  "NEEDS_JUDGMENT", "suspicious_source_instruction",
                                  0, None, None, 0, None)
    reply = client.complete(model_id, sufficiency_payload(envelope))
    if reply.model_id != model_id:
        return SufficiencyOutcome(model_id, *identity, False, None,
                                  "NEEDS_JUDGMENT", "provider_model_mismatch", 1,
                                  reply.input_tokens, reply.output_tokens,
                                  reply.latency_ms, reply.cost_usd)
    provider_error = getattr(reply, "provider_error", None)
    provider_name = getattr(reply, "provider_name", None)
    if reply.error_code:
        return SufficiencyOutcome(model_id, *identity, False, None,
                                  "NEEDS_JUDGMENT", reply.error_code, 1,
                                  reply.input_tokens, reply.output_tokens,
                                  reply.latency_ms, reply.cost_usd,
                                  provider_error, provider_name)
    state, rejection = validate_sufficiency_result(envelope, reply.result)
    return SufficiencyOutcome(model_id, *identity, rejection is None, state,
                              "NEEDS_JUDGMENT", rejection, 1,
                              reply.input_tokens, reply.output_tokens,
                              reply.latency_ms, reply.cost_usd,
                              None, provider_name)


@dataclass(frozen=True)
class LabeledAttempt:
    private_case_key: str
    expected_state: str
    repetition: int
    outcome: SufficiencyOutcome


def aggregate_attempts(attempts: list[LabeledAttempt]) -> dict:
    """Return aggregate metrics only; never include source or private case keys."""
    if not attempts:
        raise ValueError("no attempts")
    model_ids = {item.outcome.model_id for item in attempts}
    if len(model_ids) != 1 or next(iter(model_ids)) not in stage_a_allowlist():
        raise ValueError("mixed or unsupported model")
    by_case: dict[str, list[LabeledAttempt]] = defaultdict(list)
    for item in attempts:
        if item.expected_state not in SUFFICIENCY_STATES or item.repetition not in (1, 2, 3):
            raise ValueError("invalid frozen label or repetition")
        by_case[item.private_case_key].append(item)
    if any(len(rows) != 3 or {row.repetition for row in rows} != {1, 2, 3}
           or len({row.expected_state for row in rows}) != 1 for rows in by_case.values()):
        raise ValueError("incomplete or inconsistent three-run case")
    insufficient = [x for x in attempts if x.expected_state != "SUFFICIENT"]
    sufficient = [x for x in attempts if x.expected_state == "SUFFICIENT"]
    predictions = [x.outcome.state if x.outcome.accepted else "INVALID" for x in attempts]
    confusion = Counter((x.expected_state,
                         x.outcome.state if x.outcome.accepted else "INVALID") for x in attempts)
    costs = [x.outcome.cost_usd for x in attempts if x.outcome.cost_usd is not None]
    summary = {
        "model_id": next(iter(model_ids)),
        "cases": len(by_case), "attempts": len(attempts),
        "known_insufficient_caught": sum(x.outcome.accepted and
            x.outcome.state in SUFFICIENCY_STATES[1:] for x in insufficient),
        "known_insufficient_total": len(insufficient),
        "exact_subtype_correct": sum(x.outcome.accepted and
            x.outcome.state == x.expected_state for x in insufficient),
        "sufficient_all_three": sum(all(x.outcome.accepted and
            x.outcome.state == "SUFFICIENT" for x in rows)
            for rows in by_case.values() if rows[0].expected_state == "SUFFICIENT"),
        "sufficient_cases": len(sufficient) // 3,
        "false_sufficient": sum(x.outcome.accepted and x.outcome.state == "SUFFICIENT"
                                for x in insufficient),
        "false_insufficient": sum(x.outcome.accepted and
            x.outcome.state in SUFFICIENCY_STATES[1:] for x in sufficient),
        "invalid_on_sufficient": sum(not x.outcome.accepted for x in sufficient),
        "consistent_cases": sum(len({x.outcome.state if x.outcome.accepted else "INVALID"
                                     for x in rows}) == 1 for rows in by_case.values()),
        "inconsistent_cases": sum(len({x.outcome.state if x.outcome.accepted else "INVALID"
                                       for x in rows}) > 1 for rows in by_case.values()),
        "valid_schema": sum(x.outcome.accepted for x in attempts),
        "invalid_schema_or_provider": sum(not x.outcome.accepted for x in attempts),
        "error_codes": dict(Counter(x.outcome.error_code for x in attempts
                                    if x.outcome.error_code)),
        "confusion": {f"{expected} -> {predicted}": count
                      for (expected, predicted), count in sorted(confusion.items())},
        "predictions": dict(Counter(predictions)),
        "reported_cost_usd": sum(costs),
        "calls_without_reported_cost": len(attempts) - len(costs),
        "mean_reported_cost_per_call_usd": mean(costs) if costs else None,
        "mean_latency_ms": mean(x.outcome.latency_ms for x in attempts),
        "median_latency_ms": median(x.outcome.latency_ms for x in attempts),
    }
    summary["stage_a_candidate"] = (
        summary["cases"] == 11 and summary["attempts"] == 33
        and summary["known_insufficient_caught"] == 9
        and summary["exact_subtype_correct"] >= 8
        and summary["sufficient_all_three"] >= 7
    )
    return summary

