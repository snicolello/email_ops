"""Segment untrusted email and compare two non-authoritative model views.

Segmentation is an evaluation aid, not a trust boundary. The accepted source
envelope and exact result validator remain the authority checks. This module
never receives a database or Gmail client and cannot persist a model result.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .core import Thread
from .model_boundary import ModelInputEnvelope, prepare_model_input, validate_model_result
from .openrouter_shadow import OpenRouterClient, ProviderReply, ShadowOutcome


# These markers describe structural regions. They are deliberately not a list
# of route strings or attacks; unknown instructions can still reach the model.
_QUOTE_START = re.compile(r"^\s*(?:>|on .+ wrote:|from:\s.*(?:sent:|to:))", re.I)
_SIGNATURE_START = re.compile(r"^\s*(?:--\s*$|best(?: regards)?,?\s*$|regards,?\s*$|sincerely,?\s*$)", re.I)
_FOOTER_START = re.compile(r"\b(?:footer|unsubscribe|manage (?:email )?preferences|copyright)\b", re.I)
_META_LINE = re.compile(
    r"(?:\b(?:triage|routing|classification|model|assistant|evaluator|roleplay|taxonomy)\b"
    r".{0,80}\b(?:lane|route|verdict|badge|category|priority|label|instruction)\b"
    r"|\b(?:preferred lane|back-office note|base64 lane label)\b"
    r"|\[\s*role\s*:|<[^>]+\bdata-(?:review|role|route)\b)", re.I)


@dataclass(frozen=True)
class Segment:
    category: str
    source_message_id: str
    text: str


def segment_body(body: str, source_message_id: str) -> tuple[Segment, ...]:
    """Assign line-level source regions while retaining every nonblank line."""
    regions: list[Segment] = []
    tail: str | None = None
    for line in body.splitlines():
        if not line.strip():
            continue
        if _QUOTE_START.search(line):
            category = "quoted_prior_message"
            tail = category
        elif _SIGNATURE_START.search(line):
            category = "signature"
            tail = category
        elif _FOOTER_START.search(line):
            category = "footer"
            tail = category
        elif _META_LINE.search(line):
            category = "instruction_like_meta"
        elif tail:
            category = tail
        else:
            category = "current_business_content"
        regions.append(Segment(category, source_message_id, line))
    return tuple(regions)


def segmented_payload(envelope: ModelInputEnvelope, *, business_only: bool) -> dict:
    """Preserve trusted provenance and isolate email segments as untrusted data."""
    base = envelope.to_payload()  # rejects accepted-boundary suspicious signals
    base["trusted_context"]["classification_view"] = (
        "current_business_only" if business_only else "full_segmented")
    source = []
    for message in envelope.untrusted_messages:
        segments = segment_body(message.body, message.source_message_id)
        visible = [segment for segment in segments
                   if not business_only or segment.category == "current_business_content"]
        source.append({"trust_level": "untrusted_email_data",
                       "source_message_id": message.source_message_id,
                       "sender": message.sender, "subject": message.subject,
                       "segments": [{"category": segment.category, "text": segment.text}
                                    for segment in visible]})
    base["untrusted_source"] = source
    return base


def _call_view(envelope: ModelInputEnvelope, model_id: str, client: OpenRouterClient,
               *, business_only: bool) -> ShadowOutcome:
    reply: ProviderReply = client.complete(
        model_id, segmented_payload(envelope, business_only=business_only))
    if reply.error_code:
        return ShadowOutcome(model_id, True, False, "NEEDS_JUDGMENT", None, 0.0,
                             reply.error_code, reply.input_tokens, reply.output_tokens,
                             reply.latency_ms, reply.cost_usd)
    validated = validate_model_result(envelope, reply.result)
    return ShadowOutcome(model_id, True, validated.accepted,
                         validated.decision.route, validated.decision.event_type,
                         validated.decision.confidence, validated.rejection_code,
                         reply.input_tokens, reply.output_tokens, reply.latency_ms,
                         reply.cost_usd)


@dataclass(frozen=True)
class DualViewOutcome:
    model_id: str
    full: ShadowOutcome | None
    business: ShadowOutcome | None
    route: str
    event_type: str | None
    accepted: bool
    disagreement: bool
    error_code: str | None

    @property
    def calls(self) -> int:
        return int(self.full is not None) + int(self.business is not None)

    @property
    def cost_usd(self) -> float | None:
        costs = [view.cost_usd for view in (self.full, self.business) if view]
        return sum(costs) if costs and all(cost is not None for cost in costs) else None

    @property
    def latency_ms(self) -> int:
        return sum(view.latency_ms for view in (self.full, self.business) if view)


def evaluate_dual_shadow(thread: Thread, owner: str, model_id: str,
                         client: OpenRouterClient) -> DualViewOutcome:
    """Two validated shadow calls; any conflict or failure safely abstains."""
    envelope = prepare_model_input(thread, owner)
    if envelope is None:
        return DualViewOutcome(model_id, None, None, "NEEDS_JUDGMENT", None,
                               False, False, "not_eligible_or_missing_source")
    if envelope.suspicious_signals:
        return DualViewOutcome(model_id, None, None, "NEEDS_JUDGMENT", None,
                               False, False, "suspicious_source_instruction")
    full = _call_view(envelope, model_id, client, business_only=False)
    business = _call_view(envelope, model_id, client, business_only=True)
    if not full.accepted or not business.accepted:
        return DualViewOutcome(model_id, full, business, "NEEDS_JUDGMENT", None,
                               False, False, full.error_code or business.error_code)
    disagreement = (full.route, full.event_type) != (business.route, business.event_type)
    if disagreement:
        return DualViewOutcome(model_id, full, business, "NEEDS_JUDGMENT", None,
                               False, True, "dual_view_disagreement")
    return DualViewOutcome(model_id, full, business, full.route, full.event_type,
                           True, False, None)

