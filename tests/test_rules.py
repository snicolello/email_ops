"""Issue #18 named rules, extraction, waiting resolution, and review queue."""

import inspect
import re
import sqlite3

import pytest

from email_ops import core
from email_ops.core import Message, Thread, connect, decide, summary
from email_ops.extract import extract
from email_ops.gmail import normalize
from email_ops.review import review_queue
from email_ops.rules import (RESIDUE_RULES, RULES_V0_1, RULES_V0_2, _V0_1_RESIDUE, _V0_1_RULES,
                             decide_v0_2, decide_with, reconcile_with)


OWNER = "stephen@example.com"


def msg(id, sender, subject, body, *, to=OWNER, labels=(), headers=()):
    return Message(id, sender, to, subject, body, id, labels, headers)


def one(sender, subject, body, **kw):
    return Thread("gmail", f"t-{subject}", (msg("m1", sender, subject, body, **kw),))


V0_1_FIXTURES = [
    one("shop@example.com", "Offer", "Ends soon", labels=("CATEGORY_PROMOTIONS",)),
    Thread("gmail", "w", (msg("1", OWNER, "Docs", "Following up, please send documents", to="a@x.com"),)),
    Thread("gmail", "o", (msg("1", OWNER, "Thanks", "Great, thanks", to="a@x.com"),)),
    one("recruiter@example.com", "Interview invitation", "Please schedule an interview"),
    one("jobs@example.com", "Job alert", "New jobs matching your profile"),
    one("LinkedIn <jobs-noreply@linkedin.com>", "Role posted on 9/21/26", "View jobs in United States"),
    one("hr@example.com", "Application received", "We received your application"),
    one("shop@example.com", "Your receipt", "Receipt for your purchase"),
    one("billing@example.com", "Thank you for your payment", "Payment confirmation"),
    one("bank@example.com", "Statement", "For your records only"),
    one("x@example.com", "Hello", "Let's discuss"),
]


def test_every_v0_1_reason_is_mapped_to_a_named_rule():
    reasons = set(re.findall(r'Decision\("\w+", "([^"]+)"', inspect.getsource(core.decide)))
    assert reasons == set(_V0_1_RULES) | {_V0_1_RESIDUE}


@pytest.mark.parametrize("thread", V0_1_FIXTURES)
def test_v0_2_keeps_every_v0_1_route_and_names_it(thread):
    baseline, named = decide(thread, OWNER), decide_v0_2(thread, OWNER)
    if baseline.route != "NEEDS_JUDGMENT":
        assert (named.route, named.event_type) == (baseline.route, baseline.event_type)
    assert named.rule and named.reason_code


def test_v0_1_ruleset_is_the_unchanged_baseline():
    for thread in V0_1_FIXTURES:
        assert decide_with(RULES_V0_1, thread, OWNER) == decide(thread, OWNER)


