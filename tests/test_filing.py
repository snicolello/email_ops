"""M3 adversarial contracts. All message reads/writes and token checks are fake."""

import ast
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import requests

from email_ops import filing, shadow
from email_ops.gmail import SCOPES

APPROVAL = "synthetic owner approval receipt"
LABEL = "Label_123"


class Credentials:
    def __init__(self, scopes=(filing.MODIFY,), valid=True):
        self.scopes, self.valid, self.token = scopes, valid, "synthetic-access-token"

    def has_scopes(self, scopes):
        return set(scopes) <= set(self.scopes)


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self, num_retries):
        assert num_retries == 0
        return self.callback()


class FakeGmail:
    def __init__(self, labels=("INBOX", "UNREAD", "STARRED")):
        self.states, self.calls, self.writes, self.applied = {"m1": set(labels)}, [], [], []
        self.account = "owner@example.test"
        self.label_rows = [{"id": LABEL, "name": "Email Ops/Marketing", "type": "user"}]
        self.behavior = "success"
        self.label_read = None

    @property
    def state(self):
        return self.states["m1"]

    @state.setter
    def state(self, value):
        self.states["m1"] = value

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        return self

    def getProfile(self, **kw):
        self.calls.append(("profile", kw))
        return Request(lambda: {"emailAddress": self.account})

    def list(self, **kw):
        self.calls.append(("labels", kw))
        if self.label_read:
            self.label_read()
        return Request(lambda: {"labels": self.label_rows})

    def response(self, message_id="m1"):
        return {"id": message_id, "threadId": "t" + message_id[1:],
                "labelIds": sorted(self.states[message_id])}

    def get(self, **kw):
        assert kw == {"userId": "me", "id": kw["id"], "format": "minimal",
                      "fields": "id,threadId,labelIds"}
        self.calls.append(("get", kw))
        return Request(lambda: self.response(kw["id"]))

    def modify(self, **kw):
        assert kw["userId"] == "me" and kw["id"] in self.states
        assert set(kw["body"]) == {"addLabelIds", "removeLabelIds"}
        assert set(kw["body"]["addLabelIds"]) <= {LABEL, "Label_456", "INBOX"}
        assert set(kw["body"]["removeLabelIds"]) <= {LABEL, "Label_456", "INBOX"}

        def apply():
            self.writes.append(kw["body"])
            if self.behavior == "timeout_before":
                raise TimeoutError("synthetic provider error must not enter audit")
            state = self.states[kw["id"]]
            state |= set(kw["body"]["addLabelIds"])
            if self.behavior != "partial":
                state -= set(kw["body"]["removeLabelIds"])
            self.applied.append(kw["body"])
            if self.behavior == "read_during_modify":
                state.discard("UNREAD")
                state.add("CATEGORY_UPDATES")
            if self.behavior == "timeout_after":
                raise TimeoutError("synthetic provider error must not enter audit")
            if self.behavior == "malformed":
                return {}
            return self.response(kw["id"])
        return Request(apply)


@pytest.fixture(autouse=True)
def tokeninfo(monkeypatch):
    calls = []

    def get(url, **kw):
        assert url == "https://oauth2.googleapis.com/tokeninfo"
        assert kw == {"params": {"access_token": "synthetic-access-token"},
                      "timeout": 10, "allow_redirects": False}
        calls.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {
            "scope": filing.MODIFY, "expires_in": "3600"})
    monkeypatch.setattr(requests, "get", get)
    return calls


@pytest.fixture
def setup(tmp_path):
    shadow_path = tmp_path / "shadow.db"
    db = shadow.connect(shadow_path)
    db.execute("INSERT INTO runs VALUES (1, 'synthetic', 'synthetic', 1, 1, 0, 'v1')")
    db.execute("INSERT INTO decisions VALUES "
               "(1, 'm1', 't1', 1800000000000, 'sender:synthetic', 'sender@example.test', 'synthetic', "
               "1, 'ARCHIVE', 'Marketing', 'policy', 'v1', 1, NULL)")
    db.commit()
    db.close()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema_version": 1, "account": "owner@example.test",
        "kill_switch": False, "categories": {
        "Marketing": {"enabled": True, "enabled_since": "2026-01-01",
                      "approval_reference": APPROVAL}}}))
    audit = filing.connect(tmp_path / "audit.db")
    yield SimpleNamespace(db=audit, shadow=shadow_path, config=config,
                          api=FakeGmail(), credentials=Credentials(), root=tmp_path)
    audit.close()


def run(s, **kw):
    args = dict(live=True, api=s.api, credentials=s.credentials)
    args.update(kw)
    return filing.file(s.db, s.shadow, 1, s.config, **args)


def undo(s, operation, **kw):
    args = dict(live=True, api=s.api, credentials=s.credentials)
    args.update(kw)
    return filing.undo(s.db, operation, s.config, **args)


def change_config(s, update):
    config = json.loads(s.config.read_text())
    update(config)
    s.config.write_text(json.dumps(config))


