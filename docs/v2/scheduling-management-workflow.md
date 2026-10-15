# Scheduling management workflow

Status: code-backed route/template audit at the Scheduling end-to-end readiness checkpoint. This is not a browser verification record.

## October 15, 2026 workflow simplification

The weekly board remains the main management interface. Completed weeks open
read-only. An active Admin chooses **Edit previous week**, confirms once, and
uses the ordinary shift editor. **Save historical corrections** records one
source/correction note and management attestation through the existing lock
service. Correcting an attested week creates a new revision; the original
remains immutable and authoritative until the correction is saved. Inline Lead
attribution and individual historical edits do not require additional dialogs.
Historical drafts remain retained when leaving the editor.

Historical editing requires both a fully completed operating week and an
explicit editing request for a prepared historical draft. Selecting a record
alone does not enter reconstruction. Current/future selection excludes
retrospective records. Previous/next/current links clear revision and editing
parameters; Current week uses the same business date as the scheduling engine.
Exit historical editing returns to the same week's read-only board. Scheduling
subpages provide a common return to the board.

Current/future selectors default to active scheduling employees, retaining
existing assignees. Historical selectors also retain actual attendance evidence
and available employment-cutoff evidence. **Include former employees** is an
explicit Admin option for completed weeks. Search filters employee choices.
Display-name lookup remains unrestricted for retained audit records. The model
has no authoritative employment-start history, so the interface cannot promise
an exact date-effective employment roster or infer hire dates from imports.

**Change assignment** consolidates Shift Swap/replacement and Store Swap into
one styled dialog, using the existing validation, revision and publication
services. Call Out/replacement coverage uses the existing exception panel.
These verified changes update effective inventory authorization directly.

Emergency Store Authorization and employee Emergency Select Store are removed,
including grant-creation and selection endpoints and the permission definition.
Existing grant rows and audit history remain unchanged as evidence; they no
longer grant inventory access. Linked employees use effective published
coverage, with server-side store checks and stale-context protection. Unlinked
legacy Store accounts retain their existing account-scoped behavior. Historical
corrections do not authorize inventory access or send employee notifications.

Confirmed clearing now blocks when it would remove today's linked-employee
inventory authorization. There is no temporary-grant bypass. Pending clearing,
future confirmed clearing without current access, retained evidence and manual
regeneration continue through the existing services.

No schema migration, automatic data rewrite, permission expansion, deployment,
account linking or automation activation is part of this local change. Before
a separately authorized release, inspect active legacy grants and resolve any
working coverage through normal scheduling. Recovery must retain grant removal
and the existing exact-headcount/preopening protections; a schema-compatible
older artifact alone does not establish behavioral compatibility.

| Step | Management entry point | Primary action and next step |
|---|---|---|
| Readiness and generation | `/v2/scheduling/automation` | Resolve blocking setup, review warnings and Week A/B horizon slots, then generate only missing weeks. |
| Store setup | `/v2/scheduling/store-defaults` | Configure Standard Shift, Double Coverage, and authoritative store/day coverage; return to Readiness. |
| Employee roster | `/v2/scheduling/employees` | Sync Square identity, activate Scheduling participation, set Lead and Double Coverage capabilities, then open employee policy. |
| Employee policy | `/v2/scheduling/employees/{id}` | Configure Target Shifts, Week A/B days, lockouts, consecutive-day rules, store restrictions, Longview participation, and review Lead/Weekend/Request-Pattern/Longview diagnostics. Existing schedules are not rewritten. |
| Time off | `/v2/scheduling/time-off` and `/v2/scheduling/time-off/{id}` | Review advisory Fairness and Operational Burden, make the management decision, then open any affected draft/published period directly. |
| Weekly review | `/v2/scheduling/week?period_id={id}` | Review severity-ranked warnings, navigate to the affected shift/date/store, make deliberate locked corrections, and publish only under existing warning rules. |
| Published changes | Weekly board “Create editable revision” or employee transfer workflow | Published assignments remain history. Pre-shift schedule changes use an explicit revision/transfer; attendance coverage records the later fact and does not transfer the shift. |
| Attendance | Current/past published shift → Attendance | Record the configured outcome; an absent event displays “Attendance outcome not recorded” and implies no worked fact. |
| Attendance points | Attendance history → Add point entry → employee policy | Managers use Admin-configured reasons. Admin reaches policy configuration at `/v2/scheduling/store-defaults#attendance-point-policy`. |
| Fairness diagnostics | Employee policy | Weekend Saturday/Sunday, Longview scheduled/attendance-credited burden, Lead designation burden, and Request-Pattern Fairness remain distinct panels. |

## Readiness contract

Blocking means missing Standard Shift, authoritative coverage, eligible employees, plausible Lead availability, required-store eligibility, usable Longview participation, or configured Double Coverage capability prevents correct initial/missing-week generation. The generation service enforces this gate before creating any period.

Warnings include missing Target Shifts or Week A/B patterns, a small Longview pool, absent Attendance Point Policy, and pending time-off requests. They remain visible without converting optional administration or advisory work into a generation failure.

The horizon shows the configured number of Sunday–Saturday slots, each Week A/B identity and materialized status. Generation fills missing slots only. Regeneration remains an explicit single-draft action and preserves locked/manual shifts.

## Dead ends corrected

- Readiness/generation was implemented but absent from Scheduling navigation.
- Coverage requirements were authoritative but had no management create/deactivate UI.
- A partial 7-of-8 horizon had no obvious append action while drafts already existed.
- Attendance Point Policy configuration was embedded and undiscoverable from navigation.
- PTO detail identified affected schedules but did not link to their repair/review surface.
- Empty weekly boards directed managers toward an empty draft rather than the rolling-horizon workflow.
- Published-state and attendance-coverage semantics were present in services but not explained on the board.
- A missing attendance event rendered as silence instead of “outcome not recorded.”
- Manual edit locking was intentional but not explained before save.
- Employee policy edits did not remind management that future periods remain unchanged until explicit review/regeneration.
