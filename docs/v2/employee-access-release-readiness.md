# Employee access and Admin integrations

Implementation scope: Admin creates or explicitly associates an individual account, sends password instructions, the employee chooses their password, logs in, and sees only their own published current/next-week shifts. Resend configuration is maintained in Admin, with encrypted secrets. No production, Render, DNS, push, merge, or existing RC changes are part of this work.

## Baseline and release boundary

- Branch: `codex/employee-access-email`.
- Worktree: `/Users/justinrawlinson/Developer/Erupted/worktrees/employee-access`.
- Exact dependency: `adbe5ea` (shared employee-profile RC), not the later Scheduling cleanup RC.
- Production revision/schema remain unverified. This change depends on the profile RC and its contact migration. Deployment reconciliation remains outside this task.
- No Square mutation or request-policy change. No employee identity, schedule, accounting or imported identity backfill.
- Commit IDs and exact release range are reported in the completion message; this document is included in that range.

## Schema

Migration `20260929_0028`, parent `20260927_0027`:

1. Makes `principals.password_hash` nullable; existing hashes are unchanged.
2. Adds `principals.recovery_email_confirmed`, non-null, default false. This records explicit Admin confirmation of the existing login as the recovery address; it is not a second address or identity.
3. Adds `password_reset_tokens`: principal FK, canonical employee FK, login snapshot, unique SHA-256 digest, creation/expiry/consumed/revoked timestamps, principal index.
4. Adds `application_settings`: unique namespaced key, JSON ordinary values, authenticated encrypted secret envelope, update timestamp.
5. Adds `auth_throttles`: SHA-256 bucket key, window start, bounded attempt counter, window-start index. Old buckets are pruned after a day during use.

The schema contract advances to the new revision. Downgrade refuses to invent passwords or discard passwordless accounts. Back up integration/contact configuration before any separately approved rollback; dropping the settings table discards saved integration values. No production migration was run.

## Account and password lifecycle

`Principal.id -> Employee.principal_id -> Employee.id` remains canonical. New accounts have a null hash and confirmed individual email login, with existing explicit role/store and identity confirmation. Contact email only prefills the form; it never becomes recovery authority without confirmation. Legacy usernames are not converted. Existing-account association preserves its password and permissions and sends reset instructions; it never clears an established credential. This resolves the request to preserve existing hashes while allowing new accounts to await setup.

Admin creation/linking commits the account before submitting email. Missing/disabled/broken email configuration leaves a valid account and reports that the account was saved but email was not sent. Admin can fix configuration and resend. Account & Access shows password-established/pending state and **Send Password Reset**. Existing linked email logins require explicit recovery-address confirmation before first use. Non-email legacy logins remain compatible and are not silently renamed.

Null, malformed or oversized login credentials fail safely. Inactive principals and inactive employees cannot authenticate or receive password mail. Existing inactive-account route behavior is preserved (403 where the authenticated dependency rejects it). Scheduling archive/cutoff remains separate and does not revoke authentication.

Tokens use 32 random bytes, store only a SHA-256 digest, expire after one hour and bind to principal, employee and confirmed login. Principal row locks serialize issuance, redemption and login. Replacement revokes earlier outstanding links. Redemption rechecks eligibility/identity and atomically updates the hash, consumes the token, revokes other tokens and revokes all sessions. Passwords must match and contain 12–1024 characters. Opening the link is non-consuming; invalid, expired and used links are rejected on submission.

Email links carry the token in a fragment (`/password#...`), which browsers do not transmit in HTTP requests or Referer. Minimal JavaScript moves it into the POST field and removes the fragment from history. Password pages use no-store/no-referrer; no token or email body is written to audit. JavaScript is required for fragment-based links. No automatic login follows password creation/reset.

Public forgot-password always returns the same message/status and does provider work after responding. Delivery uses an in-process background task, not a durable queue: a process interruption may lose that attempt, and requesting another link is the recovery path. Provider calls have connect/overall timeouts and do not expose provider bodies or exceptions. Admin feedback distinguishes failure from provider acceptance; neither claims inbox delivery.

## Abuse protection

PostgreSQL row-locked fixed windows work across application workers. Public requests are bounded by a global 120/hour budget, a direct-peer 30/hour budget, and normalized-address 3/hour budget (including unknown addresses). Actual account sends are limited to 3/hour. Redemption uses a direct-peer 60/hour budget; Admin test sends use 5/hour per Admin. Public responses remain generic when throttled.

These new controls intentionally ignore forwarded headers. The existing general IP helper trusts `X-Forwarded-For`; no proxy/network redesign was made. Behind a proxy, direct-peer quotas can be shared and conservative. The global cap can temporarily suppress requests during an attack; ordinary login is not locked out. No claim of a deployment-verified client-IP rate limiter is made.

## Central configuration and encrypted secrets

Admin Settings -> Integrations is available at `/admin/settings/integrations`, linked from User Management and V2 Operation Settings. Every GET/POST/test action requires ADMIN plus `management.users`; writes require CSRF. There is no public settings API.

