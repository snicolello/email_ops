"""Deterministic field extraction for named routing rules.

Works on the canonical thread in memory. Only short identifiers, amounts, and
sender classes leave this module; raw bodies are never returned or persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
import re

from .core import Message


ATS_DOMAINS = ("greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co",
               "myworkday.com", "myworkdayjobs.com", "icims.com", "smartrecruiters.com",
               "ashbyhq.com", "jobvite.com", "taleo.net", "successfactors.com",
               "workablemail.com", "bamboohr.com", "recruitee.com")
_NOREPLY = re.compile(r"no-?reply|do-?not-?reply|donotreply")
_AUTOMATED_LOCAL = frozenset({
    "notification", "notifications", "notify", "alert", "alerts", "mailer-daemon",
    "postmaster", "bounce", "bounces", "update", "updates", "news", "newsletter",
    "receipt", "receipts", "billing", "invoice", "invoices", "order", "orders",
    "shipping", "auto-confirm", "digest"})
_AUTO_REPLY_SUBJECT = re.compile(
    r"^\s*(?:automatic reply|auto(?:matic)?[- ]?reply|autoreply|out of (?:the )?office|auto:)", re.I)
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})

_ID = r"[:#]?\s*([A-Z0-9][A-Z0-9_-]{2,30})"
_AMOUNT = re.compile(
    r"(?P<sym>[$€£])\s?(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)"
    r"|\b(?P<b>\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)\s?(?P<code>USD|EUR|GBP|CAD)\b")
_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}
_ORDER = re.compile(r"\border\s*(?:#|no\.?|number|num\.?|id)\s*" + _ID, re.I)
_REFERENCE = re.compile(
    r"\b(?:reference|ref\.?|confirmation|invoice|transaction)\s*(?:#|no\.?|number|num\.?|id|code)\s*" + _ID, re.I)
_JOB = re.compile(r"\b(?:job|requisition|req\.?|posting)\s*(?:#|no\.?|number|id|code)\s*" + _ID, re.I)
_APPLICATION = re.compile(r"\bapplication\s*(?:#|no\.?|number|id)\s*" + _ID, re.I)
_MAX_VALUES = 3


@dataclass(frozen=True)
class Extraction:
    sender_address: str
    sender_domain: str
    sender_class: str  # owner | automated | person
    is_ats: bool
    is_auto_reply: bool
    has_list_headers: bool
    amounts: tuple[str, ...]
    order_numbers: tuple[str, ...]
    reference_numbers: tuple[str, ...]
    job_ids: tuple[str, ...]
    application_ids: tuple[str, ...]

    def persisted_fields(self) -> tuple[tuple[str, str], ...]:
        """Short structured fields suitable for local records."""
        pairs = [("sender_domain", self.sender_domain), ("sender_class", self.sender_class)]
        for name in ("amounts", "order_numbers", "reference_numbers", "job_ids", "application_ids"):
            values = getattr(self, name)
            if values:
                pairs.append((name, ", ".join(values)))
        return tuple((k, v) for k, v in pairs if v)


def address(value: str) -> str:
    return parseaddr(value)[1].lower() or value.strip().lower()


def recipients(message: Message) -> frozenset[str]:
    cc = header(message, "cc")
    return frozenset(addr.lower() for _, addr in getaddresses([message.to, cc]) if addr)


def header(message: Message, name: str) -> str:
    for key, value in message.headers:
        if key.lower() == name:
            return value
    return ""


def is_auto_reply(message: Message) -> bool:
    submitted = header(message, "auto-submitted").lower()
    return bool(submitted.startswith("auto-replied") or header(message, "x-autoreply")
                or _AUTO_REPLY_SUBJECT.search(message.subject))


def is_automated(message: Message, owner: str) -> bool:
    sender = address(message.sender)
    if owner and sender == address(owner):
        return False
    local, _, domain = sender.partition("@")
    submitted = header(message, "auto-submitted").lower()
    return bool(_NOREPLY.search(local) or local in _AUTOMATED_LOCAL
                or _domain_in(domain, ATS_DOMAINS)
                or (submitted and submitted != "no")
                or header(message, "precedence").lower() in _BULK_PRECEDENCE
                or header(message, "list-unsubscribe") or header(message, "list-id"))


def _domain_in(domain: str, domains: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _unique(values) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen[:_MAX_VALUES])


def _ids(pattern: re.Pattern, text: str) -> tuple[str, ...]:
    return _unique(m.group(1).upper() for m in pattern.finditer(text) if re.search(r"\d", m.group(1)))


def _amounts(text: str) -> tuple[str, ...]:
    found = []
    for m in _AMOUNT.finditer(text):
        currency = _SYMBOL[m.group("sym")] if m.group("sym") else m.group("code").upper()
        found.append(f"{currency} {(m.group('a') or m.group('b')).replace(',', '')}")
    return _unique(found)


def extract(message: Message, owner: str) -> Extraction:
    sender = address(message.sender)
    domain = sender.partition("@")[2]
    owner_address = address(owner) if owner else ""
    sender_class = ("owner" if owner_address and sender == owner_address
                    else "automated" if is_automated(message, owner) else "person")
    text = f"{message.subject}\n{message.body}"
    return Extraction(
        sender_address=sender,
        sender_domain=domain,
        sender_class=sender_class,
        is_ats=_domain_in(domain, ATS_DOMAINS),
        is_auto_reply=is_auto_reply(message),
        has_list_headers=bool(header(message, "list-unsubscribe") or header(message, "list-id")),
        amounts=_amounts(text),
        order_numbers=_ids(_ORDER, text),
        reference_numbers=_ids(_REFERENCE, text),
        job_ids=_ids(_JOB, text),
        application_ids=_ids(_APPLICATION, text),
    )
