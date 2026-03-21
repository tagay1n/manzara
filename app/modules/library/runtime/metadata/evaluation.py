"""Evaluate document applicability for library management."""

from __future__ import annotations

import datetime
import json
import os
import random
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Iterable
from urllib.parse import urlparse

import fitz
import requests
from google.genai.errors import ClientError
from google.genai.errors import ServerError
from pydantic import BaseModel
from prompts.metadata_evaluation import build_library_applicability_prompt
from rich import print
from sqlalchemy import func, select

from integrations.gemini import create_client, gemini_api, stream_text
from integrations.s3 import create_session
from dirs import Dirs
from .fields import extract_flat_fields
from .schema import BookPatch
from models import Classification, Document, Metadata
from core.paths import get_in_workdir
from core.config import read_config
from core.db import get_session
from core.upstream_meta import load_upstream_metadata
from core.state import dump_expired_keys, load_expired_keys
from core.security import decrypt, prefix
from .isbn_utils import canonicalize_isbn_values
from .repository import count_docs_for_evaluation, fetch_docs_for_evaluation, mark_docs_as_non_applicable
from .url_utils import normalize_url_list


MODEL = "gemini-3-flash-preview"
EVAL_METHOD = f"{MODEL}/v1"

LEGAL_DOC_PATTERNS = [
    re.compile(r"^(?=.*common_crawl)(?=.*npa_ta_).*\.pdf$"),
    re.compile(r"^(?=.*pdf законов с pravo\.gov).*\.pdf$"),
]
ARTIFACTS_DIR = "_artifacts"
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


class Evaluation(BaseModel):
    """Classification result used to populate boolean `metadata.lib`."""

    applicable: bool = True
    reason: str | None = None
    metadata_patch: BookPatch | None = None
    library_ddc: str | None = None
    library_path: list[str] | None = None

    @classmethod
    def nonapplicable(cls, reason: str) -> "Evaluation":
        return cls(applicable=False, reason=reason)


@dataclass
class EvaluationTask:
    """Document payload needed for library applicability evaluation."""
    md5: str
    ya_path: str | None
    language: str | None
    page_count: int | None
    full: bool | None
    sharing_restricted: bool | None
    ya_public_url: str | None
    mime_type: str | None
    document_url: str | None
    upstream_meta_url: str | None
    content_url: str | None
    schema_org: dict | str | None


def evaluate(args) -> None:
    """Run batch evaluation and save results into `metadata.lib`."""
    config = read_config()
    channel = Channel(dry_run=args.dry_run)
    if args.dry_run:
        print("Running in dry-run mode: no DB/file state changes will be persisted.")

    remaining = _count_remaining(config, channel)
    print(f"Documents remaining for evaluation: {remaining}")
    excerpt_chars = max(0, args.excerpt_chars)
    stop_event = threading.Event()
    while not stop_event.is_set():
        tasks_queue = None
        workers: list[threading.Thread] = []
        try:

            docs = _load_batch(config, args.batch_size, channel)
            if not docs:
                print("No more documents to process")
                break

            docs, non_applicables = _early_skip(docs)
            if not docs:
                continue
            if not args.dry_run:
                _save_non_applicable(non_applicables)

            keys = _pick_keys(config, args.workers, channel)
            if not keys:
                print("No gemini keys available, exiting...")
                break
            known_classifications = _load_known_classifications()

            tasks_queue = _create_queue(docs)
            print(f"Processing batch of {tasks_queue.qsize()} documents with {len(keys)} worker(s)")

            for key in keys[: min(len(keys), tasks_queue.qsize())]:
                worker = LibraryApplicabilityWorker(
                    gemini_api_key=key,
                    tasks_queue=tasks_queue,
                    config=config,
                    channel=channel,
                    dry_run=args.dry_run,
                    excerpt_chars=excerpt_chars,
                    known_classifications=known_classifications,
                    stop_event=stop_event,
                )
                thread = threading.Thread(target=worker, name=f"eval-{key[-6:]}")
                thread.start()
                workers.append(thread)
                time.sleep(2)

            for thread in workers:
                thread.join()

            channel.dump()
        except (KeyboardInterrupt, Exception) as e:  # noqa: BLE001
            is_interrupt = isinstance(e, KeyboardInterrupt)
            if is_interrupt:
                print("Interrupted, shutting down workers...")
                stop_event.set()
            else:
                import traceback

                print(f"Error during evaluation batch: {e}")
                print(traceback.format_exc())

            if tasks_queue is not None:
                tasks_queue.queue.clear()
            for thread in workers:
                thread.join(timeout=120)
            channel.dump()

            if is_interrupt:
                return
            continue