`application_settings_service.py` is the single persistence/validation/decryption boundary. `ResendConfiguration` provides typed values to the transport; no cache is used. Changes apply to the next request without a restart. The `integration.resend` namespace contains enabled status, sender address/display name, optional reply-to, HTTPS portal origin and encrypted API key. Email transport uses the official Resend API endpoint; this is not an arbitrary outbound-proxy facility.

Secret storage uses `cryptography.fernet.Fernet` authenticated encryption, not custom cryptography. `INTEGRATION_ENCRYPTION_KEY` is the only new infrastructure bootstrap secret. It must be a Fernet-generated URL-safe base64 key and must not be stored in the application database or committed. There is no fallback to an insecure default. Missing/wrong keys fail closed for email and key replacement while the rest of the application stays available.

The GET configuration projection has an empty API-key field, with only Configured/Not configured state exposed. The UI never renders a saved secret. Replace API Key requires an explicit checkbox and new value; blank preserves the old ciphertext. Secret-bearing dataclass fields are excluded from repr. Settings audits contain actor, timestamp, integration and changed field names; key updates record only `api_key_replaced`. Test-mail audits record acceptance only. Provider error bodies, email bodies, raw tokens and keys are excluded.

Future integrations add typed loaders/validators/save operations and Admin UI through this service and another namespace; they do not need a second secrets store. No other providers or generalized plugin platform are implemented.

### Bootstrap and recovery

- Generate the bootstrap key with `Fernet.generate_key()` in a secure operator environment, then configure `INTEGRATION_ENCRYPTION_KEY` through the separately authorized infrastructure process.
- Back up this key separately from database backups and restrict access to both. Database backups contain ciphertext, not plaintext integration keys.
- Restore the same bootstrap key with restored database backups. Losing it makes encrypted keys unrecoverable: configure a new bootstrap key and explicitly replace each integration API key in Admin. Ordinary configuration remains readable.
- Do not casually rotate the bootstrap key: existing ciphertext must be re-encrypted under the replacement key, or each API key must be re-entered. There is no rotation endpoint/maintenance mechanism in this release. If compromised, revoke/replace the provider API key as well.

## My Schedule and authorization

`/v2/scheduling/my-schedule` resolves the employee exclusively from the authenticated principal. URL/query employee IDs confer no authority. It filters only PUBLISHED periods and dates from Monday of the current Pacific week through Sunday of next week. Drafts, archived/replaced revisions, other employees and out-of-range weeks are excluded. Cross-store assignments remain visible to their assigned employee.

Display includes week, day/date, store, start/end and opening/closing/Lead/double-coverage labels. Empty weeks are neutral. The employee page performs no transfer, notification or directory query and has no schedule-edit controls. Existing transfer services remain separate and default-off; this release does not grant their permissions.

`scheduling.view_own` defaults on for Staff/STORE, Lead, Manager and Admin, with explicit denies and the existing `staff_scheduling_v2` feature gate preserved. Unlinked accounts get an unavailable response, not directory data. Manager/Admin management permissions are unchanged. Linked Staff/Lead logins land on My Schedule; Manager/Admin landing behavior is preserved.

Legacy management password forms link employee accounts back to the profile. The backend rejects direct password replacement for linked accounts, with row locks to serialize against association. Shared-store editing still rejects linked employee accounts and preserves unrelated legacy credentials. A linked Manager changing their own password must satisfy the employee length policy; that path also invalidates tokens/sessions.

## External configuration still required

No real Resend account/key/domain or network configuration was changed or tested.

1. Separately configure the new bootstrap `INTEGRATION_ENCRYPTION_KEY`; retain existing database/bootstrap configuration. Resend's API key, sender, reply-to and portal origin do **not** need Render environment variables.
2. Verify HTTPS and existing `SESSION_COOKIE_SECURE=true`; expose `staff_scheduling_v2` to intended accounts using existing feature settings. No settings were changed in Render here.
3. Approve the sender/display name and optional reply-to; verify its sending domain/subdomain in Resend. No sender mailbox is assumed.
4. Review Resend's actual proposed DNS records against current Microsoft 365 MX/SPF/DKIM/DMARC. Preserve normal Microsoft 365 mail; do not replace root SPF or receiving MX. No DNS changes were made. Exact DNS values depend on the selected Resend domain/return path and cannot be invented in this release.
5. Keep Resend open/click tracking disabled for password emails. Save the API key and ordinary values through Admin, enable Resend, and use Send Test Email. Complete a real inbox smoke test after separately approved configuration/release.
6. Reconcile the actual production SHA/schema and pending RC before scheduling any migration/deployment. The blocked Render release remains untouched.

## Validation

Command results and complete file list follow below. All database tests create disposable local databases using `TEST_POSTGRES_ADMIN_URL`; no configured application/production database was used. Provider behavior is mocked.

Browser verification used an isolated loopback server with a disposable synthetic database: password form rendering and fragment removal, generic forgot response, legacy Admin login/landing, integration settings with no saved secret, Staff login landing on My Schedule, published shift layout and next-week empty state. Credential creation/redemption, key replacement, provider errors and concurrency were tested through the automated HTTP/database suites, not real employee accounts or real provider delivery.

