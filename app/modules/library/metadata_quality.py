"""Versioned audit and invalidation of persisted Library JSON-LD metadata."""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.modules.library.metadata_contract import (
    ACCESS_MODES,
    CONTRACT_VERSION,
    metadata_contract_issues,
    reshape_english_contributor_roles,
)
from app.modules.library.metadata_normalization import sanitize_schema_org_contract
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MetadataAssessment:
    schema_org: dict[str, Any]
    status: str
    issues: tuple[dict[str, str], ...]
    changed: bool


def _clean_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _integral_age(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 150 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and 0 <= value <= 150 else None
    raw = _clean_label(value)
    if raw and raw.isdigit() and 0 <= int(raw) <= 150:
        return int(raw)
    return None


def _normalize_legacy_audience(updated: dict[str, Any]) -> bool:
    raw_audience = updated.get("audience")
    changed = False
    if raw_audience is None:
        normalized: Any = None
    else:
        items = raw_audience if isinstance(raw_audience, list) else [raw_audience]
        normalized_items: list[Any] = []
        for item in items:
            if label := _clean_label(item):
                normalized_items.append({"@type": "Audience", "audienceType": label})
                changed = True
            else:
                normalized_items.append(deepcopy(item))
        normalized = (
            normalized_items if isinstance(raw_audience, list) else normalized_items[0]
        )

    age = _integral_age(updated.get("suggestedMinAge"))
    if age is not None:
        normalized_items = normalized if isinstance(normalized, list) else [normalized]
        target = next(
            (item for item in normalized_items if isinstance(item, dict)), None
        )
        if target is None:
            target = {"@type": "PeopleAudience"}
            normalized_items = [target]
        else:
            target["@type"] = "PeopleAudience"
        target["suggestedMinAge"] = age
        normalized = (
            normalized_items if isinstance(normalized, list) else normalized_items[0]
        )
        updated.pop("suggestedMinAge", None)
        changed = True

    if normalized is not None and normalized != raw_audience:
        updated["audience"] = normalized
        changed = True
    return changed


def _normalize_legacy_shapes(
    schema_org: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Apply only lossless representation changes to historical JSON-LD."""
    updated = deepcopy(dict(schema_org))
    changed = _normalize_legacy_audience(updated)

    edition = updated.get("bookEdition")
    if isinstance(edition, (int, float)) and not isinstance(edition, bool):
        updated["bookEdition"] = str(edition)
        changed = True

    access_mode = updated.get("accessMode")
    if isinstance(access_mode, str) and access_mode in ACCESS_MODES:
        updated["accessMode"] = [access_mode]
        changed = True

    sufficient = updated.get("accessModeSufficient")
    if (
        isinstance(sufficient, list)
        and sufficient
        and all(isinstance(item, str) and item in ACCESS_MODES for item in sufficient)
    ):
        updated["accessModeSufficient"] = [
            {"@type": "ItemList", "itemListElement": list(sufficient)}
        ]
        changed = True

    about = updated.get("about")
    about_items = about if isinstance(about, list) else []
    for item in about_items:
        if not isinstance(item, dict) or item.get("@type") != "DefinedTerm":
            continue
        termset = item.get("inDefinedTermSet")
        if isinstance(termset, str):
            name = _clean_label(termset)
            parsed = urlparse(name or "")
            if name and not (parsed.scheme in {"http", "https"} and parsed.netloc):
                item["inDefinedTermSet"] = {
                    "@type": "DefinedTermSet",
                    "name": name,
                }
                changed = True
        elif isinstance(termset, dict) and _clean_label(termset.get("name")):
            if termset.get("@type") != "DefinedTermSet":
                termset["@type"] = "DefinedTermSet"
                changed = True
    return updated, changed


def assess_metadata(schema_org: Any) -> MetadataAssessment:
    """Assess one payload after lossless shape and English-role repairs."""
    original = dict(schema_org) if isinstance(schema_org, Mapping) else {}
    normalized, shape_changed = _normalize_legacy_shapes(original)
    normalized, sanitize_changed = sanitize_schema_org_contract(normalized)
    reshaped, role_changed, requires_reextract = reshape_english_contributor_roles(
        normalized
    )
    candidate = normalized if requires_reextract else reshaped
    changed = shape_changed or sanitize_changed or role_changed
    issues = metadata_contract_issues(candidate)
    if requires_reextract and not any(
        item["code"] == "role_not_english" for item in issues
    ):
        issues.append(
            {
                "code": "role_not_english",
                "path": "$.contributor",
                "message": "canonical contributor role must be English",
            }
        )
    if issues:
        return MetadataAssessment(candidate, "invalid", tuple(issues), changed)
    return MetadataAssessment(candidate, "resolved", (), changed)


class MetadataQualityRepository:
    """Batch PostgreSQL audit with resumable, non-destructive invalidation."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self.engine: Engine = acquire_postgres_engine(
            str(database_url), schema=normalized
        )

    def dispose(self) -> None:
        release_postgres_engine(self.engine)

    def audit(
        self,
        *,
        apply: bool,
        run_id: int | None = None,
        batch_size: int = 500,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int, Mapping[str, int]], None] | None = None,
    ) -> dict[str, Any]:
        total = self._count()
        counters: Counter[str] = Counter()
        issue_counts: Counter[str] = Counter()
        cursor = ""
        while not (should_stop and should_stop()):
            rows = self._batch(cursor, max(1, int(batch_size)))
            if not rows:
                break
            decisions: list[tuple[str, MetadataAssessment]] = []
            for row in rows:
                decision = assess_metadata(row.get("schema_org"))
                decisions.append((str(row["md5"]), decision))
                counters["scanned"] += 1
                counters[decision.status] += 1
                if decision.changed:
                    counters["normalized"] += 1
                for issue in decision.issues:
                    issue_counts[issue["code"]] += 1
            if apply:
                self._persist(decisions, run_id=run_id)
            cursor = str(rows[-1]["md5"])
            if on_progress:
                on_progress(counters["scanned"], total, counters)
        return {
            "contract_version": CONTRACT_VERSION,
            "apply": bool(apply),
            "total": total,
            **dict(counters),
            "issue_counts": dict(sorted(issue_counts.items())),
        }

    def _count(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM metadata")).scalar_one())

    def _batch(self, cursor: str, limit: int) -> list[Mapping[str, Any]]:
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    text(
                        """
                    SELECT md5, schema_org
                    FROM metadata
                    WHERE md5 > :cursor
                    ORDER BY md5
                    LIMIT :limit
                    """
                    ),
                    {"cursor": cursor, "limit": limit},
                )
                .mappings()
                .all()
            )

    def _persist(
        self,
        decisions: list[tuple[str, MetadataAssessment]],
        *,
        run_id: int | None,
    ) -> None:
        with self.engine.begin() as conn:
            repairs = [
                {
                    "md5": md5,
                    "payload": json.dumps(decision.schema_org, ensure_ascii=False),
                }
                for md5, decision in decisions
                if decision.changed
            ]
            if repairs:
                conn.execute(
                    text(
                        "UPDATE metadata SET schema_org = CAST(:payload AS JSONB) WHERE md5 = :md5"
                    ),
                    repairs,
                )
            quality_rows = [
                {
                    "md5": md5,
                    "contract_version": CONTRACT_VERSION,
                    "status": decision.status,
                    "issues": json.dumps(decision.issues, ensure_ascii=False),
                    "run_id": run_id,
                }
                for md5, decision in decisions
            ]
            conn.execute(
                text(
                    """
                        INSERT INTO library_metadata_quality_state (
                            md5, contract_version, status, issues_json, last_run_id,
                            detected_at, resolved_at, updated_at
                        ) VALUES (
                            :md5, :contract_version, :status, CAST(:issues AS JSONB),
                            :run_id, CURRENT_TIMESTAMP,
                            CASE WHEN :status = 'resolved' THEN CURRENT_TIMESTAMP END,
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (md5) DO UPDATE SET
                            contract_version = EXCLUDED.contract_version,
                            status = EXCLUDED.status,
                            issues_json = EXCLUDED.issues_json,
                            last_run_id = EXCLUDED.last_run_id,
                            resolved_at = EXCLUDED.resolved_at,
                            updated_at = CURRENT_TIMESTAMP
                        """
                ),
                quality_rows,
            )
            invalid_md5s = [
                md5 for md5, decision in decisions if decision.status == "invalid"
            ]
            if invalid_md5s:
                conn.execute(
                    text(
                        "DELETE FROM library_metadata_extraction_state "
                        "WHERE md5 = ANY(:md5s)"
                    ),
                    {"md5s": invalid_md5s},
                )
                conn.execute(
                    text(
                        "DELETE FROM library_metadata_evaluation_state "
                        "WHERE md5 = ANY(:md5s)"
                    ),
                    {"md5s": invalid_md5s},
                )
            resolved_md5s = [
                md5 for md5, decision in decisions if decision.status == "resolved"
            ]
            if resolved_md5s:
                conn.execute(
                    text(
                        "DELETE FROM library_metadata_extraction_state "
                        "WHERE md5 = ANY(:md5s)"
                    ),
                    {"md5s": resolved_md5s},
                )


__all__ = ["MetadataAssessment", "MetadataQualityRepository", "assess_metadata"]