def test_default_dry_run_is_readonly_and_audited(setup, tokeninfo):
    s = setup
    result = run(s, live=False, credentials=Credentials(SCOPES))
    assert result["state"] == "dry_run"
    assert [kind for kind, _ in s.api.calls] == ["profile", "labels", "get"]
    assert not s.api.writes and not tokeninfo
    assert set(result) == {"state", "plan"}
    assert result["plan"]["added"] == [LABEL]
    assert result["plan"]["removed"] == ["INBOX"]
    assert s.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    events = s.db.execute("SELECT * FROM events").fetchall()
    assert [e["result"] for e in events] == ["dry_run"]
    details = json.loads(events[-1]["details"])
    assert details["category"] == "Marketing" and details["message_id"] == "m1"
    assert details["source"]["decision_id"] == 1
    assert all(e["mode"] == "dry-run" and e["at"] and e["operation_id"] is None for e in events)


@pytest.mark.parametrize("credentials", [Credentials(SCOPES), Credentials(()), Credentials(valid=False), None])
def test_readonly_missing_or_invalid_authority_blocks(setup, credentials, tokeninfo):
    with pytest.raises(filing.FilingError, match="gmail.modify|missing or expired"):
        run(setup, credentials=credentials)
    assert not setup.api.calls and not setup.api.writes and not tokeninfo


@pytest.mark.parametrize("payload,status", [
    ({"scope": SCOPES[0], "expires_in": 3600}, 200),
    ({"scope": filing.MODIFY, "expires_in": 0}, 200),
    ({"scope": [filing.MODIFY], "expires_in": 3600}, 200),
    ({"scope": filing.MODIFY, "expires_in": "bad"}, 200),
    ({}, 200), (None, 200), ({"scope": filing.MODIFY, "expires_in": 3600}, 302)])
def test_actual_access_token_authority_is_required(setup, monkeypatch, payload, status):
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(
        status_code=status, json=lambda: payload))
    with pytest.raises(filing.FilingError, match="access-token authority"):
        run(setup)
    assert not setup.api.calls


