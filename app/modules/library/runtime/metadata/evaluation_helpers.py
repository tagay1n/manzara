"""Evaluate document applicability for library management."""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import pymupdf as fitz
except ModuleNotFoundError:  # pragma: no cover - compatibility fallback
    import fitz  # type: ignore[no-redef]
import requests
from sqlalchemy import select

from app.document_storage import (
    load_document_storage_settings,
    materialize_cached_document,
    resolve_document_download_url,
)
from app.artifacts import flow_artifacts_dir
from integrations.s3 import create_document_session
from dirs import Dirs
from .schema import BookPatch
from models import Classification
from core.paths import get_in_workdir
from .isbn_utils import canonicalize_isbn_values
from .url_utils import normalize_url_list
from app.modules.library.metadata_contract import is_english_facet
from app.modules.library.metadata_terms import defined_term, termset_name


LEGAL_DOC_PATTERNS = [
    re.compile(r"^(?=.*common_crawl)(?=.*npa_ta_).*\.pdf$"),
    re.compile(r"^(?=.*pdf законов с pravo\.gov).*\.pdf$"),
]
ARTIFACTS_DIR = str(flow_artifacts_dir("library"))
UNPROCESSABLES_DIR = os.path.join(ARTIFACTS_DIR, "unprocessables")
DEFAULT_KNOWN_CLASSIFICATIONS_LIMIT = 500
HIGH_DEMAND_SLEEP_SECONDS = 60
ERROR_BACKOFF_SECONDS = 5
EXCERPT_PARTS = 3
EXCERPT_SEPARATOR = "\n\n[...]\n\n"
EVAL_PDF_SLICE_SIZE = 3
CODE_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", flags=re.DOTALL)
BLANK_LINES_RE = re.compile(r"\n{3,}")
YEAR_RE = re.compile(r"(1[5-9]\d{2}|20\d{2})")
INT_RE = re.compile(r"\d+")
WHITESPACE_RE = re.compile(r"\s+")
DDC_RE = re.compile(r"^\d{3}(?:\.\d+)?$")
CYRILLIC_RE = re.compile(r"[\u0400-\u052F]")
DDC_PROPERTY_NAME = "DDC"
UDC_PROPERTY_NAME = "UDC"
CATEGORY_PATH_TERMSET = "CategoryPath"
GENRE_TERMSET = "Genre"
MANAGED_TERMSETS = {
    DDC_PROPERTY_NAME.casefold(),
    UDC_PROPERTY_NAME.casefold(),
    CATEGORY_PATH_TERMSET.casefold(),
    GENRE_TERMSET.casefold(),
}


TASK_ID = "maintenance.monocorpus_meta_evaluate"
PANEL_ID = "library"


def _ensure_local_zip(md5: str, content_url: str, s3client, fallback_bucket: str) -> tuple[str, str, str]:
    local_zip = get_in_workdir(Dirs.CONTENT, file=f"{md5}.zip")
    bucket, key = _parse_s3_location(content_url, fallback_bucket, f"{md5}.zip")
    if not os.path.exists(local_zip):
        s3client.download_file(bucket, key, local_zip)
    if not os.path.exists(local_zip):
        raise FileNotFoundError(local_zip)
    return local_zip, bucket, key


def _parse_s3_location(content_url: str, fallback_bucket: str, fallback_key: str) -> tuple[str, str]:
    if content_url:
        try:
            parsed = urlparse(content_url)
            if parsed.scheme and parsed.netloc:
                path = parsed.path.lstrip("/")
                if path:
                    parts = path.split("/", 1)
                    bucket = parts[0]
                    key = parts[1] if len(parts) > 1 and parts[1] else fallback_key
                    return bucket, key
        except Exception:
            pass
    return fallback_bucket, fallback_key


