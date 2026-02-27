from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PrincipalRole(str, Enum):
    ADMIN = 'ADMIN'
    MANAGER = 'MANAGER'
    LEAD = 'LEAD'
    STORE = 'STORE'


class SessionStatus(str, Enum):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'


class SnapshotSectionType(str, Enum):
    CATEGORY = 'CATEGORY'
    RECOUNT = 'RECOUNT'


class OpeningChecklistItemType(str, Enum):
    PARENT = 'PARENT'
    SUB = 'SUB'


class ChecklistAnswerValue(str, Enum):
    Y = 'Y'
    N = 'N'
    NA = 'NA'


class ChecklistNotesType(str, Enum):
    NONE = 'NONE'
    ISSUE = 'ISSUE'
    MAINTENANCE = 'MAINTENANCE'
    SUPPLY = 'SUPPLY'
    FOLLOW_UP = 'FOLLOW_UP'
    OTHER = 'OTHER'


class DailyChoreSheetStatus(str, Enum):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'


class ChangeBoxCountStatus(str, Enum):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'


class NonSellableStockTakeStatus(str, Enum):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'


class PurchaseOrderStatus(str, Enum):
    DRAFT = 'DRAFT'
    IN_TRANSIT = 'IN_TRANSIT'
    RECEIVED_SPLIT_PENDING = 'RECEIVED_SPLIT_PENDING'
    SENT_TO_STORES = 'SENT_TO_STORES'
    COMPLETED = 'COMPLETED'
    CANCELLED = 'CANCELLED'


class PurchaseOrderConfidenceState(str, Enum):
    NORMAL = 'NORMAL'
    LOW = 'LOW'


class ParLevelSource(str, Enum):
    MANUAL = 'MANUAL'
    DYNAMIC = 'DYNAMIC'


class PurchaseOrderReceiptStatus(str, Enum):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'


class SquareSyncStatus(str, Enum):
    PENDING = 'PENDING'
    SUCCESS = 'SUCCESS'
    FAILED = 'FAILED'


class Store(Base):
    __tablename__ = 'stores'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    square_location_id: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Principal(Base):
    __tablename__ = 'principals'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[PrincipalRole] = mapped_column(SQLEnum(PrincipalRole, name='principal_role'), nullable=False)
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('stores.id'))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Campaign(Base):
    __tablename__ = 'campaigns'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    category_filter: Mapped[str | None] = mapped_column(Text)
    brand_filter: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CountGroup(Base):
    __tablename__ = 'count_groups'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CountGroupCampaign(Base):
    __tablename__ = 'count_group_campaigns'

    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('count_groups.id', ondelete='CASCADE'), primary_key=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('campaigns.id', ondelete='CASCADE'), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CountSession(Base):
    __tablename__ = 'count_sessions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id'), nullable=False)
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('campaigns.id'), nullable=False)
    count_group_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('count_groups.id'))
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        SQLEnum(SessionStatus, name='session_status'), nullable=False, default=SessionStatus.DRAFT, server_default='DRAFT'
    )
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    submitted_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    source_forced_count_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('store_forced_counts.id'))
    includes_recount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    submit_inventory_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    variance_signature: Mapped[str | None] = mapped_column(String(128))
    stable_variance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SnapshotLine(Base):
    __tablename__ = 'snapshot_lines'

    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('count_sessions.id', ondelete='CASCADE'), primary_key=True)
    variation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sku: Mapped[str | None] = mapped_column(Text)
    item_name: Mapped[str] = mapped_column(Text, nullable=False)
    variation_name: Mapped[str] = mapped_column(Text, nullable=False)
    section_type: Mapped[SnapshotSectionType] = mapped_column(
        SQLEnum(SnapshotSectionType, name='snapshot_section_type'),
        nullable=False,
        default=SnapshotSectionType.CATEGORY,
        server_default='CATEGORY',
    )
    expected_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    source_catalog_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Entry(Base):
    __tablename__ = 'entries'
    __table_args__ = (
        CheckConstraint('counted_qty >= 0', name='entries_non_negative_ck'),
    )

    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('count_sessions.id', ondelete='CASCADE'), primary_key=True)
    variation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    counted_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    updated_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StoreRotationState(Base):
    __tablename__ = 'store_rotation_state'

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), primary_key=True)
    next_campaign_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('campaigns.id'))
    next_group_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('count_groups.id'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StoreForcedCount(Base):
    __tablename__ = 'store_forced_counts'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('campaigns.id'))
    count_group_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('count_groups.id'))
    source_session_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('count_sessions.id'))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')


