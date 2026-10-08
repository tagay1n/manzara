"""Normalized durable catalog schema, installed only through Alembic."""

import re

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index,
    Integer, MetaData, PrimaryKeyConstraint, Table, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB


def build_metadata(schema: str) -> MetaData:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid catalog schema")
    metadata = MetaData(schema=schema)

    def table(name, *columns, versioned=False, **kwargs):
        if versioned:
            columns += (Column("revision", BigInteger, nullable=False, server_default="1"),
                        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
        return Table("catalog_" + name, metadata, *columns, **kwargs)

    def identifier(name):
        return Column(name, BigInteger, primary_key=True)

    def foreign(name, target, *, nullable=False, ondelete="RESTRICT", onupdate=None, constraint_name=None):
        return Column(name, BigInteger, ForeignKey(f"{schema}.catalog_{target}", ondelete=ondelete,
                      onupdate=onupdate, name=constraint_name), nullable=nullable)

    table("collections", identifier("collection_id"), Column("title", Text, nullable=False),
          Column("notes", Text), Column("include_in_library", Boolean, nullable=False, server_default="true"),
          Column("metadata_template_json", Text, nullable=False, server_default="{}"),
          Column("applied_at", Text), Column("created_at", Text), Column("normalized_title", Text),
          Column("source_updated_at", Text), versioned=True)
    nodes = table("classification_nodes", identifier("node_id"), Column("ddc", Text, nullable=False),
                  foreign("parent_id", "classification_nodes.node_id", nullable=True),
                  Column("label_en", Text, nullable=False), Column("label_tt", Text),
                  CheckConstraint("parent_id IS NULL OR parent_id <> node_id", name="ck_catalog_node_parent"), versioned=True)
    Index("uq_catalog_root", nodes.c.ddc, nodes.c.label_en, unique=True, postgresql_where=nodes.c.parent_id.is_(None))
    Index("uq_catalog_branch", nodes.c.parent_id, nodes.c.label_en, unique=True, postgresql_where=nodes.c.parent_id.is_not(None))
    table("classifications", identifier("classification_id"), foreign("node_id", "classification_nodes.node_id"),
          Column("status", Text, nullable=False, server_default="pending"),
          Column("created_by", Text, nullable=False, server_default="gemini"), Column("created_at", DateTime),
          UniqueConstraint("node_id", deferrable=True), versioned=True)
    table("publications", identifier("publication_id"), Column("name", Text), Column("work_type", Text),
          Column("description", Text), Column("edition", Text), Column("date_published", Text),
          Column("page_count", Integer),
          Column("audience_array", Boolean, nullable=False, server_default="false"),
          Column("inclusion", Text, nullable=False, server_default="pending"),
          Column("evaluation_method", Text),
          foreign("classification_id", "classifications.classification_id", nullable=True),
          foreign("collection_id", "collections.collection_id", nullable=True),
          Column("collection_item_title", Text), Column("collection_created_at", Text), Column("collection_updated_at", Text),
          Column("has_metadata", Boolean, nullable=False, server_default="true"),
          Column("metadata_present", Boolean, nullable=False, server_default="true"),
          foreign("merged_into_id", "publications.publication_id", nullable=True),
          CheckConstraint("inclusion IN ('pending','included','excluded')"),
          CheckConstraint("page_count IS NULL OR page_count > 0"),
          CheckConstraint("merged_into_id IS NULL OR merged_into_id <> publication_id", name="ck_catalog_publication_merge_target"), versioned=True)
    docs = table("documents", Column("md5", Text, primary_key=True), foreign("publication_id", "publications.publication_id"),
                 Column("mime_type", Text), Column("complete", Boolean, nullable=False, server_default="true"),
                 Column("restricted", Boolean, nullable=False, server_default="false"),
                 Column("selected", Boolean, nullable=False, server_default="true"),
                 Column("content_extraction_method", Text), Column("meta_extraction_method", Text), versioned=True)
    Index("idx_catalog_documents_publication", docs.c.publication_id)
    table("locations", identifier("location_id"),
          Column("md5", Text, ForeignKey(docs.c.md5, ondelete="CASCADE"), nullable=False),
          Column("provider", Text, nullable=False), Column("purpose", Text, nullable=False),
          Column("locator", Text), Column("source_path", Text), Column("resource_id", Text),
          Column("public_url", Text), Column("public_key", Text), Column("size", BigInteger),
          Column("etag", Text), Column("verified_at", DateTime(timezone=True)),
          UniqueConstraint("md5", "provider", "purpose"), CheckConstraint("size IS NULL OR size >= 0"), versioned=True)
    entity_columns = [Column(name, Text) for name in (
        "surname_full", "surname_initials", "name_full", "name_initials", "father_name_full",
        "father_name_initials", "title", "sex", "identity_key", "notes",
    )]
    table("entities", identifier("entity_id"), Column("kind", Text, nullable=False),
          Column("display_name", Text, nullable=False), Column("approval", Text, nullable=False, server_default="unconfirmed"),
          Column("normalized_name", Text), Column("created_at", Text), Column("source_updated_at", Text),
          Column("status", Text, nullable=False, server_default="active"),
          foreign("merged_into_id", "entities.entity_id", nullable=True), *entity_columns,
          CheckConstraint("kind IN ('person','organization','unknown')"),
          CheckConstraint("approval IN ('unconfirmed','confirmed')"),
          CheckConstraint("status IN ('active','merged','archived')"),
          CheckConstraint("merged_into_id IS NULL OR merged_into_id <> entity_id", name="ck_catalog_entity_merge_target"),
          CheckConstraint("(status = 'merged') = (merged_into_id IS NOT NULL)", name="ck_catalog_entity_merge_status"), versioned=True)
    table("entity_roles", foreign("entity_id", "entities.entity_id"), Column("role", Text, nullable=False),
          PrimaryKeyConstraint("entity_id", "role"))
    table("names", identifier("name_id"), Column("kind", Text, nullable=False), Column("raw_name", Text, nullable=False),
          UniqueConstraint("kind", "raw_name"),
          CheckConstraint("kind IN ('person','organization','unknown')", name="ck_catalog_name_kind"))
    table("aliases", identifier("alias_id"), foreign("name_id", "names.name_id"), foreign("entity_id", "entities.entity_id"),
          Column("approval", Text, nullable=False, server_default="unconfirmed"),
          UniqueConstraint("name_id", "entity_id"),
          CheckConstraint("approval IN ('unconfirmed','confirmed')", name="ck_catalog_alias_approval"), versioned=True)
    table("alias_reviews", identifier("alias_id"), foreign("name_id", "names.name_id"),
          Column("entity_type", Text, nullable=False), foreign("entity_id", "entities.entity_id", nullable=True),
          Column("normalized_name", Text, nullable=False), Column("script_label", Text, nullable=False),
          Column("decision_status", Text, nullable=False, server_default="pending"),
          *[Column(name, BigInteger, nullable=False, server_default="0") for name in ("docs_count", "mentions_count", "marker_count")],
          Column("confidence", Float),
          *[Column(name, Text) for name in ("source", "reason", "created_at", "updated_at", "successful_model", "prompt_version", "schema_version")],
          *[Column(name, Text) for name in ("surname_full", "surname_initials", "name_full", "name_initials", "father_name_full", "father_name_initials", "title", "sex")],
          Column("source_roles", JSONB, nullable=False, server_default="[]"),
          UniqueConstraint("entity_type", "name_id"))
    table("credit_groups", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("role", Text, nullable=False), Column("position", Integer, nullable=False), Column("role_name", Text),
          PrimaryKeyConstraint("publication_id", "role", "position"),
          CheckConstraint("position >= 0", name="ck_catalog_credit_group_position"))
    credits = table("contributions", identifier("contribution_id"), Column("publication_id", BigInteger, nullable=False),
                    foreign("name_id", "names.name_id"), foreign("entity_id", "entities.entity_id", nullable=True),
                    Column("role", Text, nullable=False),
                    Column("position", Integer, nullable=False), Column("nested_position", Integer, nullable=False, server_default="0"),
                    Column("resolution", Text, nullable=False, server_default="unconfirmed"),
                    ForeignKeyConstraint(["publication_id", "role", "position"],
                        [f"{schema}.catalog_credit_groups.{field}" for field in ("publication_id", "role", "position")],
                        name="fk_catalog_credit_group", ondelete="CASCADE", onupdate="CASCADE"),
                    UniqueConstraint("publication_id", "role", "position", "nested_position", name="uq_catalog_credit_slot"),
                    CheckConstraint("position >= 0 AND nested_position >= 0", name="ck_catalog_credit_positions"),
                    CheckConstraint("resolution IN ('unconfirmed','confirmed')", name="ck_catalog_credit_resolution"),
                    CheckConstraint("resolution <> 'confirmed' OR entity_id IS NOT NULL", name="ck_catalog_credit_entity"), versioned=True)
    Index("idx_catalog_contributions_publication", credits.c.publication_id)
    Index("idx_catalog_contributions_entity", credits.c.entity_id)
    Index("idx_catalog_contributions_name_role", credits.c.name_id, credits.c.role)
    table("identifiers", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("kind", Text, nullable=False), Column("value", Text, nullable=False), Column("normalized", Text, nullable=False),
          Column("position", Integer, nullable=False), PrimaryKeyConstraint("publication_id", "kind", "position"))
    table("genres", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("value", Text, nullable=False), Column("position", Integer, nullable=False), PrimaryKeyConstraint("publication_id", "position"))
    table("subjects", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("name", Text), Column("term_code", Text), Column("set_name", Text), Column("set_url", Text),
          Column("set_is_url", Boolean, nullable=False), Column("position", Integer, nullable=False), PrimaryKeyConstraint("publication_id", "position"))
    table("audiences", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("kind", Text, nullable=False), Column("audience_type", Text), Column("min_age", Integer),
          Column("max_age", Integer), Column("position", Integer, nullable=False), PrimaryKeyConstraint("publication_id", "position"),
          CheckConstraint("min_age IS NULL OR min_age >= 0", name="ck_catalog_audience_min_age"),
          CheckConstraint("max_age IS NULL OR max_age >= 0", name="ck_catalog_audience_max_age"),
          CheckConstraint("min_age IS NULL OR max_age IS NULL OR min_age <= max_age", name="ck_catalog_audience_age_range"))
    table("publication_languages", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("position", Integer, nullable=False), Column("language", Text, nullable=False),
          PrimaryKeyConstraint("publication_id", "position"),
          CheckConstraint("position >= 0", name="ck_catalog_language_position"),
          CheckConstraint("btrim(language) <> ''", name="ck_catalog_language_value"))
    table("document_access_modes", Column("md5", Text, ForeignKey(docs.c.md5, ondelete="CASCADE"), nullable=False),
          Column("position", Integer, nullable=False), Column("mode", Text, nullable=False),
          PrimaryKeyConstraint("md5", "position"),
          CheckConstraint("position >= 0", name="ck_catalog_access_mode_position"),
          CheckConstraint("mode IN ('auditory','tactile','textual','visual')", name="ck_catalog_access_mode_value"))
    table("sufficient_modes", Column("md5", Text, ForeignKey(docs.c.md5, ondelete="CASCADE"), nullable=False),
          Column("position", Integer, nullable=False), PrimaryKeyConstraint("md5", "position"),
          CheckConstraint("position >= 0", name="ck_catalog_sufficient_group_position"))
    table("sufficient_mode_items", Column("md5", Text, nullable=False), Column("group_position", Integer, nullable=False),
          Column("position", Integer, nullable=False), Column("mode", Text, nullable=False),
          PrimaryKeyConstraint("md5", "group_position", "position"),
          ForeignKeyConstraint(["md5", "group_position"],
              [f"{schema}.catalog_sufficient_modes.md5", f"{schema}.catalog_sufficient_modes.position"],
              name="fk_catalog_sufficient_mode_group", ondelete="CASCADE", onupdate="CASCADE"),
          CheckConstraint("group_position >= 0 AND position >= 0", name="ck_catalog_sufficient_mode_positions"),
          CheckConstraint("mode IN ('auditory','tactile','textual','visual')", name="ck_catalog_sufficient_mode_value"))
    table("references", foreign("publication_id", "publications.publication_id", ondelete="CASCADE"),
          Column("work_type", Text), Column("name", Text), Column("language", Text),
          Column("urls_present", Boolean, nullable=False, server_default="false"), PrimaryKeyConstraint("publication_id"))
    table("reference_urls", foreign("publication_id", "references.publication_id", ondelete="CASCADE", onupdate="CASCADE"),
          Column("position", Integer, nullable=False), Column("url", Text, nullable=False),
          PrimaryKeyConstraint("publication_id", "position"),
          CheckConstraint("position >= 0", name="ck_catalog_reference_url_position"))
    # References are supporting works rather than publication/edition identity claims.
    table("reference_authors", foreign("publication_id", "references.publication_id", ondelete="CASCADE", onupdate="CASCADE",
          constraint_name="fk_catalog_reference_authors_reference"),
          Column("kind", Text), Column("name", Text), Column("position", Integer, nullable=False), PrimaryKeyConstraint("publication_id", "position"))
    table("evidence", identifier("evidence_id"), Column("md5", Text), Column("record_kind", Text, nullable=False),
          Column("record_key", Text, nullable=False), Column("source", Text, nullable=False), Column("payload", JSONB, nullable=False),
          Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
    table("protections", Column("record_kind", Text, primary_key=True), Column("record_key", Text, primary_key=True),
          Column("field", Text, primary_key=True), Column("actor", Text, nullable=False))
    table("revisions", identifier("revision_id"), Column("record_kind", Text, nullable=False),
          Column("record_key", Text, nullable=False), Column("actor", Text, nullable=False),
          Column("before", JSONB), Column("after", JSONB),
          Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
    table("proposals", identifier("proposal_id"), Column("kind", Text, nullable=False),
          Column("status", Text, nullable=False, server_default="pending"), Column("display_name", Text),
          Column("evidence", JSONB, nullable=False, server_default="{}"),
          Column("field_changes", JSONB, nullable=False, server_default="{}"),
          foreign("publication_id", "publications.publication_id", nullable=True), versioned=True)
    members = table("proposal_members", identifier("member_id"), foreign("proposal_id", "proposals.proposal_id", ondelete="CASCADE"),
          foreign("entity_id", "entities.entity_id", nullable=True), foreign("name_id", "names.name_id", nullable=True),
          Column("reviewed_revision", BigInteger), Column("snapshot", JSONB, nullable=False),
          CheckConstraint("(entity_id IS NULL) <> (name_id IS NULL)"))
    Index("uq_catalog_proposal_entity", members.c.proposal_id, members.c.entity_id, unique=True,
          postgresql_where=members.c.entity_id.is_not(None))
    Index("uq_catalog_proposal_name", members.c.proposal_id, members.c.name_id, unique=True,
          postgresql_where=members.c.name_id.is_not(None))
    table("separations", Column("left_key", Text, primary_key=True), Column("right_key", Text, primary_key=True),
          Column("actor", Text, nullable=False), CheckConstraint("left_key < right_key"))
    previews = table("preview_requests", identifier("request_id"),
                     Column("md5", Text, ForeignKey(docs.c.md5, ondelete="CASCADE"), nullable=False),
                     Column("idempotency_key", Text, nullable=False), Column("recipe", Text, nullable=False),
                     Column("status", Text, nullable=False, server_default="pending"),
                     Column("private", Boolean, nullable=False), Column("actor", Text, nullable=False),
                     Column("claim_token", Text), Column("lease_until", DateTime(timezone=True)),
                     Column("source_page_count", Integer),
                     Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
                     UniqueConstraint("md5", "idempotency_key"))
    Index("idx_catalog_preview_queue", previews.c.status, previews.c.request_id)
    table("preview_pages", foreign("request_id", "preview_requests.request_id", ondelete="CASCADE"),
          Column("role", Text, nullable=False), Column("page_number", Integer, nullable=False),
          Column("small_key", Text), Column("large_key", Text), PrimaryKeyConstraint("request_id", "role"))
    table("imports", identifier("import_id"), Column("source_fingerprint", Text, nullable=False, unique=True),
          Column("manifest", JSONB, nullable=False), Column("state", Text, nullable=False),
          Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
    return metadata
