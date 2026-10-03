"""Bounded M3 filing executor. Read-only preflight dry-run is the default.

Existing M2 Tier 1 ARCHIVE/DIGEST decisions supply the source. This module
never creates labels or obtains OAuth consent. Uncertain writes block until
explicit read-only reconciliation; no network write is automatically retried.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
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
REFILEABLE = {"not_applied", "undone"}
UNCERTAIN = {"uncertain", "undo_uncertain"}
DISPOSITIONS = {"ARCHIVE", "DIGEST"}
MAX_RUN = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY, account TEXT NOT NULL, message_id TEXT NOT NULL,
  thread_id TEXT NOT NULL, category TEXT NOT NULL, disposition TEXT NOT NULL, label_name TEXT NOT NULL,
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


class KillSwitchError(FilingError):
    """Global stop: batch execution must not continue after this boundary."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise FilingError("audit schema is newer than this executor")
        if version < 2:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                "AND name = 'operations'").fetchone()
            if exists and db.execute("SELECT 1 FROM operations LIMIT 1").fetchone():
                raise FilingError("audit schema is older than this executor")
            db.executescript("DROP TABLE IF EXISTS operations; DROP TABLE IF EXISTS events;")
        db.execute("PRAGMA synchronous = FULL")
        db.executescript(SCHEMA + "\nPRAGMA user_version = 2;")
        return db
    except Exception:
        db.close()
        raise


def _event(db, operation_id, action, mode, result, details, *, commit=True):
    db.execute("INSERT INTO events (operation_id, at, action, mode, result, details) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (operation_id, _now(), action, mode, result,
                json.dumps(details, sort_keys=True)))
    if commit:
        db.commit()


def _state(db, op, state, action, details=None):
    db.execute("UPDATE operations SET state = ?, updated_at = ? WHERE id = ?",
               (state, _now(), op["id"]))
    _event(db, op["id"], action, "live", state, details or {})
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
                    or set(entry) != {"enabled", "approval_reference", "enabled_since"}
                    or type(entry["enabled"]) is not bool
                    or not isinstance(entry["approval_reference"], str)
                    or (entry["enabled"] and not entry["approval_reference"].strip())):
                raise FilingError("invalid category configuration")
            since = entry["enabled_since"]
            if since is not None:
                if not isinstance(since, str) or date.fromisoformat(since).isoformat() != since:
                    raise FilingError("enabled_since must be an ISO date")
            if entry["enabled"] and since is None:
                raise FilingError("enabled category requires enabled_since")
        return config
    except (OSError, ValueError, TypeError):
        raise FilingError("missing or malformed filing configuration; mutation disabled") from None


def _check_config(config, category, *, live, account=None, received_ms=None, require_enabled=True):
    if account is not None and account != config["account"]:
        raise FilingError("credential account differs from configured shadow-source account")
    if category is not None and category not in CATEGORIES:
        raise FilingError("unknown filing category")
    if not live:
        return
    if config["kill_switch"]:
        raise KillSwitchError("global kill switch is on")
    if category is not None and require_enabled:
        entry = config["categories"].get(category)
        if not entry or not entry["enabled"]:
            raise FilingError("category is not explicitly enabled")
        if received_ms is not None and received_ms < _since(entry):
            raise FilingError("decision predates category enabled_since")


def _gate(config_path, category, *, live, account=None, received_ms=None, require_enabled=True):
    config = load_config(config_path)
    _check_config(config, category, live=live, account=account, received_ms=received_ms,
                  require_enabled=require_enabled)
    return config


def _since(entry):
    return int(datetime.fromisoformat(entry["enabled_since"]).replace(tzinfo=timezone.utc).timestamp() * 1000)


