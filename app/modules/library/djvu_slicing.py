"""Render selected DjVu pages into a visual-only PDF."""

from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf

from app.modules.library.corrupt_document import CorruptDocumentError

DJVU_RENDER_SIZES = ("3000x3000", "2400x2400", "1800x1800")
DJVU_COMMAND_TIMEOUT_SECONDS = 120
MAX_DJVU_PDF_BYTES = 49_000_000


class DjvuToolError(RuntimeError):
    """DjVu rendering could not run reliably on this host."""


def select_edge_pages(page_count: int, *, edge_pages: int) -> list[int]:
    count = max(0, int(page_count))
    edge = max(1, int(edge_pages))
    return sorted(set(range(min(edge, count))) | set(range(max(0, count - edge), count)))


def _run_djvu_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=DJVU_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise DjvuToolError(f"Missing DjVuLibre command: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DjvuToolError(
            f"DjVuLibre command timed out after {DJVU_COMMAND_TIMEOUT_SECONDS}s: "
            f"{command[0]}"
        ) from exc


def create_djvu_slice(
    source: Path, destination: Path, *, edge_pages: int
) -> int:
    """Render unique edge pages from DjVu into a PDF and return their count."""
    result = _run_djvu_command(["djvused", str(source), "-e", "n"])
    if result.returncode != 0:
        raise CorruptDocumentError("djvu_page_tree", result.stderr.strip()[-1000:])
    try:
        page_count = int(result.stdout.strip())
    except ValueError as exc:
        raise CorruptDocumentError("djvu_page_tree", "Invalid DjVu page count") from exc
    pages = select_edge_pages(page_count, edge_pages=edge_pages)
    if not pages:
        raise CorruptDocumentError("djvu_page_tree", "DjVu has no usable pages")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as temporary:
        rendered_path = Path(temporary) / "slice.pdf"
        for render_size in DJVU_RENDER_SIZES:
            _render_pages(source, rendered_path, pages, render_size)
            if rendered_path.stat().st_size <= MAX_DJVU_PDF_BYTES:
                rendered_path.replace(destination)
                return len(pages)
            rendered_path.unlink()
    raise DjvuToolError(
        f"DjVu PDF slice exceeds {MAX_DJVU_PDF_BYTES} bytes "
        f"at the smallest render size: {source}"
    )


def _render_pages(
    source: Path, destination: Path, pages: list[int], render_size: str
) -> None:
    with TemporaryDirectory(dir=destination.parent) as temporary, pymupdf.open() as sliced:
        for page_index in pages:
            image_path = Path(temporary) / f"page-{page_index + 1}.tiff"
            rendered = _run_djvu_command(
                [
                    "ddjvu",
                    "-format=tiff",
                    f"-size={render_size}",
                    f"-page={page_index + 1}",
                    str(source),
                    str(image_path),
                ]
            )
            if rendered.returncode != 0:
                raise CorruptDocumentError(
                    "djvu_page_read", rendered.stderr.strip()[-1000:]
                )
            try:
                with pymupdf.open(image_path) as image:
                    pdf_bytes = image.convert_to_pdf()
                with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf_page:
                    sliced.insert_pdf(pdf_page)
            except (pymupdf.FileDataError, RuntimeError) as exc:
                raise CorruptDocumentError("djvu_page_read", str(exc)) from exc
        sliced.save(destination, deflate=True)