@pytest.mark.parametrize("thread, route, event, rule", [
    (one("no-reply@accounts.example.com", "Security alert", "New sign-in. Your payment of $5.00 was received"),
     "NEEDS_JUDGMENT", None, "security_notice_guard"),
    (one("no-reply@greenhouse.io", "Update on your application",
         "Thank you for applying. Unfortunately we will not move forward with your application."),
     "NEEDS_JUDGMENT", None, "application_outcome_guard"),
    (one("no-reply@greenhouse.io", "Acme", "Thanks for applying to Acme! Job ID: R12345"),
     "OPERATIONAL_EVIDENCE", "application_confirmation", "ats_application_received"),
    (one("orders@shop.example", "Thanks for your order", "Order #AB-12345 total $42.10"),
     "OPERATIONAL_EVIDENCE", "receipt", "order_confirmation_with_number"),
    (one("orders@shop.example", "Thanks for your order", "Order #AB-12345 has shipped. Tracking inside."),
     "NEEDS_JUDGMENT", None, "fallback"),
    (one("no-reply@utility.example", "Payment received", "We have received your payment of $80.00"),
     "OPERATIONAL_EVIDENCE", "payment_confirmation", "payment_received_with_amount"),
    (one("no-reply@utility.example", "Payment update", "Payment was processed but a charge of $80.00 failed"),
     "NEEDS_JUDGMENT", None, "fallback"),
    (one("alerts@jobs.example", "Your job recommendations", "3 roles we think fit your profile"),
     "OPERATIONAL_EVIDENCE", "job_alert", "job_recommendation_digest"),
    (one("notifications@social.example", "You have 3 new likes", "See who liked your post",
         labels=("CATEGORY_SOCIAL",)), "NO_ACTION", None, "automated_social_notification"),
    (one("notifications@social.example", "Jane sent you a message", "About a role at Acme",
         labels=("CATEGORY_SOCIAL",)), "NEEDS_JUDGMENT", None, "fallback"),
    (one("friend@example.com", "Out of Office", "I am away until Monday"),
     "NO_ACTION", None, "auto_reply"),
    (one("friend@example.com", "Payment received", "We have received your payment of $80.00"),
     "NEEDS_JUDGMENT", None, "fallback"),
])
def test_residue_rules(thread, route, event, rule):
    assert decide(thread, OWNER).route == "NEEDS_JUDGMENT"  # only v0.1 residue is touched
    decision = decide_v0_2(thread, OWNER)
    assert (decision.route, decision.event_type, decision.rule) == (route, event, rule)
    assert decision.reason_code


def test_every_residue_rule_has_a_name_and_reason_code():
    names = [rule.name for rule in RESIDUE_RULES]
    assert len(names) == len(set(names))
    assert all(rule.reason_code for rule in RESIDUE_RULES)


def test_extraction_returns_short_fields_never_bodies():
    body = "Private words. Order number: 998877. Total $1,234.50. Confirmation #ZX9981. Job ID: R-555"
    fields = extract(msg("1", "Receipts <receipts@shop.example>", "Your order", body), OWNER)
    assert fields.sender_class == "automated" and fields.sender_domain == "shop.example"
    assert fields.amounts == ("USD 1234.50",)
    assert fields.order_numbers == ("998877",)
    assert fields.reference_numbers == ("ZX9981",)
    assert fields.job_ids == ("R-555",)
    assert "Private" not in repr(fields.persisted_fields())


def test_extraction_sender_classes_and_list_headers():
    assert extract(msg("1", f"Stephen <{OWNER}>", "s", "b"), OWNER).sender_class == "owner"
    assert extract(msg("1", "Jane <jane@example.com>", "s", "b"), OWNER).sender_class == "person"
    listed = msg("1", "jane@example.com", "s", "b", headers=(("list-unsubscribe", "present"),))
    assert extract(listed, OWNER).sender_class == "automated"


def test_gmail_keeps_only_allowlisted_routing_headers():
    raw = {"id": "t", "messages": [{"id": "m", "internalDate": "1", "payload": {"headers": [
        {"name": "From", "value": "a@x.com"}, {"name": "Cc", "value": "b@x.com"},
        {"name": "List-Unsubscribe", "value": "<https://x.com/u?token=secret>"},
        {"name": "Received", "value": "from relay"}, {"name": "Auto-Submitted", "value": "auto-replied"}]}}]}
    headers = dict(normalize(raw).messages[0].headers)
    assert headers == {"cc": "b@x.com", "auto-submitted": "auto-replied", "list-unsubscribe": "present"}


def waiting_thread(*later):
    request = msg("1", f"Stephen <{OWNER}>", "Documents", "Could you send the signed form?",
                  to="Alex <alex@example.com>")
    return Thread("gmail", "wait", (request,) + later)


