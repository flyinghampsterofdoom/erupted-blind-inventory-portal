"""Additive Flavor Intelligence table definitions; not registered at application startup.

Existing Erupted table objects are foreign-key targets only. This isolated metadata
is NOT an Alembic revision. The developer-only storage adapter copies it into an
isolated SQLite database with executable integrity triggers. No engine or Square
client is created here. Production installation still requires a reviewed migration.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models import (
    Principal, Store, TouchscreenFlavor, TouchscreenFlavorCategory,
)


class ProposalBase(DeclarativeBase):
    """Deliberately independent of app.models.Base and migration metadata."""


class FlavorNote(ProposalBase):
    __tablename__ = "fi_notes"
    __table_args__ = (
        CheckConstraint("length(slug) > 0", name="fi_notes_slug_nonempty"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    family: Mapped[str | None] = mapped_column(String(80))
    # Allows the future backfill to reuse existing fruit/other-note identities.
    legacy_category_id: Mapped[int | None] = mapped_column(
        ForeignKey(TouchscreenFlavorCategory.__table__.c.id, ondelete="RESTRICT"),
        unique=True,
    )


class SensoryAxis(ProposalBase):
    __tablename__ = "fi_sensory_axes"
    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class ClassificationVersion(ProposalBase):
    __tablename__ = "fi_classification_versions"
    __table_args__ = (
        UniqueConstraint("flavor_id", "version", name="fi_versions_flavor_number"),
        UniqueConstraint("id", "flavor_id", name="fi_versions_identity_flavor"),
        Index("fi_one_unclassified_origin", "flavor_id", unique=True,
              sqlite_where=text("is_baseline = 1 AND restores_version_id IS NULL"),
              postgresql_where=text("is_baseline AND restores_version_id IS NULL")),
        CheckConstraint("version > 0", name="fi_versions_positive"),
        ForeignKeyConstraint(
            ["parent_version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_versions_parent_same_flavor", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["restores_version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_versions_restore_same_flavor", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parent_version_id IS NULL OR parent_version_id <> id",
            name="fi_versions_parent_not_self",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flavor_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[int | None] = mapped_column(BigInteger)
    # Rollback creates a new version copied from this historical version.
    restores_version_id: Mapped[int | None] = mapped_column(BigInteger)
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by_principal_id: Mapped[int] = mapped_column(
        ForeignKey(Principal.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class VersionNote(ProposalBase):
    __tablename__ = "fi_version_notes"
    version_id: Mapped[int] = mapped_column(
        ForeignKey("fi_classification_versions.id", ondelete="RESTRICT"), primary_key=True,
    )
    note_id: Mapped[int] = mapped_column(
        ForeignKey("fi_notes.id", ondelete="RESTRICT"), primary_key=True,
    )


class VersionCategory(ProposalBase):
    """Broad categories reuse the existing normalized touchscreen taxonomy."""
    __tablename__ = "fi_version_categories"
    version_id: Mapped[int] = mapped_column(
        ForeignKey("fi_classification_versions.id", ondelete="RESTRICT"), primary_key=True,
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavorCategory.__table__.c.id, ondelete="RESTRICT"),
        primary_key=True,
    )


class VersionSensory(ProposalBase):
    __tablename__ = "fi_version_sensory"
    __table_args__ = (
        CheckConstraint("value >= 0 AND value <= 1", name="fi_sensory_unit_interval"),
    )
    version_id: Mapped[int] = mapped_column(
        ForeignKey("fi_classification_versions.id", ondelete="RESTRICT"), primary_key=True,
    )
    axis_slug: Mapped[str] = mapped_column(
        ForeignKey("fi_sensory_axes.slug", ondelete="RESTRICT"), primary_key=True,
    )
    value: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)


class VersionSimilarity(ProposalBase):
    __tablename__ = "fi_version_similarities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_similarity_source_version", ondelete="RESTRICT",
        ),
        CheckConstraint("flavor_id <> related_flavor_id", name="fi_similarity_not_self"),
        CheckConstraint("weight > 0 AND weight <= 1", name="fi_similarity_weight"),
    )
    version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flavor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    related_flavor_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"), primary_key=True,
    )
    weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class SquareFlavorLink(ProposalBase):
    """Identity association only: never a second catalog or inventory authority."""
    __tablename__ = "fi_square_flavor_links"
    square_variation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    square_item_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    flavor_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Square IDs deliberately have no FK into an evictable local catalog cache.


class SuggestionReviewGroup(ProposalBase):
    __tablename__ = "fi_suggestion_review_groups"
    __table_args__ = (
        UniqueConstraint(
            "id", "flavor_id", "base_version_id", name="fi_groups_identity_baseline",
        ),
        ForeignKeyConstraint(
            ["base_version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_groups_baseline_same_flavor", ondelete="RESTRICT",
        ),
        CheckConstraint("length(patch_fingerprint) = 64", name="fi_groups_sha256"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'MODIFIED_APPROVED')",
            name="fi_groups_status",
        ),
        # A decided group is closed; a later identical submission gets a new group.
        Index(
            "fi_groups_pending_fingerprint", "flavor_id", "base_version_id",
            "patch_fingerprint", unique=True,
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flavor_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    base_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    patch_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="PENDING", server_default="PENDING",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class EmployeeSuggestion(ProposalBase):
    __tablename__ = "fi_employee_suggestions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_group_id", "flavor_id", "base_version_id"],
            ["fi_suggestion_review_groups.id", "fi_suggestion_review_groups.flavor_id",
             "fi_suggestion_review_groups.base_version_id"],
            name="fi_submissions_group_same_baseline", ondelete="RESTRICT",
        ),
        Index("fi_submissions_employee_time", "employee_id", "submitted_at"),
        Index("fi_submissions_flavor_time", "flavor_id", "submitted_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    review_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    flavor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey(Principal.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey(Store.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    author_context: Mapped[dict] = mapped_column(JSON, nullable=False)
    original_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    proposed_patch: Mapped[dict] = mapped_column(JSON, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class ReviewDecision(ProposalBase):
    __tablename__ = "fi_review_decisions"
    __table_args__ = (
        UniqueConstraint("review_group_id", name="fi_decisions_one_per_group"),
        ForeignKeyConstraint(
            ["review_group_id", "flavor_id", "base_version_id"],
            ["fi_suggestion_review_groups.id", "fi_suggestion_review_groups.flavor_id",
             "fi_suggestion_review_groups.base_version_id"],
            name="fi_decisions_group_same_baseline", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["published_version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_decisions_published_same_flavor", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED', 'MODIFIED_APPROVED')",
            name="fi_decisions_allowed",
        ),
        CheckConstraint(
            "(decision = 'REJECTED' AND published_version_id IS NULL) OR "
            "(decision IN ('APPROVED', 'MODIFIED_APPROVED') AND published_version_id IS NOT NULL)",
            name="fi_decisions_publication_required",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    review_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    flavor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reviewer_id: Mapped[int] = mapped_column(
        ForeignKey(Principal.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    published_version_id: Mapped[int | None] = mapped_column(BigInteger)
    proposed_patch_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    published_snapshot: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class PublicationRevision(ProposalBase):
    __tablename__ = "fi_publication_revisions"
    __table_args__ = (
        UniqueConstraint("flavor_id", "revision", name="fi_publication_flavor_revision"),
        UniqueConstraint("version_id", name="fi_publication_version_once"),
        ForeignKeyConstraint(
            ["version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_publication_version_same_flavor", ondelete="RESTRICT",
        ),
        CheckConstraint("revision > 0", name="fi_publication_revision_positive"),
        CheckConstraint(
            "action IN ('PUBLISH', 'DIRECT_EDIT', 'ROLLBACK')",
            name="fi_publication_action",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flavor_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewed_by_principal_id: Mapped[int] = mapped_column(
        ForeignKey(Principal.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    review_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("fi_review_decisions.id", ondelete="RESTRICT"), unique=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class IntelligenceAuditEvent(ProposalBase):
    __tablename__ = "fi_audit_events"
    __table_args__ = (
        Index("fi_audit_flavor_time", "flavor_id", "occurred_at"),
        ForeignKeyConstraint(
            ["version_id", "flavor_id"],
            ["fi_classification_versions.id", "fi_classification_versions.flavor_id"],
            name="fi_audit_version_same_flavor", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('SUBMITTED', 'REVIEWED', 'PUBLISHED', 'DIRECT_EDIT', 'ROLLED_BACK')",
            name="fi_audit_event_type",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flavor_id: Mapped[int] = mapped_column(
        ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    version_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_id: Mapped[int] = mapped_column(
        ForeignKey(Principal.__table__.c.id, ondelete="RESTRICT"), nullable=False,
    )
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey(Store.__table__.c.id, ondelete="RESTRICT"),
    )
    suggestion_id: Mapped[int | None] = mapped_column(
        ForeignKey("fi_employee_suggestions.id", ondelete="RESTRICT"),
    )
    review_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("fi_review_decisions.id", ondelete="RESTRICT"),
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    before_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    after_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )



class AdministrationOperation(ProposalBase):
    __tablename__ = "fi_administration_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    preview_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey(Principal.__table__.c.id, ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_operation_id: Mapped[str | None] = mapped_column(ForeignKey("fi_administration_operations.id", ondelete="RESTRICT"))
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdministrationOperationItem(ProposalBase):
    __tablename__ = "fi_administration_operation_items"
    __table_args__ = (
        ForeignKeyConstraint(["before_version_id","flavor_id"],["fi_classification_versions.id","fi_classification_versions.flavor_id"],ondelete="RESTRICT"),
        ForeignKeyConstraint(["restore_version_id","flavor_id"],["fi_classification_versions.id","fi_classification_versions.flavor_id"],ondelete="RESTRICT"),
        ForeignKeyConstraint(["after_version_id","flavor_id"],["fi_classification_versions.id","fi_classification_versions.flavor_id"],ondelete="RESTRICT"),
    )
    operation_id: Mapped[str] = mapped_column(ForeignKey("fi_administration_operations.id", ondelete="RESTRICT"), primary_key=True)
    flavor_id: Mapped[int] = mapped_column(ForeignKey(TouchscreenFlavor.__table__.c.id, ondelete="RESTRICT"), primary_key=True)
    before_version_id: Mapped[int | None] = mapped_column(BigInteger)
    restore_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    after_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    before_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    after_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    before_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    after_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AdministrationOperationReview(ProposalBase):
    __tablename__ = "fi_administration_operation_reviews"
    operation_id: Mapped[str] = mapped_column(ForeignKey("fi_administration_operations.id", ondelete="RESTRICT"), primary_key=True)
    review_group_id: Mapped[int] = mapped_column(ForeignKey("fi_suggestion_review_groups.id", ondelete="RESTRICT"), primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("fi_review_decisions.id", ondelete="RESTRICT"), unique=True, nullable=False)


PROPOSED_TABLES = tuple(ProposalBase.metadata.sorted_tables)
