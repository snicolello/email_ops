"""Stage A comparison cannot route, persist, or weaken the frozen contract."""

import json

import pytest

from email_ops.core import Message, Thread
from email_ops.openrouter_shadow import ProviderReply
from email_ops.sufficiency_benchmark import (
    CANDIDATE_MODELS, LabeledAttempt, aggregate_attempts, assess_candidate,
)
from email_ops.sufficiency_shadow import SufficiencyClient, SufficiencyOutcome


OWNER = "owner@example.com"


def source():
    return Thread("gmail", "synthetic-thread", (
        Message("synthetic-message", "sender@example.com", OWNER,
                "Question", "A private business question.", "1"),))


class FakeClient:
    allowed_model_ids = CANDIDATE_MODELS

    def __init__(self, state="SUFFICIENT", *, error=None, provenance_ok=True):
        self.state, self.error, self.provenance_ok = state, error, provenance_ok
        self.calls = 0

    def complete(self, model_id, payload):
        self.calls += 1
        context = payload["trusted_context"]
        result = {"source_provider": context["source_provider"],
                  "source_thread_id": context["source_thread_id"],
                  "source_message_id": context["source_message_id"] if self.provenance_ok else "wrong",
                  "sufficiency": self.state}
        return ProviderReply(model_id, result if not self.error else None,
                             100, 20, 5, self.error, 0.0001)


def test_every_candidate_uses_same_route_free_contract(monkeypatch):
    bodies = []

    class Response:
        ok = True

        def __init__(self, payload):
            self.payload = payload

        def iter_content(self, chunk_size):
            context = self.payload["trusted_context"]
            result = {"source_provider": context["source_provider"],
                      "source_thread_id": context["source_thread_id"],
                      "source_message_id": context["source_message_id"],
                      "sufficiency": "SUFFICIENT"}
            yield json.dumps({"choices": [{"message": {"content": json.dumps(result)}}],
                              "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                        "cost": 0.0001}}).encode()

        def close(self):
            pass

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        bodies.append(body)
        return Response(json.loads(body["messages"][1]["content"]))

    monkeypatch.setattr("email_ops.sufficiency_shadow.requests.post", fake_post)
    client = SufficiencyClient(api_key="test-only", allowed_model_ids=CANDIDATE_MODELS)
    for model_id in CANDIDATE_MODELS:
        outcome = assess_candidate(source(), OWNER, model_id, client)
        assert outcome.model_id == model_id and outcome.accepted
        assert outcome.state == "SUFFICIENT"
    first = {key: value for key, value in bodies[0].items() if key != "model"}
    assert all({key: value for key, value in body.items() if key != "model"} == first
               for body in bodies[1:])
    assert first["provider"] == {"require_parameters": True,
                                  "data_collection": "deny", "zdr": True}
    assert "tools" not in first
    assert "route" not in first["response_format"]["json_schema"]["schema"]["properties"]


@pytest.mark.parametrize("client,expected_error", [
    (FakeClient(error="http_503"), "http_503"),
    (FakeClient(provenance_ok=False), "missing_or_conflicting_provenance"),
    (FakeClient(state="UNSUPPORTED"), "unsupported_sufficiency_state"),
])
def test_provider_and_schema_failures_abstain_without_stage_b(monkeypatch, client, expected_error):
    def forbidden(*args, **kwargs):
        raise AssertionError("Stage B must never run in this benchmark")
    monkeypatch.setattr("email_ops.semantic_mitigation.evaluate_dual_shadow", forbidden)
    outcome = assess_candidate(source(), OWNER, CANDIDATE_MODELS[0], client)
    assert outcome.error_code == expected_error
    assert outcome.outward_route == "NEEDS_JUDGMENT" and not outcome.accepted
    assert client.calls == 1


def test_default_issue13_transport_still_rejects_other_models():
    client = SufficiencyClient(api_key="test-only")
    with pytest.raises(ValueError):
        client.complete(CANDIDATE_MODELS[0], {})


def test_aggregate_records_candidate_identity_stability_and_prespecified_gates():
    model_id = CANDIDATE_MODELS[0]
    attempts = []
    for case_number in range(11):
        expected = ("SUFFICIENT" if case_number < 8 else
                    "INSUFFICIENT_PERSONAL_CONTEXT" if case_number < 10 else
                    "INSUFFICIENT_EVENT_VOCABULARY")
        for repetition in (1, 2, 3):
            state = expected
            outcome = SufficiencyOutcome(model_id, "gmail", "private", "private", True,
                                         state, "NEEDS_JUDGMENT", None,
                                         1, 100, 20, 5, 0.0001)
            attempts.append(LabeledAttempt(f"synthetic-{case_number}", expected,
                                           repetition, outcome))
    summary = aggregate_attempts(attempts)
    assert summary["model_id"] == model_id and summary["stage_a_candidate"]
    assert summary["known_insufficient_caught"] == 9
    assert summary["exact_subtype_correct"] == 9
    assert summary["sufficient_all_three"] == 8
    assert summary["consistent_cases"] == 11 and summary["inconsistent_cases"] == 0
    assert "private" not in str(summary) and "synthetic-" not in str(summary)
    changed = list(attempts)
    row = changed[0]
    changed[0] = LabeledAttempt(row.private_case_key, row.expected_state,
                                row.repetition, SufficiencyOutcome(
                                    model_id, "gmail", "private", "private", True,
                                    "INSUFFICIENT_SOURCE_EVIDENCE", "NEEDS_JUDGMENT",
                                    None, 1, 100, 20, 5, 0.0001))
    failed = aggregate_attempts(changed)
    assert failed["false_insufficient"] == 1
    assert failed["inconsistent_cases"] == 1
    for index, item in enumerate(changed):
        if item.private_case_key == "synthetic-8" and item.repetition == 1:
            changed[index] = LabeledAttempt(item.private_case_key, item.expected_state,
                                            item.repetition, SufficiencyOutcome(
                                                model_id, "gmail", "private", "private", True,
                                                "SUFFICIENT", "NEEDS_JUDGMENT",
                                                None, 1, 100, 20, 5, 0.0001))
            break
    failed = aggregate_attempts(changed)
    assert failed["false_sufficient"] == 1
    assert not failed["stage_a_candidate"]


def test_aggregate_rejects_incomplete_repetitions():
    one = SufficiencyOutcome(CANDIDATE_MODELS[0], "gmail", "x", "y", True,
                             "SUFFICIENT", "NEEDS_JUDGMENT", None,
                             1, 1, 1, 1, 0.00001)
    with pytest.raises(ValueError):
        aggregate_attempts([LabeledAttempt("synthetic", "SUFFICIENT", 1, one)])

