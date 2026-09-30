"""Named deterministic rulesets for Issue #18.

``rules-v0.1`` is the frozen baseline (``core.decide``) and must stay unchanged
until its held-out metrics are frozen. ``rules-v0.2`` keeps every v0.1 route,
attaching a rule name and reason code, and applies new rules only to the v0.1
``NEEDS_JUDGMENT`` residue. An unmatched thread stays ``NEEDS_JUDGMENT``.

Rules marked ``evidence="synthetic"`` were written against synthetic fixtures
only and must be checked against the private development corpora before the
held-out evaluation. No model is called here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import re
import sqlite3

from . import core
from .core import Decision, Message, Thread, _persist_decision
from .extract import Extraction, address, extract, is_auto_reply, is_automated, recipients


RULES_V0_1 = "rules-v0.1"
RULES_V0_2 = "rules-v0.2"
NEEDS_JUDGMENT = "NEEDS_JUDGMENT"

# Exact v0.1 reason text -> (rule name, reason code). Every v0.1 branch must be
# listed; an unknown reason is a programming error, not a routing fallback.
_V0_1_RULES = {
    "empty thread": ("empty_thread", "empty_thread"),
    "Gmail promotion category without operational marker": ("promotion_category", "promotion_without_operational_marker"),
    "owner requested a response": ("owner_follow_up", "owner_requested_response"),
    "latest message sent by owner": ("owner_last_message", "owner_sent_latest"),
    "explicit request to owner": ("explicit_request", "explicit_request_phrase"),
    "job alert": ("job_alert_phrase", "job_alert"),
    "job listing notification": ("job_listing_notification", "job_listing"),
    "application confirmation": ("application_confirmation_phrase", "application_confirmation"),
    "receipt or order": ("receipt_or_order_phrase", "receipt"),
    "payment confirmation": ("payment_confirmation_phrase", "payment_confirmation"),
    "explicit reference marker": ("explicit_reference_marker", "reference_marker"),
}
_V0_1_RESIDUE = "no safe deterministic route"
# Same owner-request phrases as v0.1 so waiting creation is unchanged.
_OWNER_REQUEST = re.compile(r"\b(following up|checking in|please send|could you send|waiting for)\b")

_SECURITY = re.compile(
    r"\b(new sign-?in|sign-?in attempt|security alert|verification code|one-time (?:pass)?code|"
    r"password reset|reset your password|suspicious activity|2-step verification|two-factor)\b")
_APPLICATION_OUTCOME = re.compile(
    r"\b(unfortunately|not (?:to )?(?:move|moving) forward|other candidates|decided to pursue|"
    r"position has been filled|no longer (?:being )?consider)")
_APPLICATION_RECEIVED = re.compile(
    r"\b(thanks? (?:you )?for applying|we(?:'ve| have) received your application|"
    r"your application (?:to|for) .{1,80}? (?:has been|was) (?:received|submitted)|"
    r"application (?:has been )?(?:received|submitted))\b")
_ORDER_PLACED = re.compile(
    r"\b(thanks? (?:you )?for your order|your order (?:has been )?(?:placed|confirmed)|"
    r"order (?:placed|confirmed|confirmation))\b")
_ORDER_EXCLUDED = re.compile(
    r"\b(shipped|out for delivery|delivered|tracking|cancel(?:l?ed|lation)?|refund|return)\b")
_PAYMENT_RECEIVED = re.compile(
    r"\b(payment (?:was )?(?:received|successful|processed|confirmed)|"
    r"we(?:'ve| have) received your payment|autopay (?:payment )?(?:was )?(?:successful|processed))\b")
_PAYMENT_EXCLUDED = re.compile(r"\b(failed|declined|past due|overdue|unsuccessful|due (?:on|by))\b")
_JOB_RECOMMENDATIONS = re.compile(
    r"\b(recommended jobs|jobs you may be interested in|job recommendations|\d+ new jobs)\b")
_PERSONAL_OR_OPERATIONAL = re.compile(
    r"\b(interview|application|recruiter|job|role|opportunity|position|hiring|"
    r"sent you a message|messaged you|invitation|invited you)\b")


@dataclass(frozen=True)
class Context:
    thread: Thread
    owner: str
    last: Message
    text: str
    fields: Extraction
    request_index: int | None  # latest owner message that created a waiting request
    request_pending: bool       # only automated replies since that request
    resolves_waiting: bool      # a counterparty replied after that request


@dataclass(frozen=True)
class Rule:
    name: str
    reason_code: str
    route: str
    applies: Callable[[Context], bool]
    event_type: str | None = None
    confidence: float = 0.7
    evidence: str = "synthetic"


def _automated_sender(ctx: Context) -> bool:
    return ctx.fields.sender_class == "automated"


# Ordered; first match wins. Guards that return NEEDS_JUDGMENT come first so a
# later positive rule cannot route a thread that needs Stephen's context.
RESIDUE_RULES: tuple[Rule, ...] = (
    Rule("security_notice_guard", "security_notice", NEEDS_JUDGMENT,
         lambda c: bool(_SECURITY.search(c.text)), confidence=0.3),
    Rule("application_outcome_guard", "application_outcome", NEEDS_JUDGMENT,
         lambda c: bool(_APPLICATION_OUTCOME.search(c.text)) and bool(
             re.search(r"\b(application|candidate|position|role)\b", c.text)), confidence=0.3),
    Rule("auto_reply_while_waiting", "auto_reply_request_pending", "WAITING_ON_OTHER",
         lambda c: c.fields.is_auto_reply and c.request_pending),
    Rule("auto_reply", "auto_reply", "NO_ACTION",
         lambda c: c.fields.is_auto_reply and c.request_index is None),
    Rule("ats_application_received", "application_confirmation", "OPERATIONAL_EVIDENCE",
         lambda c: _automated_sender(c) and bool(_APPLICATION_RECEIVED.search(c.text)),
         event_type="application_confirmation"),
    Rule("order_confirmation_with_number", "receipt", "OPERATIONAL_EVIDENCE",
         lambda c: _automated_sender(c) and bool(c.fields.order_numbers)
         and bool(_ORDER_PLACED.search(c.text)) and not _ORDER_EXCLUDED.search(c.text),
         event_type="receipt"),
    Rule("payment_received_with_amount", "payment_confirmation", "OPERATIONAL_EVIDENCE",
         lambda c: _automated_sender(c) and bool(c.fields.amounts)
         and bool(_PAYMENT_RECEIVED.search(c.text)) and not _PAYMENT_EXCLUDED.search(c.text),
         event_type="payment_confirmation"),
    Rule("job_recommendation_digest", "job_alert", "OPERATIONAL_EVIDENCE",
         lambda c: _automated_sender(c) and bool(_JOB_RECOMMENDATIONS.search(c.text)),
         event_type="job_alert"),
    Rule("automated_social_notification", "social_notification", "NO_ACTION",
         lambda c: "CATEGORY_SOCIAL" in c.last.labels and _automated_sender(c)
         and not _PERSONAL_OR_OPERATIONAL.search(c.text)),
)


def _context(thread: Thread, owner: str) -> Context:
    owner_address = address(owner)
    last = thread.messages[-1]
    request_index = None
    for index, message in enumerate(thread.messages):
        if address(message.sender) == owner_address and _OWNER_REQUEST.search(
                f"{message.subject}\n{message.body}".lower()):
            request_index = index
    later = thread.messages[request_index + 1:] if request_index is not None else ()
    pending = bool(later) and all(is_auto_reply(m) for m in later)
    resolves = False
    if later and address(last.sender) != owner_address:
        counterparties = recipients(thread.messages[request_index]) - {owner_address}
        resolves = (not is_auto_reply(last) and not is_automated(last, owner)
                    and (not counterparties or address(last.sender) in counterparties))
    return Context(thread, owner, last, f"{last.subject}\n{last.body}".lower(),
                   extract(last, owner), request_index, pending, resolves)


def decide_v0_2(thread: Thread, owner: str) -> Decision:
    baseline = core.decide(thread, owner)
    if not thread.messages:
        return replace(baseline, rule="empty_thread", reason_code="empty_thread")
    ctx = _context(thread, owner)
    fields = ctx.fields.persisted_fields()
    if baseline.reason != _V0_1_RESIDUE:
        rule, code = _V0_1_RULES[baseline.reason]
        return replace(baseline, rule=rule, reason_code=code, fields=fields,
                       resolves_waiting=ctx.resolves_waiting)
    for rule in RESIDUE_RULES:
        if rule.applies(ctx):
            return Decision(rule.route, f"{rule.name}: {rule.reason_code}", rule.confidence,
                            ctx.last.subject if rule.route != NEEDS_JUDGMENT else None,
                            rule.event_type, rule.name, rule.reason_code, fields,
                            ctx.resolves_waiting)
    code = "reply_to_owner_request" if ctx.resolves_waiting else "no_rule_matched"
    return Decision(NEEDS_JUDGMENT, f"fallback: {code}", 0.3, None, None,
                    "fallback", code, fields, ctx.resolves_waiting)


RULESETS: dict[str, Callable[[Thread, str], Decision]] = {
    RULES_V0_1: core.decide,
    RULES_V0_2: decide_v0_2,
}


def decide_with(ruleset: str, thread: Thread, owner: str) -> Decision:
    if ruleset not in RULESETS:
        raise ValueError(f"unknown ruleset {ruleset!r}; choose from {sorted(RULESETS)}")
    return RULESETS[ruleset](thread, owner)


def reconcile_with(db: sqlite3.Connection, thread: Thread, owner: str,
                   ruleset: str = RULES_V0_1) -> Decision:
    return _persist_decision(db, thread, owner, decide_with(ruleset, thread, owner),
                             model_name=ruleset)