def test_tokeninfo_failure_is_redacted(setup, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("secret-token-bearing-url")
    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(filing.FilingError) as error:
        run(setup)
    assert "secret" not in str(error.value)
    assert "secret" not in str([dict(r) for r in setup.db.execute("SELECT * FROM events")])


@pytest.mark.parametrize("update,match", [
    (lambda c: c.update(kill_switch=True), "kill switch"),
    (lambda c: c.update(categories={}), "not explicitly enabled"),
    (lambda c: c["categories"]["Marketing"].update(enabled=False), "not explicitly enabled")])
def test_live_switches_fail_closed(setup, update, match, tokeninfo):
    change_config(setup, update)
    with pytest.raises(filing.FilingError, match=match):
        run(setup)
    assert not setup.api.calls and not tokeninfo


@pytest.mark.parametrize("raw", [None, "{", "null", "[]", "{}",
    '{"schema_version":1,"kill_switch":false,"kill_switch":true,"categories":{}}'])
def test_missing_malformed_duplicate_configuration_blocks(setup, raw):
    if raw is None:
        setup.config.unlink()
    else:
        setup.config.write_text(raw)
    with pytest.raises(filing.FilingError, match="configuration"):
        run(setup)
    assert not setup.api.calls


@pytest.mark.parametrize("update", [
    lambda c: c.update(kill_switch="false"), lambda c: c.update(schema_version=True),
    lambda c: c.update(categories=[]), lambda c: c.update(extra=True),
    lambda c: c["categories"]["Marketing"].update(enabled="true"),
    lambda c: c["categories"]["Marketing"].update(label_name="SPAM"),
    lambda c: c["categories"]["Marketing"].update(approval_reference=""),
    lambda c: c["categories"]["Marketing"].update(extra=True)])
def test_ambiguous_config_fields_block(setup, update):
    change_config(setup, update)
    with pytest.raises(filing.FilingError, match="configuration"):
        run(setup)
    assert not setup.api.calls


def test_success_is_reserved_before_write_and_reversible(setup):
    s = setup
    original = s.api.modify

    def checked(**kw):
        # A separate connection can see the receipt BEFORE the write starts.
        with filing.connect(s.root / "audit.db") as other:
            op = other.execute("SELECT * FROM operations").fetchone()
            assert op["state"] == "filing_in_flight"
            assert set(json.loads(op["prior"])) == {"INBOX", "UNREAD", "STARRED"}
            assert json.loads(op["added"]) == [LABEL]
            assert json.loads(op["removed"]) == ["INBOX"]
        return original(**kw)
    s.api.modify = checked
    result = run(s)
    assert result["state"] == "completed"
    assert s.api.state == {LABEL, "UNREAD", "STARRED"}
    assert s.api.writes == [{"addLabelIds": [LABEL], "removeLabelIds": ["INBOX"]}]
    op = filing._operation(s.db, result["operation_id"])
    assert op["account"] == "owner@example.test" and op["created_at"] and op["updated_at"]


def test_repeat_after_reopen_makes_no_duplicate_write(setup):
    first = run(setup)
    setup.db.close()
    setup.db = filing.connect(setup.root / "audit.db")
    assert run(setup) == first
    assert len(setup.api.writes) == 1


@pytest.mark.parametrize("behavior", ["timeout_before", "timeout_after", "malformed", "partial"])
def test_unknown_outcome_is_not_blindly_retried(setup, behavior):
    s = setup
    s.api.behavior = behavior
    result = run(s)
    assert result["state"] == "uncertain"
    with pytest.raises(filing.FilingError, match="already reserved"):
        run(s)
    with pytest.raises(filing.FilingError, match="requires a completed"):
        undo(s, result["operation_id"])
    inspected = filing.reconcile(s.db, result["operation_id"], s.api)
    assert inspected["state"] == ("completed" if behavior in {"timeout_after", "malformed"} else "not_applied" if behavior == "timeout_before" else "uncertain")
    assert len(s.api.writes) == 1
    if inspected["state"] == "uncertain":
        with pytest.raises(filing.FilingError):
            run(s)
    assert "synthetic provider error" not in str([dict(r) for r in s.db.execute("SELECT * FROM events")])


def test_crash_after_reservation_stays_blocked(setup):
    result = run(setup)
    filing._state(setup.db, filing._operation(setup.db, result["operation_id"]), "filing_in_flight", "file")
    with pytest.raises(filing.FilingError, match="already reserved"):
        run(setup)
    assert len(setup.api.writes) == 1
    assert filing.reconcile(setup.db, result["operation_id"], setup.api)["state"] == "completed"


@pytest.mark.parametrize("prior", [("INBOX", "UNREAD"), ("INBOX", LABEL, "UNREAD"),
                                  (LABEL, "UNREAD"), ("UNREAD",)])
def test_undo_restores_only_executor_delta(setup, prior):
    s = setup
    s.api.state = set(prior)
    result = run(s)
    s.api.state.add("Label_unrelated")
    s.api.state.discard("UNREAD")  # later manual read must not be reversed
    assert undo(s, result["operation_id"], live=False)["state"] == "dry_run"
    writes = len(s.api.writes)
    assert undo(s, result["operation_id"])["state"] == "undone"
    assert s.api.state == (set(prior) - {"UNREAD"}) | {"Label_unrelated"}
    assert undo(s, result["operation_id"])["state"] == "undone"
    assert len(s.api.writes) <= writes + 1
    assert run(s)["operation_id"] == result["operation_id"]


def test_undo_rechecks_authority_switches_and_account(setup):
    s = setup
    op = run(s)["operation_id"]
    change_config(s, lambda c: c["categories"]["Marketing"].update(enabled=False))
    for kw in ({"credentials": Credentials(SCOPES)},):
        with pytest.raises(filing.FilingError):
            undo(s, op, **kw)
    change_config(s, lambda c: c.update(kill_switch=True))
    with pytest.raises(filing.FilingError, match="kill switch"):
        undo(s, op)
    change_config(s, lambda c: c.update(kill_switch=False))
    s.api.account = "different@example.test"
    with pytest.raises(filing.FilingError, match="account differs"):
        undo(s, op)
    assert len(s.api.writes) == 1
    s.api.account = "owner@example.test"
    assert undo(s, op)["state"] == "undone"
    assert len(s.api.writes) == 2


def test_undo_managed_label_conflict_blocks(setup):
    op = run(setup)["operation_id"]
    setup.api.state.add("INBOX")
    with pytest.raises(filing.FilingError, match="mailbox changed"):
        undo(setup, op)
    assert len(setup.api.writes) == 1


@pytest.mark.parametrize("behavior", ["timeout_before", "timeout_after", "partial", "malformed"])
def test_uncertain_undo_never_retries(setup, behavior):
    op = run(setup)["operation_id"]
    setup.api.behavior = behavior
    assert undo(setup, op)["state"] == "undo_uncertain"
    with pytest.raises(filing.FilingError):
        undo(setup, op)
    assert len(setup.api.writes) == 2
    filing.reconcile(setup.db, op, setup.api)
    assert len(setup.api.writes) == 2


@pytest.mark.parametrize("label", ["TRASH", "SPAM", "DRAFT", "DRAFTS", "SENT"])
def test_forbidden_mailbox_states_are_not_touched(setup, label):
    setup.api.state.add(label)
    with pytest.raises(filing.FilingError, match="cannot be filed"):
        run(setup)
    assert not setup.api.writes


@pytest.mark.parametrize("rows", [[], [{"id": "SPAM", "name": "Email Ops/Marketing", "type": "system"}],
    [{"id": LABEL, "name": "Email Ops/Marketing", "type": "user"}] * 2])
def test_requires_unambiguous_existing_user_label(setup, rows):
    setup.api.label_rows = rows
    with pytest.raises(filing.FilingError, match="existing Email Ops user label"):
        run(setup)
    assert not setup.api.writes


def test_switch_is_rechecked_after_preflight(setup, tokeninfo):
    setup.api.label_read = lambda: change_config(setup, lambda c: c.update(kill_switch=True))
    with pytest.raises(filing.FilingError, match="kill switch"):
        run(setup)
    assert not setup.api.writes
    assert setup.db.execute("SELECT state FROM operations").fetchone()[0] == "not_applied"
    assert len(tokeninfo) == 1


@pytest.mark.parametrize("disposition,tier", [("INBOX", 2), ("ARCHIVE", 3), ("DELETE", 1)])
def test_only_archive_tier1_decisions_are_accepted(setup, disposition, tier):
    with shadow.connect(setup.shadow) as db:
        db.execute("UPDATE decisions SET disposition = ?, tier = ?", (disposition, tier))
    with pytest.raises(filing.FilingError, match="Tier 1 ARCHIVE/DIGEST"):
        run(setup)
    assert not setup.api.calls


def test_conflicting_correction_is_not_ignored(setup):
    with shadow.connect(setup.shadow) as db:
        shadow.correct(db, 1, "INBOX")
    with pytest.raises(filing.FilingError, match="conflicting correction"):
        run(setup)
    assert not setup.api.calls


def test_cli_private_paths_and_dry_run(setup, monkeypatch, capsys):
    monkeypatch.setattr(filing, "PRIVATE_ROOT", setup.root)
    monkeypatch.setattr(filing, "open_api", lambda *a: (setup.api, Credentials(SCOPES)))
    filing.main(["--audit", str(setup.root / "cli.db"), "--config", str(setup.config),
                 "--token", str(setup.root / "token.json"), "file", "1", "--shadow-db", str(setup.shadow)])
    assert json.loads(capsys.readouterr().out)["state"] == "dry_run"
    with pytest.raises(SystemExit):
        filing.main(["--audit", str(setup.root.parent / "unsafe.db"), "file", "1"])


def test_live_mode_is_explicit_and_undo_cannot_target_arbitrary_messages(setup):
    with pytest.raises(filing.FilingError, match="explicit boolean"):
        run(setup, live="true")
    with pytest.raises(filing.FilingError, match="no executor operation"):
        undo(setup, "arbitrary-message-id")
    assert not setup.api.writes


def test_only_allowlisted_gmail_endpoints_are_present():
    tree = ast.parse(Path(filing.__file__).read_text())
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"send", "delete", "trash", "untrash", "batchModify", "batchDelete",
                             "threads", "drafts", "create", "refresh", "run_local_server"}
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_kill_switch_rechecked_after_final_scope_check(setup, monkeypatch, tokeninfo):
    original = filing.verify_authority
    calls = 0

    def verify(credentials):
        nonlocal calls
        original(credentials)
        calls += 1
        if calls == 1:
            change_config(setup, lambda c: c.update(kill_switch=True))
    monkeypatch.setattr(filing, "verify_authority", verify)
    with pytest.raises(filing.FilingError, match="kill switch"):
        run(setup)
    assert not setup.api.writes
    assert setup.db.execute("SELECT state FROM operations").fetchone()[0] == "not_applied"
    assert calls == 1 and len(tokeninfo) == 1


