"""Sequential migration of legacy extracted PDF content to Backblaze."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Mapping
import zipfile

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from app.document_storage import (
    DocumentStorageSettings,
    object_url,
    parse_object_url,
)


MIGRATION_VERSION = "pdf-content-b2.v1"
SINGLE_THREAD_TRANSFER_CONFIG = TransferConfig(
    use_threads=False,
    max_concurrency=1,
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>()\[\]]*")
_MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class ContentMigrationCandidate:
    md5: str
    mime_type: str
    source_content_url: str
    status: str


class _StopAfterCheckpoint(RuntimeError):
    pass


def _etag(value: Any) -> str:
    return str(value or "").strip().strip('"')


def _head_or_none(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return dict(s3.head_object(Bucket=bucket, Key=key))
    except KeyError:
        return None
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rewrite_content_archive(
    payload: bytes,
    *,
    md5: str,
    legacy_endpoint: str,
    legacy_bucket: str,
    primary_endpoint: str,
    primary_bucket: str,
) -> tuple[bytes, tuple[str, ...], str]:
    """Rewrite only legacy image URLs in a one-Markdown-file ZIP."""
    digest = str(md5 or "").strip().lower()
    if not _MD5_PATTERN.fullmatch(digest):
        raise ValueError(f"Invalid document MD5: {md5!r}")
    try:
        with zipfile.ZipFile(BytesIO(payload)) as source:
            members = source.infolist()
            if len(members) != 1 or members[0].is_dir():
                raise ValueError("Content archive must contain exactly one file")
            member = members[0].filename
            safe_path = PurePosixPath(member)
            if (
                safe_path.is_absolute()
                or ".." in safe_path.parts
                or safe_path.suffix.lower() not in {".md", ".markdown"}
            ):
                raise ValueError("Content archive member must be safe Markdown")
            markdown = source.read(members[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise ValueError(f"Invalid content archive: {exc}") from exc

    referenced: list[str] = []

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = parse_object_url(url, legacy_endpoint)
        if parsed is None or parsed[0] != legacy_bucket:
            return url
        key = parsed[1]
        if not key.startswith(f"{digest}-") or "/" in key or key in {"", ".", ".."}:
            raise ValueError(
                f"Legacy image key does not belong to document {digest}: {key}"
            )
        if key not in referenced:
            referenced.append(key)
        return object_url(primary_endpoint, primary_bucket, key)

    rewritten_markdown = _URL_PATTERN.sub(replace, markdown)
    for match in _URL_PATTERN.finditer(rewritten_markdown):
        parsed = parse_object_url(match.group(0), legacy_endpoint)
        if parsed is not None and parsed[0] == legacy_bucket:
            raise ValueError("Rewritten Markdown still contains a legacy image URL")

    output = BytesIO()
    info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with zipfile.ZipFile(output, "w", compresslevel=9) as target:
        target.writestr(info, rewritten_markdown.encode("utf-8"))
    return output.getvalue(), tuple(referenced), member


def _remote_matches(
    head: Mapping[str, Any] | None,
    *,
    source_etag: str,
    source_size: int,
    destination_size: int,
    sha256: str,
) -> bool:
    if not head:
        return False
    metadata = head.get("Metadata")
    if not isinstance(metadata, Mapping):
        return False
    return (
        int(head.get("ContentLength") or -1) == destination_size
        and str(metadata.get("migration-version") or "") == MIGRATION_VERSION
        and str(metadata.get("source-etag") or "") == source_etag
        and str(metadata.get("source-size") or "") == str(source_size)
        and str(metadata.get("sha256") or "") == sha256
    )


def _copy_verified_file(
    *,
    source_s3: Any,
    destination_s3: Any,
    source_bucket: str,
    destination_bucket: str,
    key: str,
    path: Path,
    content_type: str,
    public_check: Callable[[str], bool],
    public_url: str,
) -> tuple[dict[str, Any], str, int, str, bool]:
    source_head = _head_or_none(source_s3, source_bucket, key)
    if source_head is None:
        raise FileNotFoundError(f"Missing source object s3://{source_bucket}/{key}")
    source_size = int(source_head.get("ContentLength") or 0)
    source_etag = _etag(source_head.get("ETag"))
    path.parent.mkdir(parents=True, exist_ok=True)
    source_s3.download_file(
        source_bucket,
        key,
        str(path),
        Config=SINGLE_THREAD_TRANSFER_CONFIG,
    )
    if path.stat().st_size != source_size:
        raise RuntimeError(f"Source size changed while downloading {key}")
    digest = _sha256(path)
    destination_head = _head_or_none(destination_s3, destination_bucket, key)
    reused = _remote_matches(
        destination_head,
        source_etag=source_etag,
        source_size=source_size,
        destination_size=source_size,
        sha256=digest,
    )
    if not reused:
        destination_s3.upload_file(
            str(path),
            destination_bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "Metadata": {
                    "migration-version": MIGRATION_VERSION,
                    "source-etag": source_etag,
                    "source-size": str(source_size),
                    "sha256": digest,
                },
            },
            Config=SINGLE_THREAD_TRANSFER_CONFIG,
        )
        destination_head = _head_or_none(destination_s3, destination_bucket, key)
    if not _remote_matches(
        destination_head,
        source_etag=source_etag,
        source_size=source_size,
        destination_size=source_size,
        sha256=digest,
    ):
        raise RuntimeError(f"Destination verification failed for {key}")
    if not public_check(public_url):
        raise RuntimeError(f"Destination is not publicly readable: {public_url}")
    return dict(destination_head or {}), source_etag, source_size, digest, reused


def _upload_rewritten_archive(
    *,
    primary_s3: Any,
    bucket: str,
    key: str,
    path: Path,
    source_etag: str,
    source_size: int,
    public_url: str,
    public_check: Callable[[str], bool],
) -> tuple[dict[str, Any], str, bool]:
    digest = _sha256(path)
    destination_size = path.stat().st_size
    head = _head_or_none(primary_s3, bucket, key)
    reused = _remote_matches(
        head,
        source_etag=source_etag,
        source_size=source_size,
        destination_size=destination_size,
        sha256=digest,
    )
    if not reused:
        primary_s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": "application/zip",
                "Metadata": {
                    "migration-version": MIGRATION_VERSION,
                    "source-etag": source_etag,
                    "source-size": str(source_size),
                    "sha256": digest,
                },
            },
            Config=SINGLE_THREAD_TRANSFER_CONFIG,
        )
        head = _head_or_none(primary_s3, bucket, key)
    if not _remote_matches(
        head,
        source_etag=source_etag,
        source_size=source_size,
        destination_size=destination_size,
        sha256=digest,
    ):
        raise RuntimeError(f"Destination verification failed for {key}")
    if not public_check(public_url):
        raise RuntimeError(f"Destination is not publicly readable: {public_url}")
    return dict(head or {}), digest, reused


def _delete_and_confirm(s3: Any, bucket: str, key: str) -> None:
    if _head_or_none(s3, bucket, key) is not None:
        s3.delete_object(Bucket=bucket, Key=key)
    if _head_or_none(s3, bucket, key) is not None:
        raise RuntimeError(f"Source object remains after deletion: s3://{bucket}/{key}")


def _verify_checkpointed_destinations(
    *,
    repository: Any,
    primary_s3: Any,
    settings: DocumentStorageSettings,
    md5: str,
    archive_key: str,
    public_check: Callable[[str], bool],
) -> list[dict[str, Any]]:
    archive = repository.get_archive_checkpoint(md5)
    if not archive:
        raise RuntimeError("Archive checkpoint is missing before source deletion")
    archive_head = _head_or_none(primary_s3, settings.content_bucket, archive_key)
    if not _remote_matches(
        archive_head,
        source_etag=str(archive.get("source_archive_etag") or ""),
        source_size=int(archive.get("source_archive_size") or 0),
        destination_size=int(archive.get("destination_archive_size") or 0),
        sha256=str(archive.get("destination_archive_sha256") or ""),
    ):
        raise RuntimeError("Checkpointed destination archive no longer verifies")
    archive_url = object_url(
        settings.primary.endpoint_url, settings.content_bucket, archive_key
    )
    if not public_check(archive_url):
        raise RuntimeError("Checkpointed destination archive is not publicly readable")
    images = repository.list_images(md5)
    for image in images:
        image_key = str(image["image_key"])
        head = _head_or_none(primary_s3, settings.content_images_bucket, image_key)
        if not _remote_matches(
            head,
            source_etag=str(image.get("source_etag") or ""),
            source_size=int(image.get("source_size") or 0),
            destination_size=int(image.get("destination_size") or 0),
            sha256=str(image.get("sha256") or ""),
        ):
            raise RuntimeError(
                f"Checkpointed destination image no longer verifies: {image_key}"
            )
        image_url = object_url(
            settings.primary.endpoint_url,
            settings.content_images_bucket,
            image_key,
        )
        if not public_check(image_url):
            raise RuntimeError(
                f"Checkpointed destination image is not publicly readable: {image_key}"
            )
    return images


def _publish_progress(
    state_db: Any,
    run_id: int,
    *,
    current: int,
    total: int,
    counters: Mapping[str, int],
    stage: str,
) -> None:
    payload = {
        "stage": stage,
        "current": current,
        "total": total,
        "percent": 100 if total == 0 else round(current / total * 100, 2),
        **{key: int(value) for key, value in counters.items()},
    }
    state_db.update_run_progress(run_id, payload)
    state_db.insert_event(
        "task.progress",
        task_id="maintenance.migrate_pdf_content",
        run_id=run_id,
        panel_id="library",
        payload={"status": "running", "progress": payload},
    )


def run_content_storage_migration(
    *,
    repository: Any,
    state_db: Any,
    legacy_s3: Any,
    primary_s3: Any,
    settings: DocumentStorageSettings,
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
    public_check: Callable[[str], bool],
    md5: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Move candidates sequentially with PostgreSQL-backed recovery points."""
    candidates = repository.list_work(md5=md5, limit=limit)
    total = len(candidates)
    counters: Counter[str] = Counter(
        migrated=0,
        failed=0,
        images_uploaded=0,
        images_reused=0,
        archives_uploaded=0,
        archives_reused=0,
        source_images_deleted=0,
        source_archives_deleted=0,
        cutover_raced=0,
    )
    processed = 0
    stopped = bool(should_stop())
    workspace.mkdir(parents=True, exist_ok=True)
    _publish_progress(
        state_db, run_id, current=0, total=total, counters=counters, stage="migrating"
    )
    for candidate in candidates:
        if stopped or should_stop():
            stopped = True
            break
        doc_dir = workspace / candidate.md5
        doc_dir.mkdir(parents=True, exist_ok=True)
        archive_key = f"{candidate.md5}.zip"
        try:
            repository.start(candidate, run_id=run_id)
            if candidate.status not in {"cutover", "deleting"}:
                source_head = _head_or_none(
                    legacy_s3, settings.legacy_content_bucket, archive_key
                )
                if source_head is None:
                    raise FileNotFoundError(f"Missing source archive {archive_key}")
                source_size = int(source_head.get("ContentLength") or 0)
                source_etag = _etag(source_head.get("ETag"))
                source_path = doc_dir / "source.zip"
                legacy_s3.download_file(
                    settings.legacy_content_bucket,
                    archive_key,
                    str(source_path),
                    Config=SINGLE_THREAD_TRANSFER_CONFIG,
                )
                if source_path.stat().st_size != source_size:
                    raise RuntimeError("Source archive size changed while downloading")
                rewritten, image_keys, member = rewrite_content_archive(
                    source_path.read_bytes(),
                    md5=candidate.md5,
                    legacy_endpoint=settings.legacy.endpoint_url,
                    legacy_bucket=settings.legacy_content_images_bucket,
                    primary_endpoint=settings.primary.endpoint_url,
                    primary_bucket=settings.content_images_bucket,
                )
                repository.retain_images(candidate.md5, image_keys)
                for image_key in image_keys:
                    if should_stop():
                        raise _StopAfterCheckpoint()
                    image_url = object_url(
                        settings.primary.endpoint_url,
                        settings.content_images_bucket,
                        image_key,
                    )
                    image_path = doc_dir / "images" / image_key
                    head, image_source_etag, image_source_size, image_sha, reused = (
                        _copy_verified_file(
                            source_s3=legacy_s3,
                            destination_s3=primary_s3,
                            source_bucket=settings.legacy_content_images_bucket,
                            destination_bucket=settings.content_images_bucket,
                            key=image_key,
                            path=image_path,
                            content_type=(
                                mimetypes.guess_type(image_key)[0]
                                or "application/octet-stream"
                            ),
                            public_check=public_check,
                            public_url=image_url,
                        )
                    )
                    repository.checkpoint_image(
                        candidate.md5,
                        image_key,
                        source_etag=image_source_etag,
                        source_size=image_source_size,
                        destination_etag=_etag(head.get("ETag")),
                        destination_size=int(head.get("ContentLength") or 0),
                        sha256=image_sha,
                        source_deleted=False,
                        run_id=run_id,
                    )
                    counters["images_reused" if reused else "images_uploaded"] += 1
                rewritten_path = doc_dir / "rewritten.zip"
                rewritten_path.write_bytes(rewritten)
                if should_stop():
                    raise _StopAfterCheckpoint()
                destination_url = object_url(
                    settings.primary.endpoint_url,
                    settings.content_bucket,
                    archive_key,
                )
                archive_head, archive_sha, archive_reused = _upload_rewritten_archive(
                    primary_s3=primary_s3,
                    bucket=settings.content_bucket,
                    key=archive_key,
                    path=rewritten_path,
                    source_etag=source_etag,
                    source_size=source_size,
                    public_url=destination_url,
                    public_check=public_check,
                )
                repository.checkpoint_archive(
                    candidate.md5,
                    source_etag=source_etag,
                    source_size=source_size,
                    destination_url=destination_url,
                    destination_etag=_etag(archive_head.get("ETag")),
                    destination_size=int(archive_head.get("ContentLength") or 0),
                    sha256=archive_sha,
                    markdown_member=member,
                    run_id=run_id,
                )
                counters[
                    "archives_reused" if archive_reused else "archives_uploaded"
                ] += 1
                if not repository.cutover(
                    candidate.md5,
                    expected_url=candidate.source_content_url,
                    destination_url=destination_url,
                    expected_mime_type=candidate.mime_type,
                    run_id=run_id,
                ):
                    counters["cutover_raced"] += 1
                    raise RuntimeError("Document changed before content URL cutover")

            images = _verify_checkpointed_destinations(
                repository=repository,
                primary_s3=primary_s3,
                settings=settings,
                md5=candidate.md5,
                archive_key=archive_key,
                public_check=public_check,
            )
            if should_stop():
                raise _StopAfterCheckpoint()
            _delete_and_confirm(
                legacy_s3, settings.legacy_content_bucket, archive_key
            )
            repository.checkpoint_archive_deleted(candidate.md5, run_id=run_id)
            counters["source_archives_deleted"] += 1
            for image in images:
                if bool(image.get("source_deleted")):
                    continue
                if should_stop():
                    raise _StopAfterCheckpoint()
                image_key = str(image["image_key"])
                _delete_and_confirm(
                    legacy_s3, settings.legacy_content_images_bucket, image_key
                )
                repository.checkpoint_image_deleted(candidate.md5, image_key)
                counters["source_images_deleted"] += 1
            repository.complete(candidate.md5, run_id=run_id)
            counters["migrated"] += 1
            print(f"content migration: completed md5={candidate.md5}", flush=True)
        except _StopAfterCheckpoint:
            stopped = True
            print(
                f"content migration: stopped at checkpoint md5={candidate.md5}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-document failures.
            repository.fail(
                candidate.md5,
                run_id=run_id,
                error_text=f"{type(exc).__name__}: {exc}",
            )
            counters["failed"] += 1
            print(
                f"content migration: failed md5={candidate.md5} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
        processed += 1
        _publish_progress(
            state_db,
            run_id,
            current=processed,
            total=total,
            counters=counters,
            stage="migrating",
        )
        if stopped or should_stop():
            stopped = True
            break
    summary = {
        "kind": "maintenance.pdf_content_migration_summary",
        "pending_before": total,
        "pending_after": repository.count_pending(),
        "processed": processed,
        "stopped": stopped,
        **counters,
    }
    _publish_progress(
        state_db,
        run_id,
        current=processed,
        total=total,
        counters=counters,
        stage="stopped" if stopped else "completed",
    )
    print(f"content migration: final {json.dumps(summary, sort_keys=True)}", flush=True)
    return summary


__all__ = [
    "ContentMigrationCandidate",
    "MIGRATION_VERSION",
    "SINGLE_THREAD_TRANSFER_CONFIG",
    "rewrite_content_archive",
    "run_content_storage_migration",
]
