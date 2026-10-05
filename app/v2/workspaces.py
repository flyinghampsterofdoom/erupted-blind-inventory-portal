"""Reviewed GET presentation destinations; never authorization grants.

The same registry supplies primary navigation, launcher and safe return targets.
Record URLs fail closed to the landing page unless explicitly reviewed here.
"""
from dataclasses import dataclass
import re
from urllib.parse import parse_qs, unquote, urlsplit
from app.v2.feature_exposure import FeatureExposure

PRIMARY_FEATURE = 'v2_primary'

@dataclass(frozen=True)
class Destination:
    group: str
    label: str
    path: str
    permission: str
    feature: str = ''
    roles: tuple[str, ...] = ()
    any_permissions: tuple[str, ...] = ()
    linked: bool = False
    assigned_store: bool = False

    @property
    def compatibility(self):
        return not self.path.startswith('/v2/')

    def allowed(self, request):
        principal = getattr(request.state, 'principal', None)
        flags = getattr(request.state, 'permission_flags', {}) or {}
        if not principal or not principal.active:
            return False
        if self.permission and not flags.get(self.permission, False):
            return False
        if self.any_permissions and not any(flags.get(p, False) for p in self.any_permissions):
            return False
        role = getattr(principal.role, 'value', principal.role)
        if self.roles and role not in self.roles:
            return False
        if self.feature and not FeatureExposure.from_settings().enabled(self.feature, principal_id=principal.id):
            return False
        if self.linked and not getattr(request.state, 'employee_id', None):
            return False
        return not self.assigned_store or principal.store_id is not None


def _entries(group, permission, rows, **guards):
    return tuple(Destination(group, label, path, permission, **guards) for label, path in rows)


