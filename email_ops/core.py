"""Provider-neutral routing and local reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    to: str
    subject: str
    body: str
    timestamp: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Thread:
    provider: str
    id: str
    messages: tuple[Message, ...]


@dataclass(frozen=True)
class Decision:
    route: str
    reason: str
    confidence: float
    description: str | None = None
    event_type: str | None = None


def decide(thread: Thread, owner: str) -> Decision:
    if not thread.messages:
        return Decision("NEEDS_JUDGMENT", "empty thread", 0.0)
    last = thread.messages[-1]
    text = f"{last.subject}\n{last.body}".lower()
    sender = last.sender.lower()
    owner = owner.lower()
    if "CATEGORY_PROMOTIONS" in last.labels and not re.search(r"\b(interview|application|recruiter|job)\b", text):
        return Decision("NO_ACTION", "Gmail promotion category without operational marker", 0.7)
    if owner and owner in sender:
        if re.search(r"\b(following up|checking in|please send|could you send|waiting for)\b", text):
            return Decision("WAITING_ON_OTHER", "owner requested a response", 0.7, last.subject)
        return Decision("REFERENCE", "latest message sent by owner", 0.55)
    if re.search(r"\b(interview invitation|schedule an interview|please schedule|action required|please complete|respond by)\b", text):
        return Decision("STEPHEN_ACTION", "explicit request to owner", 0.8, last.subject)
    if re.search(r"\b(job alert|jobs for you|new jobs matching)\b", text):
        return Decision("OPERATIONAL_EVIDENCE", "job alert", 0.75, last.subject, "job_alert")
    if ("linkedin.com" in sender and re.search(r"\bposted on \d|view jobs in\b", text)) or ("wellfound.com" in sender and "new jobs" in text):
        return Decision("OPERATIONAL_EVIDENCE", "job listing notification", 0.7, last.subject, "job_alert")
    if re.search(r"\b(application received|application confirmation|we received your application)\b", text):
        return Decision("OPERATIONAL_EVIDENCE", "application confirmation", 0.75, last.subject, "application_confirmation")
    if re.search(r"\b(receipt|order confirmation)\b", text):
        return Decision("OPERATIONAL_EVIDENCE", "receipt or order", 0.65, last.subject, "receipt")
    if "payment confirmation" in text or "thank you for your payment" in text:
        return Decision("OPERATIONAL_EVIDENCE", "payment confirmation", 0.65, last.subject, "payment_confirmation")
    if re.search(r"\b(for your records|reference number|no action required)\b", text):
        return Decision("REFERENCE", "explicit reference marker", 0.7)
    return Decision("NEEDS_JUDGMENT", "no safe deterministic route", 0.3)


def identity(kind: str, thread: Thread) -> str:
    source = f"{thread.provider}:{thread.id}:{kind}"
    return hashlib.sha256(source.encode()).hexdigest()[:24]


SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_id TEXT NOT NULL,
  subject TEXT, route TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_message_id TEXT NOT NULL, UNIQUE(provider, source_id));
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
  kind TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL,
  event_type TEXT, source_message_id TEXT NOT NULL, confidence REAL NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
  source_message_id TEXT NOT NULL, route TEXT NOT NULL, reason TEXT NOT NULL,
  confidence REAL NOT NULL, model_provider TEXT NOT NULL, model_name TEXT NOT NULL,
  created_at TEXT NOT NULL);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def _persist_decision(db: sqlite3.Connection, thread: Thread, owner: str,
                      decision: Decision, *, model_provider: str = "deterministic",
                      model_name: str = "rules-v0.1") -> Decision:
    """Persist only a decision selected by trusted local routing/validation code."""
    if not thread.messages:
        return decision
    last = thread.messages[-1]
    now = datetime.now(timezone.utc).isoformat()
    thread_id = identity("thread", thread)
    open_waiting = db.execute("SELECT 1 FROM records WHERE id=? AND kind='waiting' AND status='open'",
                              (identity("record", thread),)).fetchone()
    with db:
        db.execute("""INSERT INTO threads VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, route=excluded.route,
            updated_at=excluded.updated_at, last_message_id=excluded.last_message_id""",
            (thread_id, thread.provider, thread.id, last.subject, decision.route, now, last.id))
        kind = {"STEPHEN_ACTION": "action", "WAITING_ON_OTHER": "waiting",
                "OPERATIONAL_EVIDENCE": "event"}.get(decision.route)
        if kind:
            record_id = identity("record", thread)
            db.execute("""INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, description=excluded.description,
                status=excluded.status, event_type=excluded.event_type,
                source_message_id=excluded.source_message_id, confidence=excluded.confidence,
                updated_at=excluded.updated_at""",
                (record_id, thread_id, kind, decision.description or last.subject,
                 "open", decision.event_type, last.id, decision.confidence, now))
        elif open_waiting and owner not in last.sender.lower() and re.search(
                r"\b(attached|sent the requested|completed the requested)\b", last.body.lower()):
            db.execute("UPDATE records SET status='resolved', updated_at=? WHERE id=? AND kind='waiting'",
                       (now, identity("record", thread)))
        decision_id = hashlib.sha256(f"{thread_id}:{last.id}:{decision.route}".encode()).hexdigest()[:24]
        db.execute("""INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?)""",
                   (decision_id, thread_id, last.id, decision.route, decision.reason,
                    decision.confidence, model_provider, model_name, now))
    return decision


def reconcile(db: sqlite3.Connection, thread: Thread, owner: str) -> Decision:
    return _persist_decision(db, thread, owner, decide(thread, owner))


def summary(db: sqlite3.Connection) -> dict:
    return {"routes": dict(db.execute("SELECT route, count(*) FROM threads GROUP BY route")),
            "records": dict(db.execute("SELECT kind || ':' || status, count(*) FROM records GROUP BY kind, status")),
            "decisions": db.execute("SELECT count(*) FROM decisions").fetchone()[0]}


def normalized_events(db: sqlite3.Connection) -> list[dict]:
    """Future JD Delivery boundary; caller must handle privacy and delivery policy."""
    rows = db.execute("""SELECT r.id,r.event_type,r.description,t.provider,t.source_id,
                        r.source_message_id,r.confidence FROM records r JOIN threads t
                        ON t.id=r.thread_id WHERE r.kind='event' AND r.status='open'""")
    return [dict(zip(("event_id", "type", "summary", "source_provider", "source_thread_id",
                      "source_message_id", "confidence"), row)) for row in rows]
