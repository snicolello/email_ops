"""Local review queue: only NEEDS_JUDGMENT threads, from the private database."""

from __future__ import annotations

import argparse
import json
import sqlite3

from .core import connect


def gmail_link(source_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{source_id}"


def review_queue(db: sqlite3.Connection) -> list[dict]:
    """Latest decision for each thread's current message, NEEDS_JUDGMENT only."""
    rows = db.execute("""
        SELECT t.provider, t.source_id, t.subject, t.updated_at,
               (SELECT COALESCE(d.reason_code, d.reason) FROM decisions d
                 WHERE d.thread_id = t.id AND d.source_message_id = t.last_message_id
                   AND d.route = t.route ORDER BY d.created_at DESC LIMIT 1),
               (SELECT d.rule FROM decisions d
                 WHERE d.thread_id = t.id AND d.source_message_id = t.last_message_id
                   AND d.route = t.route ORDER BY d.created_at DESC LIMIT 1)
        FROM threads t WHERE t.route = 'NEEDS_JUDGMENT' ORDER BY t.updated_at DESC""")
    return [{"reason_code": reason or "unknown", "rule": rule, "subject": subject or "",
             "source": gmail_link(source_id) if provider == "gmail" else f"{provider}:{source_id}",
             "updated_at": updated}
            for provider, source_id, subject, updated, reason, rule in rows]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="List NEEDS_JUDGMENT threads (local only)")
    parser.add_argument("--db", default="data/email_ops.db")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db = connect(args.db)
    try:
        queue = review_queue(db)
    finally:
        db.close()
    if args.json:
        print(json.dumps({"review_queue_size": len(queue), "threads": queue}, indent=2))
        return
    print(f"{len(queue)} thread(s) need judgment")
    for item in queue:
        print(f"- [{item['reason_code']}] {item['subject']}\n  {item['source']}")


if __name__ == "__main__":
    main()
