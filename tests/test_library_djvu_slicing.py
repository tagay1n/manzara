"""Visual DjVu edge-page slicing coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app.modules.library import djvu_slicing
from app.modules.library.corrupt_document import CorruptDocumentError


def test_djvu_slice_renders_unique_edge_pages_in_source_order(
    monkeypatch, tmp_path: Path
) -> None:
    rendered_pages: list[int] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "djvused":
            return subprocess.CompletedProcess(command, 0, "10\n", "")
        page_number = int(next(arg for arg in command if arg.startswith("-page="))[6:])
        rendered_pages.append(page_number)
        Image.new("RGB", (20, 30), (page_number * 20, 0, 0)).save(command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djvu_slicing, "_run_djvu_command", run)
    destination = tmp_path / "slice.pdf"
    assert djvu_slicing.create_djvu_slice(
        tmp_path / "source.djvu", destination, edge_pages=4
    ) == 8
    assert rendered_pages == [1, 2, 3, 4, 7, 8, 9, 10]
    with pymupdf.open(destination) as pdf:
        assert pdf.page_count == 8
        assert [pdf[index].get_pixmap().samples[0] for index in range(8)] == [
            page * 20 for page in rendered_pages
        ]


def test_djvu_slice_rejects_empty_document(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        djvu_slicing,
        "_run_djvu_command",
        lambda command: subprocess.CompletedProcess(command, 0, "0\n", ""),
    )
    with pytest.raises(CorruptDocumentError, match="djvu_page_tree"):
        djvu_slicing.create_djvu_slice(
            tmp_path / "empty.djvu", tmp_path / "slice.pdf", edge_pages=4
        )


def test_missing_djvu_binary_is_operational_failure(monkeypatch) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("djvused")

    monkeypatch.setattr(djvu_slicing.subprocess, "run", missing)
    with pytest.raises(djvu_slicing.DjvuToolError, match="Missing DjVuLibre"):
        djvu_slicing._run_djvu_command(["djvused", "source.djvu", "-e", "n"])


def test_oversized_djvu_slice_rerenders_at_smaller_size(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        djvu_slicing,
        "_run_djvu_command",
        lambda command: subprocess.CompletedProcess(command, 0, "10\n", ""),
    )
    sizes: list[str] = []

    def render(_source, destination, _pages, render_size):  # noqa: ANN001
        sizes.append(render_size)
        with destination.open("wb") as output:
            output.truncate(51_000_000 if len(sizes) == 1 else 42_000_000)

    monkeypatch.setattr(djvu_slicing, "_render_pages", render, raising=False)

    assert djvu_slicing.create_djvu_slice(
        tmp_path / "source.djvu", tmp_path / "slice.pdf", edge_pages=3
    ) == 6
    assert sizes == ["3000x3000", "2400x2400"]


def test_djvu_slice_still_oversized_is_operational_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        djvu_slicing,
        "_run_djvu_command",
        lambda command: subprocess.CompletedProcess(command, 0, "10\n", ""),
    )
    sizes: list[str] = []

    def render(_source, destination, _pages, render_size):  # noqa: ANN001
        sizes.append(render_size)
        with destination.open("wb") as output:
            output.truncate(djvu_slicing.MAX_DJVU_PDF_BYTES + 1)

    monkeypatch.setattr(djvu_slicing, "_render_pages", render)
    destination = tmp_path / "slice.pdf"

    with pytest.raises(djvu_slicing.DjvuToolError, match="exceeds"):
        djvu_slicing.create_djvu_slice(
            tmp_path / "source.djvu", destination, edge_pages=3
        )

    assert sizes == list(djvu_slicing.DJVU_RENDER_SIZES)
    assert not destination.exists()
