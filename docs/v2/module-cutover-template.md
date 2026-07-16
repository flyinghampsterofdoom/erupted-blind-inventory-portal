# V2 module cutover and rollback template

Copy this file for every business-module migration. Do not mark a section “not applicable” without an owner and evidence.

The [V1 Preservation Guarantee](./v1-preservation-guarantee.md) applies. The default and current state is **V1 canonical** unless this record contains written owner approval for a later state.

## Identity and ownership

- Module name:
- Product owner:
- Engineering/operations owner:
- Decision date:
- Observation window:
- Current canonical-owner state:
  - [ ] V1 canonical
  - [ ] V1 canonical with V2 shadow/read-only
  - [ ] V1 and V2 running side by side
  - [ ] V2 canonical after explicit approval
  - [ ] V1 retired after explicit approval
- Written owner cutover approval reference:
- Separate written V1 retirement approval reference:
- Final retirement decision/status (explicit; never inferred):

## Surface inventory

- V1 page/action/export routes:
- Proposed V2 page/action/fetch routes:
- Redirect/compatibility routes:
- Active deep links/bookmarks/clients:
- Background, CLI, scheduled, and manual jobs:

## Data and authorization

- Tables/views/files:
- Historical facts versus mutable current state:
- Read owner before/during/after cutover:
- Write owner before/during/after cutover:
- Individual authenticated actor fields/events:
- Legacy shared-principal handling:
- Roles, capabilities, literal checks, and object restrictions:
- Assigned/single/multiple/all store-scope behavior:
- Data migration/backfill (or explicit none):
- Null/zero, timezone, snapshot, and correction semantics:

## Integrations and artifacts

- External reads/writes and authority:
- Idempotency/correlation:
- Partial failure and safe retry:
- PDFs/CSVs/other artifacts and storage:
- Export columns, formats, filenames, filters, timezone, and audit behavior:

## Exposure and coexistence

- Feature key:
- Local/tester/staged exposure configuration:
- In-progress record ownership by version:
- Coexistence rules:
- **Single-writer rule and enforcement:**
- V1 navigation behavior during observation:
- Proof V1 remains directly operational:
- V1 route retirement decision gate:

## Validation

- Characterization/golden fixtures:
- Automated test suite:
- Permission matrix cases:
- Store-scope cases:
- Validation queries/row reconciliation:
- Smoke tests by role/device:
- Export validation:
- Audit actor/envelope validation:
- External failure injection/reconciliation:
- No-secret/artifact checks:

## Redirect plan

- Default: no redirects.
- Eligible GET redirects and parameter preservation:
- Unsafe POST/action compatibility:
- Record-ID mapping:
- Telemetry and duration:
- Approval:

## Rollback

- Trigger thresholds:
- Person authorized to trigger:
- Immediate exposure/navigation steps:
- Stop-writer sequence:
- Route existing drafts/records to owning version:
- Data/external reconciliation:
- Schema rollback policy (destructive downgrade prohibited by default):
- Communications/runbook:
- Evidence rollback was rehearsed:

## Definition of done

- [ ] Current canonical-owner state is recorded; missing state means V1 canonical.
- [ ] V1 discovery, behavior, and data effects are complete.
- [ ] Parity requirements are documented.
- [ ] V2 implementation is complete.
- [ ] Automated tests pass.
- [ ] Browser verification is complete.
- [ ] Production-readiness review is complete.
- [ ] Data-write ownership is explicitly decided.
- [ ] Migration plan is approved.
- [ ] V2 parity accepted for permissions, scope, actor attribution, data, exports, and audit.
- [ ] Single-writer ownership enforced and rollback rehearsed.
- [ ] Observation period completed with reconciled results.
- [ ] Product owner explicitly approves V2 canonical cutover in writing.
- [ ] V1 remains operational after cutover.
- [ ] Product owner separately approves each V1 redirect/consolidation/retirement in writing.
- [ ] Documentation, support, and operational runbooks are current.
