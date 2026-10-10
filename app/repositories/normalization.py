from __future__ import annotations

from typing import Any, Dict, List

from app.repositories.personality_checkpoints import PersonalityCheckpointRepository


class NormalizationRepository(PersonalityCheckpointRepository):
    """PostgreSQL operations for the normalization domain."""


    def list_personality_source_documents(self) -> List[Dict[str, Any]]:
        """Project just the included people and language hints needed by the task."""
        roles = ('author', 'editor', 'translator', 'illustrator', 'contributor')
        people = ",".join(
            f"""'{role}', COALESCE(jsonb_agg(jsonb_build_object(
                '@type','Person','name',n.raw_name)
                ORDER BY c.position,c.nested_position)
                FILTER (WHERE c.role='{role}' AND n.kind='person'),'[]'::jsonb)"""
            for role in roles
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT d.md5,jsonb_build_object(
                        'inLanguage',COALESCE((SELECT jsonb_agg(l.language ORDER BY l.position)
                            FROM catalog_publication_languages l WHERE l.publication_id=p.publication_id),
                            '[]'::jsonb),{people}) AS schema_org
                    FROM catalog_documents d JOIN catalog_publications p USING(publication_id)
                    LEFT JOIN catalog_contributions c ON c.publication_id=p.publication_id
                        AND c.role IN ('author','editor','translator','illustrator','contributor')
                    LEFT JOIN catalog_names n USING(name_id)
                    WHERE p.inclusion='included' AND p.has_metadata AND p.metadata_present
                    GROUP BY d.md5,p.publication_id ORDER BY d.md5"""
            ).fetchall()
        return [dict(row) for row in rows]


    def persist_personality_normalization(
        self,
        *,
        raw_name: str,
        source_fingerprint: str,
        document_count: int,
        mention_count: int,
        source_roles: list[str],
        components: Dict[str, Any],
        display_name: str,
        identity_key: str,
        model: str,
        prompt_version: str,
        schema_version: str,
    ) -> Dict[str, Any]:
        """Write normalized catalog hypotheses through the shared engine."""
        from app.catalog.repository import CatalogRepository

        if self._engine is None:
            raise RuntimeError("Catalog normalization requires the shared PostgreSQL engine")
        return CatalogRepository(self._engine, schema=self.schema).normalize_personality(
            raw_name=raw_name, source_fingerprint=source_fingerprint,
            document_count=document_count, mention_count=mention_count,
            source_roles=source_roles, components=components, display_name=display_name,
            identity_key=identity_key, model=model, prompt_version=prompt_version,
            schema_version=schema_version,
        )