@pytest.mark.parametrize("field,value", [("added", '["SPAM"]'), ("removed", '["UNREAD"]'),
                                        ("prior", 'null'), ("label_id", "TRASH")])
def test_corrupt_audit_cannot_become_generic_mutation_tool(setup, field, value):
    op = run(setup)["operation_id"]
    # Field names are a bounded test parameter, never executor input.
    setup.db.execute(f"UPDATE operations SET {field} = ? WHERE id = ?", (value, op))
    setup.db.commit()
    with pytest.raises(filing.FilingError, match="invalid executor receipt"):
        undo(setup, op)
    assert len(setup.api.writes) == 1


def test_cli_refuses_path_collisions_before_opening_anything(setup, monkeypatch):
    monkeypatch.setattr(filing, "PRIVATE_ROOT", setup.root)
    before = setup.config.read_bytes()
    with pytest.raises(SystemExit):
        filing.main(["--audit", str(setup.config), "--config", str(setup.config),
                     "--token", str(setup.root / "token.json"), "file", "1",
                     "--shadow-db", str(setup.shadow)])
    assert setup.config.read_bytes() == before


def test_cli_live_preflight_failure_is_audited(setup, monkeypatch):
    monkeypatch.setattr(filing, "PRIVATE_ROOT", setup.root)
    change_config(setup, lambda c: c.update(kill_switch=True))
    monkeypatch.setattr(filing, "open_api", lambda *a: pytest.fail("kill switch opened Gmail"))
    cli_audit = setup.root / "cli.db"
    with pytest.raises(SystemExit):
        filing.main(["--audit", str(cli_audit), "--config", str(setup.config),
                     "--token", str(setup.root / "token.json"), "file", "1", "--live",
                     "--shadow-db", str(setup.shadow)])
    with filing.connect(cli_audit) as db:
        assert [r[0] for r in db.execute("SELECT result FROM events")] == ["blocked"]


def test_open_api_uses_existing_token_without_writes_or_scope_override(setup, monkeypatch):
    from google.oauth2.credentials import Credentials as GoogleCredentials
    import googleapiclient.discovery
    token_path = setup.root / "token.json"
    token_path.write_text("synthetic saved token")
    before = token_path.read_bytes()
    creds = Credentials()
    seen = []
    monkeypatch.setattr(GoogleCredentials, "from_authorized_user_file",
                        lambda path: seen.append(path) or creds)

    def build(service, version, **kw):
        assert (service, version) == ("gmail", "v1")
        pinned = kw["credentials"]
        assert pinned.token == creds.token and pinned.refresh_token is None
        assert not kw["cache_discovery"]
        return setup.api
    monkeypatch.setattr(googleapiclient.discovery, "build", build)
    api, authority = filing.open_api(token_path)
    assert api is setup.api and authority is creds and seen == [token_path]
    assert token_path.read_bytes() == before
    creds.valid = False
    with pytest.raises(filing.FilingError, match="no OAuth flow, refresh or token write"):
        filing.open_api(token_path)
    assert token_path.read_bytes() == before


