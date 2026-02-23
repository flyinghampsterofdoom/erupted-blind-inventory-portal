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
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
