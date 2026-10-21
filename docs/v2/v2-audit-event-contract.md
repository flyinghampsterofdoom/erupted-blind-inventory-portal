# V2 audit-event contract

## Product rule

Every employee has an individual authenticated account. Every V2 submission, count, checklist, cash action, customer request, exchange, receiving action, and future operational mutation records the authenticated employee’s principal ID. Store assignment is separate scope data. A typed employee name is not actor identity.

Existing V1 events and `audit_log` are not migrated or redesigned. Shared V1 principals remain historically accurate as the principal actually used; V2 must not guess a person retroactively.

## Target envelope

| Field | Rule |
|---|---|
| Actor principal | Required authenticated individual for V2 operational events |
| Action | Stable past-tense/domain verb |
| Domain | Canonical V2 domain key |
| Entity type / ID | Stable record identity |
| Store scope | Resolved server-authorized store IDs |
| Timestamp | Unambiguous UTC occurrence time |
| Before / after | Include when material and safe; preserve null/zero |
| Reason | Required for destructive/corrective/high-risk actions where defined |
| Command/correlation ID | Link result, logs, retries, and external outcomes |
| External outcome | Redacted technical state and per-target result where relevant |
| Metadata | Minimal safe context; never credentials or raw tokens |

## Compatibility adapter

`app/v2/audit.py` writes a versioned envelope into existing `audit_log.metadata` through `log_audit`; action keys use `V2:<domain>:<action>`. It does not commit the transaction, so the owning workflow decides whether audit and business fact are atomic. It does not swallow audit errors or change an action’s success policy silently.

The adapter recursively redacts keys containing password, secret, token, authorization, cookie, or access-key terms. Domain contracts must additionally exclude sensitive business fields that do not belong in audit metadata.

## Coexistence

- Free-form V1 actions remain untouched and readable.
- V2 consumers check `v2_contract_version` before interpreting metadata.
- No generic audit UI is introduced.
- Corrections append evidence; they do not overwrite an earlier event.
- Retention, sensitive employee-log visibility, and audit-view permissions remain product-owner decisions.
