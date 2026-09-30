"""Headers-only sender analysis of recent mail for rule discovery.

Fetches Gmail metadata (never bodies) for at most 1,000 recent messages, groups
them by mailing list or sender, and writes a private labeling sheet under
~/.email_ops. Stephen labels clusters, not emails; each labeled cluster is a
candidate sender rule. Read-only scope only; only aggregates are printed.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from email.utils import getaddresses
import json
from pathlib import Path
import re
import sys

from .core import Message
from .extract import address, is_automated


PRIVATE_ROOT = Path.home() / ".email_ops"
MAX_MESSAGES = 1000
MAX_SENT = 500
METADATA_HEADERS = ["From", "To", "Cc", "Subject", "List-Id", "List-Unsubscribe",
                    "Precedence", "Auto-Submitted"]
GMAIL_CATEGORIES = ("CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
                    "CATEGORY_UPDATES", "CATEGORY_FORUMS")
SAMPLE_SUBJECTS = 3
_LIST_ID = re.compile(r"<([^>]+)>")


def _list_ids(api, query: str, limit: int) -> list[str]:
    ids, token = [], None
    while len(ids) < limit:
        response = api.users().messages().list(
            userId="me", q=query, maxResults=min(500, limit - len(ids)),
            **({"pageToken": token} if token else {})).execute()
        ids += [ref["id"] for ref in response.get("messages", [])]
        token = response.get("nextPageToken")
        if not token:
            break
    return ids[:limit]


def fetch_metadata(api, query: str, limit: int, *, progress: bool = False) -> list[dict]:
    """Return header/label metadata only; format=metadata never includes bodies."""
    if not 1 <= limit <= MAX_MESSAGES:
        raise ValueError(f"limit must be 1..{MAX_MESSAGES}")
    rows = []
    ids = _list_ids(api, query, limit)
    for n, message_id in enumerate(ids, 1):
        if progress and (n % 50 == 0 or n == len(ids)):
            print(f"  {query}: {n}/{len(ids)} messages", file=sys.stderr, flush=True)
        item = api.users().messages().get(userId="me", id=message_id, format="metadata",
                                          metadataHeaders=METADATA_HEADERS).execute()
        headers = {h["name"].lower(): h.get("value", "")
                   for h in item.get("payload", {}).get("headers", [])}
        rows.append({"id": item["id"], "thread_id": item.get("threadId", ""),
                     "labels": item.get("labelIds", []), "headers": headers})
    return rows


def correspondents(sent_rows: list[dict], owner: str) -> Counter:
    """How often the owner wrote to each address, from sent-mail metadata."""
    counts: Counter = Counter()
    for row in sent_rows:
        for recipient in _recipients(row) - {address(owner)}:
            counts[recipient] += 1
    return counts


def _recipients(row: dict) -> set[str]:
    headers = row["headers"]
    return {a.lower() for _, a in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if a}


def _message(row: dict) -> Message:
    h = row["headers"]
    routing = tuple((k, h[k]) for k in ("auto-submitted", "precedence") if h.get(k))
    routing += tuple((k, "present") for k in ("list-unsubscribe", "list-id") if h.get(k))
    return Message(row["id"], h.get("from", ""), h.get("to", ""), h.get("subject", ""), "",
                   "", tuple(row["labels"]), routing)


def cluster_key(row: dict) -> str:
    list_id = row["headers"].get("list-id", "")
    if list_id:
        match = _LIST_ID.search(list_id)
        return "list:" + (match.group(1) if match else list_id).strip().lower()
    return "sender:" + address(row["headers"].get("from", ""))


def analyze(rows: list[dict], owner: str, replied: Counter) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if address(row["headers"].get("from", "")) == address(owner):
            continue  # owner's own sent messages are not inbound clusters
        groups[cluster_key(row)].append(row)
    inbound = sum(len(members) for members in groups.values())
    clusters, cumulative = [], 0
    for key, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        count = len(members)
        cumulative += count
        senders = Counter(address(m["headers"].get("from", "")) for m in members)
        categories = Counter(label for m in members for label in m["labels"]
                             if label in GMAIL_CATEGORIES)
        automated = sum(is_automated(_message(m), owner) for m in members)
        unread = sum("UNREAD" in m["labels"] for m in members)
        subjects = []
        for m in members:
            subject = m["headers"].get("subject", "")
            if subject and subject not in subjects:
                subjects.append(subject)
            if len(subjects) == SAMPLE_SUBJECTS:
                break
        replied_to = sum(replied[s] for s in senders)
        clusters.append({
            "cluster": key,
            "sender_domains": sorted({s.partition("@")[2] for s in senders}),
            "messages": count,
            "share": round(count / inbound, 4) if inbound else 0.0,
            "cumulative_share": round(cumulative / inbound, 4) if inbound else 0.0,
            "threads": len({m["thread_id"] for m in members}),
            "unread_rate": round(unread / count, 3),
            "important_rate": round(sum("IMPORTANT" in m["labels"] for m in members) / count, 3),
            "starred": sum("STARRED" in m["labels"] for m in members),
            "automated_rate": round(automated / count, 3),
            "has_unsubscribe": any(m["headers"].get("list-unsubscribe") for m in members),
            "owner_wrote_to_sender": replied_to,
            "gmail_categories": dict(categories.most_common()),
            "sample_subjects": subjects,
            "hint": _hint(count, unread, automated, replied_to),
        })
    return {"messages": len(rows), "inbound_messages": inbound, "clusters": clusters,
            "summary": _summary(clusters, inbound)}


def _hint(count: int, unread: int, automated: int, replied_to: int) -> str:
    """A labeling prompt only; never a routing rule."""
    if replied_to:
        return "correspondent"
    if automated == count and unread / count >= 0.9:
        return "archive_candidate"
    if automated == count:
        return "automated"
    if automated:
        return "mixed"
    return "person"


def _summary(clusters: list[dict], inbound: int) -> dict:
    def top(n):
        return round(sum(c["messages"] for c in clusters[:n]) / inbound, 3) if inbound else 0.0
    hints = Counter()
    for c in clusters:
        hints[c["hint"]] += c["messages"]
    return {"clusters": len(clusters), "top_10_share": top(10), "top_25_share": top(25),
            "top_50_share": top(50),
            "singleton_clusters": sum(c["messages"] == 1 for c in clusters),
            "messages_by_hint": dict(hints.most_common())}


SHEET_COLUMNS = ("cluster", "sender_domains", "messages", "share", "cumulative_share",
                 "unread_rate", "automated_rate", "owner_wrote_to_sender", "gmail_categories",
                 "sample_subjects", "hint", "category", "disposition", "notes")


def write_sheet(path: Path, clusters: list[dict]) -> None:
    """Labeling sheet: Stephen fills category and disposition per cluster."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        for c in clusters:
            writer.writerow({**{k: c[k] for k in SHEET_COLUMNS if k in c},
                             "sender_domains": " ".join(c["sender_domains"]),
                             "gmail_categories": " ".join(f"{k.split('_', 1)[1].lower()}:{v}"
                                                          for k, v in c["gmail_categories"].items()),
                             "sample_subjects": " | ".join(c["sample_subjects"]),
                             "category": "", "disposition": "", "notes": ""})


