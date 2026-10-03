"""Bounded M3 label + archive executor. Offline dry-run is the default.

Only an existing M2 Tier 1 ARCHIVE decision can be filed. This module never
creates labels or obtains OAuth consent. Private SQLite receipts reserve each
account/message once; an uncertain write is never automatically retried.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .sender_analysis import PRIVATE_ROOT
from .sender_policy import CATEGORIES
from .shadow import DEFAULT_DB as SHADOW_DB

MODIFY = "https://www.googleapis.com/auth/gmail.modify"
DEFAULT_CONFIG = PRIVATE_ROOT / "filing" / "config.json"
DEFAULT_AUDIT = PRIVATE_ROOT / "filing" / "audit.db"
FORBIDDEN = {"TRASH", "SPAM", "DRAFT", "DRAFTS", "SENT"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY, account TEXT NOT NULL, message_id TEXT NOT NULL,
  thread_id TEXT NOT NULL, category TEXT NOT NULL, label_name TEXT NOT NULL,
  label_id TEXT NOT NULL, source TEXT NOT NULL, prior TEXT NOT NULL,
  added TEXT NOT NULL, removed TEXT NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(account, message_id));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, operation_id TEXT, at TEXT NOT NULL,
  action TEXT NOT NULL, mode TEXT NOT NULL, result TEXT NOT NULL, details TEXT NOT NULL);
"""


class FilingError(ValueError):
    """A fail-closed executor boundary; messages contain no provider errors."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous = FULL")
    db.executescript(SCHEMA)
    return db


def _event(db, operation_id, action, live, result, details):
    db.execute("INSERT INTO events (operation_id, at, action, mode, result, details) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (operation_id, _now(), action, "live" if live else "dry-run", result,
                json.dumps(details, sort_keys=True)))
    db.commit()


def _state(db, op, state, action, details=None):
    db.execute("UPDATE operations SET state = ?, updated_at = ? WHERE id = ?",
               (state, _now(), op["id"]))
    _event(db, op["id"], action, True, state, details or {})
    return {"operation_id": op["id"], "state": state}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise FilingError("duplicate configuration key")
        result[key] = value
    return result


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
        if (not isinstance(config, dict)
                or set(config) != {"schema_version", "account", "kill_switch", "categories"}
                or type(config["schema_version"]) is not int or config["schema_version"] != 1
                or not isinstance(config["account"], str) or "@" not in config["account"]
                or config["account"] != config["account"].strip().lower()
                or type(config["kill_switch"]) is not bool
                or not isinstance(config["categories"], dict)):
            raise FilingError("invalid filing configuration")
        for category, entry in config["categories"].items():
            if (category not in CATEGORIES or not isinstance(entry, dict)
                    or set(entry) != {"enabled", "label_name", "approval_reference"}
                    or type(entry["enabled"]) is not bool
                    or entry["label_name"] != f"Email Ops/{category}"
                    or not isinstance(entry["approval_reference"], str)
                    or (entry["enabled"] and not entry["approval_reference"].strip())):
                raise FilingError("invalid category configuration")
        return config
    except (OSError, ValueError, TypeError):
        raise FilingError("missing or malformed filing configuration; mutation disabled") from None


def _gate(config_path, category, approval, account=None):
    config = load_config(config_path)
    if config["kill_switch"]:
        raise FilingError("global kill switch is on")
    entry = config["categories"].get(category)
    if not entry or not entry["enabled"]:
        raise FilingError("category is not explicitly enabled")
    if not approval or approval != entry["approval_reference"]:
        raise FilingError("live request must cite the category's independent Stephen approval")
    if account is not None and account != config["account"]:
        raise FilingError("credential account differs from configured shadow-source account")
    return entry


def decision(path: Path, decision_id: int) -> dict:
    """Read the existing shadow DB without schema changes or private policy edits."""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None or row["tier"] != 1 or row["disposition"] != "ARCHIVE":
            raise FilingError("requires an existing Tier 1 ARCHIVE shadow decision")
        correction = db.execute("SELECT * FROM corrections WHERE decision_id = ? "
                                "ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
        if correction and (correction["category"] != row["category"]
                           or correction["disposition"] != row["disposition"]):
            raise FilingError("shadow decision has a conflicting correction")
        if row["category"] not in CATEGORIES or not row["message_id"] or not row["thread_id"]:
            raise FilingError("invalid shadow source identity/category")
        return {"message_id": row["message_id"], "thread_id": row["thread_id"],
                "category": row["category"], "source": {"provider": "gmail",
                    "shadow_db": str(path.resolve()), "decision_id": decision_id,
                    "policy_version": row["policy_version"]}}


def verify_authority(credentials) -> None:
    """Check saved authority AND the actual access token, never requested scopes alone."""
    if not credentials or not credentials.valid or not credentials.has_scopes([MODIFY]):
        raise FilingError("credential lacks valid gmail.modify authority (readonly cannot file)")
    import requests
    try:
        response = requests.get("https://oauth2.googleapis.com/tokeninfo",
                                params={"access_token": credentials.token},
                                timeout=10, allow_redirects=False)
        info = response.json() if response.status_code == 200 else {}
        if (not isinstance(info, dict) or not isinstance(info.get("scope"), str)
                or MODIFY not in info["scope"].split()
                or int(info.get("expires_in", 0)) <= 0):
            raise FilingError("access token does not establish gmail.modify authority")
    except Exception:
        # Provider exceptions can contain the token-bearing URL. Never log them.
        raise FilingError("could not verify actual gmail.modify access-token authority") from None


def open_api(token_path: Path):
    """Existing valid token only: no consent, refresh, scope override, or token write."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    try:
        credentials = Credentials.from_authorized_user_file(token_path)
        if not credentials.valid:
            raise FilingError("existing token is missing or expired; executor cannot authorize/refresh")
        # Pin transport to this access token: client auto-refresh cannot replace it
        # after verify_authority. Expiration during a write becomes uncertain.
        transport_credentials = Credentials(token=credentials.token)
        return build("gmail", "v1", credentials=transport_credentials,
                     cache_discovery=False), credentials
    except Exception:
        raise FilingError("existing valid credential required; no OAuth flow or token write performed") from None


