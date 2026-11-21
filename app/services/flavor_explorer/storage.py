"""Executable developer-local Flavor Intelligence storage, isolated from production.

No URLs/engines are accepted from callers. SQLite files must live in this launched
session's persistent home, or explicitly in its disposable test-fixture TMPDIR.
This adapter exposes no HTTP routes and never calls Square.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re

from sqlalchemy import (
    BigInteger, Boolean, Column, ForeignKey, Integer, MetaData, String, Table, Text,
    create_engine, event, func, insert, select, update,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.engine import URL

from app.models import Principal, Store, TouchscreenFlavor, TouchscreenFlavorCategory
from app.services.flavor_explorer.schema import PROPOSED_TABLES

SCHEMA_VERSION = 2
SESSION_ROOT = Path("/Volumes/AvaOne-Development-Retained/sessions")
ADMIN_ROLES = frozenset({"ADMIN", "MANAGER"})
SNAPSHOT_FIELDS = frozenset({"description", "notes", "categories", "sensory", "similarities"})
IMMUTABLE_TABLES = (
    "fi_notes", "fi_sensory_axes", "fi_classification_versions",
    "fi_version_notes", "fi_version_categories", "fi_version_sensory",
    "fi_version_similarities", "fi_employee_suggestions", "fi_review_decisions",
    "fi_publication_revisions", "fi_audit_events",
    "fi_administration_operations", "fi_administration_operation_items", "fi_administration_operation_reviews",
)
VERSION_CONTENT = (
    "fi_version_notes", "fi_version_categories", "fi_version_sensory",
    "fi_version_similarities",
)


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validated_path(path, disposable):
    value = str(path)
    if "://" in value or value == ":memory:":
        raise ValueError("An explicit local development SQLite filename is required")
    session_id = os.environ.get("AVA_BOUNDED_SESSION", "")
    if not re.fullmatch(r"AVA-ERUPTED-[A-Za-z0-9_-]{1,60}", session_id):
        raise ValueError("A registered Erupted development session is required")
    session = SESSION_ROOT / session_id
    expected_home = (session / "persistent/home").resolve()
    if Path.home().resolve() != expected_home:
        raise ValueError("Development home differs from the registered session")
    allowed = expected_home
    if disposable:
        expected_tmp = (session / "disposable/tmp").resolve()
        if Path(os.environ.get("TMPDIR", "")).resolve() != expected_tmp:
            raise ValueError("Test fixtures require the inherited registered TMPDIR")
        allowed = expected_tmp
    result = Path(path).expanduser().resolve()
    if result.suffix not in {".sqlite", ".sqlite3"} or not result.is_relative_to(allowed):
        raise ValueError("Database must be a SQLite file within the selected session storage")
    if Path(path).expanduser().is_symlink():
        raise ValueError("A database symlink is not a development target")
    return result


def _local_metadata():
    # Copy the four existing table definitions first, then project only identity
    # columns into a fresh local metadata. No app.models table object is changed.
    reference = MetaData()
    projections = (
        (Store.__table__, ("id", "name", "square_location_id")),
        (Principal.__table__, ("id", "username", "role", "active", "store_id")),
        (TouchscreenFlavor.__table__, ("id",)),
        (TouchscreenFlavorCategory.__table__, ("id", "name", "slug", "category_type")),
    )
    for source, _columns in projections:
        source.to_metadata(reference)
    metadata = MetaData()
    for source, names in projections:
        copied = reference.tables[source.name]
        columns = []
        for name in names:
            original = copied.c[name]
            column_type = original.type.copy()
            if isinstance(column_type, CITEXT):
                column_type = Text()
            elif isinstance(column_type, BigInteger):
                column_type = column_type.with_variant(Integer(), "sqlite")
            foreign_keys = []
            if source.name == "principals" and name == "store_id":
                foreign_keys.append(ForeignKey("stores.id", ondelete="RESTRICT"))
            columns.append(Column(
                name, column_type, *foreign_keys, primary_key=original.primary_key,
                nullable=original.nullable, unique=bool(original.unique),
            ))
        Table(source.name, metadata, *columns)
    for proposed in PROPOSED_TABLES:
        copied = proposed.to_metadata(metadata)
        for column in copied.c:
            if isinstance(column.type, BigInteger):
                column.type = column.type.with_variant(Integer(), "sqlite")
    Table("fi_development_schema", metadata,
          Column("singleton", Integer, primary_key=True),
          Column("version", Integer, nullable=False))
    return metadata


def _trigger(connection, name, timing, table, body):
    # All identifiers are internal constants, never caller input.
    connection.exec_driver_sql(
        f'CREATE TRIGGER IF NOT EXISTS "{name}" {timing} ON "{table}" BEGIN {body} END'
    )


def _install_triggers(connection):
    for table in IMMUTABLE_TABLES:
        for action in ("UPDATE", "DELETE"):
            _trigger(connection, table + "_no_" + action.lower(),
                     "BEFORE " + action, table,
                     "SELECT RAISE(ABORT, 'Flavor Intelligence history is append-only');")
    for table in VERSION_CONTENT:
        _trigger(connection, table + "_published_frozen", "BEFORE INSERT", table,
                 "SELECT CASE WHEN EXISTS (SELECT 1 FROM fi_publication_revisions "
                 "WHERE version_id = NEW.version_id) THEN "
                 "RAISE(ABORT, 'Published version content is frozen') END;")
    for table, actor in (("fi_administration_operations", "actor_id"),
                         ("fi_review_decisions", "reviewer_id"),
                         ("fi_publication_revisions", "reviewed_by_principal_id")):
        _trigger(connection, table + "_admin_required", "BEFORE INSERT", table,
                 f"SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.{actor} "
                 "AND role IN ('ADMIN','MANAGER') AND active=1) THEN "
                 "RAISE(ABORT, 'Active administrator required') END;")
    connection.exec_driver_sql("DROP TRIGGER IF EXISTS fi_classification_versions_admin_required")
    _trigger(connection, "fi_classification_versions_admin_required", "BEFORE INSERT",
             "fi_classification_versions",
             "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.created_by_principal_id "
             "AND active=1 AND (role IN ('ADMIN','MANAGER') OR NEW.is_baseline=1)) THEN "
             "RAISE(ABORT, 'Active administrator required') END;")
    _trigger(connection, "fi_unclassified_baseline_empty", "BEFORE INSERT",
             "fi_classification_versions",
             "SELECT CASE WHEN NEW.is_baseline=1 AND (NEW.description<>'' OR "
             "COALESCE(json_valid(NEW.snapshot),0)<>1 OR "
             "COALESCE(json_type(NEW.snapshot,'$.description'),'invalid')<>'text' OR "
             "COALESCE(json_type(NEW.snapshot,'$.notes'),'invalid')<>'array' OR "
             "COALESCE(json_type(NEW.snapshot,'$.categories'),'invalid')<>'array' OR "
             "COALESCE(json_type(NEW.snapshot,'$.similarities'),'invalid')<>'array' OR "
             "COALESCE(json_type(NEW.snapshot,'$.sensory'),'invalid')<>'object' OR "
             "(SELECT count(*) FROM json_each(NEW.snapshot))<>5 OR "
             "COALESCE(json_extract(NEW.snapshot,'$.description'),'invalid')<>'' OR "
             "COALESCE(json_array_length(json_extract(NEW.snapshot,'$.notes')),-1)<>0 OR "
             "COALESCE(json_array_length(json_extract(NEW.snapshot,'$.categories')),-1)<>0 OR "
             "COALESCE(json_array_length(json_extract(NEW.snapshot,'$.similarities')),-1)<>0 OR "
             "COALESCE(json_extract(NEW.snapshot,'$.sensory'),'invalid')<>'{}') THEN "
             "RAISE(ABORT, 'Unclassified baseline must be empty') END;")
    _trigger(connection, "fi_group_pending_initial", "BEFORE INSERT",
             "fi_suggestion_review_groups",
             "SELECT CASE WHEN NEW.status <> 'PENDING' THEN "
             "RAISE(ABORT, 'New review groups must be pending') END;")
    _trigger(connection, "fi_group_decision_transition", "BEFORE UPDATE",
             "fi_suggestion_review_groups",
             "SELECT CASE WHEN OLD.status <> 'PENDING' OR NEW.id <> OLD.id "
             "OR NEW.flavor_id <> OLD.flavor_id OR NEW.base_version_id <> OLD.base_version_id "
             "OR NEW.patch_fingerprint <> OLD.patch_fingerprint OR NEW.created_at <> OLD.created_at "
             "OR NOT EXISTS (SELECT 1 FROM fi_review_decisions WHERE review_group_id=OLD.id "
             "AND decision=NEW.status) THEN "
             "RAISE(ABORT, 'Only a matching final review decision may close a group') END;")
    _trigger(connection, "fi_group_no_delete", "BEFORE DELETE",
             "fi_suggestion_review_groups",
             "SELECT RAISE(ABORT, 'Review attribution is retained');")
    _trigger(connection, "fi_submission_pending_employee", "BEFORE INSERT",
             "fi_employee_suggestions",
             "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM fi_suggestion_review_groups "
             "WHERE id=NEW.review_group_id AND status='PENDING') "
             "OR NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.employee_id AND active=1) "
             "THEN RAISE(ABORT, 'Suggestion requires active employee and pending group') END;")
    _trigger(connection, "fi_review_pending", "BEFORE INSERT", "fi_review_decisions",
             "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM fi_suggestion_review_groups "
             "WHERE id=NEW.review_group_id AND status='PENDING') THEN "
             "RAISE(ABORT, 'Review group is closed') END;")
    _trigger(connection, "fi_publication_chain", "BEFORE INSERT",
             "fi_publication_revisions",
             "SELECT CASE WHEN NEW.revision <> COALESCE((SELECT MAX(revision) FROM "
             "fi_publication_revisions WHERE flavor_id=NEW.flavor_id),0)+1 THEN "
             "RAISE(ABORT, 'Publication revision must extend current history') END; "
             "SELECT CASE WHEN NEW.review_decision_id IS NOT NULL AND NOT EXISTS "
             "(SELECT 1 FROM fi_review_decisions WHERE id=NEW.review_decision_id "
             "AND flavor_id=NEW.flavor_id AND published_version_id=NEW.version_id "
             "AND decision IN ('APPROVED','MODIFIED_APPROVED')) THEN "
             "RAISE(ABORT, 'Review decision and publication must agree') END; "
             "SELECT CASE WHEN NEW.action='ROLLBACK' AND NOT EXISTS "
             "(SELECT 1 FROM fi_classification_versions WHERE id=NEW.version_id "
             "AND restores_version_id IS NOT NULL) THEN "
             "RAISE(ABORT, 'Rollback requires a new restoring version') END;")
    # Database-level audit also covers an accidental direct INSERT through Core.
    _trigger(connection, "fi_publication_audit", "AFTER INSERT",
             "fi_publication_revisions",
             "INSERT INTO fi_audit_events (flavor_id,version_id,actor_id,event_type,"
             "before_snapshot,after_snapshot,reason) VALUES (NEW.flavor_id,NEW.version_id,"
             "NEW.reviewed_by_principal_id,CASE NEW.action WHEN 'ROLLBACK' THEN 'ROLLED_BACK' "
             "WHEN 'DIRECT_EDIT' THEN 'DIRECT_EDIT' ELSE 'PUBLISHED' END,"
             "COALESCE((SELECT v.snapshot FROM fi_publication_revisions p "
             "JOIN fi_classification_versions v ON v.id=p.version_id "
             "WHERE p.flavor_id=NEW.flavor_id AND p.revision<NEW.revision "
             "ORDER BY p.revision DESC LIMIT 1),'{}'),"
             "(SELECT snapshot FROM fi_classification_versions WHERE id=NEW.version_id),NEW.reason);")
    _trigger(connection, "fi_submission_audit", "AFTER INSERT",
             "fi_employee_suggestions",
             "INSERT INTO fi_audit_events (flavor_id,actor_id,store_id,suggestion_id,"
             "event_type,before_snapshot,after_snapshot,reason) VALUES "
             "(NEW.flavor_id,NEW.employee_id,NEW.store_id,NEW.id,'SUBMITTED',"
             "NEW.original_snapshot,NEW.proposed_patch,COALESCE(NEW.comment,'Employee suggestion'));")
    _trigger(connection, "fi_review_audit", "AFTER INSERT", "fi_review_decisions",
             "INSERT INTO fi_audit_events (flavor_id,version_id,actor_id,review_decision_id,"
             "event_type,before_snapshot,after_snapshot,reason) VALUES "
             "(NEW.flavor_id,NEW.published_version_id,NEW.reviewer_id,NEW.id,'REVIEWED',"
             "(SELECT snapshot FROM fi_classification_versions WHERE id=NEW.base_version_id),"
             "CASE WHEN NEW.published_snapshot IS NULL OR NEW.published_snapshot='null' THEN "
             "NEW.proposed_patch_snapshot ELSE NEW.published_snapshot END,NEW.reason);")


def initialize(path, *, disposable=False):
    """Create/reopen this session's local FI database. Never install production schema."""
    target = _validated_path(path, disposable)
    existed = target.exists() and target.stat().st_size > 0
    target.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(target)))
    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=3000")
        cursor.close()
    metadata = _local_metadata()
    try:
        with engine.begin() as connection:
            # pysqlite legacy mode does not begin transactions for SELECT/DDL.
            # Enclose the additive migration and guard replacement explicitly.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            marker_exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fi_development_schema'"
            ).first()
            if existed and not marker_exists:
                raise ValueError("Existing unrecognized database is protected")
            if marker_exists:
                version = connection.execute(select(metadata.tables["fi_development_schema"].c.version)).scalar_one()
                if version not in (1, SCHEMA_VERSION):
                    raise ValueError("Unsupported local Flavor Intelligence schema version")
                if version == 1:
                    columns={row[1] for row in connection.exec_driver_sql("PRAGMA table_info(fi_classification_versions)")}
                    if "is_baseline" not in columns:
                        connection.exec_driver_sql("ALTER TABLE fi_classification_versions ADD COLUMN is_baseline BOOLEAN NOT NULL DEFAULT 0")
            # An explicit bounded table list, never production Base.metadata.create_all().
            for table in metadata.sorted_tables:
                table.create(connection, checkfirst=True)
            if not marker_exists:
                connection.execute(insert(metadata.tables["fi_development_schema"]).values(
                    singleton=1, version=SCHEMA_VERSION))
            for index in metadata.tables["fi_classification_versions"].indexes:
                index.create(connection,checkfirst=True)
            _install_triggers(connection)
            if marker_exists:
                connection.execute(update(metadata.tables["fi_development_schema"]).values(version=SCHEMA_VERSION))
        target.chmod(0o600)
        storage = FlavorStorage(engine, metadata, target)
        from app.services.flavor_explorer.discovery import initialize_discovery
        initialize_discovery(storage)
        from app.services.flavor_explorer.product_photos import initialize_product_photos
        initialize_product_photos(storage)
        return storage
    except BaseException:
        engine.dispose()
        raise



