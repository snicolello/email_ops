# Email Ops v0.1

Bounded, read-only personal Gmail triage into a local SQLite queue. This is a review prototype, not an unattended mail service.

1. Enable Gmail API in a personal Google Cloud project and create a Desktop OAuth client. Save its JSON as `credentials.json` locally. It is ignored by Git.
2. Install `requirements.txt` in a private Python environment.
3. Run `python -m email_ops.cli --credentials credentials.json --token token.json --db data/email_ops.db --limit 12 --query 'in:inbox newer_than:30d'`.
4. Inspect aggregate counts printed by the CLI and the local SQLite database. Re-run the same bounded query to check reconciliation.

The v0.1 and M2 adapters request only `https://www.googleapis.com/auth/gmail.readonly`. Raw message content is processed in memory and is not written to the v0.1 database. Source thread and message IDs, subject, route, short action/event fields, timestamps, and decision reasons are local and sensitive. Keep the database private. These adapters call no Gmail write endpoint; M3's separately gated infrastructure is described below.

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

Sender policy (Issue #19 Tiers 1-2, advisory only): `python -m email_ops.sender_policy build --sheet ~/.email_ops/analysis/<labeled sheet>.csv` merges labeled rows into the private table `~/.email_ops/policy/sender-policy.csv`; `python -m email_ops.sender_policy measure --analysis ~/.email_ops/analysis/senders-<stamp>.json` prints tier shares. Columns are `cluster`, `category`, `disposition` (ARCHIVE, DIGEST, INBOX, MIXED), an optional `surface_when` subject regex, and `notes`; see the synthetic [`examples/sender-policy.example.csv`](examples/sender-policy.example.csv). Security, account-access, and money-trouble subjects, `surface_when` matches, and correspondents are surfaced before any filing rule applies. The real table names personal senders and stays out of Git.

Shadow triage (Issue #19 M2, Gmail read-only): `python -m email_ops.shadow run` reads header metadata for inbound mail since the last run (first run: the last day; at most 500 per run), routes it through the private sender policy, and records what would happen in `~/.email_ops/shadow/shadow.db`. `python -m email_ops.shadow digest` prints a Markdown digest of new decisions (surfaced, middle, would-digest, would-archive), `correct <#> --disposition <D> [--category <C>]` records a correction, and `status` shows each category's progress toward the promotion gate (at least 14 days and 50 reviewed filings in shadow, zero wrong in the last 50, then Stephen's approval).

## Issue #19 M3 filing executor (disabled infrastructure)

`python -m email_ops.filing file <shadow-decision-number>` defaults to a **read-only dry-run** of one existing M2 Tier 1 `ARCHIVE` or `DIGEST` decision. It opens the existing valid token without consent/refresh, reads the Gmail profile, label inventory, and the source message's minimal label state, verifies account binding and the existing Email Ops label, and prints the exact added/removed delta. `gmail.readonly` suffices; dry-run never calls tokeninfo or modifies Gmail. It records one private `dry_run` event containing the plan, inserts no operation row, and returns `state`/`plan` without an operation ID. Missing/expired tokens, source mismatch, forbidden mailbox state, or a conflicting latest correction fail closed. M2 never invokes M3 automatically.

The disabled synthetic [`examples/filing-config.example.json`](examples/filing-config.example.json) documents `~/.email_ops/filing/config.json`. The root fields are `schema_version: 1`, `account` (the shadow-source Gmail account), `kill_switch` (boolean), and `categories`. Each category has exactly:

- `enabled`: boolean; false is the safe initial state.
- `approval_reference`: the single citation to Stephen's independently recorded promotion approval; required and nonempty when enabled. There is no `--approval` CLI flag.
- `enabled_since`: canonical ISO date (`YYYY-MM-DD`), required when enabled; null is allowed while disabled. Eligibility begins inclusively at midnight UTC on this date, guarding against historical bulk cleanup.

The label name is derived as `Email Ops/<category>`; it is not a config field. Missing/malformed config, duplicate/unknown fields, non-boolean switches, invalid dates, or ambiguous/missing labels fail closed. All paths must stay under `~/.email_ops`; audit, shadow, config, and token files must be distinct. No live config is installed and no committed example enables a category.

Live execution requires explicit `--live`, Stephen's separate approval cited in the private config, category enablement, the kill switch off, matching account identity, and actual `gmail.modify` authority. Each live `file`/`undo` invocation checks authority once using the exact pinned access token. It reads config before credential/Gmail access and re-reads it at the write boundary; batch execution shares its initial check and rechecks config before each message write. Dry-run may preview disabled categories with the kill switch on. The executor never obtains consent, refreshes credentials, overrides requested scopes, or writes tokens. Saved/requested scopes alone never authorize mutation.

`ARCHIVE` adds the existing user label if absent and removes `INBOX` if present. `DIGEST` adds the same category label with an empty remove list, preserving `INBOX`. Both use a single bounded [`users.messages.modify`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify) request. [Message-level labels](https://developers.google.com/workspace/gmail/api/guides/labels) keep the change on the source message rather than its entire thread. Draft/sent/trash/spam sources are refused. There is no send/delete/trash/spam/read-state or arbitrary message/label endpoint. Label creation is not part of this executor.

`python -m email_ops.filing run [--category <category>] [--live]` processes at most **100 eligible messages** per run, dry-run by default. It selects Tier 1 ARCHIVE/DIGEST decisions in enabled categories, received on/after `enabled_since`, with no conflicting latest correction and no completed/blocking account/message receipt. The optional category narrows selection; it never overrides enablement. Selection skips report and audit every considered skip reason. Completed/blocking rows do not consume the 100-message cap or starve later eligible messages. Output contains `listed` (selected eligible count), `filed` (completed live requests), `skipped`, `skip_reasons`, per-message dry-run `plans` or live `results`, and `capped` when more eligible messages remain. A nonempty run makes one profile call, one label-list call, and one tokeninfo check in live mode (zero tokeninfo in dry-run), sharing resolved account/label IDs with per-message filing. It continues past per-message preflight blocks and stops on the first uncertain result, reporting unattempted remaining messages as skipped. No write is retried automatically; every request uses `num_retries=0`.

The private `~/.email_ops/filing/audit.db` records account, message/thread identity, shadow decision/policy version, category, disposition, label name/ID, prior labels, exact added/removed delta, operation ID, timestamps, and request/result events including the config's approval reference. It stores no body, subject, sender, or token. Full-synchronization SQLite commits an in-flight reservation and prior state before Gmail. `(account, message_id)` is unique. Repeated completed requests return the existing receipt. **REFILEABLE states are `not_applied` and `undone`:** a later explicit file command refreshes prior/delta/category/disposition/source/label identity inside one reservation transaction while retaining the operation ID and attached event history. In-flight and uncertain states block another filing command.

Audit schema `PRAGMA user_version` is 2. An older schema is recreated only when its operations table is empty; a populated older schema fails closed with `audit schema is older than this executor`. No live-operation migration is supplied. Keep one private audit store for all runs; replacing it loses idempotency and reversal evidence. Audit records are trusted local data, not a tamper-proof external ledger.

- `python -m email_ops.filing status <operation-id>` reads local state/timestamps only.
- `python -m email_ops.filing undo <operation-id> [--live]` defaults to a read-only reversal plan. Live undo uses the same authority/category/kill-switch gates, checks the recorded account/label identity and managed-label state, and reverses only the recorded delta. It preserves pre-existing Email Ops labels and unrelated later changes, including manual read state. DIGEST undo cannot remove INBOX. Malformed receipts fail closed. The CLI accepts only an executor operation ID, not arbitrary message IDs or label payloads.
- `python -m email_ops.filing reconcile <operation-id>` uses an existing valid credential for bounded read-only profile/message calls, records mode `read-only`, and performs no tokeninfo or mutation. It resolves in-flight/uncertain operations according to the managed label/INBOX bits:

| Observed managed state | Filing result | Undo result |
|---|---|---|
| Expected filed state | `completed` | `completed` (undo did not apply; explicit undo may retry) |
| Prior unfiled state | `not_applied` (explicit file may retry) | `undone` |
| Neither, or any forbidden label | `uncertain` | `undo_uncertain` |

Reconciliation never issues a retry itself. A zero-delta undo can be recognized as undone when prior and expected states coincide. Live writes compare only the managed Email Ops label/INBOX bits after validating response identity/shape; an unrelated read or CATEGORY label change does not manufacture uncertainty. A final local boundary block produces `not_applied` for filing or retains `completed` for undo. CLI exit codes are 0 for normal completion/plans, 2 for blocked requests, and 3 for uncertain outcomes. Batch selection skips alone are normal; per-message preflight blocks produce code 2. The CLI always closes its audit connection.

**Promotion stays separate:** M2/#19 owns the 14-day/50-email/zero-wrong evidence gate; Stephen owns approval. The private config cites that separate decision; it neither establishes evidence nor authenticates Stephen. M3 never promotes categories, edits sender policy, or infers approval from accuracy. M2 lacks historical account provenance, so Stephen must verify which account owns its private database before future activation; M3 checks the configured binding against Gmail's profile and source message/thread IDs. Building/merging this code authorizes no OAuth change or activation, and the saved authority remains exactly `gmail.readonly`.

Residual mailbox limit: Gmail's preflight and label write are not atomic with another client, and reconciliation reports current state rather than proving the fate of a request whose socket closed server-side. **Run reconcile only after the previous CLI process has exited.** An explicit retry after reconciliation still cannot rule out an indefinitely delayed server-side request or distinguish later identical managed-label changes. All implementation validation uses local fakes/mocks; no live mutation proof is claimed.

Review queue (local only): `python -m email_ops.review --db data/email_ops.db` lists `NEEDS_JUDGMENT` threads with reason code, subject, and Gmail link.

Run tests with `python -m pytest tests -q`.