def _account(api):
    profile = api.users().getProfile(userId="me").execute(num_retries=0)
    account = profile.get("emailAddress") if isinstance(profile, dict) else None
    if not isinstance(account, str) or not account.strip():
        raise FilingError("could not establish Gmail account identity")
    return account.lower()


def _label(api, name):
    response = api.users().labels().list(userId="me").execute(num_retries=0)
    labels = response.get("labels") if isinstance(response, dict) else None
    if not isinstance(labels, list) or not all(isinstance(item, dict) for item in labels):
        raise FilingError("malformed Gmail label list")
    matches = [item for item in labels if item.get("name") == name]
    if (len(matches) != 1 or matches[0].get("type") != "user"
            or not isinstance(matches[0].get("id"), str)
            or not matches[0]["id"].startswith("Label_")):
        raise FilingError("requires one existing Email Ops user label; no label will be created")
    return matches[0]["id"]


def _labels(response, message_id, thread_id):
    if (not isinstance(response, dict) or response.get("id") != message_id
            or response.get("threadId") != thread_id
            or not isinstance(response.get("labelIds"), list)
            or not all(isinstance(item, str) and item for item in response["labelIds"])
            or len(set(response["labelIds"])) != len(response["labelIds"])):
        raise FilingError("ambiguous Gmail message response")
    return set(response["labelIds"])


def _read(api, message_id, thread_id):
    response = api.users().messages().get(userId="me", id=message_id, format="minimal",
                fields="id,threadId,labelIds").execute(num_retries=0)
    return _labels(response, message_id, thread_id)


def _expected(op):
    return (set(json.loads(op["prior"])) | set(json.loads(op["added"]))) - set(json.loads(op["removed"]))


def _modify(db, api, credentials, op, before, added, removed, action, config_path, approval):
    """In-flight state must be durable before this function; exactly one write attempt."""
    try:
        verify_authority(credentials)
        _gate(config_path, op["category"], approval, op["account"])
    except Exception:
        _state(db, op, "not_applied" if action == "file" else "completed", action,
               {"blocked_before_write": True})
        raise FilingError("mutation boundary check failed; no Gmail write attempted") from None
    try:
        response = api.users().messages().modify(userId="me", id=op["message_id"],
            body={"addLabelIds": sorted(added), "removeLabelIds": sorted(removed)}).execute(num_retries=0)
        after = _labels(response, op["message_id"], op["thread_id"])
        if after != (before | added) - removed:
            raise FilingError("partial or unexpected Gmail result")
    except Exception:
        return _state(db, op, "uncertain" if action == "file" else "undo_uncertain", action)
    return _state(db, op, "completed" if action == "file" else "undone", action)


