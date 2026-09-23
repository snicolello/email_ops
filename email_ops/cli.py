"""Bounded local command-line run."""

import argparse
import json
from .core import connect, reconcile, summary
from .gmail import fetch_threads, service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    parser.add_argument("--db", default="data/email_ops.db")
    parser.add_argument("--query", default="in:inbox newer_than:30d")
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be 1..50")
    api = service(args.credentials, args.token)
    owner = api.users().getProfile(userId="me").execute()["emailAddress"]
    threads = fetch_threads(api, args.query, args.limit)
    db = connect(args.db)
    try:
        for thread in threads:
            reconcile(db, thread, owner)
        print(json.dumps({"sample_threads": len(threads), **summary(db)}, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()

