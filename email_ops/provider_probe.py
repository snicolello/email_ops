"""Issue #16 non-private provider probe for the frozen Stage A contract.

Never loads Gmail, tokens, or the private corpus. One synthetic thread is sent
through the exact Stage A benchmark transport; only safe metadata is returned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
import math

import requests

from .core import Message, Thread
from .sufficiency_benchmark import assess_candidate, stage_a_allowlist
from .sufficiency_shadow import SufficiencyClient, SufficiencyOutcome


MODELS_URL = "https://openrouter.ai/api/v1/models"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model_id}/endpoints"
ZDR_URL = "https://openrouter.ai/api/v1/endpoints/zdr"
REQUIRED_PARAMETERS = ("response_format", "temperature", "max_tokens")
DISPOSITIONS = ("PROBE_PASS", "REJECT_REQUIRED_CONTROLS_UNAVAILABLE",
                "MODEL_ID_NOT_IN_CATALOG", "PROBE_FAIL_SCHEMA", "INCONCLUSIVE")
MAX_CALLS = 150
MAX_COST_USD = 0.15
SYNTHETIC_OWNER = "owner@example.com"


class CallBudget:
    """Issue #16 global limits shared by probe and benchmark calls."""

    def __init__(self, calls: int = 0, cost_usd: float = 0.0,
                 max_calls: int = MAX_CALLS, max_cost_usd: float = MAX_COST_USD):
        self.calls, self.cost_usd = calls, cost_usd
        self.max_calls, self.max_cost_usd = max_calls, max_cost_usd

    def can_call(self) -> bool:
        return self.calls < self.max_calls and self.cost_usd < self.max_cost_usd

    def charge(self, cost_usd: float | None) -> None:
        self.calls += 1
        if cost_usd is not None and math.isfinite(cost_usd) and cost_usd >= 0:
            self.cost_usd += cost_usd


def synthetic_probe_thread() -> Thread:
    return Thread("synthetic", "issue16-probe-thread", (
        Message("issue16-probe-message", "coordinator@garden.example", SYNTHETIC_OWNER,
                "Garden plot update",
                "The shared garden plots will be reassigned next month. "
                "The new map is posted at the entrance.",
                "2026-01-01T00:00:00Z"),))


def eligible_endpoints(endpoints: list[dict], zdr_endpoints: list[dict],
                       model_id: str) -> tuple[str, ...]:
    """Endpoint tags that are ZDR-listed and support every frozen parameter."""
    zdr_tags = {item.get("tag") for item in zdr_endpoints if item.get("model_id") == model_id}
    return tuple(endpoint.get("tag") for endpoint in endpoints
                 if endpoint.get("tag") in zdr_tags
                 and set(REQUIRED_PARAMETERS) <= set(endpoint.get("supported_parameters") or ()))


def classify_probe(in_catalog: bool, eligible_count: int,
                   outcome: SufficiencyOutcome | None) -> str:
    if not in_catalog:
        return "MODEL_ID_NOT_IN_CATALOG"
    if outcome is None or outcome.calls != 1:
        return "INCONCLUSIVE"
    if outcome.accepted:
        return "PROBE_PASS"
    code = outcome.error_code or ""
    if code in ("http_400", "http_404") and eligible_count == 0:
        return "REJECT_REQUIRED_CONTROLS_UNAVAILABLE"
    if code.startswith("http_") or code in ("transport_error", "provider_timeout",
                                            "provider_response_too_large",
                                            "provider_model_mismatch"):
        return "INCONCLUSIVE"
    return "PROBE_FAIL_SCHEMA"


@dataclass(frozen=True)
class ProbeRecord:
    model_id: str
    in_catalog: bool
    prompt_price_per_token: str | None
    completion_price_per_token: str | None
    endpoint_count: int
    eligible_endpoints: tuple[str, ...]
    disposition: str
    http_status: int | None
    error_code: str | None
    provider_error_code: str | None
    provider_error_message: str | None
    serving_provider: str | None
    latency_ms: int | None
    cost_usd: float | None

    @property
    def contract_satisfiable(self) -> bool:
        return self.disposition == "PROBE_PASS"


def probe_model(model_id: str, catalog: list[dict], endpoints: list[dict],
                zdr_endpoints: list[dict], client: SufficiencyClient,
                budget: CallBudget) -> ProbeRecord:
    listed = next((item for item in catalog if item.get("id") == model_id), None)
    pricing = (listed or {}).get("pricing") or {}
    eligible = eligible_endpoints(endpoints, zdr_endpoints, model_id) if listed else ()
    outcome = None
    if listed and budget.can_call():
        outcome = assess_candidate(synthetic_probe_thread(), SYNTHETIC_OWNER, model_id, client)
        budget.charge(outcome.cost_usd)
    error = outcome.provider_error if outcome else None
    return ProbeRecord(
        model_id, listed is not None, pricing.get("prompt"), pricing.get("completion"),
        len(endpoints), eligible, classify_probe(listed is not None, len(eligible), outcome),
        error.http_status if error else None, outcome.error_code if outcome else None,
        error.code if error else None, error.message if error else None,
        (outcome.provider_name if outcome else None) or (error.provider_name if error else None),
        outcome.latency_ms if outcome else None, outcome.cost_usd if outcome else None)


def _get_json(url: str) -> dict:
    response = requests.get(url, timeout=(5, 20))
    response.raise_for_status()
    return response.json()


def run(models: tuple[str, ...], budget: CallBudget) -> list[ProbeRecord]:
    """Public catalog metadata needs no key; only the probe call is authenticated."""
    catalog = _get_json(MODELS_URL)["data"]
    zdr_endpoints = _get_json(ZDR_URL)["data"]
    client = SufficiencyClient(allowed_model_ids=stage_a_allowlist())
    records = []
    for model_id in models:
        endpoints = []
        if any(item.get("id") == model_id for item in catalog):
            endpoints = _get_json(ENDPOINTS_URL.format(model_id=model_id))["data"]["endpoints"]
        records.append(probe_model(model_id, catalog, endpoints, zdr_endpoints, client, budget))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue #16 synthetic provider probe")
    parser.add_argument("models", nargs="+")
    parser.add_argument("--prior-calls", type=int, default=0)
    parser.add_argument("--prior-cost", type=float, default=0.0)
    args = parser.parse_args(argv)
    budget = CallBudget(args.prior_calls, args.prior_cost)
    records = run(tuple(args.models), budget)
    print(json.dumps({"records": [dict(asdict(r), contract_satisfiable=r.contract_satisfiable)
                                  for r in records],
                      "budget": {"calls": budget.calls, "cost_usd": budget.cost_usd}},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
