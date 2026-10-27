# Schema 0052 recovery

Based on deployed web `d56aa1bf38d5880dcdf2fb09037a360402d41038`.
The configured cron entry point and scheduling business-service sources are
identical between that web baseline and deployed cron `ad5621afe525117effce6d48ab531cb0216403da`.
This allows one independently qualified recovery artifact for web and cron.

Recovery retains the deployed scheduler controls and all Purchasing code. Its
compatibility backport includes the new data fields, immutable snapshots, scoped
permission resolution, effective coverage intervals, audit semantics, Owner final
authority, and atomic backend mutations needed to safely operate on schema 0052.
It displays existing LEAD badges but omits the new assignment/movement selectors.
Cached newer clients retain the compatible explicit Owner assignment API.

Unchanged assignments keep their temporary authority. A legacy movement with no
explicit Lead choice revokes the prior temporary grant and records the removal;
it cannot silently confer that grant on a different employee, store, date, or
interval. Explicit Owner ASSIGN requests remain supported. Coverage agreement
updates retain authority only for a compatible employee and interval. Revisions
preserve designation fields. Cancellation and shift end revoke effective access.
No account role, employee capability, Purchasing code, cron schedule, or execution
gate is changed. Recovery needs no database downgrade.

Schema 0051 is retained, and the exact same additive scheduler migration 0052 is
included. The recovery artifact runs on schema 0052; before migration, the original
schema-0051 services remain the recovery point. Isolated restore cycles and exact
source/native-build qualification are required before this branch can be used.
