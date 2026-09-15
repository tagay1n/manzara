"""External converter execution, DOCX normalization, and FB2 conversion."""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import zipfile
import zlib
from html import escape
from pathlib import Path
from xml.etree import ElementTree

from app.modules.library.non_pdf_types import (
    ConverterCommandError,
    ConverterTimeoutError,
)


def require_converter_binaries() -> None:
    missing = [name for name in ("pandoc", "soffice") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "Missing required document conversion binaries: " + ", ".join(missing)
        )


def _run(
    command: list[str],
    *,
    workspace: Path,
    label: str,
    stdin: str | None = None,
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate_process_group(process)
        process.communicate()
        raise
    (workspace / f"{label}.stdout.log").write_text(stdout, encoding="utf-8")
    (workspace / f"{label}.stderr.log").write_text(stderr, encoding="utf-8")
    if timed_out:
        raise ConverterTimeoutError(
            f"{label} timed out after {timeout_seconds} seconds"
        )
    if process.returncode != 0:
        raise ConverterCommandError(
            f"{label} failed with exit code {process.returncode}: "
            f"{stderr.strip()[-1000:]}"
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _convert_to_docx(
    source: Path, *, workspace: Path, detected_format: str
) -> Path:
    converted = workspace / "converted"
    converted.mkdir(parents=True, exist_ok=True)
    # LibreOffice primarily selects its input filter from the filename. Give it
    # the byte-detected suffix instead of the unreliable catalog/cache suffix.
    staged_source = workspace / f"source.{detected_format}"
    shutil.copyfile(source, staged_source)
    profile = workspace / "libreoffice-profile"
    _run(
        [
            "soffice",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless", "--convert-to", "docx", "--outdir", str(converted),
            str(staged_source),
        ],
        workspace=workspace,
        label="libreoffice",
        timeout_seconds=900,
    )
    matches = sorted(converted.glob("*.docx"))
    if len(matches) != 1:
        raise ConverterCommandError(
            f"LibreOffice produced {len(matches)} DOCX files"
        )
    return matches[0]


def _validate_converted_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if bad_member := archive.testzip():
                raise ConverterCommandError(
                    f"Converted DOCX has a corrupt ZIP member: {bad_member}"
                )
            if "word/document.xml" not in set(archive.namelist()):
                raise ConverterCommandError(
                    "Converted DOCX is missing word/document.xml"
                )
    except ConverterCommandError:
        raise
    except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise ConverterCommandError(f"Converted DOCX is invalid: {exc}") from exc


def _normalize_docx_archive(path: Path, *, workspace: Path) -> Path:
    """Rewrite a DOCX without producer-specific ZIP data descriptors."""
    normalized_dir = workspace / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = normalized_dir / "source.docx"
    try:
        with (
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(
                normalized_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as target,
        ):
            for source_info in source.infolist():
                target_info = zipfile.ZipInfo(
                    source_info.filename,
                    date_time=source_info.date_time,
                )
                target_info.compress_type = (
                    zipfile.ZIP_STORED
                    if source_info.is_dir()
                    else zipfile.ZIP_DEFLATED
                )
                target_info.external_attr = source_info.external_attr
                target_info.create_system = source_info.create_system
                target_info.comment = source_info.comment
                with source.open(source_info) as input_stream:
                    with target.open(target_info, "w") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream)
    except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise ConverterCommandError(f"Converted DOCX normalization failed: {exc}") from exc
    return normalized_path


def _fb2_to_html(source: Path, *, workspace: Path) -> Path:
    root = ElementTree.parse(source).getroot()
    parents = {child: parent for parent in root.iter() for child in parent}
    images_dir = workspace / "fb2-media"
    images_dir.mkdir(parents=True, exist_ok=True)
    image_paths: dict[str, Path] = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "binary":
            continue
        identifier = str(node.attrib.get("id") or "").strip()
        if not identifier or not str(node.text or "").strip():
            continue
        content_type = str(node.attrib.get("content-type") or "image/png").lower()
        suffix = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/svg+xml": ".svg",
        }.get(content_type, ".bin")
        destination = images_dir / f"{identifier}{suffix}"
        destination.write_bytes(base64.b64decode("".join(str(node.text).split())))
        image_paths[identifier] = destination
    parts = ["<!doctype html><html><body>"]
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1]
        text_value = " ".join("".join(node.itertext()).split())
        if name == "title" and text_value:
            parts.append(f"<h2>{escape(text_value)}</h2>")
        elif name in {"p", "subtitle", "text-author"} and text_value:
            parent = parents.get(node)
            if (
                name == "p"
                and parent is not None
                and parent.tag.rsplit("}", 1)[-1] == "title"
            ):
                continue
            parts.append(f"<p>{escape(text_value)}</p>")
        elif name == "image":
            href = next(
                (str(value).lstrip("#") for key, value in node.attrib.items() if key.endswith("href")),
                "",
            )
            if href in image_paths:
                parts.append(f'<figure><img src="{escape(str(image_paths[href]), quote=True)}"></figure>')
    parts.append("</body></html>")
    destination = workspace / "fb2.html"
    destination.write_text("\n".join(parts), encoding="utf-8")
    return destination
