"""Run a bounded private corpus through OpenRouter in shadow mode.

The corpus and results are kept under ~/.email_ops. This driver has no Gmail or
database access; only the validated shadow outcome is recorded.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .core import Message, Thread
from .openrouter_shadow import OpenRouterClient, evaluate_shadow


PRIVATE_ROOT = Path.home() / ".email_ops"


def run_cases(corpus: dict, model_ids: tuple[str, ...], client: OpenRouterClient) -> list[dict]:
    owner = corpus["owner"]
    records = []
    for model_id in model_ids:
        for case in corpus["cases"]:
            raw = case["thread"]
            thread = Thread(raw["provider"], raw["id"],
                            tuple(Message(**message) for message in raw["messages"]))
            outcome = evaluate_shadow(thread, owner, model_id, client)
            records.append({"model_id": model_id, "case_key": case["case_key"],
                            "kind": case.get("kind", "real"),
                            "expected_route": case.get("expected_route"),
                            "expected_event_type": case.get("expected_event_type"),
                            "outcome": asdict(outcome)})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Private, non-persisting model comparison")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--model", required=True, action="append")
    args = parser.parse_args()
    root = PRIVATE_ROOT.resolve()
    if not args.corpus.resolve().is_relative_to(root) or not args.results.resolve().is_relative_to(root):
        parser.error("corpus and results must be under the private ~/.email_ops directory")
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    records = run_cases(corpus, tuple(args.model), OpenRouterClient())
    args.results.write_text(json.dumps({"records": records}, indent=2), encoding="utf-8")
    print(json.dumps({"case_count": len(corpus["cases"]), "model_count": len(args.model),
                      "record_count": len(records)}))


if __name__ == "__main__":
    main()

