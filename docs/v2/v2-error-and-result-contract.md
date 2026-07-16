# V2 error and result contract

## Boundary

`app/v2/results.py` defines reusable result types for future server-rendered actions and JSON-like endpoints. No V1 handler uses them in Milestone 3. Existing session, CSRF, and exception behavior remains unchanged.

## Result envelope

Every handled action result supplies:

- `kind`: success, validation error, authorization failure, conflict, external failure, partial external success, or unexpected server failure;
- safe user-facing `message`;
- `save_outcome`: nothing saved, local saved, local and external saved, or partial external success;
- opaque UUID correlation ID;
- optional field errors and safe response data;
- `safe_retry` and `manual_resolution_required` booleans.

## HTTP and presentation behavior

| Condition | Typical HTTP | Save statement | User behavior | Logging |
|---|---:|---|---|---|
| Validation error | 422/400 | Nothing saved | Keep safe form input, show field + summary errors | Structured reason without sensitive values |
| Authentication absent/expired | 401 or existing redirect contract | Nothing saved | Login/session recovery; autosave preserves current V1 401 | Authentication event where current policy requires |
| Authorization/scope failure | 403 | Nothing saved | Explain access/scope failure without leaking record existence | Actor, route/action, safe scope IDs |
| Conflict/stale write | 409 | Nothing saved unless stated | Reload/compare; never blind retry | Record/version/correlation |
| External failure after local save | 502/503 or accepted result per command contract | Local saved; external failed | Show local record ID and safe retry/manual path | Redacted request intent and external classification |
| Partial external success | 207-like JSON semantics or 409/502 page result, chosen by endpoint | Partial external success | List per-target outcomes; prohibit whole-command blind retry | Every target outcome and idempotency/correlation ID |
| Unexpected server failure | 500 | Explicitly state known/unknown save outcome | Reference ID and support/retry guidance | Exception with correlation, no secret payload |

Server-rendered flows may render the same envelope in a page/flash/error component. JSON responses serialize `ActionResult.as_json()`. A handler must not claim “nothing saved” after committing locally.

## Safety

- Never return or log passwords, hashes, cookies, session tokens, Square tokens, authorization headers, or raw unsafe external payloads.
- Correlation IDs are references, not authentication or idempotency secrets.
- Safe retry is true only when the command contract proves idempotency and classifies the failure.
- Authorization is rechecked on retry.
- Form posts retain CSRF; fetch commands use the same authenticated session and CSRF policy.