def _load_batch(config: dict, batch_size: int, channel: "Channel") -> list[EvaluationTask]:
    lang_codes = config["sup_langs"]["tt"]["codes"]
    rows = fetch_docs_for_evaluation(
        batch_size=batch_size,
        lang_codes=lang_codes,
        excluded_md5s=channel.get_all_unprocessable_docs(),
    )
    return [
        EvaluationTask(
            md5=doc.md5,
            ya_path=doc.ya_path,
            language=doc.language,
            page_count=extract_flat_fields(meta.schema_org if meta else None).get("page_count"),
            full=doc.full,
            sharing_restricted=doc.sharing_restricted,
            ya_public_url=doc.ya_public_url,
            mime_type=doc.mime_type,
            document_url=doc.document_url,
            upstream_meta_url=doc.upstream_meta_url,
            content_url=doc.content_url,
            schema_org=meta.schema_org if meta else None,
        )
        for doc, meta in rows
    ]


def _count_remaining(config: dict, channel: "Channel") -> int:
    """Count docs still pending evaluation for current language filters."""
    lang_codes = config["sup_langs"]["tt"]["codes"]
    return count_docs_for_evaluation(
        lang_codes=lang_codes,
        excluded_md5s=channel.get_all_unprocessable_docs(),
    )


def _pick_keys(config: dict, workers: int, channel: "Channel") -> list[str]:
    keys = list(set(config["gemini_api_keys"]) - channel.exceeded_keys_set)
    random.shuffle(keys)
    return keys[:workers]


def _early_skip(docs: Iterable[EvaluationTask]) -> tuple[list[EvaluationTask], list[tuple[str, str]]]:
    probables = []
    non_applicables = []
    for doc in docs:
        if doc.full is not True:
            non_applicables.append((doc.md5, "not full"))
            continue
        if doc.sharing_restricted is True:
            non_applicables.append((doc.md5, "sharing restricted"))
            continue
        if doc.ya_path and any(pattern.match(doc.ya_path) for pattern in LEGAL_DOC_PATTERNS):
            non_applicables.append((doc.md5, "legal doc"))
            continue
        probables.append(doc)
    return probables, non_applicables


def _save_non_applicable(non_applicables: list[tuple[str, str]]) -> None:
    if not non_applicables:
        return
    print(f"Marking {len(non_applicables)} documents as non-applicable")
    mark_docs_as_non_applicable([md5 for md5, _reason in non_applicables])


def _create_queue(docs: list[EvaluationTask]) -> Queue:
    tasks_queue: Queue = Queue()
    for doc in docs:
        tasks_queue.put(doc)
    return tasks_queue


