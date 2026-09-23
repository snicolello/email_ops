# Email Ops v0.1

Bounded, read-only personal Gmail triage into a local SQLite queue. This is a review prototype, not an unattended mail service.

1. Enable Gmail API in a personal Google Cloud project and create a Desktop OAuth client. Save its JSON as `credentials.json` locally. It is ignored by Git.
2. Install `requirements.txt` in a private Python environment.
3. Run `python -m email_ops.cli --credentials credentials.json --token token.json --db data/email_ops.db --limit 12 --query 'in:inbox newer_than:30d'`.
4. Inspect aggregate counts printed by the CLI and the local SQLite database. Re-run the same bounded query to check reconciliation.

Only `https://www.googleapis.com/auth/gmail.readonly` is requested. Raw message content is processed in memory and is not written to the database. Source thread and message IDs, subject, route, short action/event fields, timestamps, and decision reasons are local and sensitive. Keep the database private. No Gmail write endpoint is called.

The deterministic router is deliberately conservative. It can miss actions and sends uncertain items to `NEEDS_JUDGMENT`; it is not a production triage policy. Due dates are not inferred. The canonical thread and normalized event structures are independent of Gmail; a future JD Delivery consumer can take job events without reparsing Gmail. That integration is not implemented.

Run tests with `python -m pytest tests -q`.