class StoreRecountState(Base):
    __tablename__ = 'store_recount_state'

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    previous_signature: Mapped[str | None] = mapped_column(String(128))
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StoreRecountItem(Base):
    __tablename__ = 'store_recount_items'

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), primary_key=True)
    variation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sku: Mapped[str | None] = mapped_column(Text)
    item_name: Mapped[str] = mapped_column(Text, nullable=False)
    variation_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_variance: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuthEvent(Base):
    __tablename__ = 'auth_events'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempted_username: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditLog(Base):
    __tablename__ = 'audit_log'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('count_sessions.id'))
    ip: Mapped[str | None] = mapped_column(INET)
    meta: Mapped[dict] = mapped_column('metadata', JSON, nullable=False, default=dict, server_default='{}')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WebSession(Base):
    __tablename__ = 'web_sessions'
    __table_args__ = (
        UniqueConstraint('session_token', name='web_sessions_session_token_key'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_token: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpeningChecklistItem(Base):
    __tablename__ = 'opening_checklist_items'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    item_type: Mapped[OpeningChecklistItemType] = mapped_column(
        SQLEnum(OpeningChecklistItemType, name='opening_checklist_item_type'),
        nullable=False,
    )
    parent_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('opening_checklist_items.id'))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OpeningChecklistSubmission(Base):
    __tablename__ = 'opening_checklist_submissions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    submitted_by_name: Mapped[str] = mapped_column(Text, nullable=False)
    lead_name: Mapped[str | None] = mapped_column(Text)
    previous_employee: Mapped[str | None] = mapped_column(Text)
    summary_notes_type: Mapped[ChecklistNotesType] = mapped_column(
        SQLEnum(ChecklistNotesType, name='checklist_notes_type'),
        nullable=False,
        default=ChecklistNotesType.NONE,
        server_default='NONE',
    )
    summary_notes: Mapped[str | None] = mapped_column(Text)
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OpeningChecklistAnswer(Base):
    __tablename__ = 'opening_checklist_answers'

    submission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('opening_checklist_submissions.id', ondelete='CASCADE'),
        primary_key=True,
    )
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('opening_checklist_items.id'), primary_key=True)
    answer: Mapped[ChecklistAnswerValue] = mapped_column(
        SQLEnum(ChecklistAnswerValue, name='checklist_answer_value'),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DailyChoreTask(Base):
    __tablename__ = 'daily_chore_tasks'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DailyChoreSheet(Base):
    __tablename__ = 'daily_chore_sheets'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    sheet_date: Mapped[date] = mapped_column(Date, nullable=False)
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DailyChoreSheetStatus] = mapped_column(
        SQLEnum(DailyChoreSheetStatus, name='daily_chore_sheet_status'),
        nullable=False,
        default=DailyChoreSheetStatus.DRAFT,
        server_default='DRAFT',
    )
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DailyChoreEntry(Base):
    __tablename__ = 'daily_chore_entries'

    sheet_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('daily_chore_sheets.id', ondelete='CASCADE'), primary_key=True
    )
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('daily_chore_tasks.id'), primary_key=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeBoxCount(Base):
    __tablename__ = 'change_box_counts'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ChangeBoxCountStatus] = mapped_column(
        SQLEnum(ChangeBoxCountStatus, name='change_box_count_status'),
        nullable=False,
        default=ChangeBoxCountStatus.DRAFT,
        server_default='DRAFT',
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    submitted_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeBoxCountLine(Base):
    __tablename__ = 'change_box_count_lines'

    count_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('change_box_counts.id', ondelete='CASCADE'), primary_key=True
    )
    denomination_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    denomination_label: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    line_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NonSellableItem(Base):
    __tablename__ = 'non_sellable_items'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NonSellableStockTake(Base):
    __tablename__ = 'non_sellable_stock_takes'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NonSellableStockTakeStatus] = mapped_column(
        SQLEnum(NonSellableStockTakeStatus, name='non_sellable_stock_take_status'),
        nullable=False,
        default=NonSellableStockTakeStatus.DRAFT,
        server_default='DRAFT',
    )
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    submitted_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NonSellableStockTakeLine(Base):
    __tablename__ = 'non_sellable_stock_take_lines'

    stock_take_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('non_sellable_stock_takes.id', ondelete='CASCADE'), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('non_sellable_items.id'), primary_key=True)
    item_name: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal('0.000'), server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CustomerRequestItem(Base):
    __tablename__ = 'customer_request_items'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CustomerRequestSubmission(Base):
    __tablename__ = 'customer_request_submissions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CustomerRequestLine(Base):
    __tablename__ = 'customer_request_lines'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('customer_request_submissions.id', ondelete='CASCADE'), nullable=False
    )
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('customer_request_items.id'), nullable=False)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default='1')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeFormSubmission(Base):
    __tablename__ = 'change_form_submissions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    signature_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeFormLine(Base):
    __tablename__ = 'change_form_lines'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('change_form_submissions.id', ondelete='CASCADE'), nullable=False
    )
    section: Mapped[str] = mapped_column(String(64), nullable=False)
    denomination_code: Mapped[str] = mapped_column(String(64), nullable=False)
    denomination_label: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    unit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeBoxInventorySetting(Base):
    __tablename__ = 'change_box_inventory_settings'

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), primary_key=True)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeBoxInventoryLine(Base):
    __tablename__ = 'change_box_inventory_lines'

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), primary_key=True)
    denomination_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    denomination_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeBoxAuditSubmission(Base):
    __tablename__ = 'change_box_audit_submissions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    auditor_name: Mapped[str] = mapped_column(Text, nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChangeBoxAuditLine(Base):
    __tablename__ = 'change_box_audit_lines'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_submission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('change_box_audit_submissions.id', ondelete='CASCADE'), nullable=False
    )
    denomination_code: Mapped[str] = mapped_column(String(64), nullable=False)
    denomination_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    line_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExchangeReturnForm(Base):
    __tablename__ = 'exchange_return_forms'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='CASCADE'), nullable=False)
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    original_purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    original_ticket_number: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_ticket_number: Mapped[str] = mapped_column(Text, nullable=False)
    items_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    refund_given: Mapped[bool] = mapped_column(Boolean, nullable=False)
    refund_approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MasterSafeInventorySetting(Base):
    __tablename__ = 'master_safe_inventory_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default='1')
    target_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MasterSafeInventoryLine(Base):
    __tablename__ = 'master_safe_inventory_lines'

    denomination_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    denomination_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MasterSafeAuditSubmission(Base):
    __tablename__ = 'master_safe_audit_submissions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    auditor_name: Mapped[str] = mapped_column(Text, nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MasterSafeAuditLine(Base):
    __tablename__ = 'master_safe_audit_lines'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_submission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('master_safe_audit_submissions.id', ondelete='CASCADE'), nullable=False
    )
    denomination_code: Mapped[str] = mapped_column(String(64), nullable=False)
    denomination_label: Mapped[str] = mapped_column(Text, nullable=False)
    unit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    line_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal('0.00'), server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrderingMathSetting(Base):
    __tablename__ = 'ordering_math_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default='1')
    default_reorder_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default='5')
    default_stock_up_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default='10')
    default_history_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default='120')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Vendor(Base):
    __tablename__ = 'vendors'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    square_vendor_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VendorContact(Base):
    __tablename__ = 'vendor_contacts'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('vendors.id', ondelete='CASCADE'), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(Text)
    email_to: Mapped[str] = mapped_column(Text, nullable=False)
    email_cc: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VendorOrderingSetting(Base):
    __tablename__ = 'vendor_ordering_settings'

    vendor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('vendors.id', ondelete='CASCADE'), primary_key=True)
    reorder_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default='5')
    stock_up_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default='10')
    history_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default='120')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VendorSkuConfig(Base):
    __tablename__ = 'vendor_sku_configs'
    __table_args__ = (
        UniqueConstraint('vendor_id', 'sku', name='vendor_sku_configs_vendor_sku_uniq'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('vendors.id', ondelete='CASCADE'), nullable=False)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    square_variation_id: Mapped[str | None] = mapped_column(Text)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False, default=Decimal('0.0000'), server_default='0')
    pack_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default='1')
    min_order_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    is_default_vendor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PurchaseOrderPdfTemplate(Base):
    __tablename__ = 'purchase_order_pdf_templates'
    __table_args__ = (
        UniqueConstraint('vendor_id', name='purchase_order_pdf_templates_vendor_uniq'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_disclaimer: Mapped[str | None] = mapped_column(Text)
    is_generic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    vendor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('vendors.id', ondelete='CASCADE'))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ParLevel(Base):
    __tablename__ = 'par_levels'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('vendors.id', ondelete='SET NULL'))
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='SET NULL'))
    manual_par_level: Mapped[int | None] = mapped_column(Integer)
    manual_stock_up_level: Mapped[int | None] = mapped_column(Integer)
    suggested_par_level: Mapped[int | None] = mapped_column(Integer)
    par_source: Mapped[ParLevelSource] = mapped_column(
        SQLEnum(ParLevelSource, name='par_level_source'),
        nullable=False,
        default=ParLevelSource.MANUAL,
        server_default='MANUAL',
    )
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    confidence_state: Mapped[PurchaseOrderConfidenceState] = mapped_column(
        SQLEnum(PurchaseOrderConfidenceState, name='purchase_order_confidence_state'),
        nullable=False,
        default=PurchaseOrderConfidenceState.LOW,
        server_default='LOW',
    )
    locked_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    confidence_streak_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    confidence_streak_down: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    updated_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PurchaseOrder(Base):
    __tablename__ = 'purchase_orders'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('vendors.id'), nullable=False)
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        SQLEnum(PurchaseOrderStatus, name='purchase_order_status'),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT,
        server_default='DRAFT',
    )
    reorder_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default='5')
    stock_up_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default='10')
    history_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default='120')
    notes: Mapped[str | None] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    created_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    submitted_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_sent_by_principal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('principals.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PurchaseOrderLine(Base):
    __tablename__ = 'purchase_order_lines'
    __table_args__ = (
        UniqueConstraint('purchase_order_id', 'variation_id', name='purchase_order_lines_order_variation_uniq'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('purchase_orders.id', ondelete='CASCADE'), nullable=False)
    variation_id: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str | None] = mapped_column(Text)
    item_name: Mapped[str] = mapped_column(Text, nullable=False)
    variation_name: Mapped[str] = mapped_column(Text, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    suggested_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    ordered_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    received_qty_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    in_transit_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    confidence_state: Mapped[PurchaseOrderConfidenceState] = mapped_column(
        SQLEnum(PurchaseOrderConfidenceState, name='purchase_order_confidence_state'),
        nullable=False,
        default=PurchaseOrderConfidenceState.NORMAL,
        server_default='NORMAL',
    )
    par_source: Mapped[ParLevelSource] = mapped_column(
        SQLEnum(ParLevelSource, name='par_level_source'),
        nullable=False,
        default=ParLevelSource.MANUAL,
        server_default='MANUAL',
    )
    manual_par_level: Mapped[int | None] = mapped_column(Integer)
    suggested_par_level: Mapped[int | None] = mapped_column(Integer)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PurchaseOrderStoreAllocation(Base):
    __tablename__ = 'purchase_order_store_allocations'
    __table_args__ = (
        UniqueConstraint('purchase_order_line_id', 'store_id', name='purchase_order_store_allocations_line_store_uniq'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_order_line_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('purchase_order_lines.id', ondelete='CASCADE'),
        nullable=False,
    )
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('stores.id'), nullable=False)
    expected_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    allocated_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    manual_par_level: Mapped[int | None] = mapped_column(Integer)
    store_received_qty: Mapped[int | None] = mapped_column(Integer)
    variance_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PurchaseOrderReceipt(Base):
    __tablename__ = 'purchase_order_receipts'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('purchase_orders.id', ondelete='CASCADE'), nullable=False)
    status: Mapped[PurchaseOrderReceiptStatus] = mapped_column(
        SQLEnum(PurchaseOrderReceiptStatus, name='purchase_order_receipt_status'),
        nullable=False,
        default=PurchaseOrderReceiptStatus.DRAFT,
        server_default='DRAFT',
    )
    received_by_principal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('principals.id'), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PurchaseOrderReceiptLine(Base):
    __tablename__ = 'purchase_order_receipt_lines'
    __table_args__ = (
        UniqueConstraint('receipt_id', 'purchase_order_line_id', name='purchase_order_receipt_lines_receipt_line_uniq'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('purchase_order_receipts.id', ondelete='CASCADE'), nullable=False)
    purchase_order_line_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('purchase_order_lines.id', ondelete='CASCADE'),
        nullable=False,
    )
    expected_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    received_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    difference_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SquareSyncEvent(Base):
    __tablename__ = 'square_sync_events'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('purchase_orders.id', ondelete='SET NULL'))
    purchase_order_line_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey('purchase_order_lines.id', ondelete='SET NULL'),
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('stores.id', ondelete='SET NULL'))
    sync_type: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[SquareSyncStatus] = mapped_column(
        SQLEnum(SquareSyncStatus, name='square_sync_status'),
        nullable=False,
        default=SquareSyncStatus.PENDING,
        server_default='PENDING',
    )
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default='{}')
    response_payload: Mapped[dict | None] = mapped_column(JSON)
    error_text: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