def test_forward_account_must_match_explicit_source_binding(setup):
    setup.api.account = "different@example.test"
    with pytest.raises(filing.FilingError, match="configured shadow-source account"):
        run(setup)
    assert not setup.api.writes


def test_reconcile_provider_error_is_redacted_and_state_retained(setup):
    op = run(setup)["operation_id"]
    filing._state(setup.db, filing._operation(setup.db, op), "uncertain", "file")

    def fail(**kw):
        raise RuntimeError("synthetic private provider detail")
    setup.api.getProfile = fail
    with pytest.raises(filing.FilingError, match="state retained"):
        filing.reconcile(setup.db, op, setup.api)
    assert filing._operation(setup.db, op)["state"] == "uncertain"


def test_another_invocation_cannot_write_during_in_flight_operation(setup):
    original = setup.api.modify

    def overlap(**kw):
        with filing.connect(setup.root / "audit.db") as other:
            with pytest.raises(filing.FilingError, match="already reserved"):
                filing.file(other, setup.shadow, 1, setup.config, live=True,
                            api=setup.api, credentials=setup.credentials)
        return original(**kw)
    setup.api.modify = overlap
    assert run(setup)["state"] == "completed"
    assert len(setup.api.writes) == 1


@pytest.mark.parametrize("response", [{"id": "m1", "threadId": "different", "labelIds": ["INBOX"]},
                                      {"id": "m1", "threadId": "t1"}])
def test_source_mismatch_or_missing_state_blocks_before_write(setup, response):
    setup.api.response = lambda *a: response
    with pytest.raises(filing.FilingError, match="ambiguous Gmail message"):
        run(setup)
    assert not setup.api.writes


def add_decision(s, number, *, category="Marketing", disposition="ARCHIVE", received_ms=1800000000000):
    with shadow.connect(s.shadow) as db:
        db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, 'sender:synthetic', "
                   "'sender@example.test', 'synthetic', 1, ?, ?, 'policy', 'v1', 1, NULL)",
                   (number, f"m{number}", f"t{number}", received_ms, disposition, category))
    s.api.states[f"m{number}"] = {"INBOX", "UNREAD"}


def batch(s, **kw):
    args = dict(live=True, api=s.api, credentials=s.credentials)
    args.update(kw)
    return filing.run(s.db, s.shadow, s.config, **args)


def test_not_applied_can_refile_with_same_receipt_and_history(setup):
    s = setup
    s.api.label_read = lambda: change_config(s, lambda c: c.update(kill_switch=True))
    with pytest.raises(filing.FilingError, match="kill switch"):
        run(s)
    op = s.db.execute("SELECT * FROM operations").fetchone()
    assert op["state"] == "not_applied" and not s.api.writes
    change_config(s, lambda c: c.update(kill_switch=False))
    s.api.label_read = None
    result = run(s)
    assert result == {"operation_id": op["id"], "state": "completed"}
    assert len(s.api.writes) == 1
    history = [r[0] for r in s.db.execute("SELECT result FROM events WHERE operation_id = ? ORDER BY id",
                                        (op["id"],))]
    after_block = history[history.index("not_applied") + 1:]
    assert "filing_in_flight" in after_block and after_block[-1] == "completed"
    assert filing._operation(s.db, op["id"])["created_at"] == op["created_at"]


def test_unrelated_change_inside_write_is_completed(setup):
    setup.api.behavior = "read_during_modify"
    assert run(setup)["state"] == "completed"
    assert len(setup.api.writes) == 1
    assert setup.api.state == {LABEL, "STARRED", "CATEGORY_UPDATES"}


def test_explicit_refile_after_reconciled_unapplied_timeout(setup):
    setup.api.behavior = "timeout_before"
    first = run(setup)
    assert first["state"] == "uncertain"
    assert filing.reconcile(setup.db, first["operation_id"], setup.api)["state"] == "not_applied"
    setup.api.behavior = "success"
    assert run(setup) == {"operation_id": first["operation_id"], "state": "completed"}
    assert len(setup.api.writes) == 2 and len(setup.api.applied) == 1


def test_explicit_reundo_after_reconciled_unapplied_timeout(setup):
    op = run(setup)["operation_id"]
    setup.api.behavior = "timeout_before"
    assert undo(setup, op)["state"] == "undo_uncertain"
    assert filing.reconcile(setup.db, op, setup.api)["state"] == "completed"
    setup.api.behavior = "success"
    assert undo(setup, op)["state"] == "undone"
    # Two applied mailbox writes: filing and successful explicit undo. The
    # intervening socket failure is a third attempt with no applied fake effect.
    assert len(setup.api.applied) == 2 and len(setup.api.writes) == 3
    assert setup.api.state == {"INBOX", "UNREAD", "STARRED"}


