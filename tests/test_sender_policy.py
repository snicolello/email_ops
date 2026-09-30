"""Tier 1-2 sender policy: loading, routing order, measurement, private paths."""

from pathlib import Path

import pytest

from email_ops.sender_policy import (load_policy, main, measure, merge_sheet, parse_row, route,
                                     write_policy)


OWNER = "stephen@example.com"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sender-policy.example.csv"


def msg(sender, subject="Hello", labels=("UNREAD",), **headers):
    h = {"from": sender, "to": OWNER, "subject": subject}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return {"id": "m", "thread_id": "t", "labels": list(labels), "headers": h}


@pytest.fixture
def policy():
    return load_policy(EXAMPLE)


def test_example_policy_loads(policy):
    assert len(policy) == 6
    assert policy["sender:receipts@pay.example.com"].surface_when.search("A DISPUTE was opened")


@pytest.mark.parametrize("raw, message", [
    ({"cluster": "x@y.com", "category": "Marketing", "disposition": "ARCHIVE"}, "cluster"),
    ({"cluster": "sender:x@y.com", "category": "Spam", "disposition": "ARCHIVE"}, "category"),
    ({"cluster": "sender:x@y.com", "category": "Marketing", "disposition": "DELETE"}, "disposition"),
])
def test_invalid_rows_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_row(raw)


def test_categories_are_extensible():
    row = parse_row({"cluster": "sender:x@y.com", "category": "Travel", "disposition": "digest"},
                    categories=("Travel",))
    assert (row.category, row.disposition) == ("Travel", "DIGEST")


def test_tier1_files_labeled_senders(policy):
    shop = route(msg("Shop <deals@shop.example.com>", "50% off"), policy, set(), OWNER)
    assert (shop.tier, shop.disposition, shop.category) == (1, "ARCHIVE", "Marketing")
    assert route(msg("x@y.com", list_id="<weekly.news.example.org>"), policy, set(), OWNER).tier == 1
    assert route(msg("jobalerts-noreply@jobs.example.net"), policy, set(), OWNER).disposition == "DIGEST"


def test_surface_checks_run_before_filing(policy):
    receipts = "receipts@pay.example.com"
    assert route(msg(receipts, "Your payment to Cafe"), policy, set(), OWNER).tier == 1
    assert route(msg(receipts, "A dispute was opened"), policy, set(), OWNER).reason == "policy_surface_when"
    assert route(msg("deals@shop.example.com", "Suspicious sign-in attempt"), policy, set(),
                 OWNER).reason == "security_or_money"
    assert route(msg("deals@shop.example.com"), policy, {"deals@shop.example.com"},
                 OWNER).reason == "correspondent"


def test_mixed_and_unlabeled_mail(policy):
    bank = route(msg("alerts@bank.example.com", "Your balance"), policy, set(), OWNER)
    assert (bank.tier, bank.disposition) == (3, "MIXED")
    assert route(msg("Jane <jane@friend.example.org>", labels=("CATEGORY_PERSONAL",)), policy, set(),
                 OWNER).reason == "direct_human"
    assert route(msg("jane@friend.example.org", labels=("CATEGORY_UPDATES",)), policy, set(),
                 OWNER).reason == "unlabeled"
    assert route(msg("service@other.example.com"), policy, set(), OWNER).tier == 3


def test_measure_splits_tiers_with_rehinted_fallback(policy):
    clusters = [
        {"cluster": "sender:deals@shop.example.com", "messages": 50},
        {"cluster": "sender:jobalerts-noreply@jobs.example.net", "messages": 20},
        {"cluster": "sender:alerts@bank.example.com", "messages": 10},
        {"cluster": "sender:jane@friend.example.org", "messages": 5, "automated_rate": 0.0,
         "unread_rate": 0.0, "owner_wrote_to_sender": 0},
        {"cluster": "sender:service@other.example.com", "messages": 15, "automated_rate": 0.0,
         "unread_rate": 1.0, "owner_wrote_to_sender": 0},
    ]
    result = measure(clusters, policy)
    assert (result["tier1_archive"], result["tier1_digest"], result["tier1_share"]) == (0.5, 0.2, 0.7)
    assert (result["tier2_surface"], result["tier3_middle"]) == (0.05, 0.25)
    assert (result["labeled_clusters"], result["labeled_message_share"]) == (3, 0.8)


def test_merge_sheet_adds_only_labeled_rows(tmp_path, policy):
    sheet = tmp_path / "sheet.csv"
    sheet.write_text("cluster,category,disposition,surface_when,notes\n"
                     "sender:new@x.example.com,Marketing,ARCHIVE,,\n"
                     "sender:blank@x.example.com,,,,\n"
                     "sender:deals@shop.example.com,Marketing,DIGEST,,changed\n", encoding="utf-8")
    merged = merge_sheet(policy, sheet)
    assert len(merged) == 7 and "sender:blank@x.example.com" not in merged
    assert merged["sender:deals@shop.example.com"].disposition == "DIGEST"
    out = tmp_path / "policy.csv"
    write_policy(out, merged)
    assert load_policy(out) == merged


def test_cli_refuses_paths_outside_private_root(tmp_path):
    with pytest.raises(SystemExit):
        main(["measure", "--analysis", str(tmp_path / "a.json"), "--policy", str(tmp_path / "p.csv")])
