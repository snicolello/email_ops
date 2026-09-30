"""Issue #18 held-out workflow: sample, label, freeze baseline, evaluate once.

All files live under the private ~/.email_ops directory. Only aggregate metrics
are printed; per-case outcomes stay in private result files. Gates are fixed
here before any held-out result exists and must not be changed afterwards.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

from .core import Message, Thread, connect
from .rules import NEEDS_JUDGMENT, RULES_V0_1, RULES_V0_2, RULESETS, decide_with, reconcile_with


PRIVATE_ROOT = Path.home() / ".email_ops"
SCHEMA = "email_ops_heldout_v1"
MAX_THREADS = 50
HUMAN_JUDGMENT = "HUMAN_JUDGMENT_REQUIRED"
LABELS = ("NO_ACTION", "REFERENCE", "OPERATIONAL_EVIDENCE", "STEPHEN_ACTION",
          "WAITING_ON_OTHER", HUMAN_JUDGMENT)
EVENT_TYPES = ("job_alert", "application_confirmation", "payment_confirmation", "receipt")
_KEYS = {"n": "NO_ACTION", "r": "REFERENCE", "o": "OPERATIONAL_EVIDENCE",
         "a": "STEPHEN_ACTION", "w": "WAITING_ON_OTHER", "h": HUMAN_JUDGMENT}


# --- corpus I/O ---------------------------------------------------------------

def thread_to_json(thread: Thread) -> dict:
    return {"provider": thread.provider, "id": thread.id,
            "messages": [asdict(message) for message in thread.messages]}


def thread_from_json(raw: dict) -> Thread:
    messages = []
    for m in raw["messages"]:
        m = dict(m)
        m["labels"] = tuple(m.get("labels", ()))
        m["headers"] = tuple(tuple(pair) for pair in m.get("headers", ()))
        messages.append(Message(**m))
    return Thread(raw["provider"], raw["id"], tuple(messages))


def load_cases(corpus: dict) -> list[tuple[str, Thread, str | None, str | None]]:
    """Accept held-out ('label') and earlier private corpora ('expected_route')."""
    cases = []
    for case in corpus["cases"]:
        label = case.get("label", case.get("expected_route"))
        event = case.get("event_type", case.get("expected_event_type"))
        cases.append((case["case_key"], thread_from_json(case["thread"]), label, event))
    return cases


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private(parser: argparse.ArgumentParser, *paths: Path) -> None:
    root = PRIVATE_ROOT.resolve()
    for path in paths:
        if not path.resolve().is_relative_to(root):
            parser.error(f"{path} must be under the private {PRIVATE_ROOT} directory")


def _write_private(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)


def baseline_path(sample: Path) -> Path:
    return sample.with_name(sample.stem + ".baseline.json")


def evaluation_path(sample: Path, ruleset: str) -> Path:
    return sample.with_name(f"{sample.stem}.evaluation.{ruleset}.json")


# --- metrics and gates ----------------------------------------------------------

def is_correct(route: str, label: str) -> bool:
    return route == label or (label == HUMAN_JUDGMENT and route == NEEDS_JUDGMENT)


def idempotency(threads: list[Thread], owner: str, ruleset: str) -> dict:
    db = connect(":memory:")
    try:
        def counts():
            return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("threads", "records", "decisions")}
        for thread in threads:
            reconcile_with(db, thread, owner, ruleset)
        first = counts()
        for thread in threads:
            reconcile_with(db, thread, owner, ruleset)
        second = counts()
    finally:
        db.close()
    return {"first": first, "second": second, "stable": first == second}


def score(cases, owner: str, ruleset: str) -> tuple[dict, list[dict]]:
    """Return (aggregate metrics, private per-case outcomes)."""
    outcomes, fc_by_rule, nj_by_reason = [], Counter(), Counter()
    labels, correct = Counter(), Counter()
    false_confident = missed_actions = hj_kept = event_mismatch = 0
    for case_key, thread, label, event_type in cases:
        if label not in LABELS:
            raise ValueError(f"case {case_key} has no valid label")
        decision = decide_with(ruleset, thread, owner)
        rule = decision.rule or decision.reason
        labels[label] += 1
        if is_correct(decision.route, label):
            correct[label] += 1
            if event_type and decision.event_type != event_type:
                event_mismatch += 1
        elif decision.route != NEEDS_JUDGMENT:
            false_confident += 1
            fc_by_rule[rule] += 1
        if label == "STEPHEN_ACTION" and decision.route not in ("STEPHEN_ACTION", NEEDS_JUDGMENT):
            missed_actions += 1
        if label == HUMAN_JUDGMENT and decision.route == NEEDS_JUDGMENT:
            hj_kept += 1
        if decision.route == NEEDS_JUDGMENT:
            nj_by_reason[decision.reason_code or decision.reason] += 1
        outcomes.append({"case_key": case_key, "label": label, "route": decision.route,
                         "event_type": decision.event_type, "rule": rule,
                         "reason_code": decision.reason_code})
    metrics = {
        "ruleset": ruleset, "n": len(cases),
        "needs_judgment": sum(nj_by_reason.values()),
        "false_confident": false_confident,
        "missed_actions": missed_actions,
        "human_judgment_total": labels[HUMAN_JUDGMENT],
        "human_judgment_kept": hj_kept,
        "label_counts": dict(sorted(labels.items())),
        "correct_by_label": {label: correct[label] for label in sorted(labels)},
        "event_type_mismatches": event_mismatch,
        "false_confident_by_rule": dict(sorted(fc_by_rule.items())),
        "needs_judgment_by_reason": dict(sorted(nj_by_reason.items())),
        "idempotency": idempotency([thread for _, thread, _, _ in cases], owner, ruleset),
    }
    return metrics, outcomes


def gates(baseline: dict, final: dict) -> dict:
    """The six prespecified Issue #18 gates. ACCEPT only if all pass."""
    required_drop = max(math.ceil(0.2 * baseline["needs_judgment"]), 5)
    drop = baseline["needs_judgment"] - final["needs_judgment"]
    results = {
        "1_false_confident": {
            "baseline": baseline["false_confident"], "final": final["false_confident"],
            "pass": (final["false_confident"] <= baseline["false_confident"]
                     and final["false_confident"] * MAX_THREADS <= final["n"])},
        "2_missed_actions": {"baseline": baseline["missed_actions"],
                             "final": final["missed_actions"],
                             "pass": final["missed_actions"] == 0},
        "3_personal_context": {
            "baseline": f"{baseline['human_judgment_kept']}/{baseline['human_judgment_total']}",
            "final": f"{final['human_judgment_kept']}/{final['human_judgment_total']}",
            "pass": final["human_judgment_kept"] == final["human_judgment_total"]},
        "4_review_reduction": {"baseline": baseline["needs_judgment"],
                               "final": final["needs_judgment"],
                               "required_drop": required_drop, "drop": drop,
                               "pass": drop >= required_drop},
        "5_coverage": {"baseline": baseline["correct_by_label"],
                       "final": final["correct_by_label"],
                       "pass": all(final["correct_by_label"].get(label, 0) >= count
                                   for label, count in baseline["correct_by_label"].items())},
        "6_idempotency": {"baseline": baseline["idempotency"]["stable"],
                          "final": final["idempotency"]["stable"],
                          "pass": final["idempotency"]["stable"]},
    }
    verdict = "ACCEPT" if all(gate["pass"] for gate in results.values()) else "REJECT"
    return {"verdict": verdict, "failed": [k for k, g in results.items() if not g["pass"]],
            "gates": results}


