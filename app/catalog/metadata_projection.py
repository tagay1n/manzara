"""Schema.org projection with catalog-owned classification and observed names."""

from app.catalog.metadata import compose_metadata
from app.catalog.metadata_records import ClassificationPath, NormalizedMetadata
from app.catalog.metadata_store import unmanaged_subjects


def compose_classified_metadata(record: NormalizedMetadata, path: ClassificationPath | None = None) -> dict:
    """Compose without modifying the normalized record or its subject list."""
    schema = compose_metadata(record)
    if path is not None:
        schema['about'] = unmanaged_subjects(record['subjects']) + [
            {'@type': 'DefinedTerm', 'termCode': value,
             'inDefinedTermSet': {'@type': 'DefinedTermSet', 'name': termset}}
            for termset, value in (('DDC', path['ddc']), ('CategoryPath', ' > '.join(path['path_en'])))
        ]
    return schema