def _read_markdown_from_zip(zip_path: str, md5: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        md_name = f"{md5}.md"
        names = zf.namelist()
        if md_name not in names:
            md_candidates = [n for n in names if n.lower().endswith(".md")]
            if not md_candidates:
                raise ValueError("No markdown file found in archive")
            md_name = md_candidates[0]
        return zf.read(md_name).decode("utf-8", errors="replace")


def _build_content_excerpt(text: str, max_chars: int) -> str | None:
    if max_chars <= 0:
        return None

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = CODE_FENCE_RE.sub("\n", normalized)
    normalized = BLANK_LINES_RE.sub("\n\n", normalized).strip()
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized

    chunk = max_chars // EXCERPT_PARTS
    head = normalized[:chunk]
    mid_start = max(0, (len(normalized) // 2) - (chunk // 2))
    middle = normalized[mid_start : mid_start + chunk]
    tail = normalized[-chunk:]
    excerpt = EXCERPT_SEPARATOR.join([head, middle, tail])
    return excerpt[:max_chars]


def _format_response_for_log(response_text: str | None) -> str:
    """Pretty-print JSON responses for readable logs, fallback to plain text."""
    if response_text is None:
        return ""
    raw = response_text.strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _collect_patch_fields(schema_org: dict | str | None) -> list[str]:
    schema = schema_org if isinstance(schema_org, dict) else {}
    fields = [
        "isbn",
        "datePublished",
        "numberOfPages",
        "name",
        "author",
        "publisher",
        "genre",
        "description",
    ]
    return [name for name in fields if name == "genre" or _is_schema_field_missing(schema, name)]


def _is_schema_field_missing(schema: dict[str, Any], field: str) -> bool:
    value = schema.get(field)
    if field == "publisher":
        if isinstance(value, dict):
            return not _clean_text(value.get("name"))
        return _is_missing(value)
    return _is_missing(value)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    if isinstance(value, dict):
        return len(value) == 0
    return False


def _normalize_metadata_patch(raw_patch: BookPatch | dict[str, Any] | None, doc: Any, config: dict) -> BookPatch | None:
    if not isinstance(raw_patch, dict):
        if isinstance(raw_patch, BookPatch):
            raw_patch = json.loads(
                raw_patch.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                    ensure_ascii=False,
                )
            )
        else:
            raw_patch = {}

    patchable_fields = set(_collect_patch_fields(doc.schema_org))
    patch: dict[str, Any] = {}

    if "isbn" in patchable_fields:
        if isbn_values := _normalize_isbn_values(raw_patch.get("isbn")):
            patch["isbn"] = isbn_values

    if "datePublished" in patchable_fields:
        if date_published := _normalize_date_published(raw_patch.get("datePublished")):
            patch["datePublished"] = date_published

    if "numberOfPages" in patchable_fields:
        number_of_pages = _normalize_number_of_pages(raw_patch.get("numberOfPages"))
        if number_of_pages is not None:
            patch["numberOfPages"] = number_of_pages

    if "name" in patchable_fields:
        if name := _clean_text(raw_patch.get("name"), max_len=600):
            patch["name"] = name

    if "author" in patchable_fields:
        if author := _normalize_author(raw_patch.get("author")):
            patch["author"] = author

    if "publisher" in patchable_fields:
        if publisher := _normalize_publisher(raw_patch.get("publisher")):
            patch["publisher"] = publisher

    if "genre" in patchable_fields:
        if genre := _normalize_genre(raw_patch.get("genre")):
            patch["genre"] = genre

    if "description" in patchable_fields:
        if description := _clean_text(raw_patch.get("description"), max_len=5000):
            patch["description"] = description

    if not patch:
        return None
    return BookPatch.model_validate(patch)


def _normalize_isbn_values(value: Any) -> list[str] | None:
    return canonicalize_isbn_values(value)


def _normalize_date_published(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        year = int(value)
        return str(year) if 1500 <= year <= 2100 else None
    raw = _clean_text(value, max_len=40)
    if not raw:
        return None
    raw = raw.replace("/", "-")
    if re.fullmatch(r"\d{4}(-\d{2})?(-\d{2})?", raw):
        year = int(raw[:4])
        return raw if 1500 <= year <= 2100 else None
    match = YEAR_RE.search(raw)
    if not match:
        return None
    year = int(match.group(1))
    return str(year) if 1500 <= year <= 2100 else None


def _normalize_number_of_pages(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 20_000 else None
    if isinstance(value, float):
        int_val = int(value)
        return int_val if 1 <= int_val <= 20_000 else None
    raw = _clean_text(value, max_len=40)
    if not raw:
        return None
    match = INT_RE.search(raw)
    if not match:
        return None
    int_val = int(match.group(0))
    return int_val if 1 <= int_val <= 20_000 else None


def _normalize_author(value: Any) -> list[dict[str, str]] | None:
    names = _extract_candidate_strings(value, dict_keys=("name",))
    if not names:
        return None
    normalized = []
    seen = set()
    for name in names:
        clean = _clean_text(name, max_len=300)
        if not clean or not is_english_facet(clean):
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"@type": "Person", "name": clean})
    return normalized or None


def _normalize_publisher(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        name = _clean_text(value.get("name"), max_len=400)
    else:
        name = _clean_text(value, max_len=400)
    if not name:
        return None
    return {"@type": "Organization", "name": name}


def _normalize_genre(value: Any) -> list[str] | None:
    genres = _extract_candidate_strings(value, dict_keys=("name",))
    seen = set()
    normalized: list[str] = []
    for genre in genres:
        clean = _clean_text(genre, max_len=120)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    return normalized or None


def _normalize_library_classification(
    raw_ddc: Any,
    raw_path: Any,
    applicable: bool,
) -> tuple[str | None, list[str] | None]:
    if not applicable:
        return None, None

    ddc = _normalize_ddc(raw_ddc)
    path = _normalize_classification_path(raw_path)
    if not ddc or not path:
        return None, None
    return ddc, path


def _normalize_ddc(value: Any) -> str | None:
    text = _clean_text(value, max_len=32)
    if not text:
        return None
    text = text.replace(" ", "")
    return text if DDC_RE.fullmatch(text) else None


def _normalize_classification_path(value: Any) -> list[str] | None:
    if isinstance(value, str):
        values = [v.strip() for v in value.split("->")]
    elif isinstance(value, list):
        values = [str(v).strip() for v in value]
    else:
        return None
    cleaned: list[str] = []
    for item in values:
        text = _clean_text(item, max_len=180)
        if not text:
            continue
        # Classification labels are expected in English for stable taxonomy keys.
        if not is_english_facet(text):
            return None
        cleaned.append(text)
    if len(cleaned) < 2 or len(cleaned) > 8:
        return None
    return cleaned


def _resolve_classification_id(session, ddc_raw: str | None, path_raw: list[str] | None) -> int | None:
    """Resolve existing classification id or create a new pending one."""
    if not ddc_raw or not path_raw:
        return None
    ddc = _normalize_ddc(ddc_raw)
    path = _normalize_classification_path(path_raw)
    if not ddc or not path:
        return None
    path_key = _classification_path_key(path)

    stmt = select(Classification).where(
        Classification.ddc == ddc,
        Classification.path_en_key == path_key,
    )
    existing = session.scalars(stmt).first()
    if existing:
        return existing.id

    created = Classification(
        ddc=ddc,
        path_en=path,
        path_en_key=path_key,
        status="pending",
        created_by="gemini",
    )
    session.add(created)
    session.flush()
    return created.id


def _classification_path_key(path: list[str]) -> str:
    return "|".join([p.casefold() for p in path])


def _extract_candidate_strings(value: Any, dict_keys: tuple[str, ...] = ("name", "value")) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, str):
            if item.strip():
                output.append(item)
        elif isinstance(item, dict):
            for key in dict_keys:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    output.append(candidate)
                    break
    return output


def _drop_none_values(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _clean_text(value: Any, max_len: int = 1000) -> str | None:
    if value is None:
        return None
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    if not text:
        return None
    return text[:max_len]


def _insert_page_ranges(source_pdf: fitz.Document, target_pdf: fitz.Document, pages: list[int]) -> None:
    if not pages:
        return
    pages = sorted(set(pages))
    start = pages[0]
    prev = pages[0]
    for current in pages[1:]:
        if current == prev + 1:
            prev = current
            continue
        target_pdf.insert_pdf(source_pdf, from_page=start, to_page=prev)
        start = current
        prev = current
    target_pdf.insert_pdf(source_pdf, from_page=start, to_page=prev)


def _download_file(url: str, local_path: str) -> None:
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(local_path, "wb") as fh:
            for chunk in response.iter_content(1024 * 64):
                if chunk:
                    fh.write(chunk)


def _resolve_doc_source_url(doc: Any, config: dict, s3client: Any) -> str | None:
    storage = load_document_storage_settings(config)
    return resolve_document_download_url(
        document_url=doc.document_url,
        fallback_url=doc.ya_public_url,
        encryption_key=config["encryption_key"],
        endpoint_url=storage.primary.endpoint_url,
        private_bucket=storage.private_bucket,
        s3=s3client,
    )


def _ensure_pdf_in_shared_cache(
    doc: Any,
    config: dict,
    s3client: Any,
) -> str:
    storage = load_document_storage_settings(config)

    def download(destination: Path) -> None:
        source_url = _resolve_doc_source_url(doc, config, s3client)
        if not source_url:
            raise ValueError(f"Document has no downloadable source: {doc.md5}")
        _download_file(source_url, str(destination))

    return str(
        materialize_cached_document(
            cache_path=storage.cache_path,
            expected_md5=doc.md5,
            extension=".pdf",
            download=download,
        )
    )


def _fallback_pdf_page_count(doc: Any, config: dict) -> int | None:
    if doc.mime_type != "application/pdf":
        return None
    local_pdf = _ensure_pdf_in_shared_cache(
        doc,
        config,
        create_document_session(config),
    )
    with fitz.open(local_pdf) as pdf:
        count = int(pdf.page_count)
        return count if count > 0 else None


def _apply_metadata_patch(schema_org: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    updated = dict(schema_org)
    applied: list[str] = []
    for key, value in patch.items():
        if key == "genre":
            normalized = _normalize_genre(value)
            current = _normalize_genre(updated.get("genre"))
            if normalized and normalized != current:
                updated["genre"] = normalized
                applied.append("genre")
            continue
        if key == "publisher":
            current = updated.get("publisher")
            missing = _is_missing(current) or (
                isinstance(current, dict) and not _clean_text(current.get("name"))
            )
            if missing and value:
                updated["publisher"] = value
                applied.append("publisher")
            continue
        if _is_schema_field_missing(updated, key) and not _is_missing(value):
            updated[key] = value
            applied.append(key)
    return updated, applied


def _sync_auxiliary_terms_in_about(
    schema_org: dict[str, Any],
    applicable: bool,
    ddc: str | None,
    path: list[str] | None,
) -> tuple[dict[str, Any], list[str]]:
    updated = dict(schema_org)
    raw_about = updated.get("about")
    raw_genre = updated.get("genre")

    before_about = json.dumps(raw_about, ensure_ascii=False, sort_keys=True) if raw_about is not None else None
    before_genre = _normalize_genre(raw_genre)

    existing_about_items = raw_about if isinstance(raw_about, list) else ([raw_about] if raw_about else [])
    existing_genre_terms = _extract_about_term_values(existing_about_items, GENRE_TERMSET)
    existing_udc_terms = _extract_about_term_values(existing_about_items, UDC_PROPERTY_NAME)
    retained_about_items: list[Any] = []
    for item in existing_about_items:
        if _is_managed_about_term(item):
            continue
        if isinstance(item, dict) and str(item.get("@type") or "").strip().casefold() == "definedterm":
            normalized_item = dict(item)
            normalized_item.pop("name", None)
            retained_about_items.append(normalized_item)
            continue
        retained_about_items.append(item)

    genres = _normalize_genre(raw_genre) or existing_genre_terms
    if genres:
        updated["genre"] = genres
    else:
        updated.pop("genre", None)

    for udc in existing_udc_terms:
        retained_about_items.append(_build_defined_term(udc, UDC_PROPERTY_NAME))

    if applicable and ddc and path:
        retained_about_items.append(_build_defined_term(ddc, DDC_PROPERTY_NAME))
        retained_about_items.append(
            _build_defined_term(" > ".join(path), CATEGORY_PATH_TERMSET)
        )

    if retained_about_items:
        updated["about"] = retained_about_items
    else:
        updated.pop("about", None)

    updated.pop("additionalProperty", None)

    applied: list[str] = []
    after_about = json.dumps(updated.get("about"), ensure_ascii=False, sort_keys=True) if updated.get("about") is not None else None
    if before_about != after_about:
        applied.append("about")
    if "additionalProperty" in schema_org:
        applied.append("additionalProperty")
    if before_genre != _normalize_genre(updated.get("genre")):
        applied.append("genre")
    return updated, applied


def _sanitize_schema_urls(schema_org: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Drop invalid URL values from schema.org payload."""
    updated = dict(schema_org)
    changed = False

    if "url" in updated:
        normalized = normalize_url_list(updated.get("url"))
        if normalized:
            if updated.get("url") != normalized:
                updated["url"] = normalized
                changed = True
        else:
            updated.pop("url", None)
            changed = True

    based_on = updated.get("isBasedOn")
    if isinstance(based_on, dict):
        normalized_based_on = dict(based_on)
        normalized_urls = normalize_url_list(based_on.get("url"))
        if normalized_urls:
            if based_on.get("url") != normalized_urls:
                normalized_based_on["url"] = normalized_urls
                changed = True
        elif "url" in normalized_based_on:
            normalized_based_on.pop("url", None)
            changed = True
        updated["isBasedOn"] = normalized_based_on

    return updated, changed


def _build_defined_term(term_code: str, termset: str) -> dict[str, Any]:
    return defined_term(term_code, termset)


def _is_managed_about_term(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    termset = termset_name(item.get("inDefinedTermSet"))
    return bool(termset and termset.casefold() in MANAGED_TERMSETS)


def _extract_about_term_values(items: list[Any], termset: str) -> list[str]:
    target_set = termset.casefold()
    values: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_set = termset_name(item.get("inDefinedTermSet"))
        if not item_set or item_set.casefold() != target_set:
            continue
        value = _clean_text(item.get("termCode"), max_len=500) or _clean_text(item.get("name"), max_len=500)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values
