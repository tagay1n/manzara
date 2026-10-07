"""Library compatibility imports for the shared catalog JSON-LD contract."""

from app.catalog.schema_org import (
    ACCESS_MODES as ACCESS_MODES,
    CONTRACT_VERSION as CONTRACT_VERSION,
    SCHEMA_CONTEXT as SCHEMA_CONTEXT,
    SUPPORTED_TYPES as SUPPORTED_TYPES,
    is_english_facet as is_english_facet,
    metadata_contract_issues as metadata_contract_issues,
    reshape_english_contributor_roles as reshape_english_contributor_roles,
)

__all__ = [
    "ACCESS_MODES", "CONTRACT_VERSION", "SCHEMA_CONTEXT", "SUPPORTED_TYPES",
    "is_english_facet", "metadata_contract_issues", "reshape_english_contributor_roles",
]