def file(db, shadow_path: Path, decision_id: int, config_path: Path, *, live=False,
         approval=None, api=None, credentials=None, token_path=None) -> dict:
    if type(live) is not bool:
        raise FilingError("live mode must be an explicit boolean")
    request = decision(shadow_path, decision_id)
    category = request["category"]
    request["label_name"] = f"Email Ops/{category}"
    request_id = uuid.uuid4().hex
    _event(db, request_id, "file", live, "requested", request)
    if not live:
        _event(db, request_id, "file", False, "dry_run", request)
        return {"operation_id": request_id, "state": "dry_run"}
    try:
        _gate(config_path, category, approval)
        if api is None:
            api, credentials = open_api(token_path)
        verify_authority(credentials)
        account = _account(api)
        _gate(config_path, category, approval, account)
        db.execute("BEGIN IMMEDIATE")  # serialize reservations across CLI processes
        existing = db.execute("SELECT * FROM operations WHERE account = ? AND message_id = ?",
                              (account, request["message_id"])).fetchone()
        if existing:
            db.rollback()
            if (existing["state"] == "completed" and existing["category"] == category
                    and existing["thread_id"] == request["thread_id"]):
                _event(db, existing["id"], "file", True, "already_completed", request)
                return {"operation_id": existing["id"], "state": "completed"}
            raise FilingError("message already reserved; inspect/reconcile/undo its recorded operation")
        label_id = _label(api, request["label_name"])
        prior = _read(api, request["message_id"], request["thread_id"])
        if prior & FORBIDDEN:
            raise FilingError("draft/sent/trash/spam messages cannot be filed")
        added, removed = {label_id} - prior, {"INBOX"} & prior
        stamp = _now()
        db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (request_id, account, request["message_id"], request["thread_id"], category,
                    request["label_name"], label_id, json.dumps(request["source"]),
                    json.dumps(sorted(prior)), json.dumps(sorted(added)), json.dumps(sorted(removed)),
                    "filing_in_flight", stamp, stamp))
        _event(db, request_id, "file", True, "filing_in_flight",
               {"approval_reference": approval})  # commits prior + reservation before write
        op = db.execute("SELECT * FROM operations WHERE id = ?", (request_id,)).fetchone()
    except Exception as exc:
        db.rollback()
        _event(db, request_id, "file", True, "blocked", {"reason": type(exc).__name__})
        if isinstance(exc, FilingError):
            raise
        raise FilingError("filing preflight failed; no Gmail mutation attempted") from None
    if not added and not removed:
        return _state(db, op, "completed", "file", {"no_change": True})
    # Re-read operational switches immediately before the write, failing closed.
    try:
        _gate(config_path, category, approval, account)
    except FilingError:
        _state(db, op, "not_applied", "file")
        raise
    return _modify(db, api, credentials, op, prior, added, removed, "file", config_path, approval)


def _operation(db, operation_id):
    op = db.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if op is None:
        raise FilingError("no executor operation with that identifier")
    try:
        prior = json.loads(op["prior"])
        if (op["category"] not in CATEGORIES or op["label_name"] != f"Email Ops/{op['category']}"
                or not op["label_id"].startswith("Label_")
                or not isinstance(prior, list)
                or not all(isinstance(item, str) and item for item in prior)
                or len(set(prior)) != len(prior) or set(prior) & FORBIDDEN
                or json.loads(op["added"]) != sorted({op["label_id"]} - set(prior))
                or json.loads(op["removed"]) != sorted({"INBOX"} & set(prior))):
            raise FilingError("invalid executor receipt")
    except (ValueError, TypeError, AttributeError):
        raise FilingError("invalid executor receipt; mutation disabled") from None
    return op


def undo(db, operation_id: str, config_path: Path, *, live=False, approval=None,
         api=None, credentials=None, token_path=None) -> dict:
    if type(live) is not bool:
        raise FilingError("live mode must be an explicit boolean")
    op = _operation(db, operation_id)
    _event(db, op["id"], "undo", live, "requested", {"category": op["category"]})
    if not live:
        _event(db, op["id"], "undo", False, "dry_run", {})
        return {"operation_id": op["id"], "state": "dry_run"}
    try:
        _gate(config_path, op["category"], approval, op["account"])
        if api is None:
            api, credentials = open_api(token_path)
        verify_authority(credentials)
        if _account(api) != op["account"]:
            raise FilingError("credential account differs from recorded operation")
        db.execute("BEGIN IMMEDIATE")
        op = _operation(db, operation_id)
        if op["state"] == "undone":
            db.rollback()
            _event(db, op["id"], "undo", True, "already_undone", {})
            return {"operation_id": op["id"], "state": "undone"}
        if op["state"] != "completed":
            raise FilingError("undo requires a completed executor operation; reconcile uncertainty first")
        if _label(api, op["label_name"]) != op["label_id"]:
            raise FilingError("recorded label identity changed")
        current = _read(api, op["message_id"], op["thread_id"])
        managed = {op["label_id"], "INBOX"}
        if current & FORBIDDEN or (current & managed) != (_expected(op) & managed):
            raise FilingError("mailbox changed on managed labels; undo blocked for judgment")
        _state(db, op, "undo_in_flight", "undo", {"before": sorted(current),
                   "approval_reference": approval})
    except Exception as exc:
        db.rollback()
        _event(db, op["id"], "undo", True, "blocked", {"reason": type(exc).__name__})
        if isinstance(exc, FilingError):
            raise
        raise FilingError("undo preflight failed; no Gmail mutation attempted") from None
    added, removed = set(json.loads(op["removed"])), set(json.loads(op["added"]))
    if not added and not removed:
        return _state(db, op, "undone", "undo", {"no_change": True})
    return _modify(db, api, credentials, op, current, added, removed, "undo", config_path, approval)


