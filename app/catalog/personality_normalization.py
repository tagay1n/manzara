"""Atomic AI identity hypotheses; never resolves contextual contributions."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.catalog.contracts import CatalogConflict


class PersonalityNormalizationStore:
    def normalize_personality(
        self, *, raw_name, source_fingerprint, document_count, mention_count,
        source_roles, components, display_name, identity_key, model,
        prompt_version, schema_version,
    ):
        """Retain an unconfirmed hypothesis and its checkpoint in one transaction."""
        actor = "task"
        now = datetime.now(timezone.utc).isoformat()
        entities = self.table("entities")
        aliases = self.table("aliases")
        reviews = self.table("alias_reviews")
        checkpoint_table = f'"{self.schema}".personality_normalization_checkpoints'
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(text("SELECT set_config('manzara.catalog_actor', 'task', true)"))
            # Serialize creation even when there is no matching row yet.
            for key in sorted(("identity:" + identity_key, "name:" + raw_name)):
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:key))"),
                             {"scope": "personality-normalization:" + self.schema, "key": key})
            previous = conn.execute(text(
                f"SELECT canonical_id FROM {checkpoint_table} WHERE raw_name=:name FOR UPDATE"
            ), {"name": raw_name}).scalar()
            matches = [dict(row) for row in conn.execute(select(entities).where(
                entities.c.kind == "person", entities.c.status == "active",
                entities.c.identity_key == identity_key,
            ).order_by(entities.c.entity_id).with_for_update()).mappings()
                if all(not row[field] or not value or row[field] == value
                       for field, value in components.items() if field != "title")]
            preferred = next((row for row in matches if row["entity_id"] == previous), None)
            if len(matches) > 1 and preferred is None:
                raise CatalogConflict("Multiple compatible identities; review this source name before retrying.")
            entity = preferred or (matches[0] if matches else None)
            if entity is None:
                entity = dict(conn.execute(entities.insert().values(
                    kind="person", display_name=display_name, approval="unconfirmed",
                    identity_key=identity_key, status="active", created_at=now,
                    **{key: value for key, value in components.items() if key != "identity_key"},
                ).returning(entities)).mappings().one())
                self._audit(conn, "entity", entity["entity_id"], None, entity, actor)
            entity_id = entity["entity_id"]
            roles = self.table("entity_roles")
            role_added = conn.execute(insert(roles).values(entity_id=entity_id, role="personality")
                                     .on_conflict_do_nothing().returning(roles.c.entity_id)).scalar()
            name_id = self._name(conn, "person", raw_name)
            # Alias insertion can create a catalog.owner placeholder via a trigger.
            # Only a review that existed before that insertion can be a human decision.
            existing_review = conn.execute(select(reviews).where(
                reviews.c.entity_type == "personality", reviews.c.name_id == name_id,
            ).with_for_update()).mappings().first()
            write_review = existing_review is None or existing_review["source"] == "gemini_personality_normalizer"
            # DO NOTHING preserves any existing human confirmation.
            alias = conn.execute(insert(aliases).values(
                name_id=name_id, entity_id=entity_id, approval="unconfirmed",
            ).on_conflict_do_nothing().returning(aliases)).mappings().first()
            if alias is not None:
                self._audit(conn, "alias", alias["alias_id"], None, dict(alias), actor)
            if alias is not None or role_added is not None:
                # Association changes advance the entity revision without changing its fields.
                self._update(conn, "entity", entity_id, entity, {}, actor)
            targets = list(conn.execute(select(aliases.c.entity_id).join(entities).where(
                aliases.c.name_id == name_id, entities.c.status == "active",
            )).scalars())
            before = conn.execute(select(reviews).where(
                reviews.c.entity_type == "personality", reviews.c.name_id == name_id,
            ).with_for_update()).mappings().first()
            review = {
                "entity_type": "personality", "name_id": name_id,
                "entity_id": entity_id if len(targets) == 1 else None,
                "normalized_name": identity_key, "script_label": "other",
                "decision_status": "linked" if len(targets) == 1 else "pending",
                "docs_count": document_count, "mentions_count": mention_count, "marker_count": 0,
                "confidence": 1.0, "source": "gemini_personality_normalizer",
                "reason": "structured_success", "successful_model": model,
                "prompt_version": prompt_version, "schema_version": schema_version,
                "source_roles": sorted(set(source_roles)), "updated_at": now, **components,
            }
            # The summary cannot overwrite a manually reviewed association.
            if write_review:
                if before is None:
                    after = conn.execute(reviews.insert().values(created_at=now, **review).returning(reviews)).mappings().one()
                else:
                    after = conn.execute(reviews.update().where(reviews.c.alias_id == before["alias_id"])
                                         .values(**review).returning(reviews)).mappings().one()
                self._audit(conn, "alias_review", after["alias_id"], dict(before) if before else None, dict(after), actor)
            values = {
                "raw_name": raw_name, "source_fingerprint": source_fingerprint,
                "document_count": document_count, "mention_count": mention_count,
                "source_roles": json.dumps(sorted(set(source_roles)), ensure_ascii=False),
                "prompt_version": prompt_version, "schema_version": schema_version,
                "canonical_id": entity_id, "now": now, "model": model,
            }
            conn.execute(text(f"""
                INSERT INTO {checkpoint_table} (
                    raw_name,source_fingerprint,document_count,mention_count,source_roles,
                    prompt_version,schema_version,state,decision_reason,successful_model,decision_evidence,
                    canonical_id,updated_at,completed_at
                ) VALUES (:raw_name,:source_fingerprint,:document_count,:mention_count,
                    CAST(:source_roles AS jsonb),:prompt_version,:schema_version,
                    'succeeded',NULL,:model,'{{}}'::jsonb,:canonical_id,:now,:now)
                ON CONFLICT(raw_name) DO UPDATE SET
                    source_fingerprint=excluded.source_fingerprint,document_count=excluded.document_count,
                    mention_count=excluded.mention_count,source_roles=excluded.source_roles,
                    prompt_version=excluded.prompt_version,schema_version=excluded.schema_version,
                    state='succeeded',decision_reason=NULL,successful_model=excluded.successful_model,
                    decision_evidence='{{}}'::jsonb,canonical_id=excluded.canonical_id,
                    updated_at=excluded.updated_at,completed_at=excluded.completed_at
            """), values)
            return {**entity, "canonical_id": entity_id}
