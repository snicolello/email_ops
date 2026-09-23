"""Trusted boundary for a future model candidate; no model or tool is invoked here.

Email text is source data. Only local code defines eligibility, the result schema,
and persistence. Instruction-pattern signals are advisory; the strict output
contract and separation from tools/credentials are the primary boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import sqlite3
from typing import ClassVar

from .core import Decision, Thread, _persist_decision, decide, reconcile


_REASON_FOR_ROUTE = {
    "NO_ACTION": "irrelevant",
    "REFERENCE": "informational",
    "OPERATIONAL_EVIDENCE": "operational_event",
    "STEPHEN_ACTION": "explicit_request",
    "WAITING_ON_OTHER": "owner_waiting",
    "NEEDS_JUDGMENT": "uncertain",
}
_EVENT_TYPES = frozenset({"job_alert", "application_confirmation", "payment_confirmation", "receipt"})
_RESULT_FIELDS = frozenset({"source_provider", "source_thread_id", "source_message_id",
                            "route", "reason_code", "event_type", "confidence"})
_INSTRUCTION_SIGNALS = {
    "policy_override": re.compile(r"\b(ignore (?:all |any |the )?(?:previous|prior|above) (?:instructions|rules)|change (?:your |the )?routing policy|override (?:the )?(?:policy|rules))\b", re.I),
    "secret_request": re.compile(r"\b(reveal|show|print|expose)\b.{0,40}\b(secrets?|tokens?|credentials?|api keys?|passwords?)\b", re.I),
    "mail_mutation_request": re.compile(r"\b(send|delete|archive|label|mark as read)\b.{0,30}\b(email|message|gmail|inbox)\b", re.I),
    "command_request": re.compile(r"\b(execute|run)\b.{0,30}\b(command|shell|powershell|terminal|cmd)\b", re.I),
    "permission_change": re.compile(r"\b(change|expand|request|upgrade)\b.{0,30}\b(oauth|gmail)\b.{0,30}\b(scopes?|permissions?|access)\b", re.I),
    "result_manipulation": re.compile(r"\b(return|output|set)\b.{0,30}\b(unsupported action|route|tool call)\b", re.I),
}


@dataclass(frozen=True)
class UntrustedMessage:
    source_message_id: str
    sender: str
    subject: str
    body: str


@dataclass(frozen=True)
class SuspiciousSignal:
    code: str
    source_provider: str
    source_thread_id: str
    source_message_id: str
    part: str


@dataclass(frozen=True)
class ModelInputEnvelope:
    """Trusted framing with source material kept in a separate untrusted field."""

    task: ClassVar[str] = "classify_unresolved_email_v1"
    policy_version: ClassVar[str] = "email_ops_model_boundary_v1"
    source_provider: str
    source_thread_id: str
    source_message_id: str
    untrusted_messages: tuple[UntrustedMessage, ...]
    suspicious_signals: tuple[SuspiciousSignal, ...]

    def to_payload(self) -> dict:
        if self.suspicious_signals:
            raise ValueError("suspicious source material is ineligible for model invocation")
        return {
            "trusted_context": {"task": self.task, "policy_version": self.policy_version,
                                "source_provider": self.source_provider,
                                "source_thread_id": self.source_thread_id,
                                "source_message_id": self.source_message_id},
            "untrusted_source": [{"trust_level": "untrusted_email_data",
                                  "source_message_id": message.source_message_id,
                                  "sender": message.sender, "subject": message.subject,
                                  "body": message.body}
                                 for message in self.untrusted_messages],
        }


@dataclass(frozen=True)
class ValidationOutcome:
    accepted: bool
    decision: Decision
    rejection_code: str | None
    suspicious_signals: tuple[SuspiciousSignal, ...] = ()


def _signals(thread: Thread) -> tuple[SuspiciousSignal, ...]:
    found = []
    for message in thread.messages:
        for part, text in (("subject", message.subject), ("body", message.body)):
            for code, pattern in _INSTRUCTION_SIGNALS.items():
                if pattern.search(text):
                    found.append(SuspiciousSignal(code, thread.provider, thread.id,
                                                  message.id, part))
    return tuple(found)


def _has_provenance(thread: Thread) -> bool:
    message_ids = [message.id for message in thread.messages]
    return bool(thread.provider and thread.id and message_ids and
                all(message_ids) and len(set(message_ids)) == len(message_ids))


def prepare_model_input(thread: Thread, owner: str) -> ModelInputEnvelope | None:
    """Only unresolved, sourced threads are eligible for a future model."""
    if not _has_provenance(thread) or decide(thread, owner).route != "NEEDS_JUDGMENT":
        return None
    return ModelInputEnvelope(
        source_provider=thread.provider, source_thread_id=thread.id,
        source_message_id=thread.messages[-1].id,
        untrusted_messages=tuple(UntrustedMessage(m.id, m.sender, m.subject, m.body)
                                 for m in thread.messages),
        suspicious_signals=_signals(thread),
    )


def _reject(code: str, envelope: ModelInputEnvelope | None = None) -> ValidationOutcome:
    return ValidationOutcome(False, Decision("NEEDS_JUDGMENT", code, 0.0), code,
                             envelope.suspicious_signals if envelope else ())


def validate_model_result(envelope: ModelInputEnvelope, result: object) -> ValidationOutcome:
    """Accept only an exact source-bound route candidate, never instructions."""
    if envelope.suspicious_signals:
        return _reject("suspicious_source_instruction", envelope)
    if type(result) is not dict or set(result) != _RESULT_FIELDS:
        return _reject("invalid_result_fields", envelope)
    for name, expected in (("source_provider", envelope.source_provider),
                           ("source_thread_id", envelope.source_thread_id),
                           ("source_message_id", envelope.source_message_id)):
        if type(result[name]) is not str or result[name] != expected:
            return _reject("missing_or_conflicting_provenance", envelope)
    route = result["route"]
    if type(route) is not str or route not in _REASON_FOR_ROUTE:
        return _reject("unsupported_route", envelope)
    if type(result["reason_code"]) is not str or result["reason_code"] != _REASON_FOR_ROUTE[route]:
        return _reject("conflicting_reason", envelope)
    event_type = result["event_type"]
    if route == "OPERATIONAL_EVIDENCE":
        if type(event_type) is not str or event_type not in _EVENT_TYPES:
            return _reject("unsupported_event_type", envelope)
    elif event_type is not None:
        return _reject("unexpected_event_type", envelope)
    confidence = result["confidence"]
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return _reject("invalid_confidence", envelope)
    decision = Decision(route, f"validated_candidate:{result['reason_code']}",
                        float(confidence), event_type=event_type)
    return ValidationOutcome(True, decision, None, envelope.suspicious_signals)


def reconcile_model_result(db: sqlite3.Connection, thread: Thread, owner: str,
                           result: object) -> ValidationOutcome:
    """Trusted local code validates before it can persist a model candidate."""
    if not _has_provenance(thread):
        return _reject("missing_source_provenance")
    envelope = prepare_model_input(thread, owner)
    if envelope is None:
        decision = reconcile(db, thread, owner)
        return ValidationOutcome(False, decision, "not_eligible_or_missing_source")
    outcome = validate_model_result(envelope, result)
    if outcome.accepted:
        _persist_decision(db, thread, owner, outcome.decision,
                          model_provider="validated_candidate", model_name="schema-v1")
    else:
        reconcile(db, thread, owner)
    return outcome
