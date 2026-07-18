# Staff Scheduling V2 weekly board

Milestone 3 makes `/v2/scheduling/week?start=YYYY-MM-DD` the management scheduling workspace. Dates normalize to Sunday in `America/Los_Angeles`; the board resolves an active draft before the current published revision and otherwise presents an empty-week workflow.

The feature remains default-disabled behind `staff_scheduling_v2`. Route authorization is independent of navigation and requires `scheduling.view_all` or meaningful store-scoped `scheduling.view_store` access. Draft creation, shift editing, deletion, labor totals, published cloning, and hard-unavailability overrides retain their separate Milestone 2 capabilities.

The server serializer emits only the selected store scope. It includes active employees, referenced inactive employees, availability/preference indicators, approved time-off intervals without reasons or notes, shifts, aggregate summaries, and warnings. Hourly rates are never emitted. Aggregate labor data is omitted unless `scheduling.view_labor_cost` is effective; private scheduler notes require `scheduling.manage_preferences`.

Draft shifts can be created, edited, duplicated, deleted, or moved between employees, dates, and open-shift rows. Pointer dragging uses a movement threshold and preserves time, store, role, break, opener/closer flags, and employee-visible notes. Every card also exposes an accessible Move dialog for keyboard and touch use. Store changes are explicit. All writes send the whole-period expected version and use server-canonical responses; a stale version produces a visible refresh action.

Warnings are deterministically rebuilt on board reads and after mutations. Summary counts, day/store markers, shift markers, and the central keyboard-accessible warning panel distinguish informational, conflict, and serious findings. Warnings do not prevent draft saves.

This milestone does not add month views, self-service scheduling, time-off administration, template CRUD, automated scheduling, notifications, swaps, call-outs, overtime, payroll, or third-party scheduling integrations.
