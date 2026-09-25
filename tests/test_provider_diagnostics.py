"""Issue #16 sanitized provider errors, synthetic probe, and replacement rules."""

import json
import sys

import pytest

from email_ops import provider_probe
from email_ops.core import Message, Thread
from email_ops.provider_probe import (
    CallBudget, classify_probe, eligible_endpoints, probe_model, synthetic_probe_thread,
)
from email_ops.sufficiency_benchmark import (
    CANDIDATE_MODELS, assess_candidate, select_benchmark_models,
)
from email_ops.sufficiency_shadow import (
    ERROR_BODY_LIMIT_BYTES, SufficiencyClient, SufficiencyOutcome,
    parse_provider_error, sanitize_error_message,
)


OWNER = "owner@example.com"
MODEL = CANDIDATE_MODELS[0]


def private_thread():
    return Thread("gmail", "private-thread-7f3a", (
        Message("private-message-9c1d", "alice@private.example", OWNER,
                "Hospital appointment", "Your cardiology appointment is confirmed.", "1"),))


class ErrorResponse:
    ok = False

    def __init__(self, status, body: bytes):
        self.status_code, self.body, self.read = status, body, 0

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            chunk = self.body[start:start + chunk_size]
            self.read += len(chunk)
            yield chunk

    def close(self):
        pass


def post_returning(monkeypatch, response):
    monkeypatch.setattr("email_ops.sufficiency_shadow.requests.post",
                        lambda url, **kwargs: response)
    return SufficiencyClient(api_key="test-only", allowed_model_ids=CANDIDATE_MODELS)


def test_structured_404_keeps_only_status_code_message_and_provider(monkeypatch):
    body = json.dumps({"error": {
        "code": 404,
        "message": "No endpoints found that can handle the requested parameters.",
        "metadata": {"provider_name": "Azure", "raw": "private raw provider body"}}}).encode()
    client = post_returning(monkeypatch, ErrorResponse(404, body))
    outcome = assess_candidate(private_thread(), OWNER, MODEL, client)
    assert outcome.error_code == "http_404" and not outcome.accepted
    error = outcome.provider_error
    assert (error.http_status, error.code, error.provider_name) == (404, "404", "Azure")
    assert error.message == "No endpoints found that can handle the requested parameters."
    assert "raw" not in repr(error) and outcome.outward_route == "NEEDS_JUDGMENT"


@pytest.mark.parametrize("body", [b"", b"<html>not json", b"{\"error\": \"flat\"}",
                                  b"\xff\xfe\x00garbage"])
def test_empty_or_malformed_body_degrades_to_http_status(monkeypatch, body):
    client = post_returning(monkeypatch, ErrorResponse(404, body))
    outcome = assess_candidate(private_thread(), OWNER, MODEL, client)
    assert outcome.error_code == "http_404"
    assert outcome.provider_error.http_status == 404
    assert (outcome.provider_error.code, outcome.provider_error.message,
            outcome.provider_error.provider_name) == (None, None, None)


def test_oversized_error_body_reads_at_most_limit(monkeypatch):
    body = b"{\"error\": {\"message\": \"" + b"x" * 200_000 + b"\"}}"
    response = ErrorResponse(502, body)
    client = post_returning(monkeypatch, response)
    outcome = assess_candidate(private_thread(), OWNER, MODEL, client)
    assert outcome.error_code == "http_502"
    assert response.read <= ERROR_BODY_LIMIT_BYTES
    assert outcome.provider_error.message is None  # truncated JSON degrades safely


def test_long_message_is_capped():
    error = parse_provider_error(400, json.dumps({"error": {"message": "bad " * 200}}).encode())
    assert len(error.message) <= 160


@pytest.mark.parametrize("secret", [
    "alice@private.example", "Bearer abc.def.ghi", "sk-or-v1-0123456789abcdef",
    "deadbeefdeadbeefdeadbeefdeadbeef", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY1234abcd",
])
def test_redacts_secret_like_material(secret):
    message = sanitize_error_message(f"Provider rejected {secret} here")
    assert secret not in message and "[redacted]" in message


