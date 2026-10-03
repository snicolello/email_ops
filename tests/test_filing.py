"""M3 adversarial contracts. All message reads/writes and token checks are fake."""

import ast
import json
from pathlib import Path
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
        self.state, self.calls, self.writes = set(labels), [], []
        self.account = "owner@example.test"
        self.label_rows = [{"id": LABEL, "name": "Email Ops/Marketing", "type": "user"}]
        self.behavior = "success"
        self.label_read = None

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

    def response(self):
        return {"id": "m1", "threadId": "t1", "labelIds": sorted(self.state)}

    def get(self, **kw):
        assert kw == {"userId": "me", "id": "m1", "format": "minimal",
                      "fields": "id,threadId,labelIds"}
        self.calls.append(("get", kw))
        return Request(self.response)

    def modify(self, **kw):
        assert kw["userId"] == "me" and kw["id"] == "m1"
        assert set(kw["body"]) == {"addLabelIds", "removeLabelIds"}
        assert set(kw["body"]["addLabelIds"]) <= {LABEL, "INBOX"}
        assert set(kw["body"]["removeLabelIds"]) <= {LABEL, "INBOX"}

        def apply():
            self.writes.append(kw["body"])
            if self.behavior == "timeout_before":
                raise TimeoutError("synthetic provider error must not enter audit")
            self.state |= set(kw["body"]["addLabelIds"])
            if self.behavior != "partial":
                self.state -= set(kw["body"]["removeLabelIds"])
            if self.behavior == "timeout_after":
                raise TimeoutError("synthetic provider error must not enter audit")
            if self.behavior == "malformed":
                return {}
            return self.response()
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
               "(1, 'm1', 't1', 1, 'sender:synthetic', 'sender@example.test', 'synthetic', "
               "1, 'ARCHIVE', 'Marketing', 'policy', 'v1', 1, NULL)")
    db.commit()
    db.close()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema_version": 1, "account": "owner@example.test",
        "kill_switch": False, "categories": {
        "Marketing": {"enabled": True, "label_name": "Email Ops/Marketing",
                      "approval_reference": APPROVAL}}}))
    audit = filing.connect(tmp_path / "audit.db")
    yield SimpleNamespace(db=audit, shadow=shadow_path, config=config,
                          api=FakeGmail(), credentials=Credentials(), root=tmp_path)
    audit.close()


def run(s, **kw):
    args = dict(live=True, approval=APPROVAL, api=s.api, credentials=s.credentials)
    args.update(kw)
    return filing.file(s.db, s.shadow, 1, s.config, **args)


def undo(s, operation, **kw):
    args = dict(live=True, approval=APPROVAL, api=s.api, credentials=s.credentials)
    args.update(kw)
    return filing.undo(s.db, operation, s.config, **args)


def change_config(s, update):
    config = json.loads(s.config.read_text())
    update(config)
    s.config.write_text(json.dumps(config))


def test_default_dry_run_is_offline_and_audited(setup, tokeninfo):
    s = setup
    s.config.unlink()
    result = filing.file(s.db, s.shadow, 1, s.config)
    assert result["state"] == "dry_run"
    assert not s.api.calls and not tokeninfo
    assert s.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    events = s.db.execute("SELECT * FROM events").fetchall()
    assert [e["result"] for e in events] == ["requested", "dry_run"]
    details = json.loads(events[-1]["details"])
    assert details["category"] == "Marketing" and details["message_id"] == "m1"
    assert details["source"]["decision_id"] == 1
    assert all(e["mode"] == "dry-run" and e["at"] and e["operation_id"] for e in events)


@pytest.mark.parametrize("credentials", [Credentials(SCOPES), Credentials(()), Credentials(valid=False), None])
def test_readonly_missing_or_invalid_authority_blocks(setup, credentials, tokeninfo):
    with pytest.raises(filing.FilingError, match="gmail.modify"):
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


@pytest.mark.parametrize("approval", [None, "", "a different approval"])
def test_config_enablement_does_not_supply_owner_approval(setup, approval):
    with pytest.raises(filing.FilingError, match="independent Stephen approval"):
        run(setup, approval=approval)
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
    assert inspected["state"] == ("completed" if behavior in {"timeout_after", "malformed"} else "uncertain")
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
    with pytest.raises(filing.FilingError, match="already reserved"):
        run(s)


def test_undo_rechecks_authority_switches_and_account(setup):
    s = setup
    op = run(s)["operation_id"]
    for kw in ({"credentials": Credentials(SCOPES)}, {"approval": None}):
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


def test_switch_is_rechecked_after_preflight(setup):
    setup.api.label_read = lambda: change_config(setup, lambda c: c.update(kill_switch=True))
    with pytest.raises(filing.FilingError, match="kill switch"):
        run(setup)
    assert not setup.api.writes
    assert setup.db.execute("SELECT state FROM operations").fetchone()[0] == "not_applied"


@pytest.mark.parametrize("disposition,tier", [("DIGEST", 1), ("INBOX", 2), ("ARCHIVE", 3), ("DELETE", 1)])
def test_only_archive_tier1_decisions_are_accepted(setup, disposition, tier):
    with shadow.connect(setup.shadow) as db:
        db.execute("UPDATE decisions SET disposition = ?, tier = ?", (disposition, tier))
    with pytest.raises(filing.FilingError, match="Tier 1 ARCHIVE"):
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
    monkeypatch.setattr(filing, "open_api", lambda *a: pytest.fail("dry-run opened Gmail"))
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


def test_kill_switch_rechecked_after_final_scope_check(setup, monkeypatch):
    original = filing.verify_authority
    calls = 0

    def verify(credentials):
        nonlocal calls
        original(credentials)
        calls += 1
        if calls == 2:
            change_config(setup, lambda c: c.update(kill_switch=True))
    monkeypatch.setattr(filing, "verify_authority", verify)
    with pytest.raises(filing.FilingError, match="boundary check"):
        run(setup)
    assert not setup.api.writes
    assert setup.db.execute("SELECT state FROM operations").fetchone()[0] == "not_applied"


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
                     "--approval", APPROVAL, "--shadow-db", str(setup.shadow)])
    with filing.connect(cli_audit) as db:
        assert [r[0] for r in db.execute("SELECT result FROM events")] == ["requested", "blocked"]


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
    with pytest.raises(filing.FilingError, match="no OAuth flow or token write"):
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
                filing.file(other, setup.shadow, 1, setup.config, live=True, approval=APPROVAL,
                            api=setup.api, credentials=setup.credentials)
        return original(**kw)
    setup.api.modify = overlap
    assert run(setup)["state"] == "completed"
    assert len(setup.api.writes) == 1


@pytest.mark.parametrize("response", [{"id": "m1", "threadId": "different", "labelIds": ["INBOX"]},
                                      {"id": "m1", "threadId": "t1"}])
def test_source_mismatch_or_missing_state_blocks_before_write(setup, response):
    setup.api.response = lambda: response
    with pytest.raises(filing.FilingError, match="ambiguous Gmail message"):
        run(setup)
    assert not setup.api.writes
