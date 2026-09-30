"""Headers-only sender analysis: metadata format, clustering, labeling sheet."""

import csv
from collections import Counter

import pytest

from email_ops.sender_analysis import (analyze, cluster_key, correspondents, fetch_metadata,
                                       write_sheet)


OWNER = "stephen@example.com"


def row(id, sender, *, subject="s", labels=("UNREAD",), thread=None, **headers):
    h = {"from": sender, "to": OWNER, "subject": subject}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return {"id": id, "thread_id": thread or id, "labels": list(labels), "headers": h}


class FakeMessages:
    def __init__(self, total):
        self.total, self.calls = total, []

    def list(self, **kw):
        self.calls.append(("list", kw))
        start = int(kw.get("pageToken", 0))
        page = list(range(start, min(start + kw["maxResults"], self.total)))
        response = {"messages": [{"id": f"m{i}"} for i in page]}
        if page and page[-1] + 1 < self.total:
            response["nextPageToken"] = str(page[-1] + 1)
        self._next = response
        return self

    def get(self, **kw):
        self.calls.append(("get", kw))
        self._next = {"id": kw["id"], "threadId": "t", "labelIds": ["UNREAD"],
                      "payload": {"headers": [{"name": "From", "value": "a@x.com"}]}}
        return self

    def execute(self):
        return self._next


class FakeApi:
    def __init__(self, total):
        self.endpoint = FakeMessages(total)

    def users(self):
        return self

    def messages(self):
        return self.endpoint


def test_fetch_is_metadata_only_bounded_and_paginated():
    api = FakeApi(1200)
    rows = fetch_metadata(api, "in:inbox", 700)
    assert len(rows) == 700
    gets = [kw for kind, kw in api.endpoint.calls if kind == "get"]
    assert {kw["format"] for kw in gets} == {"metadata"}
    assert [kw["maxResults"] for kind, kw in api.endpoint.calls if kind == "list"] == [500, 200]
    with pytest.raises(ValueError):
        fetch_metadata(api, "in:inbox", 1001)


def test_cluster_key_prefers_list_id_then_sender():
    assert cluster_key(row("1", "News <news@a.com>", list_id="Weekly <weekly.a.com>")) == "list:weekly.a.com"
    assert cluster_key(row("2", "Jane <Jane@B.com>")) == "sender:jane@b.com"


def test_analysis_groups_ranks_and_hints():
    rows = [row(str(i), "Deals <no-reply@shop.com>", list_unsubscribe="<x>",
                labels=("UNREAD", "CATEGORY_PROMOTIONS")) for i in range(6)]
    rows += [row("f1", "Friend <friend@example.org>", labels=()),
             row("f2", "Friend <friend@example.org>", labels=("UNREAD",))]
    rows += [row("o1", f"Stephen <{OWNER}>")]  # owner's own messages are excluded
    report = analyze(rows, OWNER, correspondents(
        [row("s1", OWNER, to="friend@example.org")], OWNER))
    first, second = report["clusters"]
    assert (first["cluster"], first["messages"], first["hint"]) == (
        "sender:no-reply@shop.com", 6, "archive_candidate")
    assert first["gmail_categories"] == {"CATEGORY_PROMOTIONS": 6}
    assert (second["hint"], second["owner_wrote_to_sender"], second["unread_rate"]) == (
        "correspondent", 1, 0.5)
    assert report["inbound_messages"] == 8
    assert second["cumulative_share"] == 1.0
    assert report["summary"]["messages_by_hint"] == {"archive_candidate": 6, "correspondent": 2}


def test_labeling_sheet_has_blank_decision_columns(tmp_path):
    report = analyze([row("1", "a@x.com", subject="Hello")], OWNER, Counter())
    path = tmp_path / "sheet.csv"
    write_sheet(path, report["clusters"])
    [line] = list(csv.DictReader(path.open()))
    assert (line["cluster"], line["sample_subjects"], line["category"], line["disposition"]) == (
        "sender:a@x.com", "Hello", "", "")
