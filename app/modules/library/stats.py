"""Library dataset statistics sourced from the shared monocorpus database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[3]
_REDACTED_SENTINEL = "<REDACTED>"


def _candidate_config_paths() -> Iterable[Path]:
    """Return config candidates in local-first order."""
    return (
        REPO_ROOT / "config.local.yaml",
        REPO_ROOT / "config.yaml",
    )


def _load_runtime_config() -> tuple[Dict[str, Any], Path]:
    """Load config used by embedded runtime stats lookups."""
    for path in _candidate_config_paths():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict):
            continue
        return payload, path
    raise FileNotFoundError("No config file found for library stats")


def get_runtime_database_url() -> tuple[str, str]:
    """Return runtime database URL and source config filename."""
    config, config_source = _load_runtime_config()
    db_url = str(config.get("database_url") or "").strip()
    if not db_url:
        raise ValueError("database_url is missing in runtime config")
    if _REDACTED_SENTINEL in db_url:
        raise ValueError("database_url is masked; use local unmasked config")
    return db_url, str(config_source.name)


def create_runtime_engine() -> tuple[Engine, str]:
    """Create SQLAlchemy engine for monocorpus runtime database."""
    db_url, config_source = get_runtime_database_url()
    return create_engine(db_url), config_source


def _parse_json_path(path_value: Any) -> str:
    """Format classification path JSON into a short readable string."""
    if isinstance(path_value, list):
        items = [str(item).strip() for item in path_value if str(item).strip()]
        return " / ".join(items)
    if isinstance(path_value, str):
        raw = path_value.strip()
        if not raw:
            return "-"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, list):
            items = [str(item).strip() for item in parsed if str(item).strip()]
            return " / ".join(items) if items else "-"
        return raw
    return "-"


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def get_library_dataset_stats(top_limit: int = 8) -> Dict[str, Any]:
    """Return aggregate Library metrics from the shared runtime database."""
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            total_documents = int(conn.execute(text("SELECT COUNT(*) FROM document")).scalar() or 0)
            metadata_rows = int(conn.execute(text("SELECT COUNT(*) FROM metadata")).scalar() or 0)
            applicable_docs = int(
                conn.execute(text("SELECT COUNT(*) FROM metadata WHERE lib IS TRUE")).scalar() or 0
            )
            non_applicable_docs = int(
                conn.execute(text("SELECT COUNT(*) FROM metadata WHERE lib IS FALSE")).scalar() or 0
            )
            pending_evaluation = int(
                conn.execute(text("SELECT COUNT(*) FROM metadata WHERE lib IS NULL")).scalar() or 0
            )
            classified_docs = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM metadata WHERE lib IS TRUE AND classification_id IS NOT NULL")
                ).scalar()
                or 0
            )

            top_rows = conn.execute(
                text(
                    """
                    WITH usage AS (
                        SELECT
                            m.classification_id AS classification_id,
                            COUNT(m.md5) AS usage_count
                        FROM metadata m
                        WHERE m.classification_id IS NOT NULL
                        GROUP BY m.classification_id
                    )
                    SELECT
                        c.id AS classification_id,
                        c.ddc AS ddc,
                        c.path_en AS path_en,
                        u.usage_count AS usage_count
                    FROM usage u
                    JOIN classification c ON c.id = u.classification_id
                    ORDER BY u.usage_count DESC, c.ddc ASC
                    LIMIT :limit
                    """
                ),
                {"limit": max(1, int(top_limit))},
            ).mappings().all()
        engine.dispose()

        evaluated_docs = applicable_docs + non_applicable_docs
        return {
            "available": True,
            "error": None,
            "config_source": str(config_source),
            "stats": {
                "total_documents": total_documents,
                "metadata_rows": metadata_rows,
                "applicable_docs": applicable_docs,
                "non_applicable_docs": non_applicable_docs,
                "pending_evaluation": pending_evaluation,
                "classified_docs": classified_docs,
                "evaluated_docs": evaluated_docs,
                "acceptance_rate": _safe_ratio(applicable_docs, evaluated_docs),
                "classification_coverage": _safe_ratio(classified_docs, applicable_docs),
            },
            "top_classifications": [
                {
                    "classification_id": int(row.get("classification_id") or 0),
                    "ddc": str(row.get("ddc") or ""),
                    "path": _parse_json_path(row.get("path_en")),
                    "usage_count": int(row.get("usage_count") or 0),
                }
                for row in top_rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "stats": {
                "total_documents": 0,
                "metadata_rows": 0,
                "applicable_docs": 0,
                "non_applicable_docs": 0,
                "pending_evaluation": 0,
                "classified_docs": 0,
                "evaluated_docs": 0,
                "acceptance_rate": 0.0,
                "classification_coverage": 0.0,
            },
            "top_classifications": [],
        }


__all__ = ["get_library_dataset_stats"]
