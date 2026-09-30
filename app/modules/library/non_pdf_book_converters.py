"""ODT HTML and MOBI EPUB conversion using installed command-line tools."""

from __future__ import annotations

import os
import shutil
import zipfile
import zlib
from pathlib import Path

from app.modules.library.non_pdf_converters import _run
from app.modules.library.non_pdf_types import ConverterCommandError


def _convert_odt_to_html(source: Path, *, workspace: Path) -> Path:
    converted = workspace / "converted"
    converted.mkdir(parents=True, exist_ok=True)
    staged_source = workspace / "source.odt"
    shutil.copyfile(source, staged_source)
    profile = workspace / "libreoffice-profile"
    _run(
        [
            "soffice",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            "html",
            "--outdir",
            str(converted),
            str(staged_source),
        ],
        workspace=workspace,
        label="libreoffice",
        timeout_seconds=900,
    )
    matches = sorted(converted.glob("*.html"))
    if len(matches) != 1 or matches[0].stat().st_size == 0:
        raise ConverterCommandError(
            f"LibreOffice produced {len(matches)} nonempty ODT HTML files"
        )
    return matches[0]


def _convert_mobi_to_epub(source: Path, *, workspace: Path) -> Path:
    if shutil.which("ebook-convert") is None:
        raise ConverterCommandError("Missing MOBI converter binary: ebook-convert")
    converted = workspace / "converted"
    converted.mkdir(parents=True, exist_ok=True)
    staged_source = workspace / "source.mobi"
    shutil.copyfile(source, staged_source)
    target = converted / "source.epub"
    calibre_config = workspace / "calibre-config"
    calibre_config.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CALIBRE_CONFIG_DIRECTORY"] = str(calibre_config)
    _run(
        ["ebook-convert", str(staged_source), str(target)],
        workspace=workspace,
        label="calibre",
        timeout_seconds=900,
        env=env,
    )
    return target


def _validate_converted_epub(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if bad_member := archive.testzip():
                raise ConverterCommandError(
                    f"Converted EPUB has a corrupt ZIP member: {bad_member}"
                )
            names = set(archive.namelist())
            if archive.read("mimetype") != b"application/epub+zip" or (
                "META-INF/container.xml" not in names
                or not any(name.endswith((".html", ".xhtml")) for name in names)
            ):
                raise ConverterCommandError("Converted EPUB lacks ebook content")
    except ConverterCommandError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise ConverterCommandError(f"Converted EPUB is invalid: {exc}") from exc
