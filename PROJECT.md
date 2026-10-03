# Email Ops

- Project code: `EMAIL-OPS`
- Trust zone: `Personal`
- Repository: https://github.com/snicolello/email_ops
- Portfolio board: https://github.com/users/snicolello/projects/1
- Coordination issue: https://github.com/snicolello/stephen-project-hub/issues/50
- Implementation baseline: [`STATE.md`](STATE.md)
- Active v0.1 work contract: https://github.com/snicolello/email_ops/issues/1

## Outcome

Convert personal email into low-noise actionable state and reusable structured information with minimal manual review.

## Boundaries

- In scope: Stephen's personal Gmail, bounded read-only sampling, local state, source provenance, and a future normalized-event handoff.
- Issue #19 expands scope to label + archive infrastructure with private audit and bounded undo. M2 remains read-only; M3 is dry-run by default and requires separate Stephen-approved category promotion and verified `gmail.modify` for live execution. The currently saved authority remains exactly `gmail.readonly`; infrastructure implementation/merge does not authorize a scope change or activation.
- Out of scope: company mail, send/delete/trash/spam/read-state changes, historical bulk cleanup, and JD Delivery implementation.
- Stephen approves Gmail mutation, large historical runs, cross-project changes, external automation, and trust-zone promotion.

## Verification

Synthetic tests establish implementation behavior. Real-mail success requires separate evidence from a bounded personal Gmail run; a connected agent mailbox is not proof that the repository's OAuth flow works.

