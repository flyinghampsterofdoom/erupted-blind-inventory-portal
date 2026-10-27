# Schema 0052 release remediation

The preservation regression compared complete JSON rows across an upgrade from
0045 to the current head. Adding the false-by-default `is_shift_lead` field changes
that JSON shape even when all pre-existing values are identical. The test now
captures every original column before upgrading, hashes all original values after
upgrading, checks preservation of all original foreign keys, and independently
asserts that both new designation columns default to false. This is stricter than
the former blanket exclusion of three column names across every table.

The operational exception regression encoded the superseded rule that an Owner
cannot publish without a designated Lead. It now verifies successful, audited
Owner publication, rejection for an ordinary employee or Lead without the required
final Owner authority, and separate rejection of overlapping assignments even
when the Owner authorizes operational exceptions. Approval failures must leave
no approval or partial publication behind.

No production migration or deployment is authorized by these tests alone. Exact
candidate CI, source-bound artifacts, schema-52-aware web and cron recovery,
production-equivalent migration preservation, and protected merge remain gates.
