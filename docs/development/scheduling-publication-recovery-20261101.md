# Schedule publication recovery — November 1, 2026

Baseline: `be3837b`, isolated branch `codex/scheduling-publication-fix-20261101`.

## Verified failure path

`approve_and_publish_api` calls the combined lifecycle service inside an outer
savepoint. Approval, warning/PTO override validation, approval snapshot, publication
snapshot, publication audit and any supersession audit participate in that same
transaction. The route builds `_success_response` (including the serialized board),
then commits, then returns to FastAPI for response encoding and HTTP delivery.
A failure constructing the board before commit rolls back the operation. Losing
response delivery after commit cannot undo publication.

The browser API helper aborts requests after 15 seconds. In strict JavaScript,
assigning to an aborted fetch's DOMException `message` throws exactly:
`Cannot set property message of  which has only a getter`. The former catch block
assigned `error.message` when the transport failed, masking the original abort.
The publication handler then displayed that secondary TypeError inside the dialog.
It never reconciled database state, and its ordinary success path reloaded the page
instead of rendering the returned board.

This path is reproduced with a real DOMException. No production request trace was
available in this investigation; the specific reason individual live requests
exceeded the timeout is not established by this reproduction.

## Correction

- Wrap uncertain transport failures in a new Error with the original cause;
  never mutate an exception's read-only message. Apply the same correction to
  the post-save rendering error handler.
- On confirmed publication, close the dialog, render the server board, update
  publication controls, and show a persistent success confirmation and live
  announcement. No reload is used in this success path.
- On abort, malformed response, server error or stale/repeated response, read
  the exact period's publication state before claiming failure. This read uses
  the same scheduling and row locks as publication, so it waits for an in-flight
  commit/rollback. It preserves feature, permission and store-scope checks and
  returns an uncached result.
- If the state read also fails, report that status cannot be confirmed. A retry
  checks state again while the existing uncertain-write guard blocks another POST.
- Treat the immutable period ID as the retry identity. Both combined and publish-only
  services return an existing publication before validation, approval, snapshot,
  supersession or audit side effects. An archived publication is recognized without
  making it current again. New revisions still use existing validation gates.
- No notification/email dispatch exists in these publication services. The retry
  path returns before lifecycle/audit writes, leaving no repeated publication
  event for downstream consumers.

No schema migration, data repair, schedule rewrite or history rewrite is required.

## Regression coverage

`tests/js/scheduling_api_error.test.cjs` reproduces DOMException and frozen-error
handling. `tests/js/scheduling_publication.test.cjs` exercises dialog closure,
immediate board rendering, confirmation, double submission, warning and PTO
payloads, abort/broken-JSON/500/409 reconciliation, confirmed unpublished state,
unavailable state and definitive validation rejection.

`tests/test_scheduling_completion.py` exercises real PostgreSQL publication,
concurrent and repeated requests, history counts, supersession retry, authorization,
waiting for an in-flight commit, rollback before response construction, and an
ASGI delivery interruption after the real HTTP handler commits. The lost-response
test then reads authoritative state and retries the POST, verifying unchanged
approval, snapshot and audit counts. Existing owner override, PTO, operational
exception, lifecycle, shift-lead and emergency coverage suites cover existing gates.

`tests/js/scheduling_publication_transport.test.cjs` runs the actual API helper and
dialog handler together: POST abort, offline state read, user retry via GET only,
then successful immediate rendering without another POST.

Validation: all 14 scheduling JavaScript test scripts pass, both edited JavaScript
files pass syntax checks, and `git diff --check` passes. The six PostgreSQL suites
ran 117 tests: 116 passed initially; the single generation/history test could not
import the local test environment's missing OR-Tools dependency. After installing
the repository-pinned dependency, that exact test passed (1 passed). The earlier
HTTP-fixture dependency gaps (cryptography and ijson) were likewise resolved before
the final suite run. PostgreSQL uses disposable databases under localhost only.

These are local source changes and isolated regression results; no production
deployment or production data mutation was performed.