def open_existing(path, *, disposable=False, reference=None):
    """Open an already initialized local FI database with SQLite's read-only URI."""
    if reference is not None:
        raise ValueError("Registered local preview references are not supported by the hosted release.")
    else:
        target = _validated_path(path, disposable)
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError("Initialized local Flavor Intelligence database is required")
    engine = create_engine(URL.create(
        "sqlite", database="file:" + str(target), query={"mode": "ro", "uri": "true"},
    ))
    @event.listens_for(engine, "connect")
    def configure_read_only(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA query_only=ON")
        cursor.execute("PRAGMA busy_timeout=3000")
        cursor.close()
    metadata = _local_metadata()
    try:
        with engine.connect() as connection:
            marker = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fi_development_schema'"
            ).first()
            if not marker:
                raise ValueError("Existing unrecognized database is protected")
            version = connection.execute(select(
                metadata.tables["fi_development_schema"].c.version)).scalar_one()
            if version != SCHEMA_VERSION:
                raise ValueError("Unsupported local Flavor Intelligence schema version")
        return FlavorStorage(engine, metadata, target)
    except BaseException:
        engine.dispose()
        raise

def _snapshot(value):
    if not isinstance(value, dict) or set(value) - SNAPSHOT_FIELDS:
        raise ValueError("Only flavor classification fields may be published")
    description = value.get("description", "")
    notes = value.get("notes", [])
    categories = value.get("categories", [])
    sensory = value.get("sensory", {})
    similarities = value.get("similarities", [])
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("Invalid flavor description")
    if not isinstance(notes, list) or not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9-]{1,80}", x) for x in notes):
        raise ValueError("Notes must be normalized flavor slugs")
    if not isinstance(categories, list) or not all(type(x) is int and x > 0 for x in categories):
        raise ValueError("Categories must reference existing category identities")
    if not isinstance(sensory, dict) or not all(
        isinstance(k, str) and re.fullmatch(r"[a-z0-9-]{1,80}", k)
        and type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
        for k, v in sensory.items()
    ):
        raise ValueError("Sensory attributes must be finite values from 0 to 1")
    if not isinstance(similarities, list):
        raise ValueError("Invalid similarity relationships")
    clean_similarities = []
    for item in similarities:
        if not isinstance(item, dict) or set(item) != {"flavor_id", "weight", "reason"}:
            raise ValueError("Similarity fields are flavor_id, weight and reason")
        if type(item["flavor_id"]) is not int or item["flavor_id"] <= 0:
            raise ValueError("Similarity requires a flavor identity")
        if type(item["weight"]) not in (int, float) or not math.isfinite(item["weight"]) or not 0 < item["weight"] <= 1:
            raise ValueError("Similarity weight must be from 0 exclusive to 1 inclusive")
        if not isinstance(item["reason"], str):
            raise ValueError("Similarity requires a reason")
        clean_similarities.append(dict(item))
    if len({x["flavor_id"] for x in clean_similarities}) != len(clean_similarities):
        raise ValueError("Duplicate similarity relationship")
    return json.loads(_json(dict(
        description=description, notes=sorted(set(notes)), categories=sorted(set(categories)),
        sensory=sensory, similarities=sorted(clean_similarities, key=lambda x: x["flavor_id"]),
    )))