@pytest.mark.parametrize("action,observed,state", [
    ("file", {LABEL}, "completed"), ("file", {"INBOX"}, "not_applied"),
    ("file", {LABEL, "INBOX"}, "uncertain"), ("file", {LABEL, "SPAM"}, "uncertain"),
    ("undo", {LABEL}, "completed"), ("undo", {"INBOX"}, "undone"),
    ("undo", {LABEL, "INBOX"}, "undo_uncertain"), ("undo", {"INBOX", "TRASH"}, "undo_uncertain")])
def test_reconciliation_state_table_and_readonly_event(setup, action, observed, state, tokeninfo):
    op = run(setup)["operation_id"]
    filing._state(setup.db, filing._operation(setup.db, op),
                  "uncertain" if action == "file" else "undo_uncertain", action)
    setup.api.state = observed | {"Label_unrelated"}
    count = len(tokeninfo)
    assert filing.reconcile(setup.db, op, setup.api)["state"] == state
    assert len(setup.api.writes) == 1 and len(tokeninfo) == count
    event = setup.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert event["mode"] == "read-only" and event["result"] == state


def test_refile_updates_disposition_and_prior_in_same_operation(setup):
    first = run(setup)
    undo(setup, first["operation_id"])
    with shadow.connect(setup.shadow) as db:
        db.execute("UPDATE decisions SET disposition = 'DIGEST', policy_version = 'v2'")
    setup.api.state.add("Label_unrelated")
    assert run(setup)["operation_id"] == first["operation_id"]
    op = filing._operation(setup.db, first["operation_id"])
    assert op["disposition"] == "DIGEST" and json.loads(op["removed"]) == []
    assert "Label_unrelated" in json.loads(op["prior"])
    assert json.loads(op["source"])["policy_version"] == "v2"


def test_digest_labels_without_archiving_and_undo_only_removes_label(setup):
    with shadow.connect(setup.shadow) as db:
        db.execute("UPDATE decisions SET disposition = 'DIGEST'")
    result = run(setup)
    assert setup.api.writes == [{"addLabelIds": [LABEL], "removeLabelIds": []}]
    assert "INBOX" in setup.api.state
    assert filing._operation(setup.db, result["operation_id"])["disposition"] == "DIGEST"
    assert undo(setup, result["operation_id"])["state"] == "undone"
    assert setup.api.writes[-1] == {"addLabelIds": [], "removeLabelIds": [LABEL]}
    assert setup.api.state == {"INBOX", "UNREAD", "STARRED"}


def test_digest_receipt_with_inbox_removal_is_rejected(setup):
    with shadow.connect(setup.shadow) as db:
        db.execute("UPDATE decisions SET disposition = 'DIGEST'")
    op = run(setup)["operation_id"]
    setup.db.execute("UPDATE operations SET removed = '[\"INBOX\"]' WHERE id = ?", (op,))
    setup.db.commit()
    with pytest.raises(filing.FilingError, match="invalid executor receipt"):
        undo(setup, op)
    assert len(setup.api.writes) == 1


