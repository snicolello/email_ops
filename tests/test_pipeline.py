import base64
from email_ops.core import Message, Thread, connect, decide, normalized_events, reconcile, summary
from email_ops.gmail import normalize, fetch_threads, SCOPES


OWNER = "Stephen <stephen@example.com>"


def msg(id, sender, subject, body):
    return Message(id, sender, OWNER, subject, body, id)


def test_gmail_normalization_keeps_provenance_and_plain_text():
    raw = {"id": "t1", "messages": [{"id": "m1", "internalDate": "1", "payload": {
        "headers": [{"name": "From", "value": "sender@example.com"},
                    {"name": "Subject", "value": "Hello"}],
        "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"body").decode()}}}]}
    thread = normalize(raw)
    assert (thread.provider, thread.id, thread.messages[0].id, thread.messages[0].body) == (
        "gmail", "t1", "m1", "body")


def test_routing_and_event_boundary(tmp_path):
    db = connect(tmp_path / "state.db")
    action = Thread("gmail", "a", (msg("1", "recruiter@example.com", "Interview invitation", "Please schedule an interview"),))
    waiting = Thread("gmail", "w", (msg("2", OWNER, "Documents", "Following up, please send documents"),))
    job = Thread("gmail", "j", (msg("3", "jobs@example.com", "Job alert", "New jobs matching your profile"),))
    ambiguous = Thread("gmail", "u", (msg("4", "x@example.com", "Hello", "Let's discuss"),))
    assert [reconcile(db, x, "stephen@example.com").route for x in (action, waiting, job, ambiguous)] == [
        "STEPHEN_ACTION", "WAITING_ON_OTHER", "OPERATIONAL_EVIDENCE", "NEEDS_JUDGMENT"]
    event = normalized_events(db)[0]
    assert event["type"] == "job_alert" and event["source_thread_id"] == "j"
    assert event["source_message_id"] == "3"
    before = summary(db)
    for item in (action, waiting, job, ambiguous):
        reconcile(db, item, "stephen@example.com")
    assert summary(db) == before


def test_multi_message_resolves_waiting(tmp_path):
    db = connect(tmp_path / "state.db")
    initial = Thread("gmail", "w", (msg("1", OWNER, "Documents", "Following up, please send documents"),))
    reconcile(db, initial, "stephen@example.com")
    ambiguous = Thread("gmail", "w", initial.messages + (msg("2", "other@example.com", "Re: Documents", "I will look into it"),))
    reconcile(db, ambiguous, "stephen@example.com")
    assert summary(db)["records"] == {"waiting:open": 1}
    replied = Thread("gmail", "w", ambiguous.messages + (msg("3", "other@example.com", "Re: Documents", "Attached the documents you requested"),))
    assert reconcile(db, replied, "stephen@example.com").route == "NEEDS_JUDGMENT"
    assert summary(db)["records"] == {"waiting:resolved": 1}
    assert summary(db)["decisions"] == 3


def test_provider_isolation_and_read_only_api():
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    class Endpoint:
        def __init__(self): self.calls = []
        def list(self, **kw):
            self.calls.append(("list", kw))
            return self
        def get(self, **kw):
            self.calls.append(("get", kw))
            return self
        def execute(self):
            return {"threads": [{"id": "t"}]} if self.calls[-1][0] == "list" else {"id": "t", "messages": []}
    endpoint = Endpoint()
    class Api:
        def users(self): return self
        def threads(self): return endpoint
    assert len(fetch_threads(Api(), "in:inbox", 1)) == 1
    assert [x[0] for x in endpoint.calls] == ["list", "get"]


def test_sample_informed_routing():
    promotion = Thread("gmail", "p", (Message("1", "shop@example.com", OWNER, "Offer", "Ends soon", "1", ("CATEGORY_PROMOTIONS",)),))
    linkedin = Thread("gmail", "j", (msg("2", "LinkedIn <jobs-noreply@linkedin.com>", "Role posted on 9/21/26", "View jobs in United States"),))
    receipt = Thread("gmail", "r", (msg("3", "billing@example.com", "Thank you for your payment", "Payment confirmation"),))
    assert decide(promotion, "stephen@example.com").route == "NO_ACTION"
    assert decide(linkedin, "stephen@example.com").event_type == "job_alert"
    assert decide(receipt, "stephen@example.com").event_type == "payment_confirmation"

