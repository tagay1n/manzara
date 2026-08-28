"""Versioned audit and invalidation of persisted Library JSON-LD metadata."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.modules.library.metadata_contract import (
    CONTRACT_VERSION,
    metadata_contract_issues,
    reshape_english_contributor_roles,
)


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MetadataAssessment:
    schema_org: dict[str, Any]
    status: str
    issues: tuple[dict[str, str], ...]
    changed: bool


def assess_metadata(schema_org: Any) -> MetadataAssessment:
    """Assess one payload, applying only deterministic English role reshaping."""
    original = dict(schema_org) if isinstance(schema_org, Mapping) else {}
    reshaped, changed, requires_reextract = reshape_english_contributor_roles(original)
    candidate = original if requires_reextract else reshaped
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
        return MetadataAssessment(original, "invalid", tuple(issues), False)
    return MetadataAssessment(candidate, "resolved", (), changed)


class MetadataQualityRepository:
    """Batch PostgreSQL audit with resumable, non-destructive invalidation."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self.engine: Engine = create_engine(
            str(database_url),
            connect_args={"options": f"-csearch_path={normalized},public"},
        )

    def dispose(self) -> None:
        self.engine.dispose()

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
                    counters["roles_repaired"] += 1
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


__all__ = ["MetadataAssessment", "MetadataQualityRepository", "assess_metadata"]
