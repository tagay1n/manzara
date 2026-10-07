"""Lossless source exceptions without duplicating retained workflow tables."""

RETIRED_FIELDS = {
    "document": {"md5", "mime_type", "ya_path", "ya_public_url", "ya_public_key", "ya_resource_id", "language",
                 "content_extraction_method", "meta_extraction_method", "full", "sharing_restricted", "document_url",
                 "content_url", "primary_storage_size", "primary_storage_etag", "primary_storage_verified_at"},
    "metadata": {"md5", "schema_org", "lib", "lib_eval_method", "classification_id"},
    "classification": {"id", "ddc", "path_en", "path_en_key", "path_tt", "status", "created_by", "created_at"},
    "normalization_canonicals": {"canonical_id", "entity_type", "display_name", "normalized_name", "status",
        "merged_into_id", "notes", "created_at", "updated_at", "surname_full", "surname_initials", "name_full",
        "name_initials", "father_name_full", "father_name_initials", "title", "sex", "identity_key"},
    "normalization_aliases": {"alias_id", "entity_type", "raw_name", "normalized_name", "script_label", "docs_count",
        "mentions_count", "marker_count", "decision_status", "canonical_id", "confidence", "source", "reason",
        "created_at", "updated_at", "surname_full", "surname_initials", "name_full", "name_initials",
        "father_name_full", "father_name_initials", "title", "sex", "source_roles", "successful_model",
        "prompt_version", "schema_version"},
    "library_collections": {"collection_id", "title", "normalized_title", "include_in_library", "metadata_template_json",
                            "notes", "applied_at", "created_at", "updated_at"},
    "library_collection_items": {"collection_id", "md5", "item_title", "created_at", "updated_at"},
}


def essential_payload(table, row):
    """Retained tables already own their records; retired exceptions need evidence."""
    if table not in RETIRED_FIELDS:
        return {}
    payload = {key: value for key, value in row.items() if key not in RETIRED_FIELDS[table]}
    if table == "document":
        # Import deliberately closes unknown privacy/completeness and resolves
        # language disagreements; keep the original values for source review.
        for key in ("sharing_restricted", "full", "language"):
            if key in row and (row[key] is None or key == "language"):
                payload[key] = row[key]
        if row.get("sharing_restricted") is not False:
            for key in ("ya_public_url", "ya_public_key"):
                if row.get(key) is not None:
                    payload[key] = row[key]
    if table == "metadata" and isinstance(row.get("schema_org"), dict):
        about = row["schema_org"].get("about") or []
        if isinstance(about, dict):
            about = [about]
        managed = [item for item in about if isinstance(item, dict)
                   and isinstance(item.get("inDefinedTermSet"), dict)
                   and str(item["inDefinedTermSet"].get("name", "")).lower() in {"ddc", "categorypath"}]
        if managed and row.get("classification_id") is not None:
            payload["original_managed_terms"] = managed
    if table == "classification":
        # Shared branches can acquire spelling/translation from another path.
        payload.update({key: row[key] for key in ("path_en", "path_en_key", "path_tt") if key in row})
    return payload