def _load_known_classifications(limit: int = DEFAULT_KNOWN_CLASSIFICATIONS_LIMIT) -> list[dict[str, Any]]:
    """Load top-N known classifications ordered by current usage frequency."""
    if limit <= 0:
        return []

    usage_subquery = (
        select(
            Metadata.classification_id.label("classification_id"),
            func.count(Metadata.md5).label("usage_count"),
        )
        .where(Metadata.classification_id.is_not(None))
        .group_by(Metadata.classification_id)
        .subquery()
    )

    known: list[dict[str, Any]] = []
    with get_session() as session:
        stmt = (
            select(Classification)
            .outerjoin(
                usage_subquery,
                usage_subquery.c.classification_id == Classification.id,
            )
            .order_by(
                func.coalesce(usage_subquery.c.usage_count, 0).desc(),
                Classification.ddc.asc(),
                Classification.id.asc(),
            )
            .limit(limit)
        )
        rows = session.scalars(stmt).all()
        for row in rows:
            path = _normalize_classification_path(row.path_en)
            ddc = _normalize_ddc(row.ddc)
            if not path or not ddc:
                continue
            known.append({"id": row.id, "ddc": ddc, "path": path})
    return known


class LibraryApplicabilityWorker:
    """Single worker that consumes docs and saves applicability result."""

    def __init__(
        self,
        gemini_api_key: str,
        tasks_queue: Queue,
        config: dict,
        channel: "Channel",
        dry_run: bool,
        excerpt_chars: int,
        known_classifications: list[dict[str, Any]] | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.key = gemini_api_key
        self.tasks_queue = tasks_queue
        self.config = config
        self.channel = channel
        self.dry_run = dry_run
        self.excerpt_chars = excerpt_chars
        self.known_classifications = known_classifications or []
        self.stop_event = stop_event or threading.Event()
        self._s3client = None

    def __call__(self) -> None:
        gemini_client = create_client(self.key)
        prev_req_time = None
        while True:
            if self.stop_event.is_set():
                self.log("Stop signal received, shutting down")
                return
            try:
                doc = self.tasks_queue.get(block=False)
            except Empty:
                self.log("No tasks left, shutting down")
                return

            try:
                self.log(f"Evaluating {doc.md5}")
                prev_req_time = self._sleep_if_needed(prev_req_time, min_delay_seconds=12)
                evaluation = self._evaluate(doc, gemini_client)
                if not evaluation:
                    self.log(f"Empty model response for {doc.md5}")
                    self.channel.add_unprocessable_doc(doc.md5)
                    continue
                self._save_result(doc.md5, evaluation)
            except ServerError as e:
                if _is_retryable_server_error(e):
                    self.log(f"Gemini 503 for {doc.md5}")
                    self.tasks_queue.put(doc)
                    prev_req_time = self._sleep_if_needed(prev_req_time, min_delay_seconds=50)
                    continue
                self.log(f"ServerError for {doc.md5}: {e}")
                self.channel.add_unprocessable_doc(doc.md5)
            except ClientError as e:
                self.log(f"ClientError for {doc.md5}: {e}")
                # if _is_daily_free_tier_quota_exhausted(e):
                #     self.channel.add_exceeded_key(self.key)
                #     self.tasks_queue.put(doc)
                #     return
                # self.channel.add_unprocessable_doc(doc.md5)
                self.channel.add_exceeded_key(self.key)
                self.channel.add_unprocessable_doc(doc.md5)
                return
            except Exception as e:  # noqa: BLE001
                import traceback

                self.log(f"Unhandled error for {doc.md5}: {e}\n{traceback.format_exc()}")
                self.channel.add_unprocessable_doc(doc.md5)

    def _sleep_if_needed(
        self,
        prev_req_time: datetime.datetime | None,
        min_delay_seconds: int = 60,
    ) -> datetime.datetime:
        if prev_req_time:
            elapsed = datetime.datetime.now() - prev_req_time
            if elapsed < datetime.timedelta(seconds=min_delay_seconds):
                time.sleep(min_delay_seconds - elapsed.total_seconds())
        return datetime.datetime.now()

    def _evaluate(self, doc: EvaluationTask, gemini_client) -> Evaluation | None:
        flattened_meta = extract_flat_fields(doc.schema_org)
        excerpt = self._load_content_excerpt(doc)
        upstream_metadata = self._load_upstream_metadata(doc)
        files: dict[str, str] = {}
        if excerpt is None and doc.mime_type == "application/pdf":
            if slice_path := self._prepare_pdf_slice_for_eval(doc):
                files[slice_path] = "application/pdf"
        payload = _drop_none_values({
            # "md5": doc.md5,
            # "ya_path": doc.ya_path,
            "title": flattened_meta["title"],
            "author": flattened_meta["author"],
            "publisher": flattened_meta["publisher"],
            "genre": flattened_meta["genre"],
            # "language": doc.language,
            "publish_year": flattened_meta["publish_year"],
            "isbn": flattened_meta["isbn"],
            "page_count": doc.page_count,
            "upstream_metadata": upstream_metadata,
            "pdf_slice_attached": bool(files),
            "missing_fields": _collect_patch_fields(doc.schema_org),
            "known_classifications": [
                {"ddc": item["ddc"], "path": item["path"]}
                for item in self.known_classifications
            ],
        })

        prompt = build_library_applicability_prompt(payload, content_excerpt=excerpt)
        self._dump_prompt(doc.md5, prompt)

        response, uploaded_files = gemini_api(
            prompt=prompt,
            model=MODEL,
            client=gemini_client,
            files=files,
            schema=Evaluation,
            timeout_sec=180,
        )
        try:
            raw_response = stream_text(response)
            self.log(
                f"Raw eval response for {doc.md5}:\n"
                f"{_format_response_for_log(raw_response)}"
            )
            if not raw_response:
                return None
            evaluation = Evaluation.model_validate_json(raw_response)
            evaluation.metadata_patch = _normalize_metadata_patch(
                evaluation.metadata_patch,
                doc,
                self.config,
            )
            normalized_ddc, normalized_path = _normalize_library_classification(
                evaluation.library_ddc,
                evaluation.library_path,
                applicable=evaluation.applicable,
            )
            evaluation.library_ddc = normalized_ddc
            evaluation.library_path = normalized_path
            if evaluation.applicable and (not evaluation.library_ddc or not evaluation.library_path):
                self.log(f"Missing mandatory library classification for {doc.md5}")
                return None
            return evaluation
        finally:
            for file in uploaded_files:
                try:
                    gemini_client.files.delete(name=file.name)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Failed to delete uploaded eval file {file.name}: {exc}")

    def _load_content_excerpt(self, doc: EvaluationTask) -> str | None:
        if self.excerpt_chars <= 0:
            return None
        if not doc.content_url:
            return None
        try:
            content_bucket = self.config["yandex"]["cloud"]["bucket"]["content"]
            local_zip = get_in_workdir(Dirs.CONTENT, file=f"{doc.md5}.zip")
            if not os.path.exists(local_zip):
                s3client = self._get_s3client()
                local_zip, _, _ = _ensure_local_zip(doc.md5, doc.content_url, s3client, content_bucket)
            markdown = _read_markdown_from_zip(local_zip, doc.md5)
            return _build_content_excerpt(markdown, self.excerpt_chars)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not build excerpt for {doc.md5}: {exc}")
            return None

    def _load_upstream_metadata(self, doc: EvaluationTask) -> str | None:
        if not doc.upstream_meta_url:
            return None
        try:
            return load_upstream_metadata(doc.upstream_meta_url, doc.md5)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not load upstream metadata for {doc.md5}: {exc}")
            return None

    def _dump_prompt(self, md5: str, prompt: list[dict[str, Any]]) -> None:
        try:
            prompt_path = get_in_workdir(Dirs.PROMPTS, file=f"{md5}-meta-eval-prompt.txt")
            with open(prompt_path, "w") as fh:
                fh.write(json.dumps(prompt, ensure_ascii=False, indent=4))
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not dump eval prompt for {md5}: {exc}")

    def _prepare_pdf_slice_for_eval(self, doc: EvaluationTask) -> str | None:
        if doc.mime_type != "application/pdf":
            return None
        local_pdf = get_in_workdir(Dirs.ENTRY_POINT, file=f"{doc.md5}.pdf")
        try:
            if not os.path.exists(local_pdf):
                source_url = _resolve_doc_source_url(doc, self.config)
                if not source_url:
                    return None
                _download_file(source_url, local_pdf)

            slice_path = get_in_workdir(Dirs.DOC_SLICES, doc.md5, file="slice-for-eval.pdf")
            with fitz.open(local_pdf) as pdf_doc, fitz.open() as doc_slice:
                pages = list(range(0, pdf_doc.page_count))
                pages = sorted(list(set(pages[:EVAL_PDF_SLICE_SIZE] + pages[-EVAL_PDF_SLICE_SIZE:])))
                _insert_page_ranges(pdf_doc, doc_slice, pages)
                doc_slice.save(slice_path)
            return slice_path
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not prepare PDF slice for {doc.md5}: {exc}")
            return None

    def _get_s3client(self):
        if self._s3client is None:
            self._s3client = create_session(self.config)
        return self._s3client

    def _save_result(self, md5: str, evaluation: Evaluation) -> None:
        if self.dry_run:
            self.log(f"Dry-run: would persist evaluation for {md5}")
            return
        with get_session() as session:
            metadata = session.get(Metadata, md5)
            if metadata:
                metadata.lib = bool(evaluation.applicable)
                metadata.lib_eval_method = EVAL_METHOD
                if evaluation.applicable and evaluation.library_ddc and evaluation.library_path:
                    metadata.classification_id = _resolve_classification_id(
                        session,
                        evaluation.library_ddc,
                        evaluation.library_path,
                    )
                else:
                    metadata.classification_id = None
                schema_org = metadata.schema_org if isinstance(metadata.schema_org, dict) else {}
                applied: list[str] = []
                if evaluation.metadata_patch:
                    patch_payload = json.loads(
                        evaluation.metadata_patch.model_dump_json(
                            by_alias=True,
                            exclude_none=True,
                            ensure_ascii=False,
                        )
                    )
                    schema_org, patch_applied = _apply_metadata_patch(schema_org, patch_payload)
                    applied.extend(patch_applied)
                schema_org, classification_applied = _sync_auxiliary_terms_in_about(
                    schema_org=schema_org,
                    applicable=evaluation.applicable,
                    ddc=evaluation.library_ddc,
                    path=evaluation.library_path,
                )
                applied.extend(classification_applied)
                schema_org, url_applied = _sanitize_schema_urls(schema_org)
                if url_applied:
                    applied.append("url")
                if applied:
                    metadata.schema_org = schema_org
                    self.log(f"Patched metadata for {md5}: {', '.join(applied)}")
                session.commit()

    def log(self, message: str) -> None:
        print(f"{threading.current_thread().name} {time.strftime('%d-%m-%y %H:%M:%S')} {self.key[-7:]}: {message}")


class Channel:
    """Shared state between workers: exhausted keys and failed docs."""

    def __init__(self, dry_run: bool):
        self.lock = threading.Lock()
        self.dry_run = dry_run
        self.keys_dir = os.path.join(ARTIFACTS_DIR, "expired_keys")
        self.unprocessable_docs = self._load_file(UNPROCESSABLES_DIR, "unprocessables_eval.txt")
        self.exceeded_keys_set = load_expired_keys(dir=self.keys_dir)

    def get_all_unprocessable_docs(self) -> set[str]:
        return self.unprocessable_docs

    def dump(self) -> None:
        if self.dry_run:
            return
        with self.lock:
            dump_expired_keys(self.exceeded_keys_set, dir=self.keys_dir)
            self._dump_to_file(UNPROCESSABLES_DIR, "unprocessables_eval.txt", self.unprocessable_docs)

    def _load_file(self, dir_name: str, file_name: str) -> set[str]:
        candidates = [os.path.join(dir_name, file_name)]
        if dir_name.startswith(f"{ARTIFACTS_DIR}/"):
            candidates.append(os.path.join(dir_name.removeprefix(f"{ARTIFACTS_DIR}/"), file_name))

        loaded = set()
        for file_path in candidates:
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    loaded.update({line.strip() for line in f.readlines() if line.strip()})
        return loaded

    def _dump_to_file(self, dir_name: str, file_name: str, items: set[str]) -> None:
        os.makedirs(dir_name, exist_ok=True)
        file_path = os.path.join(dir_name, file_name)
        with open(file_path, "w") as f:
            f.write("\n".join(sorted(items)))

    def add_exceeded_key(self, key: str) -> None:
        with self.lock:
            self.exceeded_keys_set.add(key)
            if not self.dry_run:
                dump_expired_keys(self.exceeded_keys_set, dir=self.keys_dir)

    def add_unprocessable_doc(self, md5: str) -> None:
        with self.lock:
            self.unprocessable_docs.add(md5)
            if not self.dry_run:
                self._dump_to_file(UNPROCESSABLES_DIR, "unprocessables_eval.txt", self.unprocessable_docs)


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


def _is_retryable_server_error(error: Exception) -> bool:
    """Return True for temporary retryable Gemini server errors (503)."""
    for value in (getattr(error, "status_code", None), getattr(error, "code", None)):
        if isinstance(value, int) and value == 503:
            return True
    message = str(error).casefold()
    if "503" in message and "unavailable" in message:
        return True
    if "currently experiencing high demand" in message:
        return True
    return False


def _is_daily_free_tier_quota_exhausted(error: Exception) -> bool:
    """Return True when key hit daily free-tier request quota and worker should rotate key."""
    text = str(error)
    if "GenerateRequestsPerDayPerProjectPerModel-FreeTier" in text:
        return True
    if "generate_content_free_tier_requests" in text:
        return True
    details = getattr(error, "details", None)
    if not details:
        return False
    if isinstance(details, str):
        return "GenerateRequestsPerDayPerProjectPerModel-FreeTier" in details
    if not isinstance(details, dict):
        return False
    error_obj = details.get("error")
    if not isinstance(error_obj, dict):
        return False
    detail_items = error_obj.get("details")
    if not isinstance(detail_items, list):
        return False
    for item in detail_items:
        if not isinstance(item, dict):
            continue
        violations = item.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            quota_id = str(violation.get("quotaId") or "")
            if quota_id == "GenerateRequestsPerDayPerProjectPerModel-FreeTier":
                return True
    return False


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


def _normalize_metadata_patch(raw_patch: BookPatch | dict[str, Any] | None, doc: EvaluationTask, config: dict) -> BookPatch | None:
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

    schema = doc.schema_org if isinstance(doc.schema_org, dict) else {}
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
        if not clean:
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
        if CYRILLIC_RE.search(text):
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


def _resolve_doc_source_url(doc: EvaluationTask, config: dict) -> str | None:
    source_url = doc.document_url or doc.ya_public_url
    if not source_url:
        return None
    if source_url.startswith(prefix):
        return decrypt(source_url, config)
    return source_url


def _fallback_pdf_page_count(doc: EvaluationTask, config: dict) -> int | None:
    if doc.mime_type != "application/pdf":
        return None
    local_pdf = get_in_workdir(Dirs.ENTRY_POINT, file=f"{doc.md5}.pdf")
    if not os.path.exists(local_pdf):
        source_url = _resolve_doc_source_url(doc, config)
        if not source_url:
            return None
        _download_file(source_url, local_pdf)
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


def _build_defined_term(term_code: str, termset: str) -> dict[str, str]:
    return {
        "@type": "DefinedTerm",
        "termCode": term_code,
        "inDefinedTermSet": termset,
    }


def _is_managed_about_term(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    termset = _clean_text(item.get("inDefinedTermSet"), max_len=120)
    return bool(termset and termset.casefold() in MANAGED_TERMSETS)


def _extract_about_term_values(items: list[Any], termset: str) -> list[str]:
    target_set = termset.casefold()
    values: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_set = _clean_text(item.get("inDefinedTermSet"), max_len=120)
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