def main(argv: list[str] | None = None) -> None:
    from .gmail import service
    parser = argparse.ArgumentParser(description="Headers-only sender analysis (private output)")
    parser.add_argument("--query", default="-in:sent -in:chats -in:drafts")
    parser.add_argument("--limit", type=int, default=MAX_MESSAGES)
    parser.add_argument("--sent-limit", type=int, default=MAX_SENT)
    parser.add_argument("--out-dir", type=Path, default=PRIVATE_ROOT / "analysis")
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= MAX_MESSAGES or not 0 <= args.sent_limit <= MAX_SENT:
        parser.error(f"--limit must be 1..{MAX_MESSAGES} and --sent-limit 0..{MAX_SENT}")
    if not args.out_dir.resolve().is_relative_to(PRIVATE_ROOT.resolve()):
        parser.error(f"--out-dir must be under the private {PRIVATE_ROOT} directory")
    api = service(args.credentials, args.token)
    owner = api.users().getProfile(userId="me").execute()["emailAddress"]
    sent = fetch_metadata(api, "in:sent", args.sent_limit, progress=True) if args.sent_limit else []
    report = analyze(fetch_metadata(api, args.query, args.limit, progress=True), owner,
                     correspondents(sent, owner))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"senders-{stamp}.json").write_text(
        json.dumps({"query": args.query, "generated_at": stamp, **report}, indent=2),
        encoding="utf-8")
    write_sheet(args.out_dir / f"senders-{stamp}.csv", report["clusters"])
    print(json.dumps({"messages": report["messages"], "inbound_messages": report["inbound_messages"],
                      **report["summary"], "out_dir": str(args.out_dir)}, indent=2))


if __name__ == "__main__":
    main()
