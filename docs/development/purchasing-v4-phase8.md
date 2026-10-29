# Purchasing V4 Phase 8 — isolated vendor email

Accepted baseline: 5356c42f434b92312c60a2934b9d3ef4b3097c07. This module is not mounted in production. It requires an explicit loopback disposable PostgreSQL database; no ambient provider credentials are loaded. No live vendor email, DNS, production migration, Square posting, cron, deployment or subsequent phase is enabled.

## Owner and vendor experience

An eligible vendor's saved opt-in settings determine automatic delivery after owner authorization. Standard suppliers receive the immutable authorized PO in their configured format. Spreadsheet suppliers receive the existing Phase 6 protected quotation template. A vendor fills its quantities and prices and replies with the file attached, without an account, portal, upload website or manually entered request number.

Authenticated Resend events are retained before processing. The worker retrieves the original MIME and attachment, checks the saved reply address, vendor sender, provider authentication and immutable request revision, then invokes the same Phase 6 validator used by manual upload. Valid responses remain unselected evidence until owner review. Incoming email never authorizes a PO or changes expected costs. Changed, duplicate, invalid, suspicious, unmatched and outdated replies remain retained and visible.

Needs Attention belongs to the purchasing owner. It shows delivery states, replies, validation outcomes, original downloads and explicit recovery actions. Existing Purchases and Vendor quotes retain document download and manual workbook upload. The owner can record a manual handoff and hold automatic delivery, including while email is paused. This retains the original provider outcome without fabricating a provider delivery acknowledgment. There is no new approval hierarchy or receiving workflow. Phase 7 remains fixed HWY 99, Receive confirmation, reviewed saved allocations and one Save & Submit.

## Data and integration ownership

Seven private migrations through p8_0007 add nine correspondence tables: mail_delivery, mail_state, mail_attempt, mail_event, mail_event_state, mail_message, mail_attachment, mail_import and mail_resolution. Original correspondence facts cannot be updated, deleted or truncated. No automatic deletion or expiration policy exists. Message bodies, MIME and attachments remain private; HTML is not rendered and downloads use safe attachment responses.

Correspondence is bound to the established Phase 5 integration_command, integration_state and integration_attempt records. A PURCHASING_EMAIL command has the same immutable UUID, envelope and digest as its mail_delivery extension. Request revisions have distinct deterministic identities. Existing legacy document commands keep their uniqueness; revision-aware mail has separate constrained uniqueness and a deferred exact binding check. No historical purchasing command is rewritten or promoted to provider delivery.

The existing document worker generates standard PO originals. It does not queue a competing generic sender when a Phase 8 correspondence binding exists. The old sandbox sender rejects bound Resend mail before claiming it. Mail work uses the existing restricted outbox-worker login with narrowly granted correspondence/intake permissions. It cannot read internal authorized or quotation snapshots, modify PO records, or authorize purchases.

A mail claim and canonical outbox lease are durable before a provider call. Provider calls and workbook parsing do not hold receipt or worksheet locks. Canonical outbox acknowledgment means provider acceptance, while mail_state retains the separate delivered, failed, held and uncertain evidence. Signed events arriving before the send response can reconcile the same identity. A bounce never reverses a PO or releases the delivery for blind resend.

## Resend boundary

ResendProvider accepts explicit injected API/download clients, controlled test recipients and qualified CDN hosts. Its narrow request helper reuses transactional_email_service.py without changing existing password-email behavior, settings, credentials or routing. Only the qualified mail read endpoints and POST /emails are allowed; no domain or account administration APIs exist in this adapter.

The preferred From identity is purchasing@orders.eruptedvapor.com. Reply-To contains an opaque delivery token on that same purchasing subdomain. Workbook manifests and persisted revision identities establish correlation; subject and filename alone do not. Recipient, document, routing and opt-in facts are pinned; full outgoing payloads use an explicit public allowlist. Internal costs, margins, retail prices, forecasting, financing, valuation and store allocations never enter the email projection.

Webhook verification uses the exact raw body and Svix headers with HMAC-SHA256, multiple presented signature support and timestamp replay-window validation. Event IDs are unique and changing bytes under an existing ID is rejected. Content is retained before acknowledgment, with processing outcomes retained separately.