def test_one_authority_check_and_two_config_reads_per_live_invocation(setup, monkeypatch, tokeninfo):
    original = filing.load_config
    reads = []

    def load(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(filing, "load_config", load)
    op = run(setup)["operation_id"]
    assert len(reads) == 2 and len(tokeninfo) == 1
    reads.clear()
    undo(setup, op)
    assert len(reads) == 2 and len(tokeninfo) == 2
    events = setup.db.execute("SELECT * FROM events WHERE result IN ('filing_in_flight', 'undo_in_flight')")
    assert all(json.loads(event["details"])["approval_reference"] == APPROVAL for event in events)


@pytest.mark.parametrize("credentials", [None, Credentials(valid=False)])
def test_dry_run_missing_expired_token_fails_closed(setup, credentials, tokeninfo):
    with pytest.raises(filing.FilingError, match="missing or expired"):
        run(setup, live=False, credentials=credentials)
    assert not setup.api.calls and not setup.api.writes and not tokeninfo


@pytest.mark.parametrize("field,value", [("enabled_since", None), ("enabled_since", "2026-13-01"),
    ("enabled_since", "20260101"), ("enabled_since", "2026-01-01T00:00:00Z"),
    ("enabled_since", True), ("approval_reference", " ")])
def test_enabled_config_requires_canonical_date_and_approval(setup, field, value):
    change_config(setup, lambda c: c["categories"]["Marketing"].update({field: value}))
    with pytest.raises(filing.FilingError, match="configuration"):
        run(setup)
    assert not setup.api.calls


def test_single_live_file_cannot_bypass_enabled_since(setup):
    change_config(setup, lambda c: c["categories"]["Marketing"].update(enabled_since="2030-01-01"))
    with pytest.raises(filing.FilingError, match="predates"):
        run(setup)
    assert not setup.api.calls


def test_batch_selection_and_every_skip_reason(setup, tokeninfo):
    s = setup
    add_decision(s, 2, category="Receipts")
    add_decision(s, 3, received_ms=1)
    add_decision(s, 4)
    add_decision(s, 5)
    add_decision(s, 6, disposition="DIGEST")
    with shadow.connect(s.shadow) as db:
        shadow.correct(db, 4, "INBOX")
    run(s)
    op = filing.file(s.db, s.shadow, 5, s.config, live=True, api=s.api, credentials=s.credentials)
    filing._state(s.db, filing._operation(s.db, op["operation_id"]), "uncertain", "file")
    s.api.calls.clear()
    tokeninfo.clear()
    outcome = batch(s, live=False, credentials=Credentials(SCOPES))
    assert outcome["listed"] == 1 and outcome["filed"] == 0 and outcome["skipped"] == 5
    assert {r["reason"] for r in outcome["skip_reasons"]} == {
        "category_not_enabled", "before_enabled_since", "conflicting_correction", "already_completed",
        "reserved_uncertain"}
    assert outcome["plans"][0]["decision_id"] == 6 and outcome["plans"][0]["removed"] == []
    assert [kind for kind, _ in s.api.calls] == ["profile", "labels", "get"] and not tokeninfo
    assert len(s.api.writes) == 2


def test_batch_latest_correction_wins_and_named_category_is_bounded(setup):
    add_decision(setup, 2, category="Receipts")
    with shadow.connect(setup.shadow) as db:
        shadow.correct(db, 1, "INBOX")
        shadow.correct(db, 1, "ARCHIVE")
    result = batch(setup, category="Marketing", live=False)
    assert result["listed"] == 1 and result["skipped"] == 0
    assert result["plans"][0]["message_id"] == "m1"
    assert not setup.api.writes


def test_batch_enabled_since_is_inclusive_at_utc_midnight(setup):
    since = filing._since(json.loads(setup.config.read_text())["categories"]["Marketing"])
    with shadow.connect(setup.shadow) as db:
        db.execute("UPDATE decisions SET received_ms = ?", (since,))
    add_decision(setup, 2, received_ms=since - 1)
    result = batch(setup, live=False)
    assert result["listed"] == 1 and result["plans"][0]["message_id"] == "m1"
    assert result["skip_reasons"] == [{"decision_id": 2, "reason": "before_enabled_since"}]


def test_batch_one_profile_label_and_token_check_for_multiple_messages(setup, tokeninfo):
    add_decision(setup, 2, disposition="DIGEST")
    result = batch(setup)
    assert result["listed"] == result["filed"] == 2 and result["skipped"] == 0
    assert [kind for kind, _ in setup.api.calls].count("profile") == 1
    assert [kind for kind, _ in setup.api.calls].count("labels") == 1
    assert [kind for kind, _ in setup.api.calls].count("get") == 2
    assert len(tokeninfo) == 1 and len(setup.api.writes) == 2
    assert "INBOX" in setup.api.states["m2"]


def test_batch_stops_on_first_uncertainty(setup, tokeninfo):
    add_decision(setup, 2)
    setup.api.behavior = "timeout_after"
    result = batch(setup)
    assert result["state"] == "uncertain" and result["listed"] == 2 and result["filed"] == 0
    assert result["skip_reasons"] == [{"decision_id": 2, "reason": "stopped_after_uncertain"}]
    assert len(setup.api.writes) == 1 and len(tokeninfo) == 1
    assert "INBOX" in setup.api.states["m2"]


def test_batch_continues_past_per_message_block_and_audits_it(setup):
    add_decision(setup, 2)
    setup.api.state.add("SPAM")
    result = batch(setup)
    assert result["filed"] == 1 and result["skipped"] == 1 and result["state"] == "blocked"
    assert result["results"][0]["decision_id"] == 2 and len(setup.api.writes) == 1
    assert setup.db.execute("SELECT count(*) FROM events WHERE result = 'blocked'").fetchone()[0] == 1


@pytest.mark.parametrize("live", [False, True])
def test_batch_cap_is_100_and_completed_rows_do_not_starve_next_run(setup, live, tokeninfo):
    for number in range(2, 103):
        add_decision(setup, number)
    first = batch(setup, live=live)
    assert first["listed"] == 100 and first["capped"]
    assert [kind for kind, _ in setup.api.calls].count("get") == 100
    assert len(setup.api.writes) == (100 if live else 0)
    assert len(tokeninfo) == (1 if live else 0)
    if live:
        second = batch(setup)
        assert second["listed"] == second["filed"] == 2 and second["skipped"] == 100
        assert len(setup.api.writes) == 102


@pytest.mark.parametrize("state", ["filing_in_flight", "uncertain", "undo_in_flight", "undo_uncertain"])
def test_batch_does_not_bypass_blocking_states(setup, state):
    op = run(setup)["operation_id"]
    filing._state(setup.db, filing._operation(setup.db, op), state, "file")
    result = batch(setup)
    assert result["listed"] == 0 and result["skipped"] == 1
    assert result["skip_reasons"][0]["reason"] == f"reserved_{state}"
    assert len(setup.api.writes) == 1


@pytest.mark.parametrize("state", ["undone", "not_applied"])
def test_batch_refiles_only_refileable_rows(setup, state):
    op = run(setup)["operation_id"]
    undo(setup, op)
    filing._state(setup.db, filing._operation(setup.db, op), state, "file")
    result = batch(setup)
    assert result["listed"] == result["filed"] == 1
    assert result["results"][0]["operation_id"] == op


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("populated", [False, True])
def test_old_audit_schema_is_recreated_only_without_operations(setup, version, populated):
    path = setup.root / f"legacy-{version}-{populated}.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE operations (id TEXT)")
        db.execute(f"PRAGMA user_version = {version}")
        if populated:
            db.execute("INSERT INTO operations VALUES ('synthetic-existing-operation')")
    before = path.read_bytes()
    if populated:
        with pytest.raises(filing.FilingError, match="audit schema is older than this executor"):
            filing.connect(path)
        assert path.read_bytes() == before
    else:
        db = filing.connect(path)
        try:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert "disposition" in {r[1] for r in db.execute("PRAGMA table_info(operations)")}
        finally:
            db.close()