DESTINATIONS = (
    Destination('My Schedule', 'My Schedule', '/v2/scheduling/my-schedule', 'scheduling.view_own', 'staff_scheduling_v2', linked=True),
    *_entries('Store Operations', 'store.access', (
        ('Daily inventory count', '/store/daily-count'), ('Daily chore sheet', '/store/daily-chore-sheet'),
        ('Opening checklist', '/store/opening-checklist'), ('Non-sellable stock take', '/store/non-sellable-stock-take'),
        ('Change-box count', '/store/change-box-count'), ('Change request', '/store/change-form'),
        ('Customer requests', '/store/customer-requests'), ('Exchange / return form', '/store/exchange-return-form'),
    ), assigned_store=True),
    Destination('Store Operations', 'Current store', '/v2/current-store', 'store.access', 'daily_store_logs_v2'),
    Destination('Store Operations', 'Daily store log', '/v2/store-operations/daily-logs', 'store.access', 'daily_store_logs_v2'),
    Destination('Store Operations', 'Exchange / return form (V2)', '/v2/customer-forms/exchanges-returns', 'store.access', 'exchanges_returns_v2', assigned_store=True),
    *_entries('Store Operations', 'management.access', (
        ('Chore submissions', '/management/daily-chore-lists'), ('Opening checklists', '/management/opening-checklists'),
        ('Change-box submissions', '/management/change-box-count'), ('Change requests', '/management/change-forms'),
        ('Non-sellable submissions', '/management/non-sellable-stock-take'), ('Customer request review', '/management/customer-requests'),
        ('Exchange / return history', '/management/exchange-return-forms'),
    )),
    Destination('Store Operations', 'Daily log history', '/v2/store-operations/daily-logs/history', 'management.access', 'daily_store_logs_v2'),
    Destination('Counts & Cash', 'Inventory count history', '/management/sessions', 'management.access'),
    Destination('Counts & Cash', 'Count correction review', '/management/audit-queue', 'management.access', roles=('ADMIN', 'MANAGER', 'LEAD')),
    *_entries('Counts & Cash', 'management.admin', (
        ('Admin store count', '/management/store-count'), ('Cash reconciliation', '/management/cash-reconciliation'),
        ('Change-box audit', '/management/change-box-audit'), ('Master-safe audit', '/management/master-safe-audit'),
        ('Store change / non-sellable needs', '/management/store-par-reset'), ('Delivery queue', '/management/store-par-reset/load-delivery'),
    )),
    Destination('Scheduling', 'Schedule board', '/v2/scheduling/week', '', 'staff_scheduling_v2', any_permissions=('scheduling.view_all', 'scheduling.view_store')),
    Destination('Scheduling', 'Time-off review', '/v2/scheduling/time-off', 'scheduling.time_off.view', 'staff_scheduling_v2'),
    Destination('Scheduling', 'Coverage requirements', '/v2/scheduling/coverage', 'scheduling.manage_coverage', 'staff_scheduling_v2'),
    Destination('Scheduling', 'Scheduling rules', '/v2/scheduling/rules', 'scheduling.manage_preferences', 'staff_scheduling_v2'),
    Destination('Scheduling', 'Store defaults', '/v2/scheduling/store-defaults', 'scheduling.manage_preferences', 'staff_scheduling_v2'),
    Destination('Scheduling', 'Store shifts', '/v2/scheduling/store-shifts/manage', 'scheduling.store_shifts.manage', 'staff_scheduling_v2'),
    Destination('Scheduling', 'Generation & readiness', '/v2/scheduling/automation', 'scheduling.manage_automation', 'staff_scheduling_v2'),
    Destination('Scheduling', 'Transfer approvals', '/v2/scheduling/transfer-approvals', 'scheduling.approve_transfer_hours', 'staff_scheduling_v2'),
    Destination('Inventory / Ordering', 'Purchase orders', '/management/ordering-tool', 'ordering.manage'),
    *_entries('Inventory / Ordering', 'management.admin', (
        ('Vendor SKU mappings', '/management/ordering-tool/mappings'), ('Par levels', '/management/ordering-tool/par-levels'),
        ('PDF templates', '/management/ordering-tool/pdf-templates'), ('Emergency on-hand correction', '/management/ordering-tool/emergency-editor'),
    )),
    Destination('Inventory / Ordering', 'Ordering intelligence', '/v2/ordering', 'management.admin', 'ordering_intelligence_v2'),
    Destination('Inventory / Ordering', 'Product lifecycle', '/v2/ordering/products', 'ordering.lifecycle.manage', 'ordering_intelligence_v2', any_permissions=('management.admin',)),
    *_entries('Financials', 'management.admin', (
        ('Order payments', '/v2/order-payments'), ('Payment methods', '/v2/payment-methods'),
        ('Consignment', '/v2/consignment'), ('Funding accounts', '/v2/funding-accounts'),
    ), feature='order_payments_v2', roles=('ADMIN', 'MANAGER')),
    *_entries('Reports', 'reports.workbench.view', (('Reporting workbench', '/v2/reports'), ('Vendor inventory', '/v2/reports/vendor-inventory'))),
    Destination('Reports', 'COGS report', '/management/reports/cogs', 'management.access'),
    *_entries('Reports', 'management.admin', (
        ('Stock value', '/management/reports/stock-value-on-hand'), ('Recount changes', '/management/reports/recount-changes'),
        ('Master-safe change usage', '/management/reports/master-safe-change-usage'),
    )),
    *_entries('Reports', '', tuple((label, '/management/reports/' + slug) for label, slug in (
        ('Count Square sync', 'count-square-sync'), ('Sales transactions', 'sales-transactions'),
        ('Gross sales by store', 'gross-sales-by-store'), ('Sales by vendor', 'sales-by-vendor'),
        ('Employee sales', 'employee-sales'), ('Targeted SKU demand', 'targeted-sku-demand'),
        ('Inventory velocity', 'inventory-velocity'), ('Stock coverage purchase', 'stock-coverage-purchase'),
    )), roles=('ADMIN',)),
    Destination('Employees / HR', 'Employees', '/v2/hr/employees', 'scheduling.manage_preferences', 'staff_scheduling_v2'),
    Destination('Employees / HR', 'Employee logs', '/management/employee-logs', 'management.access', roles=('ADMIN', 'MANAGER', 'LEAD')),
    *_entries('Administration / Settings', 'management.groups', (
        ('Count groups & shared credentials', '/management/groups'), ('Count group audit', '/management/groups/audit-count-groups'),
    )),
    *_entries('Administration / Settings', 'management.users', (('Users', '/management/users'), ('Access controls', '/management/access-controls'))),
    *_entries('Administration / Settings', 'management.admin', (
        ('Chore task editor', '/management/daily-chore-tasks'), ('Dashboard configuration', '/management/dashboard-settings'),
    )),
    Destination('Administration / Settings', 'Integrations', '/admin/settings/integrations', 'management.users', roles=('ADMIN',)),
    *_entries('Digital Signage', 'digital_signage.view', (
        ('Advertisement groups', '/v2/digital-signage/groups'), ('Media', '/v2/digital-signage/media'), ('Displays', '/v2/digital-signage/displays'),
    ), feature='digital_signage_v2'),
    *_entries('Touchscreen', 'touchscreen.view', (
        ('Flavors', '/v2/touchscreen/flavors'), ('Categories', '/v2/touchscreen/categories'),
        ('Devices', '/v2/touchscreen/devices'), ('Cache status', '/v2/touchscreen/sync'),
    ), feature='touchscreen_v2'),
    Destination('Touchscreen', 'Store preview', '/v2/touchscreen/preview', 'touchscreen.preview', 'touchscreen_v2'),
)


