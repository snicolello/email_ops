"""M2 shadow triage: read-only runs, dedupe, digest, corrections, promotion status."""

from pathlib import Path

import pytest

import email_ops.sender_analysis as sender_analysis
from email_ops import shadow


OWNER = "stephen@example.com"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sender-policy.example.csv"
NOW = 1_800_000_000
DAY_MS = 86_400_000


class FakeGmail:
    """Serves list/get for in:sent vs inbound queries and records every call."""

    def __init__(self, inbound, sent=()):
        self.inbound, self.sent, self.calls = list(inbound), list(sent), []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw["q"]))
        pool = self.sent if kw["q"].startswith("in:sent") else self.inbound
        self._next = {"messages": [{"id": m["id"]} for m in pool][:kw["maxResults"]]}
        return self

    def get(self, **kw):
        self.calls.append(("get", kw["format"]))
        m = next(m for m in self.inbound + self.sent if m["id"] == kw["id"])
        self._next = {"id": m["id"], "threadId": m["id"], "labelIds": m.get("labels", ["UNREAD"]),
                      "internalDate": str(m.get("ms", NOW * 1000)),
                      "payload": {"headers": [{"name": k, "value": v} for k, v in m["headers"].items()]}}
        return self

    def execute(self):
        return self._next


def mail(id, sender, subject="Hello", ms=NOW * 1000, labels=("UNREAD",), **headers):
    return {"id": id, "ms": ms, "labels": list(labels),
            "headers": {"From": sender, "To": OWNER, "Subject": subject, **headers}}


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(sender_analysis._QuotaPacer, "wait", lambda self: None)


@pytest.fixture
def db(tmp_path):
    return shadow.connect(tmp_path / "shadow.db")


def test_run_is_metadata_only_routes_and_dedupes(db):
    api = FakeGmail(
        [mail("a", "deals@shop.example.com", "50% off"),
         mail("b", "receipts@pay.example.com", "A dispute was opened"),
         mail("c", "Jane <jane@friend.example.org>", labels=("CATEGORY_PERSONAL",)),
         mail("d", "Pal <pal@friend.example.org>", labels=("CATEGORY_UPDATES",)),
         mail("e", f"Me <{OWNER}>")],
        sent=[{"id": "s1", "headers": {"From": OWNER, "To": "Pal <PAL@friend.example.org>"}}])
    result = shadow.run(db, api, OWNER, EXAMPLE, now=NOW)
    assert (result["listed"], result["recorded"], result["truncated"]) == (5, 4, False)
    assert {fmt for kind, fmt in api.calls if kind == "get"} == {"metadata"}
    assert [q for kind, q in api.calls if kind == "list"] == [
        "in:sent", f"-in:sent -in:chats -in:drafts after:{NOW - 86400}"]
    routed = {r["message_id"]: (r["tier"], r["disposition"], r["reason"])
              for r in db.execute("SELECT * FROM decisions")}
    assert routed == {"a": (1, "ARCHIVE", "policy"), "b": (2, "INBOX", "policy_surface_when"),
                      "c": (2, "INBOX", "direct_human"), "d": (2, "INBOX", "correspondent")}
    again = shadow.run(db, api, OWNER, EXAMPLE, now=NOW + 600)
    assert again["recorded"] == 0
    assert [q for kind, q in api.calls if kind == "list"][-2:] == [
        f"in:sent after:{NOW - 3600}", f"-in:sent -in:chats -in:drafts after:{NOW - 3600}"]


def test_run_reports_truncation(db):
    api = FakeGmail([mail(str(i), "deals@shop.example.com") for i in range(3)])
    assert shadow.run(db, api, OWNER, EXAMPLE, limit=2, now=NOW)["truncated"] is True


def test_digest_groups_escapes_and_marks_shown(db):
    api = FakeGmail([mail("a", "deals@shop.example.com", "Sale | today"),
                     mail("b", "jobalerts-noreply@jobs.example.net", "New jobs"),
                     mail("c", "alerts@bank.example.com", "Your balance"),
                     mail("d", "service@other.example.com", "Update")])
    shadow.run(db, api, OWNER, EXAMPLE, now=NOW)
    text = shadow.digest(db, mark=False)
    assert "Would archive **1**, would digest **1**, surfaced **0**, middle **2**" in text
    assert "Sale / today" in text and "policy_mixed (Financial)" in text
    assert text.index("### Middle") < text.index("### Would DIGEST") < text.index("### Would ARCHIVE")
    assert "Would archive **1**" in shadow.digest(db)
    assert "No new mail" in shadow.digest(db)


def test_corrections_validate_and_flag_wrong_filings(db):
    shadow.run(db, FakeGmail([mail("a", "deals@shop.example.com"),
                              mail("b", "alerts@bank.example.com")]), OWNER, EXAMPLE, now=NOW)
    ids = {r["message_id"]: r["id"] for r in db.execute("SELECT * FROM decisions")}
    assert shadow.correct(db, ids["a"], "inbox")["wrong_filing"] is True
    assert shadow.correct(db, ids["b"], "DIGEST")["wrong_filing"] is False  # middle, not a filing
    with pytest.raises(ValueError, match="disposition"):
        shadow.correct(db, ids["a"], "DELETE")
    with pytest.raises(ValueError, match="category"):
        shadow.correct(db, ids["a"], "ARCHIVE", "Spam")
    with pytest.raises(ValueError, match="no shadow decision"):
        shadow.correct(db, 999, "ARCHIVE")


def test_status_requires_days_volume_and_clean_window(db):
    start = (NOW - 15 * 86400) * 1000
    api = FakeGmail([mail(f"m{i}", "deals@shop.example.com", ms=start + i * DAY_MS // 4)
                     for i in range(50)] +
                    [mail(f"j{i}", "jobalerts-noreply@jobs.example.net", ms=NOW * 1000 - i)
                     for i in range(5)])
    shadow.run(db, api, OWNER, EXAMPLE, limit=100, now=NOW)
    before = {r["category"]: r for r in shadow.status(db, now=NOW)}
    assert before == {}  # nothing reviewed until it appears in a digest
    shadow.digest(db)
    report = {r["category"]: r for r in shadow.status(db, now=NOW)}
    assert report["Marketing"]["eligible_for_approval"] is True
    assert report["Career"]["eligible_for_approval"] is False
    marketing_id = db.execute("SELECT id FROM decisions WHERE message_id = 'm49'").fetchone()["id"]
    shadow.correct(db, marketing_id, "INBOX")
    report = {r["category"]: r for r in shadow.status(db, now=NOW)}
    assert (report["Marketing"]["wrong_in_last_50"], report["Marketing"]["eligible_for_approval"]) == (1, False)
    shadow.correct(db, marketing_id, "ARCHIVE")  # a later correction replaces the earlier one
    assert shadow.status(db, now=NOW)[1]["wrong_in_last_50"] == 0


def test_cli_refuses_database_outside_private_root(tmp_path):
    with pytest.raises(SystemExit):
        shadow.main(["--db", str(tmp_path / "shadow.db"), "status"])
