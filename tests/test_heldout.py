"""Issue #18 held-out workflow: frozen baseline, single evaluation, fixed gates."""

import json

import pytest

from email_ops import heldout
from email_ops.core import Message, Thread
from email_ops.heldout import SCHEMA, gates, thread_to_json


OWNER = "stephen@example.com"


def case(key, sender, subject, body, label, *, labels=(), event_type=None):
    thread = Thread("gmail", f"t-{key}", (Message("m1", sender, OWNER, subject, body, "1", labels),))
    return {"case_key": key, "thread": thread_to_json(thread), "label": label, "event_type": event_type}


def sample_cases():
    return [
        case("h001", "recruiter@example.com", "Interview invitation", "Please schedule an interview", "STEPHEN_ACTION"),
        case("h002", "jobs@example.com", "Job alert", "New jobs matching your profile", "OPERATIONAL_EVIDENCE"),
        case("h003", "x@example.com", "Hello", "Let's discuss", "HUMAN_JUDGMENT_REQUIRED"),
        case("h004", "no-reply@accounts.example", "Security alert", "New sign-in", "HUMAN_JUDGMENT_REQUIRED"),
        case("h005", "no-reply@greenhouse.io", "Acme", "Thanks for applying to Acme!", "OPERATIONAL_EVIDENCE"),
        case("h006", "orders@shop.example", "Thanks for your order", "Order #AB-12345", "OPERATIONAL_EVIDENCE"),
        case("h007", "no-reply@utility.example", "Payment received", "We have received your payment of $80.00", "OPERATIONAL_EVIDENCE"),
        case("h008", "alerts@jobs.example", "Your job recommendations", "Roles for you", "OPERATIONAL_EVIDENCE"),
        case("h009", "notifications@social.example", "3 new likes", "See who liked your post", "NO_ACTION",
             labels=("CATEGORY_SOCIAL",)),
        case("h010", "friend@example.com", "Out of Office", "Away until Monday", "NO_ACTION"),
    ]


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(heldout, "PRIVATE_ROOT", tmp_path)
    return tmp_path


def write_sample(root, cases):
    path = root / "issue18" / "heldout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": SCHEMA, "owner": OWNER, "query": "q", "cases": cases}))
    return path


def test_freeze_then_single_evaluation_accepts(private, capsys):
    sample = write_sample(private, sample_cases())
    heldout.main(["freeze", "--sample", str(sample)])
    baseline = json.loads(heldout.baseline_path(sample).read_text())
    assert baseline["label_sha256"] == heldout.sha256_file(sample)
    assert baseline["metrics"]["needs_judgment"] == 8
    assert "cases" not in baseline and "false_confident_by_rule" not in baseline["metrics"]
    heldout.main(["evaluate", "--sample", str(sample)])
    printed = capsys.readouterr().out
    assert "h00" not in printed  # aggregates only; per-case outcomes stay in the private file
    result = json.loads(heldout.evaluation_path(sample, "rules-v0.2").read_text())
    assert len(result["cases"]) == 10
    assert result["verdict"] == "ACCEPT", result["failed"]
    assert result["gates"]["4_review_reduction"] == {
        "baseline": 8, "final": 2, "required_drop": 5, "drop": 6, "pass": True}
    with pytest.raises(SystemExit):  # evaluated once
        heldout.main(["evaluate", "--sample", str(sample)])


def test_changed_labels_after_freeze_are_refused(private):
    sample = write_sample(private, sample_cases())
    heldout.main(["freeze", "--sample", str(sample)])
    with pytest.raises(SystemExit):
        heldout.main(["freeze", "--sample", str(sample)])
    corpus = json.loads(sample.read_text())
    corpus["cases"][2]["label"] = "NO_ACTION"
    sample.write_text(json.dumps(corpus))
    with pytest.raises(SystemExit):
        heldout.main(["evaluate", "--sample", str(sample)])


def test_unlabeled_or_public_samples_are_refused(private, tmp_path_factory):
    cases = sample_cases()
    cases[0]["label"] = None
    with pytest.raises(SystemExit):
        heldout.main(["freeze", "--sample", str(write_sample(private, cases))])
    outside = tmp_path_factory.mktemp("public") / "heldout.json"
    outside.write_text("{}")
    with pytest.raises(SystemExit):
        heldout.main(["freeze", "--sample", str(outside)])


def test_dev_score_refuses_the_held_out_sample(private, capsys):
    sample = write_sample(private, sample_cases())
    with pytest.raises(SystemExit):
        heldout.main(["dev-score", "--corpus", str(sample)])
    dev = private / "dev.json"
    dev.write_text(json.dumps({"owner": OWNER, "cases": [
        {**c, "expected_route": c.pop("label")} for c in sample_cases()]}))
    heldout.main(["dev-score", "--corpus", str(dev)])
    report = json.loads(capsys.readouterr().out)
    assert report["rules-v0.1"]["needs_judgment"] == 8
    assert report["rules-v0.2"]["needs_judgment"] == 2
    assert "cases" not in report


def metrics(**overrides):
    base = {"n": 50, "needs_judgment": 30, "false_confident": 1, "missed_actions": 0,
            "human_judgment_total": 4, "human_judgment_kept": 4,
            "correct_by_label": {"NO_ACTION": 5, "STEPHEN_ACTION": 2},
            "idempotency": {"stable": True}}
    return {**base, **overrides}


def test_gates_accept_only_when_all_six_pass():
    assert gates(metrics(), metrics(needs_judgment=24))["verdict"] == "ACCEPT"
    # 20% of 30 is 6, which is stricter than 5.
    assert gates(metrics(), metrics(needs_judgment=25))["failed"] == ["4_review_reduction"]


@pytest.mark.parametrize("final, failed", [
    (metrics(needs_judgment=20, false_confident=2), "1_false_confident"),
    (metrics(needs_judgment=20, missed_actions=1), "2_missed_actions"),
    (metrics(needs_judgment=20, human_judgment_kept=3), "3_personal_context"),
    (metrics(needs_judgment=20, correct_by_label={"NO_ACTION": 4, "STEPHEN_ACTION": 3}), "5_coverage"),
    (metrics(needs_judgment=20, idempotency={"stable": False}), "6_idempotency"),
])
def test_each_gate_can_reject(final, failed):
    result = gates(metrics(), final)
    assert result["verdict"] == "REJECT" and result["failed"] == [failed]


def test_false_confident_rate_is_bounded_for_small_samples():
    small = metrics(n=40, false_confident=1, needs_judgment=20)
    assert gates(metrics(n=40, false_confident=1), small)["failed"] == ["1_false_confident"]