class FlavorStorage:
    def __init__(self, engine, metadata, path):
        self.engine, self.metadata, self.path = engine, metadata, path

    def close(self):
        self.engine.dispose()

    def table(self, name):
        return self.metadata.tables[name]

    @contextmanager
    def _write(self):
        # Serialize baseline comparison + version/revision assignment in SQLite.
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _principal(self, connection, principal_id, admin=False):
        if type(principal_id) is not int or principal_id <= 0:raise PermissionError("Authenticated principal identity required")
        row = connection.execute(select(self.table("principals")).where(
            self.table("principals").c.id == principal_id)).mappings().first()
        if not row or not row["active"] or (admin and row["role"] not in ADMIN_ROLES):
            raise PermissionError("An active administrator is required" if admin else "An active employee is required")
        return row

    def mirror_store(self, *, store_id, name, square_location_id):
        """Mirror existing location identity; never write to Square."""
        with self._write() as connection:
            table = self.table("stores")
            existing = connection.execute(select(table).where(table.c.id == store_id)).mappings().first()
            values = dict(id=store_id, name=name, square_location_id=square_location_id)
            if existing:
                if dict(existing) != values:
                    raise ValueError("Existing local location identity differs")
            else:
                connection.execute(insert(table).values(**values))

    def mirror_principal(self, *, principal_id, username, role, store_id=None, active=True):
        """Explicit local identity mirror for tests/existing staff; never auto-provisioned."""
        if role not in {"ADMIN", "MANAGER", "LEAD", "STORE"}:
            raise ValueError("Unknown existing Erupted role")
        with self._write() as connection:
            table = self.table("principals")
            if connection.execute(select(table.c.id).where(table.c.id == principal_id)).first():
                raise ValueError("Principal already mirrored; identity changes require reviewed synchronization")
            connection.execute(insert(table).values(
                id=principal_id, username=username, role=role, store_id=store_id, active=active))

    def mirror_square_identity(self, square_item_id, square_variation_ids, *, observed_at=None):
        """Create only a stable local flavor identity and external Square ID associations."""
        if not isinstance(square_item_id, str) or not square_item_id or not square_variation_ids:
            raise ValueError("Square item and variation identities are required")
        ids = sorted(set(square_variation_ids))
        if not all(isinstance(x, str) and x for x in ids):
            raise ValueError("Invalid Square variation identity")
        with self._write() as connection:
            links, flavors = self.table("fi_square_flavor_links"), self.table("touchscreen_flavors")
            linked = connection.execute(select(links).where(
                links.c.square_variation_id.in_(ids))).mappings().all()
            flavor_ids = {row["flavor_id"] for row in linked}
            if len(flavor_ids) > 1:
                raise ValueError("Supplied variations span different branded flavor associations")
            flavor_id = next(iter(flavor_ids)) if flavor_ids else connection.execute(insert(flavors).values()).inserted_primary_key[0]
            for variation_id in ids:
                existing = connection.execute(select(links).where(
                    links.c.square_variation_id == variation_id)).mappings().first()
                if existing:
                    if existing["square_item_id"] != square_item_id or existing["flavor_id"] != flavor_id:
                        raise ValueError("Square variation identity may not be silently reassigned")
                else:
                    connection.execute(insert(links).values(
                        square_variation_id=variation_id, square_item_id=square_item_id,
                        flavor_id=flavor_id, observed_at=observed_at or _now()))
            return flavor_id

    def mirror_category(self, *, category_id, name, slug, category_type="BROAD"):
        if (type(category_id) is not int or category_id <= 0 or not isinstance(name, str)
                or not name.strip() or not isinstance(slug, str) or not slug.strip()
                or category_type not in {"BROAD", "FRUIT", "OTHER_NOTE"}):
            raise ValueError("Invalid existing taxonomy identity")
        values = dict(id=category_id, name=name, slug=slug, category_type=category_type)
        with self._write() as connection:
            table = self.table("touchscreen_flavor_categories")
            existing = connection.execute(select(table).where(
                (table.c.id == category_id) | ((func.lower(table.c.slug) == slug.casefold())
                                            & (table.c.category_type == category_type))
            )).mappings().all()
            if existing:
                if len(existing) != 1 or dict(existing[0]) != values:
                    raise ValueError("Existing taxonomy ID or slug conflicts; explicit reconciliation required")
            else:
                connection.execute(insert(table).values(**values))

    def _current(self, connection, flavor_id):
        publications, versions = self.table("fi_publication_revisions"), self.table("fi_classification_versions")
        return connection.execute(select(
            publications.c.revision, versions.c.id.label("version_id"), versions.c.snapshot, versions.c.is_baseline,
        ).join(versions, versions.c.id == publications.c.version_id).where(
            publications.c.flavor_id == flavor_id).order_by(publications.c.revision.desc()).limit(1)).mappings().first()

    def _version(self, connection, *, flavor_id, snapshot, reviewer_id, reason, restores_version_id=None, is_baseline=False):
        current = self._current(connection, flavor_id)
        versions = self.table("fi_classification_versions")
        number = connection.execute(select(func.coalesce(func.max(versions.c.version), 0)).where(
            versions.c.flavor_id == flavor_id)).scalar_one() + 1
        version_id = connection.execute(insert(versions).values(
            flavor_id=flavor_id, version=number,
            parent_version_id=current["version_id"] if current else None,
            restores_version_id=restores_version_id, is_baseline=is_baseline, description=snapshot["description"],
            snapshot=snapshot, created_by_principal_id=reviewer_id, reason=reason,
        )).inserted_primary_key[0]
        notes, axes = self.table("fi_notes"), self.table("fi_sensory_axes")
        for slug in snapshot["notes"]:
            note_id = connection.execute(select(notes.c.id).where(notes.c.slug == slug)).scalar_one_or_none()
            if note_id is None:
                note_id = connection.execute(insert(notes).values(slug=slug, label=slug.replace("-", " ").title())).inserted_primary_key[0]
            connection.execute(insert(self.table("fi_version_notes")).values(version_id=version_id, note_id=note_id))
        for category_id in snapshot["categories"]:
            connection.execute(insert(self.table("fi_version_categories")).values(version_id=version_id, category_id=category_id))
        for slug, value in snapshot["sensory"].items():
            if not connection.execute(select(axes.c.slug).where(axes.c.slug == slug)).first():
                connection.execute(insert(axes).values(slug=slug, label=slug.title(), description="Normalized 0 to 1 sensory intensity"))
            connection.execute(insert(self.table("fi_version_sensory")).values(version_id=version_id, axis_slug=slug, value=value))
        for related in snapshot["similarities"]:
            connection.execute(insert(self.table("fi_version_similarities")).values(
                version_id=version_id, flavor_id=flavor_id, related_flavor_id=related["flavor_id"],
                weight=related["weight"], reason=related["reason"]))
        return version_id, (current["revision"] + 1 if current else 1)

    def _publication(self, connection, *, flavor_id, version_id, revision, reviewer_id, reason, action, decision_id=None):
        connection.execute(insert(self.table("fi_publication_revisions")).values(
            flavor_id=flavor_id, version_id=version_id, revision=revision, action=action,
            reviewed_by_principal_id=reviewer_id, reason=reason, review_decision_id=decision_id))

    def publish_classification(self, *, flavor_id, snapshot, reviewer_id, reason, expected_version_id=None):
        """Developer foundation method; callers must supply authenticated staff identity."""
        clean = _snapshot(snapshot)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An audit reason is required")
        with self._write() as connection:
            self._principal(connection, reviewer_id, admin=True)
            current = self._current(connection, flavor_id)
            if expected_version_id != (current["version_id"] if current else None):
                raise ValueError("Published baseline changed; explicit rereview is required")
            version_id, revision = self._version(connection, flavor_id=flavor_id,
                snapshot=clean, reviewer_id=reviewer_id, reason=reason)
            self._publication(connection, flavor_id=flavor_id, version_id=version_id,
                revision=revision, reviewer_id=reviewer_id, reason=reason, action="DIRECT_EDIT")
            return version_id

    def submit_suggestion(self, *, flavor_id, employee_id, store_id, base_version_id, proposed_patch, comment=None):
        from app.services.flavor_explorer.administration import submit_suggestion
        return submit_suggestion(self,flavor_id=flavor_id,employee_id=employee_id,store_id=store_id,
                                 base_version_id=base_version_id,proposed_patch=proposed_patch,comment=comment)

    def review_suggestion(self, *, review_group_id, reviewer_id, decision, reason, modified_patch=None):
        from app.services.flavor_explorer.administration import FlavorAdministration
        service=FlavorAdministration(self)
        plan=service.preview_review(actor_id=reviewer_id,group_ids=[review_group_id],decision=decision,
             modified_patches={str(review_group_id):modified_patch} if modified_patch is not None else None,reason=reason)
        receipt=service.commit(actor_id=reviewer_id,plan=plan)
        return receipt["changes"][0]["afterVersionId"] if receipt["changes"] else None

    def rollback(self, *, flavor_id, restores_version_id, reviewer_id, reason, expected_version_id):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An audit reason is required")
        with self._write() as connection:
            self._principal(connection, reviewer_id, admin=True)
            current = self._current(connection, flavor_id)
            if not current or current["version_id"] != expected_version_id:
                raise ValueError("Published baseline changed; explicit rereview is required")
            publications, versions = self.table("fi_publication_revisions"), self.table("fi_classification_versions")
            old = connection.execute(select(versions.c.snapshot).join(
                publications, publications.c.version_id == versions.c.id).where(
                versions.c.id == restores_version_id, versions.c.flavor_id == flavor_id)).scalar_one()
            version_id, revision = self._version(connection, flavor_id=flavor_id,
                snapshot=old, reviewer_id=reviewer_id, reason=reason,
                restores_version_id=restores_version_id)
            self._publication(connection, flavor_id=flavor_id, version_id=version_id,
                revision=revision, reviewer_id=reviewer_id, reason=reason, action="ROLLBACK")
            return version_id

    def public_classification_for_square(self, variation_ids):
        """Read current approved profiles in bounded batches, never pending suggestions."""
        ids = list(dict.fromkeys(variation_ids))
        if not ids:
            return {}
        links = self.table("fi_square_flavor_links")
        publications = self.table("fi_publication_revisions")
        versions = self.table("fi_classification_versions")
        categories = self.table("touchscreen_flavor_categories")
        latest = select(
            publications.c.flavor_id,
            func.max(publications.c.revision).label("revision"),
        ).group_by(publications.c.flavor_id).subquery()
        # Keep SQLite bind counts below its historical 999-variable ceiling.
        batch_size = 800
        rows, profiles, category_ids = [], {}, set()
        with self.engine.connect() as connection:
            for offset in range(0, len(ids), batch_size):
                statement = select(
                    links.c.square_variation_id, links.c.flavor_id,
                    versions.c.id.label("version_id"), versions.c.snapshot,
                    publications.c.revision,
                ).select_from(links).join(
                    latest, latest.c.flavor_id == links.c.flavor_id,
                ).join(
                    publications,
                    (publications.c.flavor_id == latest.c.flavor_id)
                    & (publications.c.revision == latest.c.revision),
                ).join(
                    versions, versions.c.id == publications.c.version_id,
                ).where(links.c.square_variation_id.in_(ids[offset:offset + batch_size]))
                for row in connection.execute(statement).mappings():
                    rows.append((row["square_variation_id"], row["flavor_id"]))
                    if row["flavor_id"] not in profiles:
                        profiles[row["flavor_id"]] = {
                            "snapshot": row["snapshot"], "version_id": row["version_id"],
                            "revision": row["revision"],
                        }
                        category_ids.update(row["snapshot"]["categories"])
            category_slugs = {}
            ordered_category_ids = sorted(category_ids)
            for offset in range(0, len(ordered_category_ids), batch_size):
                category_slugs.update(connection.execute(select(
                    categories.c.id, categories.c.slug,
                ).where(categories.c.id.in_(
                    ordered_category_ids[offset:offset + batch_size],
                ))).all())
            related_variations = {}
            related_ids=sorted({item["flavor_id"] for profile in profiles.values() for item in profile["snapshot"].get("similarities",[])})
            for offset in range(0,len(related_ids),batch_size):
                for related_id,variation_id in connection.execute(select(links.c.flavor_id,links.c.square_variation_id).where(
                        links.c.flavor_id.in_(related_ids[offset:offset+batch_size]))):
                    related_variations.setdefault(related_id,[]).append(variation_id)
        public_profiles = {}
        for flavor_id, profile in profiles.items():
            snapshot = profile["snapshot"]
            if not any(snapshot.get(key) for key in SNAPSHOT_FIELDS):
                continue
            public_profiles[flavor_id] = {
                "description": snapshot["description"], "notes": snapshot["notes"],
                "categories": sorted({"fruity":"fruit","mint-menthol":"legacy-mint-ice","mint-menthol-identity":"mint","ice-freeze":"ice"}.get(category_slugs[id_],category_slugs[id_]) for id_ in snapshot["categories"]),
                "sensory": snapshot["sensory"], "sensoryScale": 1,
                "similarities": [{"squareVariationIds":sorted(related_variations.get(item["flavor_id"],[])),
                    "weight":item["weight"],"reason":item["reason"]} for item in snapshot.get("similarities",[])
                    if related_variations.get(item["flavor_id"])],
                "classificationStatus": "approved",
                "classificationVersionId": profile["version_id"],
                "publicationRevision": profile["revision"],
            }
        return {variation_id: public_profiles[flavor_id] for variation_id, flavor_id in rows if flavor_id in public_profiles}