def test_counterparty_reply_resolves_waiting_without_magic_words(tmp_path):
    db = connect(tmp_path / "s.db")
    assert reconcile_with(db, waiting_thread(), OWNER, RULES_V0_2).route == "WAITING_ON_OTHER"
    reply = msg("2", "Alex <alex@example.com>", "Re: Documents", "Here you go, let me know if anything else.")
    decision = reconcile_with(db, waiting_thread(reply), OWNER, RULES_V0_2)
    assert (decision.route, decision.reason_code) == ("NEEDS_JUDGMENT", "reply_to_owner_request")
    assert summary(db)["records"] == {"waiting:resolved": 1}
    assert db.execute("SELECT resolved_by_message_id FROM records").fetchone() == ("2",)
    before = summary(db)
    reconcile_with(db, waiting_thread(reply), OWNER, RULES_V0_2)
    assert summary(db) == before


def test_auto_reply_and_third_party_do_not_resolve_waiting(tmp_path):
    db = connect(tmp_path / "s.db")
    reconcile_with(db, waiting_thread(), OWNER, RULES_V0_2)
    ooo = msg("2", "Alex <alex@example.com>", "Automatic reply: Documents", "I am out until Monday",
              headers=(("auto-submitted", "auto-replied"),))
    decision = reconcile_with(db, waiting_thread(ooo), OWNER, RULES_V0_2)
    assert (decision.route, decision.rule) == ("WAITING_ON_OTHER", "auto_reply_while_waiting")
    assert summary(db)["records"] == {"waiting:open": 1}
    stranger = msg("3", "Pat <pat@elsewhere.example>", "Re: Documents", "Not me, sorry")
    reconcile_with(db, waiting_thread(ooo, stranger), OWNER, RULES_V0_2)
    assert summary(db)["records"] == {"waiting:open": 1}


def test_reply_that_asks_for_action_replaces_waiting_with_action(tmp_path):
    db = connect(tmp_path / "s.db")
    reconcile_with(db, waiting_thread(), OWNER, RULES_V0_2)
    reply = msg("2", "alex@example.com", "Re: Documents", "Action required: please complete page 2")
    assert reconcile_with(db, waiting_thread(reply), OWNER, RULES_V0_2).route == "STEPHEN_ACTION"
    assert summary(db)["records"] == {"action:open": 1}


def test_named_decisions_are_persisted_with_rule_and_fields(tmp_path):
    db = connect(tmp_path / "s.db")
    thread = one("no-reply@greenhouse.io", "Acme", "Thanks for applying to Acme! Job ID: R12345")
    reconcile_with(db, thread, OWNER, RULES_V0_2)
    assert db.execute("SELECT rule, reason_code, model_name FROM decisions").fetchone() == (
        "ats_application_received", "application_confirmation", RULES_V0_2)
    fields = db.execute("SELECT fields FROM records").fetchone()[0]
    assert '"job_ids": "R12345"' in fields and "Thanks" not in fields


def test_legacy_database_is_migrated_additively(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(core.SCHEMA)
    old.execute("INSERT INTO threads VALUES ('t','gmail','s','subj','REFERENCE','now','m')")
    old.commit()
    old.close()
    db = connect(path)
    columns = {row[1] for row in db.execute("PRAGMA table_info(decisions)")}
    assert {"rule", "reason_code"} <= columns
    assert db.execute("SELECT count(*) FROM threads").fetchone() == (1,)
    connect(path).close()  # repeat migration is a no-op


def test_review_queue_lists_only_needs_judgment_with_reason_and_link(tmp_path):
    db = connect(tmp_path / "s.db")
    reconcile_with(db, one("x@example.com", "Hello", "Let's discuss"), OWNER, RULES_V0_2)
    reconcile_with(db, one("no-reply@a.example", "Security alert", "New sign-in"), OWNER, RULES_V0_2)
    reconcile_with(db, one("jobs@example.com", "Job alert", "New jobs matching"), OWNER, RULES_V0_2)
    reconcile_with(db, one("y@example.com", "Legacy", "Let's talk"), OWNER, RULES_V0_1)
    queue = review_queue(db)
    assert sorted(item["reason_code"] for item in queue) == [
        "no safe deterministic route", "no_rule_matched", "security_notice"]
    assert all(item["source"].startswith("https://mail.google.com/mail/u/0/#all/t-") for item in queue)
    assert "Job alert" not in {item["subject"] for item in queue}
