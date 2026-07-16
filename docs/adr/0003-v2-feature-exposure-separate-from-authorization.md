# V2 feature exposure is separate from authorization

- Status: Accepted
- Date: 2026-07-15

## Context

V1 and V2 must coexist during local and limited testing. Unfinished V2 modules must be hidden without treating rollout configuration as permission.

## Decision

V2 business routes require both an explicit feature-exposure key and existing authorization/store-scope checks. Features are disabled by default and may be exposed globally or to named principal IDs. Disabled routes use hidden/not-found behavior. Exposure never grants a role, capability, object access, or store scope.

## Consequences

Navigation and direct routes remain undiscoverable without exposure, while authorized testers can be selected without source edits. Every module still needs independent backend authorization. Cutover/rollback first changes exposure, then follows the module’s writer/route plan.

Exposure also remains separate from canonical ownership. Under the [V1 Preservation Guarantee](0005-v1-preservation-guarantee.md), enabling or deploying a feature does not redirect, disable, replace, deprecate, or retire V1.