def decision(path: Path, decision_id: int) -> dict:
    """Read the existing shadow DB without schema changes or private policy edits."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None or row["tier"] != 1 or row["disposition"] not in DISPOSITIONS:
            raise FilingError("requires an existing Tier 1 ARCHIVE/DIGEST shadow decision")
        correction = db.execute("SELECT * FROM corrections WHERE decision_id = ? "
                                "ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
        if correction and (correction["category"] != row["category"]
                           or correction["disposition"] != row["disposition"]):
            raise FilingError("shadow decision has a conflicting correction")
        if (row["category"] not in CATEGORIES or not row["message_id"] or not row["thread_id"]
                or type(row["received_ms"]) is not int or row["received_ms"] < 0):
            raise FilingError("invalid shadow source identity/category")
        return {"message_id": row["message_id"], "thread_id": row["thread_id"],
                "category": row["category"], "disposition": row["disposition"],
                "received_ms": row["received_ms"], "source": {"provider": "gmail",
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
        raise FilingError("existing token is missing or expired; no OAuth flow, refresh or token write performed") from None


def _account(api):
    profile = api.users().getProfile(userId="me").execute(num_retries=0)
    account = profile.get("emailAddress") if isinstance(profile, dict) else None
    if not isinstance(account, str) or not account.strip():
        raise FilingError("could not establish Gmail account identity")
    return account.lower()


def _label_list(api):
    response = api.users().labels().list(userId="me").execute(num_retries=0)
    labels = response.get("labels") if isinstance(response, dict) else None
    if not isinstance(labels, list) or not all(isinstance(item, dict) for item in labels):
        raise FilingError("malformed Gmail label list")
    return labels


def _label(labels, name):
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


def _session(config, *, live, api, credentials, token_path):
    """Resolve the account/label inventory once; batch filing shares these reads."""
    if api is None:
        api, credentials = open_api(token_path)
    if not credentials or not credentials.valid:
        raise FilingError("existing token is missing or expired")
    if live:
        verify_authority(credentials)
    account = _account(api)
    _check_config(config, None, live=live, account=account)
    return {"api": api, "account": account, "labels": _label_list(api), "config": config, "live": live}


def _delta(prior, label_id, disposition):
    return {label_id} - prior, {"INBOX"} & prior if disposition == "ARCHIVE" else set()


def _modify(db, api, op, before, added, removed, action, config_path, received_ms=None):
    """Durable in-flight receipt, final config read, then at most one write attempt."""
    try:
        config = _gate(config_path, op["category"], live=True, account=op["account"],
                       received_ms=received_ms, require_enabled=action == "file")
    except FilingError:
        _state(db, op, "not_applied" if action == "file" else "completed", action,
               {"blocked_before_write": True})
        raise
    approval = config["categories"].get(op["category"], {}).get("approval_reference", "")
    _event(db, op["id"], action, "live", "write_authorized", {"approval_reference": approval})
    if not added and not removed:
        return _state(db, op, "completed" if action == "file" else "undone", action,
                      {"no_change": True})
    try:
        response = api.users().messages().modify(userId="me", id=op["message_id"],
            body={"addLabelIds": sorted(added), "removeLabelIds": sorted(removed)}).execute(num_retries=0)
        after = _labels(response, op["message_id"], op["thread_id"])
        managed = {op["label_id"], "INBOX"}
        if (after & managed) != (((before | added) - removed) & managed):
            raise FilingError("partial or unexpected Gmail result")
    except Exception:
        return _state(db, op, "uncertain" if action == "file" else "undo_uncertain", action)
    return _state(db, op, "completed" if action == "file" else "undone", action)


def file(db, shadow_path: Path, decision_id: int, config_path: Path, *, live=False,
         api=None, credentials=None, token_path=None, session=None, label_id=None) -> dict:
    if type(live) is not bool:
        raise FilingError("live mode must be an explicit boolean")
    operation_id, request = None, {"decision_id": decision_id}
    mode = "live" if live else "dry-run"
    try:
        request = decision(shadow_path, decision_id)
        category = request["category"]
        request["label_name"] = f"Email Ops/{category}"
        if session is None:
            config = _gate(config_path, category, live=live, received_ms=request["received_ms"])
            session = _session(config, live=live, api=api, credentials=credentials, token_path=token_path)
        config, api, account = session["config"], session["api"], session["account"]
        if session["live"] is not live:
            raise FilingError("preflight mode differs from requested mode")
        _check_config(config, category, live=live, account=account, received_ms=request["received_ms"])
        label_id = label_id or _label(session["labels"], request["label_name"])
        if live:
            db.execute("BEGIN IMMEDIATE")  # reservation/read/update stay in one transaction
            existing = db.execute("SELECT * FROM operations WHERE account = ? AND message_id = ?",
                                  (account, request["message_id"])).fetchone()
            if existing:
                operation_id = existing["id"]
                existing = _operation(db, operation_id)
                if (existing["state"] == "completed" and existing["category"] == category
                        and existing["thread_id"] == request["thread_id"]
                        and existing["disposition"] == request["disposition"]):
                    db.rollback()
                    _gate(config_path, category, live=True, account=account,
                          received_ms=request["received_ms"])
                    _event(db, operation_id, "file", mode, "already_completed", request)
                    return {"operation_id": operation_id, "state": "completed"}
                if existing["state"] not in REFILEABLE:
                    raise FilingError("message already reserved; inspect/reconcile/undo its recorded operation")
            else:
                operation_id = uuid.uuid4().hex
        prior = _read(api, request["message_id"], request["thread_id"])
        if prior & FORBIDDEN:
            raise FilingError("draft/sent/trash/spam messages cannot be filed")
        added, removed = _delta(prior, label_id, request["disposition"])
        plan = {**request, "account": account, "label_id": label_id,
                "prior": sorted(prior), "added": sorted(added), "removed": sorted(removed)}
        if not live:
            _event(db, None, "file", mode, "dry_run", plan)
            return {"state": "dry_run", "plan": plan}
        approval = config["categories"][category]["approval_reference"]
        _event(db, operation_id, "file", mode, "requested", {**request, "approval_reference": approval},
               commit=False)
        values = (request["thread_id"], category, request["disposition"], request["label_name"], label_id,
                  json.dumps(request["source"]), json.dumps(sorted(prior)), json.dumps(sorted(added)),
                  json.dumps(sorted(removed)), "filing_in_flight", _now())
        if existing:
            db.execute("UPDATE operations SET thread_id=?, category=?, disposition=?, label_name=?, "
                       "label_id=?, source=?, prior=?, added=?, removed=?, state=?, updated_at=? WHERE id=?",
                       (*values, operation_id))
        else:
            db.execute("INSERT INTO operations (id, account, message_id, thread_id, category, disposition, "
                       "label_name, label_id, source, prior, added, removed, state, updated_at, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (operation_id, account, request["message_id"], *values, _now()))
        _event(db, operation_id, "file", mode, "filing_in_flight",
               {**plan, "approval_reference": approval})  # durable prior before Gmail
        op = _operation(db, operation_id)
    except Exception as exc:
        db.rollback()
        reason = str(exc) if isinstance(exc, FilingError) else "filing preflight failed"
        _event(db, operation_id, "file", mode, "blocked", {**request, "reason": reason})
        raise FilingError(reason) from None
    return _modify(db, api, op, prior, added, removed, "file", config_path, request["received_ms"])


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
                or op["disposition"] not in DISPOSITIONS
                or json.loads(op["added"]) != sorted({op["label_id"]} - set(prior))
                or json.loads(op["removed"]) != sorted(_delta(set(prior), op["label_id"],
                                                              op["disposition"])[1])):
            raise FilingError("invalid executor receipt")
    except (ValueError, TypeError, AttributeError):
        raise FilingError("invalid executor receipt; mutation disabled") from None
    return op


def undo(db, operation_id: str, config_path: Path, *, live=False,
         api=None, credentials=None, token_path=None) -> dict:
    if type(live) is not bool:
        raise FilingError("live mode must be an explicit boolean")
    op = _operation(db, operation_id)
    mode = "live" if live else "dry-run"
    try:
        config = _gate(config_path, op["category"], live=live, account=op["account"], require_enabled=False)
        session = _session(config, live=live, api=api, credentials=credentials, token_path=token_path)
        api = session["api"]
        if session["account"] != op["account"]:
            raise FilingError("credential account differs from recorded operation")
        if live:
            db.execute("BEGIN IMMEDIATE")
        op = _operation(db, operation_id)
        if live and op["state"] == "undone":
            db.rollback()
            _gate(config_path, op["category"], live=True, account=op["account"], require_enabled=False)
            _event(db, op["id"], "undo", mode, "already_undone", {})
            return {"operation_id": op["id"], "state": "undone"}
        if op["state"] != "completed":
            raise FilingError("undo requires a completed executor operation; reconcile uncertainty first")
        if _label(session["labels"], op["label_name"]) != op["label_id"]:
            raise FilingError("recorded label identity changed")
        current = _read(api, op["message_id"], op["thread_id"])
        managed = {op["label_id"], "INBOX"}
        if current & FORBIDDEN or (current & managed) != (_expected(op) & managed):
            raise FilingError("mailbox changed on managed labels; undo blocked for judgment")
        added, removed = set(json.loads(op["removed"])), set(json.loads(op["added"]))
        plan = {"undo_operation_id": op["id"], "category": op["category"], "account": op["account"],
                "message_id": op["message_id"], "thread_id": op["thread_id"], "prior": sorted(current),
                "added": sorted(added), "removed": sorted(removed)}
        if not live:
            _event(db, op["id"], "undo", mode, "dry_run", plan)
            return {"state": "dry_run", "plan": plan}
        _event(db, op["id"], "undo", mode, "requested", {"category": op["category"]}, commit=False)
        _state(db, op, "undo_in_flight", "undo", {"before": sorted(current),
                   "approval_reference": config["categories"].get(op["category"], {}).get("approval_reference", "")})
    except Exception as exc:
        db.rollback()
        _event(db, op["id"], "undo", mode, "blocked",
               {"reason": str(exc) if isinstance(exc, FilingError) else "undo preflight failed"})
        if isinstance(exc, FilingError):
            raise
        raise FilingError("undo preflight failed; no Gmail mutation attempted") from None
    return _modify(db, api, op, current, added, removed, "undo", config_path)


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
        observed = current & managed
        if current & FORBIDDEN:
            state = "undo_uncertain" if undoing else "uncertain"
        elif observed == expected:
            state = "undone" if undoing and prior == expected else "completed"
        elif observed == prior:
            state = "undone" if undoing else "not_applied"
        else:
            state = "undo_uncertain" if undoing else "uncertain"
        db.execute("UPDATE operations SET state = ?, updated_at = ? WHERE id = ?",
                   (state, _now(), op["id"]))
        _event(db, op["id"], "reconcile", "read-only", state, {"observed_labels": sorted(current)})
        return {"operation_id": op["id"], "state": state}
    except Exception as exc:
        db.rollback()
        if isinstance(exc, FilingError):
            raise
        raise FilingError("read-only reconciliation failed; operation state retained") from None


def run(db, shadow_path: Path, config_path: Path, *, category=None, live=False,
        api=None, credentials=None, token_path=None) -> dict:
    """At most 100 eligible messages; shared profile, label inventory and authority check."""
    if type(live) is not bool:
        raise FilingError("live mode must be an explicit boolean")
    mode = "live" if live else "dry-run"
    result = {"state": "completed" if live else "dry_run", "listed": 0, "filed": 0,
              "skipped": 0, "skip_reasons": [], "plans": [], "results": [], "capped": False}

    def skip(decision_id, reason):
        details = {"decision_id": decision_id, "reason": reason}
        result["skipped"] += 1
        result["skip_reasons"].append(details)
        _event(db, None, "run", mode, "skipped", details)

    try:
        config = _gate(config_path, category, live=live)
        existing = {row["message_id"]: row["state"] for row in db.execute(
            "SELECT message_id, state FROM operations WHERE account = ?", (config["account"],))}
        selected = []
        with closing(sqlite3.connect(shadow_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            rows = source.execute(
                "SELECT d.*, c.category AS corrected_category, c.disposition AS corrected_disposition "
                "FROM decisions d LEFT JOIN corrections c ON c.id = "
                "(SELECT MAX(id) FROM corrections WHERE decision_id = d.id) "
                "WHERE d.tier = 1 AND d.disposition IN ('ARCHIVE', 'DIGEST') "
                "ORDER BY d.received_ms, d.id")
            for row in rows:
                if category is not None and row["category"] != category:
                    continue  # outside the requested category, not a skipped filing
                entry = config["categories"].get(row["category"])
                reason = None
                if not entry or not entry["enabled"]:
                    reason = "category_not_enabled"
                elif row["received_ms"] < _since(entry):
                    reason = "before_enabled_since"
                elif row["corrected_disposition"] is not None and (
                        row["corrected_disposition"] != row["disposition"]
                        or row["corrected_category"] != row["category"]):
                    reason = "conflicting_correction"
                elif row["message_id"] in existing and existing[row["message_id"]] not in REFILEABLE:
                    reason = "already_completed" if existing[row["message_id"]] == "completed" else (
                        "reserved_" + existing[row["message_id"]])
                if reason:
                    skip(row["id"], reason)
                    continue
                if len(selected) == MAX_RUN:
                    result["capped"] = True
                    break
                selected.append((row["id"], row["category"]))
        result["listed"] = len(selected)
        if not selected:
            return result
        session = _session(config, live=live, api=api, credentials=credentials, token_path=token_path)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, FilingError) else "batch preflight failed"
        _event(db, None, "run", mode, "blocked", {"reason": reason})
        raise FilingError(reason) from None
    # Resolve once per category from the one label inventory. A missing category
    # label blocks only its messages and never causes label creation.
    label_ids = {}
    blocked = False
    for index, (decision_id, target) in enumerate(selected):
        try:
            if target not in label_ids:
                label_ids[target] = _label(session["labels"], f"Email Ops/{target}")
            outcome = file(db, shadow_path, decision_id, config_path, live=live,
                           session=session, label_id=label_ids[target])
        except KillSwitchError as exc:
            _event(db, None, "run", mode, "blocked", {"decision_id": decision_id, "reason": str(exc)})
            for remaining, _ in selected[index:]:
                skip(remaining, "stopped_by_kill_switch")
            result["state"] = "blocked"
            break
        except FilingError as exc:
            blocked = True
            skip(decision_id, str(exc))
            continue
        if live:
            result["results"].append({"decision_id": decision_id, **outcome})
            if outcome["state"] in UNCERTAIN:
                result["state"] = outcome["state"]
                for remaining, _ in selected[index + 1:]:
                    skip(remaining, "stopped_after_uncertain")
                break
            result["filed"] += 1
        else:
            result["plans"].append({"decision_id": decision_id, **outcome["plan"]})
    if result["state"] not in UNCERTAIN and blocked:
        result["state"] = "blocked"
    return result


def _private(parser, path):
    path = path.expanduser().resolve()
    if not path.is_relative_to(PRIVATE_ROOT.resolve()):
        parser.error(f"all filing paths must be under {PRIVATE_ROOT}")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="M3 filing executor (read-only dry-run by default)")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--token", type=Path, default=PRIVATE_ROOT / "token.json")
    sub = parser.add_subparsers(dest="command", required=True)
    filing = sub.add_parser("file", help="file one M2 Tier 1 ARCHIVE/DIGEST decision")
    filing.add_argument("decision", type=int)
    filing.add_argument("--shadow-db", type=Path, default=SHADOW_DB)
    undoing = sub.add_parser("undo", help="reverse only a recorded executor operation")
    undoing.add_argument("operation_id")
    batch = sub.add_parser("run", help="file up to 100 incoming decisions in enabled categories")
    batch.add_argument("--category", choices=CATEGORIES)
    batch.add_argument("--shadow-db", type=Path, default=SHADOW_DB)
    for command in (filing, undoing, batch):
        command.add_argument("--live", action="store_true")
    inspection = sub.add_parser("reconcile", help="read-only reconciliation; never retries Gmail writes")
    inspection.add_argument("operation_id")
    status = sub.add_parser("status", help="local operation state; no Gmail access")
    status.add_argument("operation_id")
    args = parser.parse_args(argv)
    audit = _private(parser, args.audit)
    config = _private(parser, args.config)
    token = _private(parser, args.token)
    shadow_path = _private(parser, args.shadow_db) if args.command in {"file", "run"} else None
    paths = [audit, config, token] + ([shadow_path] if shadow_path else [])
    if len(set(paths)) != len(paths) or audit == SHADOW_DB.resolve():
        parser.error("audit, shadow database, configuration and token paths must be distinct")
    db = None
    try:
        db = connect(audit)
        if args.command == "file":
            result = file(db, shadow_path, args.decision, config, live=args.live, token_path=token)
        elif args.command == "undo":
            result = undo(db, args.operation_id, config, live=args.live, token_path=token)
        elif args.command == "run":
            result = run(db, shadow_path, config, category=args.category, live=args.live, token_path=token)
        elif args.command == "reconcile":
            api, _ = open_api(token)
            result = reconcile(db, args.operation_id, api)
        else:
            op = _operation(db, args.operation_id)
            result = {key: op[key] for key in ("state", "created_at", "updated_at")}
            result["operation_id"] = op["id"]
        print(json.dumps(result, indent=2))
        if result["state"] in UNCERTAIN:
            raise SystemExit(3)
        if result["state"] in {"not_applied", "blocked"}:
            raise SystemExit(2)
    except (FilingError, sqlite3.Error) as exc:
        parser.exit(2, f"Filing blocked: {exc if isinstance(exc, FilingError) else 'private database unavailable'}\n")
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