def reconcile(db, operation_id: str, api) -> dict:
    """Read-only inspection after uncertainty; never retry or fill in partial changes."""
    db.execute("BEGIN IMMEDIATE")
    try:
        op = _operation(db, operation_id)
        if _account(api) != op["account"]:
            raise FilingError("credential account differs from recorded operation")
        if op["state"] not in {"filing_in_flight", "uncertain", "undo_in_flight", "undo_uncertain"}:
            raise FilingError("operation does not need reconciliation")
        current = _read(api, op["message_id"], op["thread_id"])
        managed = {op["label_id"], "INBOX"}
        prior, expected = set(json.loads(op["prior"])) & managed, _expected(op) & managed
        undoing = op["state"].startswith("undo_")
        # An unchanged/partial ambiguous filing stays reserved forever. It cannot
        # be blindly retried, even if a delayed Gmail write might still arrive.
        state = ("undone" if undoing else "uncertain") if current & managed == prior else (
            "completed" if not undoing else "undo_uncertain") if current & managed == expected else (
            "undo_uncertain" if undoing else "uncertain")
        if current & FORBIDDEN:
            state = "undo_uncertain" if undoing else "uncertain"
        db.execute("UPDATE operations SET state = ?, updated_at = ? WHERE id = ?",
                   (state, _now(), op["id"]))
        _event(db, op["id"], "reconcile", False, state, {"observed_labels": sorted(current)})
        return {"operation_id": op["id"], "state": state}
    except Exception as exc:
        db.rollback()
        if isinstance(exc, FilingError):
            raise
        raise FilingError("read-only reconciliation failed; operation state retained") from None


def _private(parser, path):
    path = path.expanduser().resolve()
    if not path.is_relative_to(PRIVATE_ROOT.resolve()):
        parser.error(f"all filing paths must be under {PRIVATE_ROOT}")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="M3 filing executor (offline dry-run by default)")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--token", type=Path, default=PRIVATE_ROOT / "token.json")
    sub = parser.add_subparsers(dest="command", required=True)
    filing = sub.add_parser("file", help="label + archive one M2 Tier 1 ARCHIVE decision")
    filing.add_argument("decision", type=int)
    filing.add_argument("--shadow-db", type=Path, default=SHADOW_DB)
    undoing = sub.add_parser("undo", help="reverse only a recorded executor operation")
    undoing.add_argument("operation_id")
    for command in (filing, undoing):
        command.add_argument("--live", action="store_true")
        command.add_argument("--approval", help="reference to Stephen's separate category approval")
    inspection = sub.add_parser("reconcile", help="read-only reconciliation; never retries Gmail writes")
    inspection.add_argument("operation_id")
    status = sub.add_parser("status", help="local operation state; no Gmail access")
    status.add_argument("operation_id")
    args = parser.parse_args(argv)
    audit = _private(parser, args.audit)
    config = _private(parser, args.config)
    token = _private(parser, args.token)
    shadow_path = _private(parser, args.shadow_db) if args.command == "file" else None
    paths = [audit, config, token] + ([shadow_path] if shadow_path else [])
    if len(set(paths)) != len(paths) or audit == SHADOW_DB.resolve():
        parser.error("audit, shadow database, configuration and token paths must be distinct")
    with connect(audit) as db:
        try:
            if args.command == "file":
                result = file(db, shadow_path, args.decision, config, live=args.live,
                              approval=args.approval, token_path=token)
            elif args.command == "undo":
                result = undo(db, args.operation_id, config, live=args.live,
                              approval=args.approval, token_path=token)
            elif args.command == "reconcile":
                api, _ = open_api(token)
                result = reconcile(db, args.operation_id, api)
            else:
                op = _operation(db, args.operation_id)
                result = {key: op[key] for key in ("state", "created_at", "updated_at")}
                result["operation_id"] = op["id"]
            print(json.dumps(result, indent=2))
            if result["state"] in {"uncertain", "undo_uncertain", "not_applied"}:
                raise SystemExit(2)
        except (FilingError, sqlite3.Error) as exc:
            parser.exit(2, f"Filing blocked: {exc if isinstance(exc, FilingError) else 'private database unavailable'}\n")


if __name__ == "__main__":
    main()
