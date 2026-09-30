"""Tier 1-2 sender policy for incoming mail (Issue #19).

The real policy table names personal senders, so it lives under
~/.email_ops/policy and never in Git; examples/sender-policy.example.csv is a
synthetic illustration. Tier 2 surface checks run before Tier 1 filing so a
security or money notice from a filed sender still reaches Stephen. Routing is
advisory only: nothing here reads or changes Gmail.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .extract import address, is_automated
from .sender_analysis import (GMAIL_CATEGORIES, PRIVATE_ROOT, cluster_key, looks_automated_sender,
                              metadata_message, rehint)


CATEGORIES = ("Career", "Receipts", "Marketing", "Social Media", "Financial", "Accounts",
              "Personal", "Newsletters")
DISPOSITIONS = ("ARCHIVE", "DIGEST", "INBOX", "MIXED")
POLICY_COLUMNS = ("cluster", "category", "disposition", "surface_when", "notes")
DEFAULT_POLICY = PRIVATE_ROOT / "policy" / "sender-policy.csv"
_BULK_CATEGORIES = frozenset(GMAIL_CATEGORIES) - {"CATEGORY_PERSONAL"}
# Global Tier 2: security, account-access, and money-trouble subjects are never filed.
_SURFACE_SUBJECT = re.compile(
    r"\b(?:fraud|suspicious|unusual (?:activity|sign-?in)|unauthori[sz]ed|security (?:alert|code)"
    r"|verification code|verify your|password (?:reset|change)|new (?:sign-?in|device|login)"
    r"|locked|past due|overdue|payment (?:failed|declined)|declined|final notice)\b", re.I)


@dataclass(frozen=True)
class PolicyRow:
    cluster: str
    category: str
    disposition: str
    surface_when: re.Pattern | None = None
    notes: str = ""


@dataclass(frozen=True)
class Route:
    tier: int  # 1 filed by rule, 2 surfaced to Stephen, 3 middle (model tier)
    disposition: str
    category: str
    reason: str


def parse_row(raw: dict, categories: tuple[str, ...] = CATEGORIES) -> PolicyRow:
    cluster = (raw.get("cluster") or "").strip().lower()
    category = (raw.get("category") or "").strip()
    disposition = (raw.get("disposition") or "").strip().upper()
    surface_when = (raw.get("surface_when") or "").strip()
    if not cluster.startswith(("sender:", "list:")):
        raise ValueError(f"cluster must start with sender: or list: ({cluster!r})")
    if category not in categories:
        raise ValueError(f"unknown category {category!r} for {cluster}")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r} for {cluster}")
    return PolicyRow(cluster, category, disposition,
                     re.compile(surface_when, re.I) if surface_when else None,
                     (raw.get("notes") or "").strip())


def load_policy(path: Path, categories: tuple[str, ...] = CATEGORIES) -> dict[str, PolicyRow]:
    policy: dict[str, PolicyRow] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            row = parse_row(raw, categories)
            if row.cluster in policy:
                raise ValueError(f"duplicate policy row for {row.cluster}")
            policy[row.cluster] = row
    return policy


def write_policy(path: Path, policy: dict[str, PolicyRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=POLICY_COLUMNS)
        writer.writeheader()
        for row in policy.values():
            writer.writerow({"cluster": row.cluster, "category": row.category,
                             "disposition": row.disposition,
                             "surface_when": row.surface_when.pattern if row.surface_when else "",
                             "notes": row.notes})


def merge_sheet(policy: dict[str, PolicyRow], sheet: Path,
                categories: tuple[str, ...] = CATEGORIES) -> dict[str, PolicyRow]:
    """Add labeled sheet rows (those with a disposition); a sheet row replaces its policy row."""
    merged = dict(policy)
    with sheet.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            if (raw.get("disposition") or "").strip():
                row = parse_row(raw, categories)
                merged[row.cluster] = row
    return merged


def _looks_human(row: dict, owner: str) -> bool:
    sender = row["headers"].get("from", "")
    return not (is_automated(metadata_message(row), owner) or looks_automated_sender(sender)
                or _BULK_CATEGORIES & set(row["labels"]))


def route(row: dict, policy: dict[str, PolicyRow], correspondents: set[str], owner: str) -> Route:
    """Route one metadata row ({"headers": {...}, "labels": [...]}) through Tiers 1-2."""
    headers = row["headers"]
    subject = headers.get("subject", "")
    rule = policy.get(cluster_key(row))
    category = rule.category if rule else ""
    if address(headers.get("from", "")) in correspondents:
        return Route(2, "INBOX", category, "correspondent")
    if _SURFACE_SUBJECT.search(subject):
        return Route(2, "INBOX", category, "security_or_money")
    if rule and rule.surface_when and rule.surface_when.search(subject):
        return Route(2, "INBOX", category, "policy_surface_when")
    if rule:
        if rule.disposition in ("ARCHIVE", "DIGEST"):
            return Route(1, rule.disposition, category, "policy")
        if rule.disposition == "INBOX":
            return Route(2, "INBOX", category, "policy_inbox")
        return Route(3, "MIXED", category, "policy_mixed")
    if _looks_human(row, owner):
        return Route(2, "INBOX", "", "direct_human")
    return Route(3, "UNDECIDED", "", "unlabeled")


_BUCKETS = {"ARCHIVE": "tier1_archive", "DIGEST": "tier1_digest", "INBOX": "tier2_surface",
            "MIXED": "tier3_middle"}


def measure(clusters: list[dict], policy: dict[str, PolicyRow]) -> dict:
    """Cluster-level tier shares for a saved sender analysis.

    Saved reports keep only sample subjects, so subject-level Tier 2 overrides are
    not measured here; the Tier 1 share is an upper bound until shadow runs on new mail.
    """
    counts: Counter = Counter()
    labeled = labeled_messages = 0
    for cluster in clusters:
        rule = policy.get(cluster["cluster"])
        if rule:
            labeled += 1
            labeled_messages += cluster["messages"]
            bucket = _BUCKETS[rule.disposition]
        else:
            bucket = ("tier2_surface" if rehint(cluster) in ("correspondent", "person")
                      else "tier3_middle")
        counts[bucket] += cluster["messages"]
    total = sum(c["messages"] for c in clusters)
    share = {k: round(counts[k] / total, 3) if total else 0.0
             for k in ("tier1_archive", "tier1_digest", "tier2_surface", "tier3_middle")}
    return {"messages": total, "clusters": len(clusters), "labeled_clusters": labeled,
            "labeled_message_share": round(labeled_messages / total, 3) if total else 0.0,
            "tier1_share": round(share["tier1_archive"] + share["tier1_digest"], 3), **share}


def _private(parser: argparse.ArgumentParser, path: Path) -> Path:
    if not path.resolve().is_relative_to(PRIVATE_ROOT.resolve()):
        parser.error(f"{path} must be under the private {PRIVATE_ROOT} directory")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sender policy table (private data, aggregate output)")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="merge labeled sheet rows into the private policy table")
    build.add_argument("--sheet", type=Path, required=True)
    build.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    report = sub.add_parser("measure", help="tier shares for a saved sender analysis")
    report.add_argument("--analysis", type=Path, required=True)
    report.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    policy_path = _private(parser, args.policy)
    policy = load_policy(policy_path) if policy_path.exists() else {}
    if args.command == "build":
        merged = merge_sheet(policy, _private(parser, args.sheet))
        write_policy(policy_path, merged)
        print(json.dumps({"policy_rows": len(merged), "new_rows": len(merged) - len(policy),
                          "dispositions": dict(Counter(r.disposition for r in merged.values()))},
                         indent=2))
        return
    clusters = json.loads(_private(parser, args.analysis).read_text(encoding="utf-8"))["clusters"]
    print(json.dumps(measure(clusters, policy), indent=2))


if __name__ == "__main__":
    main()
