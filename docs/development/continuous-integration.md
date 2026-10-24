# Continuous integration

`Erupted CI` validates pull requests targeting main, pushes to main and manual
workflow dispatch. CI never deploys or receives production credentials. Render
independent auto-deploy and previews remain disabled.

## Validation levels

Every PR runs the fast integrity tier: pinned build/dependencies, migration graph
and populated upgrade tests, authorization/CSRF, cascade/store access, Square write
and cost boundaries, purchasing arithmetic/receiving and browser JavaScript.
Only Markdown documentation and CSS-only PRs can omit the expanded tier. Every
other path, including templates, browser JavaScript, tests, shared config/models,
locks, migrations, release tooling and unknown files, expands. Renames consider
both removed and added paths. Pushes to main and manual dispatch always expand.

The expanded tier partitions every Python test file recursively under `tests/` exactly once across
fast, scheduling foundation, two scheduling workflow shards, purchasing, inventory/access,
platform/forecast and other groups. New files default into a required group.
No group is optional once planned. Tests of the policy enforce this partition
and shared-dependency expansion. File counts and JUnit/logs accompany each job.

Python 3.10.13 and Node 22.14.0 are exact versions. Production and test dependency
locks install wheels with hashes and pass pip check. PostgreSQL 18.6 uses a pinned
Docker image digest; its pg_dump/pg_restore binaries also run through that image
with a shared ephemeral /tmp for synthetic dump tests. Each job gets its own
service database. Tests create/drop disposable uniquely named databases inside
that service; both PostgreSQL opt-in environment variables are explicitly set.

The test phase runs as a dedicated test UID with an explicit environment
allowlist, and rejects its new outbound connections except loopback and the local
CI PostgreSQL container, in IPv4 and IPv6. Dependency installation, checkout and
artifact delivery occur outside that phase. The runner supervisor retains network access for cancellation, timeouts and log
delivery. The test UID can invoke the ephemeral Docker database client tools;
this guard prevents accidental network access rather than sandboxing malicious
workflow code. Firewall cleanup runs on failure.
No production Square, Render, R2, database or encryption credentials are supplied.
The checkout token is read-only and not persisted. No `pull_request_target`,
self-hosted runner, production environment or deployment credential is used.

## Results and enforcement

The only branch-required check is **CI required**, bound to the GitHub Actions
app. It always runs, and fails if planning or any planned matrix job fails,
cancels or unexpectedly skips. Do not require a conditionally skipped job alone:
GitHub can consider a skipped job successful for branch protection.

Every test job publishes stage outcomes, exact files, JUnit counts, slow tests,
skip reasons, logs, Python duration and build identity. Checkout/service/setup
failures also fail the final check; GitHub initialization logs describe failures
that happen before any step can upload artifacts. Artifacts contain synthetic
fixtures and expire in seven days. They never include .env files, DB dumps,
provider logs or credentials. GitHub's run/job timestamps give total duration.

Unexpected skips and xfails are failures. The only allowed skips are the two
explicit private R2 delivery/preservation integrations; those require separate
hosted-test credentials and unique object prefixes and are not enabled in CI.
A test/assertion failure is reported separately from setup/collection failures;
a failed assertion alone cannot determine whether the test or application is
wrong. Inspect JUnit and reproduce before classifying the underlying defect.

## Local reproduction

Use the pinned Python interpreter and both hash locks. Set ENVIRONMENT=test,
SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false and both forecast execution flags
false. Set DATABASE_URL, TEST_POSTGRES_ADMIN_URL and P1_TEST_DATABASE_URL to an
explicitly disposable local PostgreSQL 18 database. The purchasing URL must use
a database name beginning `erupted_purchasing_p1_`; fixtures create their own
unique databases and clean them up. Never copy a production .env into CI.

Run `python -m scripts.ci.run fast` or a group listed in
`scripts/ci/policy.py`. Run `python scripts/ci/javascript.py`.
`offline.sh`, `database_tools.sh` and the stricter preflight are for disposable
GitHub-hosted Linux runners and service containers, not an operator workstation.

## Main protection and emergency recovery

After demonstrated successful GitHub runs, protect main with the Actions-bound
`CI required` check, require it against the current base, prohibit force pushes
and deletion, and require PRs with zero approving reviews. Apply enforcement to
administrators too. Ordinary releases need no Owner approval or waiting period.
Do not use an admin merge override as routine development practice.

Emergency recovery remains an explicitly authorized administrator operation:
record the incident, intended immutable recovery SHA, reason CI cannot run,
validation evidence and original protection JSON before changing a rule. Under
separate emergency authorization, an administrator may temporarily suspend admin
enforcement for that one recovery, perform a normal reviewed fast-forward/PR
operation, immediately restore the captured rule and verify it through GET.
Record the exact commands, actor, timestamps, GitHub response and resulting SHA
in the incident evidence and follow-up PR. Never force-push or deploy implicitly.
A restored branch rule and successful resumed CI close the incident. This is an
exceptional documented policy change, not a permanent admin exemption. The Phase
2 exercise uses only a disposable protected branch, not unvalidated main code.

## Limits

A contributor deliberately weakening the workflow itself is a trusted-source
security change, not a test-selection optimization. Repository administrators
can edit protection and must audit exceptional changes. CI proves the pinned
Linux synthetic environment, not production scale, private hosted R2 behavior,
a new natural cron run or production readiness. No deployment automation is
part of this phase. Runtime/lock/action/image upgrades require explicit review.
