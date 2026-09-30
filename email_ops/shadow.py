"""M2 shadow triage of incoming mail (Issue #19). Gmail is read, never changed.

Each run fetches header metadata for new inbound mail, routes it through the
private sender policy (Tiers 1-2), and records what *would* happen in a private
SQLite database under ~/.email_ops/shadow. The digest is Markdown for Stephen to
read inline; corrections feed per-category promotion counts and later rule
proposals. Subjects are stored only in that private database.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import getaddresses
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from .extract import address
from .sender_analysis import PRIVATE_ROOT, _QuotaPacer, cluster_key, fetch_metadata
from .sender_policy import (CATEGORIES, DEFAULT_POLICY, DISPOSITIONS, PolicyRow, load_policy,
                            route)


DEFAULT_DB = PRIVATE_ROOT / "shadow" / "shadow.db"
INBOUND_QUERY = "-in:sent -in:chats -in:drafts"
MAX_PER_RUN = 500
SENT_SEED = 500
SENT_PER_RUN = 200
FIRST_RUN_DAYS = 1
OVERLAP_SECONDS = 3600  # re-list the last hour; already-recorded message IDs are skipped
PROMOTION_DAYS = 14
PROMOTION_EMAILS = 50
PROMOTION_WINDOW = 50
SUBJECT_WIDTH = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, query TEXT NOT NULL,
  listed INTEGER NOT NULL, recorded INTEGER NOT NULL, truncated INTEGER NOT NULL,
  policy_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL,
  received_ms INTEGER NOT NULL, cluster TEXT NOT NULL, sender TEXT NOT NULL,
  subject TEXT NOT NULL, tier INTEGER NOT NULL, disposition TEXT NOT NULL,
  category TEXT NOT NULL, reason TEXT NOT NULL, policy_version TEXT NOT NULL,
  run_id INTEGER NOT NULL REFERENCES runs(id), digested_at TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL REFERENCES decisions(id),
  corrected_at TEXT NOT NULL, disposition TEXT NOT NULL, category TEXT NOT NULL,
  note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS correspondents (
  address TEXT PRIMARY KEY, sent_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def policy_version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _state(db: sqlite3.Connection, key: str) -> str | None:
    found = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return found["value"] if found else None


def _set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))


def add_correspondents(db: sqlite3.Connection, sent_rows: list[dict], owner: str) -> None:
    for row in sent_rows:
        headers = row["headers"]
        for _, recipient in getaddresses([headers.get("to", ""), headers.get("cc", "")]):
            recipient = recipient.lower()
            if recipient and recipient != address(owner):
                db.execute("INSERT INTO correspondents VALUES (?, 1) ON CONFLICT(address) "
                           "DO UPDATE SET sent_count = sent_count + 1", (recipient,))


def correspondents(db: sqlite3.Connection) -> set[str]:
    return {r["address"] for r in db.execute("SELECT address FROM correspondents")}


def record(db: sqlite3.Connection, rows: list[dict], policy: dict[str, PolicyRow], owner: str,
           version: str, run_id: int) -> int:
    """Route and store rows not seen before; returns how many were recorded."""
    known = correspondents(db)
    recorded = 0
    for row in rows:
        sender = address(row["headers"].get("from", ""))
        if sender == address(owner):
            continue
        decision = route(row, policy, known, owner)
        cursor = db.execute(
            "INSERT OR IGNORE INTO decisions (message_id, thread_id, received_ms, cluster, sender, "
            "subject, tier, disposition, category, reason, policy_version, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row.get("thread_id", ""), row.get("internal_date", 0), cluster_key(row),
             sender, " ".join(row["headers"].get("subject", "").split()), decision.tier,
             decision.disposition, decision.category, decision.reason, version, run_id))
        recorded += cursor.rowcount
    return recorded


def run(db: sqlite3.Connection, api, owner: str, policy_path: Path, *, limit: int = MAX_PER_RUN,
        now: float | None = None) -> dict:
    """One read-only shadow pass over inbound mail since the last run."""
    now = time.time() if now is None else now
    policy, version = load_policy(policy_path), policy_version(policy_path)
    last = _state(db, "last_run_epoch")
    since = int(float(last)) - OVERLAP_SECONDS if last else int(now) - FIRST_RUN_DAYS * 86400
    pacer = _QuotaPacer()
    if not _state(db, "correspondents_seeded"):
        add_correspondents(db, fetch_metadata(api, "in:sent", SENT_SEED, pacer=pacer), owner)
        _set_state(db, "correspondents_seeded", _now())
    else:
        add_correspondents(db, fetch_metadata(api, f"in:sent after:{since}", SENT_PER_RUN,
                                              pacer=pacer), owner)
    query = f"{INBOUND_QUERY} after:{since}"
    rows = fetch_metadata(api, query, limit, progress=True, pacer=pacer)
    run_id = db.execute(
        "INSERT INTO runs (started_at, query, listed, recorded, truncated, policy_version) "
        "VALUES (?, ?, ?, 0, ?, ?)", (_now(), query, len(rows), int(len(rows) >= limit),
                                      version)).lastrowid
    recorded = record(db, rows, policy, owner, version, run_id)
    db.execute("UPDATE runs SET recorded = ? WHERE id = ?", (recorded, run_id))
    _set_state(db, "last_run_epoch", str(int(now)))
    db.commit()
    return {"run": run_id, "listed": len(rows), "recorded": recorded,
            "truncated": len(rows) >= limit, "policy_version": version}


def _cell(text: str, width: int = SUBJECT_WIDTH) -> str:
    text = " ".join(text.split()).replace("|", "/").replace("`", "'")
    return text if len(text) <= width else text[:width - 1] + "…"


def _received(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%a %H:%M") if ms else ""


def digest(db: sqlite3.Connection, *, mark: bool = True) -> str:
    """Markdown digest of decisions not yet shown; marks them shown unless mark=False."""
    rows = db.execute("SELECT * FROM decisions WHERE digested_at IS NULL "
                      "ORDER BY tier, disposition, category, sender, received_ms").fetchall()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not rows:
        return f"## Shadow digest, {stamp}\n\nNo new mail since the last digest. Gmail unchanged."
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        key = ("surfaced" if r["tier"] == 2 else "middle" if r["tier"] == 3
               else r["disposition"].lower())
        groups[key].append(r)
    counts = {k: len(v) for k, v in groups.items()}
    lines = [f"## Shadow digest, {stamp}",
             "",
             f"{len(rows)} new messages. Gmail unchanged; this is what *would* happen. "
             f"Would archive **{counts.get('archive', 0)}**, would digest "
             f"**{counts.get('digest', 0)}**, surfaced **{counts.get('surfaced', 0)}**, "
             f"middle **{counts.get('middle', 0)}**.",
             "",
             "To correct one: `python -m email_ops.shadow correct <#> --disposition <ARCHIVE|DIGEST|INBOX> "
             "[--category <name>]`."]
    sections = (("surfaced", "Surfaced to you", "Why"), ("middle", "Middle (model tier later)", "Why"),
                ("digest", "Would DIGEST", "Category"), ("archive", "Would ARCHIVE", "Category"))
    for key, title, last_col in sections:
        if key not in groups:
            continue
        lines += ["", f"### {title} ({counts[key]})", "", f"| # | Received | From | Subject | {last_col} |",
                  "|---|---|---|---|---|"]
        for r in groups[key]:
            extra = r["category"] if key in ("digest", "archive") else (
                r["reason"] + (f" ({r['category']})" if r["category"] else ""))
            lines.append(f"| {r['id']} | {_received(r['received_ms'])} | {_cell(r['sender'], 40)} | "
                         f"{_cell(r['subject'])} | {extra} |")
    if mark:
        db.executemany("UPDATE decisions SET digested_at = ? WHERE id = ?",
                       [(_now(), r["id"]) for r in rows])
        db.commit()
    return "\n".join(lines)


def correct(db: sqlite3.Connection, decision_id: int, disposition: str, category: str = "",
            note: str = "") -> dict:
    disposition = disposition.upper()
    if disposition not in DISPOSITIONS:
        raise ValueError(f"disposition must be one of {', '.join(DISPOSITIONS)}")
    decision = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if decision is None:
        raise ValueError(f"no shadow decision #{decision_id}")
    category = category or decision["category"]
    if category and category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    db.execute("INSERT INTO corrections (decision_id, corrected_at, disposition, category, note) "
               "VALUES (?, ?, ?, ?, ?)", (decision_id, _now(), disposition, category, note))
    db.commit()
    return {"decision": decision_id, "was": f"{decision['disposition']} {decision['category']}".strip(),
            "now": f"{disposition} {category}".strip(), "wrong_filing": _wrong_filing(
                decision, disposition, category)}


def _wrong_filing(decision, disposition: str, category: str) -> bool:
    return decision["tier"] == 1 and (disposition != decision["disposition"]
                                      or category != decision["category"])


def status(db: sqlite3.Connection, *, now: float | None = None) -> list[dict]:
    """Per-category promotion progress over reviewed (digested) Tier 1 decisions."""
    now = time.time() if now is None else now
    latest = {r["decision_id"]: r for r in db.execute(
        "SELECT * FROM corrections ORDER BY id")}  # the last correction per decision wins
    by_category: dict[str, list] = defaultdict(list)
    for r in db.execute("SELECT * FROM decisions WHERE tier = 1 AND digested_at IS NOT NULL "
                        "ORDER BY received_ms"):
        by_category[r["category"]].append(r)
    report = []
    for category, rows in sorted(by_category.items()):
        wrong = [_wrong_filing(r, latest[r["id"]]["disposition"], latest[r["id"]]["category"])
                 if r["id"] in latest else False for r in rows]
        days = (now * 1000 - rows[0]["received_ms"]) / 86_400_000
        recent_wrong = sum(wrong[-PROMOTION_WINDOW:])
        report.append({"category": category, "reviewed_filings": len(rows),
                       "days_in_shadow": round(days, 1),
                       "dispositions": dict(Counter(r["disposition"] for r in rows)),
                       "wrong_in_last_50": recent_wrong,
                       "eligible_for_approval": (days >= PROMOTION_DAYS and len(rows) >= PROMOTION_EMAILS
                                                 and recent_wrong == 0)})
    return report


def _private(parser: argparse.ArgumentParser, path: Path) -> Path:
    if not path.resolve().is_relative_to(PRIVATE_ROOT.resolve()):
        parser.error(f"{path} must be under the private {PRIVATE_ROOT} directory")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Shadow triage of incoming mail (Gmail read-only)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="read new inbound headers and record shadow decisions")
    run_cmd.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    run_cmd.add_argument("--limit", type=int, default=MAX_PER_RUN)
    run_cmd.add_argument("--credentials", default=str(PRIVATE_ROOT / "credentials.json"))
    run_cmd.add_argument("--token", default=str(PRIVATE_ROOT / "token.json"))
    digest_cmd = sub.add_parser("digest", help="Markdown digest of decisions not yet shown")
    digest_cmd.add_argument("--peek", action="store_true", help="do not mark items as shown")
    correct_cmd = sub.add_parser("correct", help="record Stephen's correction for one decision")
    correct_cmd.add_argument("decision", type=int)
    correct_cmd.add_argument("--disposition", required=True)
    correct_cmd.add_argument("--category", default="")
    correct_cmd.add_argument("--note", default="")
    sub.add_parser("status", help="per-category promotion progress")
    args = parser.parse_args(argv)
    db = connect(_private(parser, args.db))
    if args.command == "run":
        if not 1 <= args.limit <= MAX_PER_RUN:
            parser.error(f"--limit must be 1..{MAX_PER_RUN}")
        from .gmail import service
        api = service(args.credentials, args.token)
        owner = api.users().getProfile(userId="me").execute()["emailAddress"]
        print(json.dumps(run(db, api, owner, _private(parser, args.policy), limit=args.limit),
                         indent=2))
    elif args.command == "digest":
        sys.stdout.reconfigure(encoding="utf-8")
        print(digest(db, mark=not args.peek))
    elif args.command == "correct":
        try:
            print(json.dumps(correct(db, args.decision, args.disposition, args.category, args.note)))
        except ValueError as error:
            parser.error(str(error))
    else:
        print(json.dumps(status(db), indent=2))


if __name__ == "__main__":
    main()