### Evidence from the local runs

- Expanded focused suites (employee access/profile, migrations/schema, Square cost boundary, digital signage, daily logs and exchange returns): **126 passed, 1 skipped**, 86.71 seconds. The skip is the optional real-R2 integration; no live provider testing was enabled.
- Final employee-access/security suite, including the last self-change and malformed-configuration guards: **30 passed**, 28.76 seconds.
- Fresh requirements environment: Python 3.10.13, FastAPI 0.142.0, Starlette 1.7.0, pytest 9.1.1. Full implementation run: **890 passed, 7 failed, 1 skipped**, 293.11 seconds, plus 7 passing subtests.
- Untouched `git archive adbe5ea` in the same fresh environment: **860 passed, 7 failed, 1 skipped**, 266.44 seconds, plus 7 passing subtests. The seven failure names are identical. The new implementation introduces no additional failing tests in that comparison.
- Rechecking those seven baseline failures with the existing repository runtime (FastAPI 0.129.0, Starlette 0.52.1, pytest 9.0.2) gives **6 passed, 1 failed**, 4.19 seconds. The six route-registry introspection assertions depend on the older framework's route representation; no tests were weakened to hide these failures.

Pre-existing failure names in the fresh environment:

- `test_v2_funding_reports.py::test_draft_delete_ui_and_route_are_owner_csrf_protected`
- `test_v2_funding_reports.py::test_compact_payment_routes_preserve_owner_csrf_guards`
- `test_v2_order_payments.py::test_external_cogs_actions_have_a_separate_default_off_gate`
- `test_v2_order_payments.py::test_all_mutations_have_feature_owner_and_csrf_dependencies`
- `test_v2_ordering_routes.py::test_native_ordering_route_is_get_only_and_has_separate_feature_and_capability_dependencies`
- `test_v2_scheduling_foundation.py::test_automation_page_renders_owner_workflow_and_draft_query_requires_management`
- `test_v2_shell.py::test_v1_ordering_bridge_destinations_remain_direct_get_routes_with_scoped_access`

The Scheduling failure expects the existing automation page to contain “Review Schedule.” It is reproduced on the exact unmodified dependency baseline and in both runtimes. The application requirements historically leave framework versions unpinned; release operators should use the validated runtime and reconcile that separate dependency/test compatibility issue rather than treating a fresh-install test result as an employee-access regression.

Commands used the disposable-database setting `TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://justinrawlinson@localhost/postgres` and `python -m pytest -q`. Full logs remain local at `/tmp/employee-final-full.log`, `/tmp/employee-baseline-suite.log`, `/tmp/employee-baseline-existing-runtime.log`, `/tmp/employee-final-focused.log`, `/tmp/employee-final-security.log`, and `/tmp/employee-compatible-full.log`.

### Full run with the repository's existing framework versions

The implementation was also run in a separate temporary environment matching the repository's existing FastAPI 0.129.0 / Starlette 0.52.1 / pytest 9.0.2 versions, with cryptography 46.0.7: **897 passed, 1 failed, 1 skipped**, 338.35 seconds, plus 7 passing subtests. The sole failure is the reproduced baseline Scheduling “Review Schedule” assertion above. The final dedicated 30-test security run covers the final malformed-URL validation and linked-manager password-change guards.

Readiness: implementation and local verification complete; no newly failing tests identified. The suite is not represented as wholly green. The existing baseline assertion, dependency-version compatibility, actual deployed baseline/schema, bootstrap key, Resend sender/domain and real inbox validation remain explicit release considerations. No production/infrastructure changes or real emails were performed. This remains one dependent employee-access release.

### Complete file-change list

- `.env.example`
- `app/config.py`
- `app/main.py`
- `app/models.py`
- `app/routers/auth.py`
- `app/routers/integrations.py`
- `app/routers/management.py`
- `app/routers/password_access.py`
- `app/routers/v2_employees.py`
- `app/routers/v2_scheduling.py`
- `app/schema_contract.py`
- `app/security/passwords.py`
- `app/security/sessions.py`
- `app/services/access_control_service.py`
- `app/services/application_settings_service.py`
- `app/services/employee_profile_service.py`
- `app/services/password_reset_service.py`
- `app/services/session_service.py`
- `app/services/transactional_email_service.py`
- `app/templates/integrations.html`
- `app/templates/login.html`
- `app/templates/management_users.html`
- `app/templates/password_access.html`
- `app/templates/v2/employees/account.html`
- `app/templates/v2/scheduling/my_schedule.html`
- `app/v2/navigation.py`
- `docs/v2/employee-access-release-readiness.md`
- `migrations/versions/20260929_0028_employee_access.py`
- `requirements.txt`
- `tests/test_employee_access.py`
- `tests/test_schema_migration_postgres.py`
- `tests/test_square_cost_workflow_boundaries.py`
- `tests/test_v2_digital_signage.py`
- `tests/test_v2_employee_profiles.py`
- `tests/test_v2_scheduling_foundation.py`