def test_redacts_request_derived_and_quoted_content(monkeypatch):
    body = json.dumps({"error": {"code": "invalid_prompt", "message":
        "Content flagged: cardiology appointment in 'Hospital appointment' for private-thread-7f3a",
        "metadata": {"provider_name": "Example"}}}).encode()
    client = post_returning(monkeypatch, ErrorResponse(400, body))
    outcome = assess_candidate(private_thread(), OWNER, MODEL, client)
    message = outcome.provider_error.message
    for private in ("cardiology", "appointment", "Hospital", "7f3a"):
        assert private not in message
    assert outcome.provider_error.code == "invalid_prompt"


def test_unsafe_code_and_provider_name_are_dropped():
    error = parse_provider_error(404, json.dumps({"error": {
        "code": "bad code with spaces and <html>", "message": "m",
        "metadata": {"provider_name": "x" * 200}}}).encode())
    assert error.code is None and error.provider_name is None


def test_success_captures_serving_provider(monkeypatch):
    class Ok:
        ok = True

        def iter_content(self, chunk_size):
            yield json.dumps({"provider": "DeepInfra", "choices": [{"message": {
                "content": json.dumps({"source_provider": "gmail",
                                       "source_thread_id": "private-thread-7f3a",
                                       "source_message_id": "private-message-9c1d",
                                       "sufficiency": "SUFFICIENT"})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.00001}}).encode()

        def close(self):
            pass

    client = post_returning(monkeypatch, Ok())
    outcome = assess_candidate(private_thread(), OWNER, MODEL, client)
    assert outcome.accepted and outcome.provider_name == "DeepInfra"
    assert outcome.provider_error is None


def test_frozen_request_contract_unchanged_by_diagnostics(monkeypatch):
    bodies = []

    def fake_post(url, **kwargs):
        bodies.append(kwargs["json"])
        return ErrorResponse(404, b"")

    monkeypatch.setattr("email_ops.sufficiency_shadow.requests.post", fake_post)
    client = SufficiencyClient(api_key="test-only", allowed_model_ids=CANDIDATE_MODELS)
    assess_candidate(synthetic_probe_thread(), OWNER, MODEL, client)
    body = bodies[0]
    assert body["temperature"] == 0 and body["max_tokens"] == 256
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny", "zdr": True}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in body and "reasoning" not in body


ZDR = [{"model_id": "a/model", "tag": "zdr-ok"}, {"model_id": "a/model", "tag": "zdr-no-temp"}]
ENDPOINTS = [
    {"tag": "zdr-ok", "supported_parameters": ["response_format", "temperature", "max_tokens"]},
    {"tag": "zdr-no-temp", "supported_parameters": ["response_format", "max_tokens"]},
    {"tag": "not-zdr", "supported_parameters": ["response_format", "temperature", "max_tokens"]},
]


def test_eligible_endpoints_require_zdr_and_every_frozen_parameter():
    assert eligible_endpoints(ENDPOINTS, ZDR, "a/model") == ("zdr-ok",)
    assert eligible_endpoints(ENDPOINTS[1:], ZDR, "a/model") == ()


def outcome(accepted=False, error=None, calls=1):
    return SufficiencyOutcome(MODEL, "synthetic", "t", "m", accepted,
                              "SUFFICIENT" if accepted else None, "NEEDS_JUDGMENT",
                              error, calls, None, None, 1, None)


@pytest.mark.parametrize("in_catalog,eligible,result,expected", [
    (False, 0, None, "MODEL_ID_NOT_IN_CATALOG"),
    (True, 1, outcome(True), "PROBE_PASS"),
    (True, 0, outcome(error="http_404"), "REJECT_REQUIRED_CONTROLS_UNAVAILABLE"),
    (True, 2, outcome(error="http_404"), "INCONCLUSIVE"),
    (True, 1, outcome(error="http_503"), "INCONCLUSIVE"),
    (True, 1, outcome(error="missing_or_conflicting_provenance"), "PROBE_FAIL_SCHEMA"),
    (True, 1, outcome(error="malformed_provider_output"), "PROBE_FAIL_SCHEMA"),
    (True, 1, None, "INCONCLUSIVE"),
])
def test_probe_dispositions(in_catalog, eligible, result, expected):
    assert classify_probe(in_catalog, eligible, result) == expected


def test_probe_uses_synthetic_thread_counts_budget_and_skips_uncatalogued(monkeypatch):
    sent = []

    def fake_assess(thread, owner, model_id, client):
        sent.append(thread)
        return outcome(error="http_404")

    monkeypatch.setattr(provider_probe, "assess_candidate", fake_assess)
    budget = CallBudget(calls=1)
    catalog = [{"id": MODEL, "pricing": {"prompt": "1", "completion": "2"}}]
    record = probe_model(MODEL, catalog, ENDPOINTS[1:], [], None, budget)
    assert record.disposition == "REJECT_REQUIRED_CONTROLS_UNAVAILABLE"
    assert not record.contract_satisfiable and budget.calls == 2
    assert sent[0].provider == "synthetic"
    missing = probe_model("x/missing", catalog, [], [], None, budget)
    assert missing.disposition == "MODEL_ID_NOT_IN_CATALOG" and budget.calls == 2
    exhausted = probe_model(MODEL, catalog, ENDPOINTS, ZDR, None, CallBudget(calls=150))
    assert exhausted.disposition == "INCONCLUSIVE" and len(sent) == 1


def test_probe_module_never_imports_gmail():
    assert "email_ops.gmail" not in sys.modules or not hasattr(provider_probe, "gmail")
    source = open(provider_probe.__file__, encoding="utf-8").read()
    assert "gmail" not in source.replace("Gmail", "").lower()
    assert ".email_ops" not in source


def test_budget_counts_cost_and_calls():
    budget = CallBudget(calls=149, cost_usd=0.0)
    assert budget.can_call()
    budget.charge(0.01)
    assert not budget.can_call()
    assert not CallBudget(cost_usd=0.15).can_call()


def test_replacement_only_for_unavailable_controls_and_must_pass_probe():
    luna, qwen, gemini = CANDIDATE_MODELS
    replacement = {luna: "vendor/replacement"}
    dispositions = {luna: "REJECT_REQUIRED_CONTROLS_UNAVAILABLE", qwen: "PROBE_PASS",
                    gemini: "PROBE_PASS", "vendor/replacement": "PROBE_PASS"}
    assert select_benchmark_models(dispositions, replacement) == (
        "vendor/replacement", qwen, gemini)
    dispositions["vendor/replacement"] = "PROBE_FAIL_SCHEMA"
    assert select_benchmark_models(dispositions, replacement) == (qwen, gemini)
    with pytest.raises(ValueError):
        select_benchmark_models({**dispositions, luna: "PROBE_FAIL_SCHEMA"}, replacement)
    with pytest.raises(ValueError):
        select_benchmark_models(dispositions, {"not/original": "vendor/replacement"})
    assert select_benchmark_models({qwen: "PROBE_PASS"}, {}) == (qwen,)


def test_unapproved_replacement_cannot_be_benchmarked():
    client = SufficiencyClient(api_key="test-only",
                               allowed_model_ids=CANDIDATE_MODELS + ("vendor/unapproved",))
    with pytest.raises(ValueError):
        assess_candidate(private_thread(), OWNER, "vendor/unapproved", client)


def test_stage_b_unreachable_for_benchmark_and_replacement_models(monkeypatch):
    from email_ops.sufficiency_shadow import continue_after_sufficiency

    def forbidden(*args, **kwargs):
        raise AssertionError("Stage B must never run for Issue #16")

    monkeypatch.setattr("email_ops.sufficiency_shadow.evaluate_dual_shadow", forbidden)
    thread = private_thread()
    for model_id in CANDIDATE_MODELS[:2] + ("vendor/replacement",):
        stage_a = SufficiencyOutcome(model_id, "gmail", thread.id, thread.messages[-1].id,
                                     True, "SUFFICIENT", "NEEDS_JUDGMENT", None,
                                     1, 1, 1, 1, 0.0)
        assert continue_after_sufficiency(thread, OWNER, model_id, stage_a, None) is None
