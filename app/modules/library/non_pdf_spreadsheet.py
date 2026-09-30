"""Keep only visible sheet headings and cell tables from Calc HTML exports."""

from __future__ import annotations

from typing import Any

from app.modules.library.non_pdf_types import ConverterCommandError


def _clean_table(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("t") == "Image":
            return {"t": "Str", "c": ""}
        return {key: _clean_table(item) for key, item in value.items()}
    if isinstance(value, list):
        if (
            len(value) == 3
            and isinstance(value[0], str)
            and isinstance(value[1], list)
            and isinstance(value[2], list)
            and all(
                isinstance(item, list) and len(item) == 2
                for item in value[2]
            )
        ):
            return [
                value[0],
                value[1],
                [
                    item for item in value[2]
                    if not str(item[0]).lower().endswith("formula")
                ],
            ]
        return [_clean_table(item) for item in value]
    return value


def select_spreadsheet_blocks(ast: dict[str, Any]) -> None:
    """Drop Calc navigation and visuals while retaining table order and labels."""
    kept: list[dict[str, Any]] = []
    heading: dict[str, Any] | None = None
    sheet_number = 0
    for block in ast.get("blocks", []):
        if block.get("t") == "Header":
            heading = block
        elif block.get("t") == "Table":
            sheet_number += 1
            kept.append(heading or {
                "t": "Header",
                "c": [
                    1,
                    ["", [], []],
                    [
                        {"t": "Str", "c": "Sheet"},
                        {"t": "Space"},
                        {"t": "Str", "c": str(sheet_number)},
                    ],
                ],
            })
            kept.append(_clean_table(block))
            heading = None
    if not any(block.get("t") == "Table" for block in kept):
        raise ConverterCommandError("LibreOffice produced no spreadsheet cell tables")
    ast["blocks"] = kept
