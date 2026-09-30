# Email Ops v0.1

Bounded, read-only personal Gmail triage into a local SQLite queue. This is a review prototype, not an unattended mail service.

1. Enable Gmail API in a personal Google Cloud project and create a Desktop OAuth client. Save its JSON as `credentials.json` locally. It is ignored by Git.
2. Install `requirements.txt` in a private Python environment.
3. Run `python -m email_ops.cli --credentials credentials.json --token token.json --db data/email_ops.db --limit 12 --query 'in:inbox newer_than:30d'`.
4. Inspect aggregate counts printed by the CLI and the local SQLite database. Re-run the same bounded query to check reconciliation.

Only `https://www.googleapis.com/auth/gmail.readonly` is requested. Raw message content is processed in memory and is not written to the database. Source thread and message IDs, subject, route, short action/event fields, timestamps, and decision reasons are local and sensitive. Keep the database private. No Gmail write endpoint is called.

The deterministic router is deliberately conservative. It can miss actions and sends uncertain items to `NEEDS_JUDGMENT`; it is not a production triage policy. Due dates are not inferred. The canonical thread and normalized event structures are independent of Gmail; a future JD Delivery consumer can take job events without reparsing Gmail. That integration is not implemented.

## Issue #18 deterministic rules

`rules-v0.1` (`core.decide`) is the frozen baseline and stays the CLI default. `rules-v0.2` (`email_ops/rules.py`) keeps every v0.1 route, names each rule with a reason code, and applies new rules only to the v0.1 `NEEDS_JUDGMENT` residue. Unmatched threads stay `NEEDS_JUDGMENT`. Select it with `--rules rules-v0.2`. Extracted fields (sender class, amounts, order/reference/job/application identifiers) are short values stored on records; raw bodies are not persisted. A reply from the addressee of an owner request resolves the waiting record and records the resolving message; auto-replies and third parties do not.

Held-out workflow (all files must be under `~/.email_ops`; only aggregates are printed):

1. `python -m email_ops.heldout sample --query '<one bounded query>' --limit 50 --out ~/.email_ops/issue18/heldout.json`
2. `python -m email_ops.heldout label --sample ~/.email_ops/issue18/heldout.json` (blind to rule output)
3. `python -m email_ops.heldout freeze --sample ~/.email_ops/issue18/heldout.json` records the label SHA-256 and the `rules-v0.1` baseline once.
4. Develop rules only with `python -m email_ops.heldout dev-score --corpus ~/.email_ops/<earlier corpus>.json`, which refuses the held-out sample.
5. `python -m email_ops.heldout evaluate --sample ~/.email_ops/issue18/heldout.json --rules rules-v0.2` runs once, applies the six prespecified gates, and reports ACCEPT or REJECT.

Sender analysis (headers only, approved up to 1,000 recent messages): `python -m email_ops.sender_analysis --limit 1000` fetches Gmail metadata (never bodies), groups mail by mailing list or sender, and writes a private JSON report and a labeling sheet (`~/.email_ops/analysis/senders-*.csv`). Fill `category` and `disposition` per cluster; labeled clusters become candidate sender rules. Only aggregates are printed.

Review queue (local only): `python -m email_ops.review --db data/email_ops.db` lists `NEEDS_JUDGMENT` threads with reason code, subject, and Gmail link.

Run tests with `python -m pytest tests -q`.