def cli_args(s, command):
    return ["--audit", str(s.root / "cli.db"), "--config", str(s.config),
            "--token", str(s.root / "token.json"), *command]


@pytest.mark.parametrize("command,expected", [("file", 3), ("run", 3)])
def test_cli_uncertainty_is_exit_3_and_connection_is_closed(setup, monkeypatch, command, expected):
    monkeypatch.setattr(filing, "PRIVATE_ROOT", setup.root)
    monkeypatch.setattr(filing, "open_api", lambda *a: (setup.api, setup.credentials))
    setup.api.behavior = "timeout_after"
    connections = []
    original = filing.connect

    def connect(path):
        db = original(path)
        connections.append(db)
        return db
    monkeypatch.setattr(filing, "connect", connect)
    cmd = [command] + (["1"] if command == "file" else []) + ["--live", "--shadow-db", str(setup.shadow)]
    with pytest.raises(SystemExit) as error:
        filing.main(cli_args(setup, cmd))
    assert error.value.code == expected
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_cli_rejects_removed_approval_flag_and_blocks_with_exit_2(setup, monkeypatch):
    monkeypatch.setattr(filing, "PRIVATE_ROOT", setup.root)
    monkeypatch.setattr(filing, "open_api", lambda *a: pytest.fail("blocked request opened credentials"))
    change_config(setup, lambda c: c.update(kill_switch=True))
    for extra in ([], ["--approval", APPROVAL]):
        with pytest.raises(SystemExit) as error:
            filing.main(cli_args(setup, ["file", "1", "--live", "--shadow-db", str(setup.shadow), *extra]))
        assert error.value.code == 2


@pytest.mark.parametrize("demotion", ["disabled", "removed"])
@pytest.mark.parametrize("moment", ["preflight", "before_write"])
def test_undo_survives_demotion_or_removed_category_at_both_gates(setup, demotion, moment):
    op = run(setup)["operation_id"]

    def demote():
        if demotion == "disabled":
            change_config(setup, lambda c: c["categories"]["Marketing"].update(
                enabled=False, enabled_since="2060-01-01"))
        else:
            change_config(setup, lambda c: c.update(categories={}))

    if moment == "preflight":
        demote()
    else:
        original = setup.api.get

        def get(**kw):
            demote()
            return original(**kw)
        setup.api.get = get
    assert undo(setup, op)["state"] == "undone"
    assert len(setup.api.writes) == 2  # forward filing plus exactly one undo
    assert setup.api.state == {"INBOX", "UNREAD", "STARRED"}
    event = setup.db.execute("SELECT * FROM events WHERE action = 'undo' AND result = 'write_authorized' "
                             "ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(event["details"])["approval_reference"] == ("" if demotion == "removed" else APPROVAL)


def flip_kill_switch_during_modify(s):
    original = s.api.modify
    observed = {}

    def modify(**kw):
        request = original(**kw)

        def apply():
            response = request.execute(num_retries=0)
            change_config(s, lambda c: c.update(kill_switch=True))
            observed["gets_at_flip"] = sum(kind == "get" for kind, _ in s.api.calls)
            return response
        return Request(apply)
    s.api.modify = modify
    return observed


def test_batch_stops_after_kill_switch_flips_during_first_write(setup):
    add_decision(setup, 2)
    add_decision(setup, 3)
    observed = flip_kill_switch_during_modify(setup)
    result = batch(setup)
    assert result["state"] == "blocked" and result["listed"] == 3 and result["filed"] == 1
    assert result["skipped"] == 2 and result["skip_reasons"] == [
        {"decision_id": 2, "reason": "stopped_by_kill_switch"},
        {"decision_id": 3, "reason": "stopped_by_kill_switch"}]
    assert len(setup.api.writes) == 1
    assert sum(kind == "get" for kind, _ in setup.api.calls) - observed["gets_at_flip"] <= 1
    assert setup.api.states["m2"] == setup.api.states["m3"] == {"INBOX", "UNREAD"}


def test_cli_batch_kill_switch_is_exit_2(setup, monkeypatch, capsys):
    add_decision(setup, 2)
    add_decision(setup, 3)
    flip_kill_switch_during_modify(setup)
    monkeypatch.setattr(filing, "PRIVATE_ROOT", setup.root)
    monkeypatch.setattr(filing, "open_api", lambda *a: (setup.api, setup.credentials))
    with pytest.raises(SystemExit) as error:
        filing.main(cli_args(setup, ["run", "--live", "--shadow-db", str(setup.shadow)]))
    assert error.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "blocked" and result["filed"] == 1
    assert all(item["reason"] == "stopped_by_kill_switch" for item in result["skip_reasons"])
    assert len(setup.api.writes) == 1