# --- commands ------------------------------------------------------------------

def _sample(args, parser) -> None:
    from .gmail import fetch_threads, service
    if not 1 <= args.limit <= MAX_THREADS:
        parser.error(f"--limit must be 1..{MAX_THREADS}")
    if args.out.exists():
        parser.error(f"{args.out} already exists; a held-out sample is drawn once")
    api = service(args.credentials, args.token)
    owner = api.users().getProfile(userId="me").execute()["emailAddress"]
    threads = fetch_threads(api, args.query, args.limit)
    _write_private(args.out, {
        "schema": SCHEMA, "query": args.query, "limit": args.limit, "owner": owner,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "cases": [{"case_key": f"h{i:03d}", "thread": thread_to_json(t),
                   "label": None, "event_type": None} for i, t in enumerate(threads, 1)]})
    print(json.dumps({"sample_threads": len(threads), "path": str(args.out)}))


def _label(args, parser) -> None:
    if baseline_path(args.sample).exists():
        parser.error("baseline is frozen; labels can no longer change")
    corpus = json.loads(args.sample.read_text(encoding="utf-8"))
    keys = "  ".join(f"{k}={v}" for k, v in _KEYS.items())
    for case in corpus["cases"]:
        if case["label"] in LABELS and not args.relabel:
            continue
        thread = thread_from_json(case["thread"])
        print("\n" + "=" * 72 + f"\n{case['case_key']}  ({len(thread.messages)} message(s))")
        for message in thread.messages[-3:]:
            print(f"\nFrom: {message.sender}\nTo: {message.to}\nSubject: {message.subject}\n"
                  f"{' '.join(message.body.split())[:800]}")
        while True:
            choice = input(f"\n{keys}  s=skip  q=quit > ").strip().lower()
            if choice in ("s", "q") or choice in _KEYS:
                break
        if choice == "q":
            break
        if choice == "s":
            continue
        case["label"] = _KEYS[choice]
        case["event_type"] = None
        if choice == "o":
            options = "  ".join(f"{i}={e}" for i, e in enumerate(EVENT_TYPES, 1))
            pick = input(f"event type ({options}, blank=none fits) > ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(EVENT_TYPES):
                case["event_type"] = EVENT_TYPES[int(pick) - 1]
        args.sample.write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    remaining = sum(case["label"] not in LABELS for case in corpus["cases"])
    print(json.dumps({"labeled": len(corpus["cases"]) - remaining, "remaining": remaining}))


def _freeze(args, parser) -> None:
    target = baseline_path(args.sample)
    if target.exists():
        parser.error(f"{target} already exists; the baseline is frozen once")
    corpus = json.loads(args.sample.read_text(encoding="utf-8"))
    if corpus.get("schema") != SCHEMA:
        parser.error("not a held-out sample")
    missing = [c["case_key"] for c in corpus["cases"] if c.get("label") not in LABELS]
    if missing:
        parser.error(f"{len(missing)} case(s) still unlabeled")
    metrics, _ = score(load_cases(corpus), corpus["owner"], RULES_V0_1)
    # Per-case and per-rule held-out detail is withheld until the final evaluation
    # so it cannot steer rule development.
    for detail in ("false_confident_by_rule", "needs_judgment_by_reason"):
        metrics.pop(detail)
    digest = sha256_file(args.sample)
    _write_private(target, {"label_sha256": digest, "sample": args.sample.name,
                            "frozen_at": datetime.now(timezone.utc).isoformat(),
                            "metrics": metrics})
    print(json.dumps({"label_sha256": digest, "baseline": metrics}, indent=2))


def _evaluate(args, parser) -> None:
    if args.rules == RULES_V0_1:
        parser.error("rules-v0.1 is the baseline; evaluate a successor ruleset")
    frozen = baseline_path(args.sample)
    if not frozen.exists():
        parser.error("freeze the rules-v0.1 baseline first")
    target = evaluation_path(args.sample, args.rules)
    if target.exists():
        parser.error(f"{target} already exists; the held-out sample is evaluated once")
    baseline = json.loads(frozen.read_text(encoding="utf-8"))
    digest = sha256_file(args.sample)
    if digest != baseline["label_sha256"]:
        parser.error("held-out sample changed after the baseline was frozen")
    corpus = json.loads(args.sample.read_text(encoding="utf-8"))
    metrics, outcomes = score(load_cases(corpus), corpus["owner"], args.rules)
    result = gates(baseline["metrics"], metrics)
    _write_private(target, {"label_sha256": digest, "evaluated_at": datetime.now(timezone.utc).isoformat(),
                            **result, "metrics": metrics, "cases": outcomes})
    print(json.dumps({"label_sha256": digest, **result,
                      "review_queue_size": metrics["needs_judgment"],
                      "false_confident_by_rule": metrics["false_confident_by_rule"]}, indent=2))


def _frozen_hashes() -> set[str]:
    hashes = set()
    for path in PRIVATE_ROOT.rglob("*.baseline.json") if PRIVATE_ROOT.exists() else ():
        try:
            hashes.add(json.loads(path.read_text(encoding="utf-8"))["label_sha256"])
        except (OSError, ValueError, KeyError):
            continue
    return hashes


def _dev_score(args, parser) -> None:
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    if corpus.get("schema") == SCHEMA or sha256_file(args.corpus) in _frozen_hashes():
        parser.error("refusing to score the held-out sample during development")
    cases = [c for c in load_cases(corpus) if c[2] in LABELS]
    report = {}
    for ruleset in (RULES_V0_1, args.rules):
        metrics, outcomes = score(cases, corpus["owner"], ruleset)
        report[ruleset] = metrics
        if args.cases and ruleset == args.rules:
            report["cases"] = outcomes
    print(json.dumps(report, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Issue #18 held-out evaluation (private files only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="draw one bounded read-only sample (<=50 threads)")
    sample.add_argument("--query", required=True)
    sample.add_argument("--limit", type=int, default=MAX_THREADS)
    sample.add_argument("--out", type=Path, required=True)
    sample.add_argument("--credentials", default="credentials.json")
    sample.add_argument("--token", default="token.json")
    label = sub.add_parser("label", help="label the sample interactively (blind to rule output)")
    label.add_argument("--sample", type=Path, required=True)
    label.add_argument("--relabel", action="store_true")
    freeze = sub.add_parser("freeze", help="record label SHA-256 and rules-v0.1 baseline once")
    freeze.add_argument("--sample", type=Path, required=True)
    evaluate = sub.add_parser("evaluate", help="evaluate the final ruleset once against the gates")
    evaluate.add_argument("--sample", type=Path, required=True)
    evaluate.add_argument("--rules", default=RULES_V0_2, choices=sorted(RULESETS))
    dev = sub.add_parser("dev-score", help="score a non-held-out private corpus during development")
    dev.add_argument("--corpus", type=Path, required=True)
    dev.add_argument("--rules", default=RULES_V0_2, choices=sorted(RULESETS))
    dev.add_argument("--cases", action="store_true", help="include per-case outcomes (private)")
    args = parser.parse_args(argv)
    paths = [getattr(args, name) for name in ("out", "sample", "corpus") if hasattr(args, name)]
    _private(parser, *paths)
    {"sample": _sample, "label": _label, "freeze": _freeze,
     "evaluate": _evaluate, "dev-score": _dev_score}[args.command](args, parser)


if __name__ == "__main__":
    main()