def primary_enabled(principal):
    return bool(principal and getattr(principal, 'active', False) and FeatureExposure.from_settings().enabled(PRIMARY_FEATURE, principal_id=principal.id))


def visible_destinations(request):
    return tuple(d for d in DESTINATIONS if d.allowed(request))


def local_get_target(value):
    raw = str(value or '')
    if not raw or len(raw) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    path = parsed.path
    if (parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith('/')
            or path.startswith('//') or '\\' in raw or unquote(path) != path
            or any(part in {'.', '..'} for part in path.split('/'))):
        return None
    return raw


def authorized_return(request, value, db=None):
    target = local_get_target(value)
    if target is None:
        return None
    parsed = urlsplit(target)
    from app.v2.entry import has_operational_workspace
    shell_allowed = (parsed.path == '/v2/access-help'
        or parsed.path == '/v2/overview' and has_operational_workspace(request)
        or parsed.path == '/v2/store-operations' and request.state.permission_flags.get('store.access'))
    if not shell_allowed and not any(d.path == parsed.path for d in visible_destinations(request)):
        if db is None or not _record_return_allowed(request, db, parsed.path):
            return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if 'period_id' in query or 'schedule_period_id' in query:
        if db is None or parsed.path != '/v2/scheduling/week' or 'schedule_period_id' in query:
            return None
        from app.routers.v2_scheduling import _authorize_explicit_period
        from fastapi import HTTPException
        try:
            period_id = int(query['period_id'][-1])
            if not 0 < period_id < 2**63:
                return None
            _authorize_explicit_period(db, principal=request.state.principal, schedule_period_id=period_id)
        except (ValueError, HTTPException):
            return None
    principal = request.state.principal
    if 'saved_view_id' in query:
        if db is None or parsed.path != '/v2/reports':
            return None
        from app.services.v2_reporting_workbench_service import get_saved_view
        try:
            view_id = int(query['saved_view_id'][-1])
            if not 0 < view_id < 2**63:
                return None
            get_saved_view(db, principal_id=principal.id, view_id=view_id)
        except (ValueError, LookupError):
            return None
    if getattr(principal.role, 'value', principal.role) == 'STORE':
        if any(value != str(principal.store_id) for value in query.get('store_id', ())):
            return None
        if query.get('scope') and query['scope'] != ['all']:
            return None
    return target


def primary_rollout_configured():
    exposure = FeatureExposure.from_settings()
    return PRIMARY_FEATURE in exposure.global_features or any(
        feature == PRIMARY_FEATURE for _, feature in exposure.principal_features)


# Record links require both the entry guard and existence/ownership checks. No
# broad prefix matching: mutation-shaped URLs and downloads are not replayed.
RECORD_DESTINATIONS = (
    (r'/management/ordering-tool/orders/([1-9][0-9]*)', '/management/ordering-tool', 'PurchaseOrder'),
    (r'/management/sessions/([1-9][0-9]*)', '/management/sessions', 'CountSession'),
    (r'/store/sessions/([1-9][0-9]*)', '/store/daily-count', 'CountSession'),
    (r'/v2/hr/employees/([1-9][0-9]*)', '/v2/hr/employees', 'Employee'),
    (r'/v2/scheduling/employees/([1-9][0-9]*)', '/v2/hr/employees', 'Employee'),
    (r'/management/daily-chore-lists/([1-9][0-9]*)', '/management/daily-chore-lists', 'DailyChoreSheet'),
    (r'/management/opening-checklists/([1-9][0-9]*)', '/management/opening-checklists', 'OpeningChecklistSubmission'),
    (r'/management/change-box-count/([1-9][0-9]*)', '/management/change-box-count', 'ChangeBoxCount'),
    (r'/management/change-forms/([1-9][0-9]*)', '/management/change-forms', 'ChangeFormSubmission'),
    (r'/management/non-sellable-stock-take/([1-9][0-9]*)', '/management/non-sellable-stock-take', 'NonSellableStockTake'),
    (r'/management/exchange-return-forms/([1-9][0-9]*)', '/management/exchange-return-forms', 'ExchangeReturnForm'),
)


def _record_return_allowed(request, db, path):
    from app import models
    from fastapi import HTTPException
    allowed_entries = {d.path for d in visible_destinations(request)}
    for pattern, entry, model in RECORD_DESTINATIONS:
        match = re.fullmatch(pattern, path)
        if match is None or entry not in allowed_entries:
            continue
        record_id = int(match[1])
        if record_id >= 2**63:
            return False
        if path.startswith('/store/sessions/'):
            from app.services.session_service import get_session_for_principal
            try:
                get_session_for_principal(db, session_id=record_id, principal=request.state.principal)
                return True
            except (ValueError, PermissionError, HTTPException):
                return False
        return db.get(getattr(models, model), record_id) is not None
    return False
