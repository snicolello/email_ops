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

`python -m email_ops.filing file <shadow-decision-number>` is an **offline dry-run**: it reads one existing Tier 1 `ARCHIVE` decision from the private M2 database and records a plan in `~/.email_ops/filing/audit.db`. It makes zero Gmail calls and never opens credentials. Conflicting latest corrections block filing. `DIGEST`, surfaced, and middle decisions are refused; label-only DIGEST execution is deferred rather than treating DIGEST as archive. M2 is unchanged and never invokes M3 automatically.

The disabled synthetic [`examples/filing-config.example.json`](examples/filing-config.example.json) documents the private `~/.email_ops/filing/config.json` format. No live config is installed by this implementation. Missing/malformed configuration, duplicate or unknown keys/categories, non-boolean switches, or ambiguous labels fail closed. All CLI paths must stay under `~/.email_ops`, and audit/source/config/token files must be distinct. Keep the same audit database for every run; deleting or replacing it loses reservations and reversal evidence.

Future live execution requires all of these, for both filing and undo:

- An explicit `--live` request with `--approval <reference>` matching the category's recorded `approval_reference`.
- A valid config binding `account` to the shadow-source Gmail account, with `kill_switch: false`, that exact category's `enabled: true`, and its label name exactly `Email Ops/<category>`. The active Gmail profile must match the configured account and the receipt's account on undo/reconciliation.
- An existing valid credential carrying `gmail.modify`, plus a fresh check of Google's access-token scope response before each write. Saved/requested scopes alone are insufficient. The transport uses the exact checked token and cannot auto-refresh into different authority.
- One existing Gmail **user** label with that exact name. The executor never creates, deletes, renames, or configures labels.

**Promotion is separate:** M2/#19 owns the 14-day/50-email/zero-wrong evidence gate; Stephen owns approval. The executor never promotes a category, reads accuracy as approval, or changes sender policy. Configuration and the CLI approval reference are operational attestations to an independently recorded owner decision; they do not establish promotion evidence or authenticate Stephen. No category or credential is promoted by copying the example, building this module, or merging its PR. The saved token remains `gmail.readonly`; this ticket contains no OAuth upgrade procedure. The executor never obtains consent, refreshes credentials, overrides requested scopes, or writes a token; expired credentials fail closed and require separately managed authorization/lifecycle work.

The only forward write is a single [`users.messages.modify`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify): add the intended user label if absent and remove `INBOX` if present. [Message-level labels](https://developers.google.com/workspace/gmail/api/guides/labels) bound the change to the source message; later messages in its thread are untouched. Draft, sent, trash, and spam sources are rejected. `UNREAD`, stars, importance, and other labels are preserved. There is no arbitrary message/label request interface and no send/delete/trash/spam endpoint.

The private SQLite receipt records account, source message/thread, shadow decision and policy version, category, label identity, all prior labels, exact added/removed delta, operation ID, UTC timestamps, requested mode/approval reference, and result events. It stores no body, subject, sender, or token. SQLite commits the reservation/prior state with full synchronization **before** attempting Gmail; `(account, message_id)` is unique. Concurrent invocations using the same database cannot reserve the message twice. Completed repeats return the original receipt without another write. Undone or blocked reservations cannot be silently refiled. Audit records are trusted local data, not a tamper-proof external ledger; retain them privately outside Git.

Commands after an executor receipt exists:

- `python -m email_ops.filing status <operation-id>` reads only local state/timestamps.
- `python -m email_ops.filing undo <operation-id>` previews undo offline. After separate authorization, the live form uses the same `--live --approval <reference>` gates. Undo verifies the account and existing label identity, requires the recorded managed-label state to still match, and reverses only the executor delta. It preserves pre-existing Email Ops labels, unrelated later label changes, and later manual read-state changes. It never accepts an arbitrary message ID or label payload. The global kill switch and category disablement also block undo.
- `python -m email_ops.filing reconcile <operation-id>` makes bounded read-only profile/message calls using an existing valid credential. It never modifies Gmail or retries writes. Reconciliation does not require modify authority and never edits the token.

States are `filing_in_flight` → `completed`, `not_applied` (a known local boundary block), or `uncertain`; undo uses `undo_in_flight` → `undone` or `undo_uncertain`. A crash, timeout, malformed response, or partial/unexpected result leaves a durable reservation. Network write retries are zero. Explicit reconciliation can recognize observed completion or reversal. An unchanged or partially applied ambiguous filing remains uncertain/reserved; an unsuccessful ambiguous undo remains reserved without automatic retry. Partial states and managed-label conflicts need owner judgment; M3 deliberately supplies no automatic repair or arbitrary recovery tool. CLI uncertainty/blocking exits nonzero.

M2's current database has no stored account identity. Before future activation Stephen must independently verify that the private shadow database belongs to the configured account; M3 checks that binding against Gmail's profile and checks both source message and thread IDs. It cannot reconstruct missing historical account provenance.

Gmail has no conditional label write in this API: preflight reads and a write cannot be atomic with Stephen or another Gmail client. Undo cannot establish who made a later identical label change, and read-only reconciliation observes current state rather than proving the fate of an indefinitely delayed request. Use one audit store, inspect uncertainty before recovery, and avoid concurrent manual changes on the managed label/INBOX bits. This is the concrete limit of the bounded executor, not a mailbox transaction system. No live Gmail mutation was used for validation; all mutation tests use local fakes.

Review queue (local only): `python -m email_ops.review --db data/email_ops.db` lists `NEEDS_JUDGMENT` threads with reason code, subject, and Gmail link.

Run tests with `python -m pytest tests -q`.

