"""Additive compatibility tables on the existing portal data authority.

No schema is installed at import or ordinary application startup. Explicit
development preparation uses the same FI file; production requires a migration.
"""
from sqlalchemy import (MetaData, Table, Column, String, Text, Integer, Boolean,
                        JSON, ForeignKey, UniqueConstraint, Index, CheckConstraint)

def metadata_for(storage):
    m = MetaData()
    for name in ("principals", "stores"):
        storage.table(name).to_metadata(m)
    Table("cp_schema", m, Column("id", Integer, primary_key=True),
          Column("version", Integer, nullable=False))
    Table("cp_manufacturers", m, Column("id", String(160), primary_key=True),
          Column("name", Text, nullable=False), Column("aliases", JSON, nullable=False))
    Table("cp_products", m,
          Column("id", String(160), primary_key=True),
          Column("manufacturer_id", ForeignKey("cp_manufacturers.id", ondelete="RESTRICT"), nullable=False),
          Column("name", Text, nullable=False), Column("kind", String(20), nullable=False),
          Column("model", Text, nullable=False), Column("family", Text, nullable=False),
          Column("generation", Text, nullable=False), Column("resistance_ohm", String(30)),
          Column("capacity_ml", String(30)), Column("configuration", Text, nullable=False),
          Column("integrated_coil", Boolean, nullable=False), Column("aliases", JSON, nullable=False),
          Column("search_text", Text, nullable=False, index=True), Column("evidence", JSON, nullable=False),
          Column("version", Integer, nullable=False), Column("active", Boolean, nullable=False),
          CheckConstraint("kind IN ('device','pod','coil','tank','family')"),
          CheckConstraint("version > 0"))
    Table("cp_identifiers", m, Column("id", String(160), primary_key=True),
          Column("product_id", ForeignKey("cp_products.id", ondelete="RESTRICT"), nullable=False, index=True),
          Column("kind", String(20), nullable=False), Column("value", String(512), nullable=False),
          Column("exact_key", String(512), nullable=False, index=True),
          Column("gtin_key", String(40), index=True),
          Column("package_configuration", Text, nullable=False), Column("evidence", JSON, nullable=False),
          Column("active", Boolean, nullable=False, server_default="1"),
          UniqueConstraint("product_id", "kind", "value", "package_configuration"))
    Table("cp_relationships", m, Column("id", String(160), primary_key=True),
          Column("from_id", ForeignKey("cp_products.id", ondelete="RESTRICT"), nullable=False, index=True),
          Column("to_id", ForeignKey("cp_products.id", ondelete="RESTRICT"), nullable=False, index=True),
          Column("kind", String(30), nullable=False), Column("conditions", Text, nullable=False),
          Column("evidence", JSON, nullable=False), Column("active", Boolean, nullable=False),
          Column("version", Integer, nullable=False),
          CheckConstraint("from_id <> to_id"),
          CheckConstraint("kind IN ('accepts_pod','accepts_coil','includes_tank')"))
    Table("cp_catalog_associations", m, Column("id", String(160), primary_key=True),
          Column("product_id", ForeignKey("cp_products.id", ondelete="RESTRICT"), nullable=False, index=True),
          Column("square_variation_id", String(255), nullable=False, unique=True),
          Column("package_configuration", Text, nullable=False),
          Column("verification_note", Text, nullable=False), Column("evidence", JSON, nullable=False),
          Column("active", Boolean, nullable=False, server_default="1"))
    Table("cp_suggestions", m, Column("id", Integer, primary_key=True, autoincrement=True),
          Column("employee_id", ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False),
          Column("store_id", ForeignKey("stores.id", ondelete="RESTRICT")),
          Column("kind", String(30), nullable=False), Column("product_id", String(160)),
          Column("target_product_id", String(160)), Column("base_version", Integer),
          Column("proposed", JSON, nullable=False), Column("original", JSON, nullable=False),
          Column("scanned_identifier", Text), Column("comment", Text, nullable=False),
          Column("fingerprint", String(64), nullable=False, index=True),
          Column("status", String(30), nullable=False, index=True),
          Column("submitted_at", String(50), nullable=False),
          Column("decided_at", String(50)), Column("decided_by", ForeignKey("principals.id", ondelete="RESTRICT")),
          Column("decision_reason", Text), Column("published", JSON),
          CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','MODIFIED_APPROVED')"))
    Table("cp_audit", m, Column("id", Integer, primary_key=True, autoincrement=True),
          Column("event", String(30), nullable=False), Column("actor_id", Integer),
          Column("suggestion_id", ForeignKey("cp_suggestions.id", ondelete="RESTRICT")),
          Column("product_id", String(160)), Column("before", JSON), Column("after", JSON),
          Column("created_at", String(50), nullable=False))
    Table("cp_seed_imports", m, Column("digest", String(64), primary_key=True),
          Column("counts", JSON, nullable=False), Column("created_at", String(50), nullable=False))
    Index("cp_pending_identity", m.tables["cp_suggestions"].c.employee_id,
          m.tables["cp_suggestions"].c.fingerprint, m.tables["cp_suggestions"].c.status)
    return m

def initialize_compatibility(storage, seed_path=None):
    from sqlalchemy import select, insert
    metadata = metadata_for(storage)
    with storage._write() as c:
        for table in metadata.sorted_tables:
            if table.name.startswith("cp_"):
                table.create(c, checkfirst=True)
        for name in ("cp_identifiers","cp_catalog_associations"):
            columns={row[1] for row in c.exec_driver_sql("PRAGMA table_info("+name+")")}
            if "active" not in columns:
                c.exec_driver_sql("ALTER TABLE "+name+" ADD COLUMN active BOOLEAN NOT NULL DEFAULT 1")
        marker = metadata.tables["cp_schema"]
        version = c.scalar(select(marker.c.version))
        if version is None:
            c.execute(insert(marker).values(id=1, version=1))
        elif version != 1:
            raise ValueError("Unsupported compatibility schema.")
        # SQL statement tables are fixed, never derived from employee input.
        for table in ("cp_audit", "cp_seed_imports"):
            for action in ("UPDATE", "DELETE"):
                c.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS " + table + "_" + action.lower()
                    + " BEFORE " + action + " ON " + table
                    + " BEGIN SELECT RAISE(ABORT,'Compatibility audit history is immutable'); END")
        c.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS cp_original_suggestion_guard "
            "BEFORE UPDATE ON cp_suggestions WHEN OLD.employee_id<>NEW.employee_id "
            "OR OLD.kind<>NEW.kind OR OLD.proposed<>NEW.proposed OR OLD.original<>NEW.original "
            "OR OLD.fingerprint<>NEW.fingerprint OR OLD.submitted_at<>NEW.submitted_at "
            "OR OLD.product_id IS NOT NEW.product_id OR OLD.target_product_id IS NOT NEW.target_product_id "
            "OR OLD.base_version IS NOT NEW.base_version OR OLD.store_id IS NOT NEW.store_id "
            "OR OLD.scanned_identifier IS NOT NEW.scanned_identifier OR OLD.comment<>NEW.comment "
            "BEGIN SELECT RAISE(ABORT,'Original compatibility suggestion is immutable'); END")
        c.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS cp_suggestion_delete_guard "
            "BEFORE DELETE ON cp_suggestions BEGIN SELECT RAISE(ABORT,'Compatibility suggestions are retained'); END")
        c.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS cp_decision_guard "
            "BEFORE UPDATE ON cp_suggestions WHEN OLD.status<>'PENDING' "
            "BEGIN SELECT RAISE(ABORT,'Compatibility decisions are immutable'); END")
    if seed_path:
        from .store import CompatibilityService
        CompatibilityService(storage).import_seed(seed_path)
    return metadata