Resend's documented idempotency retention is 24 hours. Local command retention is indefinite. An uncertain send is never automatically repeated, including after that provider window. Authenticated matching events or an explicitly identified provider record may reconcile the original command without sending. Provider acceptance is distinct from delivery to the recipient's mail server, and neither proves inbox display.

Download URLs come only from authenticated provider metadata, use explicitly qualified HTTPS CDN hosts, reject private addresses and redirects, and enforce resource bounds. Original MIME is retained even when an individual workbook exceeds the existing parser limit. Oversized or unavailable originals remain explicit exceptions rather than successful imports.

## Isolation, operation and recovery

Run migrate_email(connection) explicitly in one isolated transaction before starting email_app. Startup does not migrate. The standalone CLI has no live-provider credential option. --simulate-email uses a durable SQL Resend fixture, with no HTTP or actual email. Default provider=None leaves manual fallback available. The existing web/cron entrypoint is unchanged and no purchasing cron is introduced.

After owner authorization, request preparation/reissue or document preparation, bounded background work resumes already authorized mail. Startup resumes NOT_SENT commands; it never reclaims SENDING or UNCERTAIN for another send. In-process preparation is serialized independently of the worksheet. A failed child remains visible while other children progress. Failed intake can be retried against its retained event. Confirm an interrupted worker has stopped before recovery; do not invent another operation ID.

Operational rollback disables email using --disable-email and removes the provider adapter. Keep the current compatible schema and source. History, originals, PO documents and manual Phase 6 upload stay available. Do not disable purchasing authorization merely to pause email, because the existing manual review/upload capability uses that separate guard. No schema downgrade, evidence deletion, regenerated original or purchasing reversal is a mail rollback.

For cold recovery, preserve both public and private schemas, roles, quotation signing material, original MIME, uploads, artifacts and outbox identities. Restore to a new disposable database, compare exact record/schema/sequence fingerprints, then reconcile accepted external effects against independently retained provider evidence. A restored SENDING command stays unresolved until that evidence is available. Restoring an application backup cannot unsend mail and never grants permission to resend it.

## Remaining activation boundary

Public DNS inspection found existing Google root-domain MX records and no MX records on orders.eruptedvapor.com. Neither DNS administrative control nor account-specific Resend capability is established by public DNS. The in-app browser was unauthenticated, but the existing Brave session provided read-only account inspection. The existing admin.eruptedvapor.com domain is verified for sending and has receiving disabled; orders.eruptedvapor.com is not configured. With explicit owner authorization, the application Resend adapter sent one clearly labelled original-workbook test from the existing verified admin identity to justin.rawlinson@gmail.com. The owner confirmed receipt. Existing configuration was read with a database-enforced read-only connection; credentials were not exported. The sending-scoped key returned HTTP 401 for sent-message and received-message retrieval, so inbound API permissions remain unqualified.

No live DNS, root MX, employee mailbox, production inbound routing, transactional integration setting or paid resource was changed. Any later activation requires separately approved exact purchasing-subdomain records, provider account/domain capability verification, controlled recipient testing, account-specific raw-download host qualification and webhook credentials. Do not improvise an alternative that reroutes existing business email. Local protocol tests and durable-provider simulations are not live Resend certification.

## Qualified development build

The completion package retains 331 passing V4 tests, 799 passing legacy tests (one optional hosted R2 test skipped), 78 Chromium/Firefox browser checks and 18 model tests. All six raw maximum grid gates pass with 2,000 and 5,000 worksheet rows against 20,000 and 50,000 catalog fixtures. Cold restore compares 199 public and 70 private tables, original documents, sequences, schema objects and grants. Independent provider acceptance after the backup reconciles the original SENDING command without a resend. Migration interruption rolls back transactionally, then forwards to p8_0007. Email-disabled rollback preserves original downloads, manual upload, manual handoff, ordinary edits and sealed-row protection. Native Linux deployment and live inbound Resend delivery are not qualified by these local tests.

Run tests using the existing isolated PostgreSQL test infrastructure and TEST_POSTGRES_ADMIN_URL; no production URL is accepted by the V4 isolation guard. Browser tests use V4_BROWSER_URL and explicit isolated fixture metadata. Do not execute provider-controlled-test.py again: the retained controlled-provider-result.json is the one-send receipt guard.
