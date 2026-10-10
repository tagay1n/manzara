"""Run-scoped evaluation evidence using verified primary storage only."""

from app.document_storage import download_cached_primary_document, verify_primary_document_object
from app.modules.library.djvu_slicing import create_djvu_slice
from app.modules.library.metadata_extraction import create_pdf_slice, load_text_slice
from app.modules.library.runtime.metadata.evaluation_patch import _collect_patch_fields
from app.modules.library.runtime.metadata.evaluation_text import _build_content_excerpt, _drop_none_values
from app.modules.library.runtime.metadata.fields import extract_flat_fields
from app.modules.library.runtime.prompts.metadata_evaluation import build_library_applicability_prompt
from app.modules.library.upstream_metadata import sanitize_upstream_metadata


EXCERPT_CHARS = 500
EDGE_PAGES = 2


def prepare_evaluation_request(candidate, schema_org, *, workspace, storage, primary_s3, known, log):
    excerpt = None
    files = {}
    is_djvu = candidate.mime_type == 'image/vnd.djvu'
    if candidate.content_url and not is_djvu:
        try:
            verify_primary_document_object(settings=storage, s3=primary_s3,
                document_url=candidate.document_url, expected_size=candidate.primary_storage_size)
            text = load_text_slice(candidate, workspace=workspace)
            excerpt = _build_content_excerpt(text, EXCERPT_CHARS)
        except Exception as exc:
            if candidate.mime_type != 'application/pdf':
                raise
            log(f'Evaluation text unavailable md5={candidate.md5}; trying PDF evidence: {exc}')
    if excerpt is None:
        if candidate.mime_type not in {'application/pdf', 'image/vnd.djvu'}:
            raise ValueError(f'No usable evaluation content for {candidate.md5}')
        source = download_cached_primary_document(settings=storage, s3=primary_s3,
            document_url=candidate.document_url, expected_md5=candidate.md5,
            expected_size=candidate.primary_storage_size, extension='.djvu' if is_djvu else '.pdf')
        slice_path = workspace / candidate.md5 / 'slice-for-eval.pdf'
        if is_djvu:
            create_djvu_slice(source, slice_path, edge_pages=EDGE_PAGES)
        else:
            create_pdf_slice(source, slice_path, edge_pages=EDGE_PAGES)
        files[slice_path] = 'application/pdf'
    flat = extract_flat_fields(schema_org)
    payload = _drop_none_values({
        'title': flat['title'], 'author': flat['author'], 'publisher': flat['publisher'],
        'genre': flat['genre'], 'publish_year': flat['publish_year'], 'isbn': flat['isbn'],
        'page_count': schema_org.get('numberOfPages'), 'inLanguage': schema_org.get('inLanguage'),
        'upstream_metadata': sanitize_upstream_metadata(candidate.upstream_metadata),
        'pdf_slice_attached': bool(files), 'work_type': schema_org.get('@type'),
        'missing_fields': _collect_patch_fields(schema_org),
        'known_classifications': [{'ddc': row['ddc'], 'path': row['path']} for row in known],
    })
    payload['work_type'] = schema_org.get('@type')
    if payload['work_type'] != 'Book':
        payload.pop('isbn', None)
    return build_library_applicability_prompt(payload, content_excerpt=excerpt), files
