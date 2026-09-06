"""Path-independent Library collection discovery and validation helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import text

from app.modules.library.metadata_terms import termset_name
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine

DETECTOR_VERSION = "metadata-v1"
PROMPT_VERSION = "collection-validation-v1"
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^0-9a-zа-яёәҗңөүһіғқҫ]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:18|19|20)\d{2}\b")
_ISSUE_RE = re.compile(
    r"(?i)(?:^|\s)(?:№|#|issue|vol(?:ume)?|том|выпуск|сан|number|num)\s*[\w.-]+(?=[\s,;:]|$)[,;:]?"
)
_TAIL_NUMBER_RE = re.compile(
    r"[\s._-](?:\d{1,4}|[ivxlcdm]{1,8})(?:[\s._-]*\d{0,4})?$", re.I
)
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BATCH_STEPS = (1, 2, 5, 10, 20)
_ALLOWED_VERDICTS = {"belongs", "does_not_belong", "uncertain"}

LEGAL_GENRE_BLACKLIST = frozenset(
    {
        "legislation",
        "law",
        "laws",
        "legal",
        "legal act",
        "legal acts",
        "legal document",
        "legal documents",
        "legal code",
        "legal draft",
        "legal amendments",
        "local legislation",
        "administrative legislation",
        "draft law",
        "federal law",
        "decree",
        "decrees",
        "government decree",
        "governmental decree",
        "local decree",
        "municipal decree",
        "administrative decree",
        "official decree",
        "resolution",
        "resolutions",
        "government resolution",
        "local government resolution",
        "municipal resolution",
        "administrative resolution",
        "official resolution",
        "regulation",
        "regulations",
        "administrative regulation",
        "administrative regulations",
        "government regulation",
        "local regulation",
        "regulatory document",
        "rules and regulations",
        "statute",
        "statutes",
        "charter",
        "charters",
        "constitution",
        "ordinance",
        "ordinances",
        "administrative act",
        "administrative document",
        "administrative documents",
        "administrative order",
        "official document",
        "official documents",
        "government document",
        "court ruling",
        "закон",
        "указ",
        "постановление",
        "распоряжение",
        "приказ",
        "административ регламент",
        "карар",
        "карар проекты",
        "боерык",
        "хокукый акт",
        "норматив хокукый акт",
        "рәсми документ",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema() -> str:
    import os

    value = (
        str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip() or "monocorpus"
    )
    return value if _SCHEMA_RE.fullmatch(value) else "monocorpus"


def _set_search_path(conn: Any) -> None:
    conn.execute(text(f'SET search_path TO "{_schema()}", public'))


def normalize_collection_text(value: Any) -> str:
    normalized = _PUNCT_RE.sub(" ", str(value or "").casefold())
    return _SPACE_RE.sub(" ", normalized).strip()


def _title(schema: Mapping[str, Any]) -> str:
    for key in ("name", "headline", "title", "alternateName"):
        value = schema.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def title_core(value: str) -> str:
    stripped = _ISSUE_RE.sub(" ", value or "")
    stripped = _YEAR_RE.sub(" ", stripped)
    stripped = _TAIL_NUMBER_RE.sub("", stripped)
    return normalize_collection_text(stripped)


def _names(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            raw = item.get("name") or item.get("@type")
        else:
            raw = item
        clean = str(raw or "").strip()
        if clean and clean not in output:
            output.append(clean)
    return output


def _normalized_genre_is_legal(value: str) -> bool:
    normalized = normalize_collection_text(value)
    if normalized in LEGAL_GENRE_BLACKLIST:
        return True
    tokens = set(normalized.split())
    if tokens & {
        "legislation",
        "decree",
        "decrees",
        "resolution",
        "resolutions",
        "regulation",
        "regulations",
    }:
        return True
    return any(
        phrase in normalized
        for phrase in (
            "government decree",
            "administrative regulation",
            "legal document",
            "норматив хокукый акт",
        )
    )


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str


class CollectionEligibilityPolicy:
    """Central policy for metadata eligible for collection discovery."""

    def evaluate(self, schema: Any) -> EligibilityResult:
        if not isinstance(schema, Mapping):
            return EligibilityResult(False, "missing_metadata")
        if not _title(schema):
            return EligibilityResult(False, "missing_title")
        if normalize_collection_text(schema.get("@type")) == "legislation":
            return EligibilityResult(False, "excluded_work_type")
        if any(
            _normalized_genre_is_legal(item) for item in _names(schema.get("genre"))
        ):
            return EligibilityResult(False, "excluded_genre")
        return EligibilityResult(True, "eligible")


def _issue_number(schema: Mapping[str, Any]) -> str:
    for item in schema.get("about") if isinstance(schema.get("about"), list) else []:
        if not isinstance(item, Mapping):
            continue
        if normalize_collection_text(termset_name(item.get("inDefinedTermSet"))) in {
            "issuenumber",
            "issue number",
        }:
            value = str(item.get("termCode") or item.get("name") or "").strip()
            if value:
                return value
    return ""


def _series_hints(schema: Mapping[str, Any]) -> list[str]:
    hints: list[str] = []
    for key in ("isPartOf", "publication", "partOfSeries"):
        for name in _names(schema.get(key)):
            normalized = normalize_collection_text(name)
            if normalized and normalized not in hints:
                hints.append(normalized)
    return hints


def build_document_features(md5: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build durable metadata-only features; source locations are intentionally absent."""
    raw_title = _title(schema)
    published = str(schema.get("datePublished") or "").strip()
    year_match = re.search(r"\b(?:18|19|20)\d{2}\b", published)
    canonical_json = json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    genres = _names(schema.get("genre"))
    return {
        "md5": str(md5),
        "input_hash": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        "title": raw_title,
        "normalized_title": normalize_collection_text(raw_title),
        "title_core": title_core(raw_title),
        "work_type": str(schema.get("@type") or "").strip(),
        "publication_date": published,
        "publication_year": int(year_match.group(0)) if year_match else None,
        "issue_number": _issue_number(schema),
        "publishers": _names(schema.get("publisher")),
        "authors": _names(schema.get("author")),
        "genres": genres,
        "description": str(schema.get("description") or "").strip()[:5000],
        "series_hints": _series_hints(schema),
        "has_issue_marker": bool(
            _ISSUE_RE.search(raw_title)
            or _YEAR_RE.search(raw_title)
            or _TAIL_NUMBER_RE.search(raw_title)
        ),
    }


class AdaptiveBatchSizer:
    """A per-model additive recovery/multiplicative decrease batch policy."""

    def __init__(self, *, initial_size: int = 20):
        nearest = min(_BATCH_STEPS, key=lambda value: abs(value - int(initial_size)))
        self._sizes: dict[str, int] = defaultdict(lambda: nearest)
        self._successes: Counter[str] = Counter()

    def size_for(self, model_name: str) -> int:
        return self._sizes[model_name]

    def set_size(self, model_name: str, size: int) -> int:
        nearest = min(_BATCH_STEPS, key=lambda value: abs(value - int(size)))
        self._sizes[model_name] = nearest
        self._successes[model_name] = 0
        return nearest

    def record_failure(self, model_name: str) -> int:
        current = self.size_for(model_name)
        index = _BATCH_STEPS.index(current)
        self._sizes[model_name] = _BATCH_STEPS[max(0, index - 1)]
        self._successes[model_name] = 0
        return self._sizes[model_name]

    def record_success(self, model_name: str) -> int:
        self._successes[model_name] += 1
        if self._successes[model_name] < 3:
            return self.size_for(model_name)
        self._successes[model_name] = 0
        current = self.size_for(model_name)
        index = _BATCH_STEPS.index(current)
        self._sizes[model_name] = _BATCH_STEPS[min(len(_BATCH_STEPS) - 1, index + 1)]
        return self._sizes[model_name]


def parse_validation_response(
    payload: Any, *, requested_md5s: Sequence[str]
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Gemini response must be a JSON object")
    if not isinstance(payload.get("is_named_collection"), bool):
        raise ValueError("Gemini is_named_collection must be a boolean")
    for field in ("canonical_name", "rationale"):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"Gemini {field} must be a string")

    def confidence(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Gemini confidence must be numeric")
        normalized = float(value)
        if not 0.0 <= normalized <= 1.0:
            raise ValueError("Gemini confidence must be between 0 and 1")
        return normalized

    group_confidence = confidence(payload.get("confidence"))
    documents = payload.get("documents")
    if not isinstance(documents, list):
        raise ValueError("Gemini response documents must be an array")
    requested = [str(value) for value in requested_md5s]
    returned = [
        str(item.get("md5") or "") for item in documents if isinstance(item, Mapping)
    ]
    if Counter(returned) != Counter(requested):
        raise ValueError(
            "Gemini response must contain every requested MD5 exactly once"
        )
    normalized_docs: list[dict[str, Any]] = []
    for item in documents:
        if not isinstance(item, Mapping):
            raise ValueError("Gemini document result must be an object")
        if not isinstance(item.get("md5"), str):
            raise ValueError("Gemini document MD5 must be a string")
        if not isinstance(item.get("verdict"), str):
            raise ValueError("Gemini document verdict must be a string")
        if not isinstance(item.get("rationale"), str):
            raise ValueError("Gemini document rationale must be a string")
        verdict = str(item.get("verdict") or "").strip()
        if verdict not in _ALLOWED_VERDICTS:
            raise ValueError(f"Unsupported collection verdict: {verdict!r}")
        item_confidence = confidence(item.get("confidence"))
        normalized_docs.append(
            {
                "md5": str(item["md5"]),
                "verdict": verdict,
                "confidence": item_confidence,
                "rationale": str(item.get("rationale") or "").strip(),
            }
        )
    return {
        "is_named_collection": payload["is_named_collection"],
        "canonical_name": payload["canonical_name"].strip(),
        "confidence": group_confidence,
        "rationale": payload["rationale"].strip(),
        "documents": normalized_docs,
    }


def _proposal_key(
    proposal_type: str, target_collection_id: int | None, md5s: Iterable[str]
) -> str:
    identity = "|".join(
        [
            DETECTOR_VERSION,
            proposal_type,
            str(target_collection_id or "new"),
            *sorted(set(md5s)),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def cluster_near_title_cores(cores: Iterable[str]) -> dict[str, str]:
    """Join title cores that differ by one insertion, deletion, or substitution."""
    unique = sorted({value for value in cores if len(value) >= 6})
    parents = {value: value for value in unique}

    def find(value: str) -> str:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    signatures: dict[str, str] = {}
    for value in unique:
        # The full value links a one-character insertion to the shorter title.
        for signature in {
            value,
            *(value[:index] + value[index + 1 :] for index in range(len(value))),
        }:
            existing = signatures.get(signature)
            if existing is None:
                signatures[signature] = value
            else:
                union(value, existing)
    return {value: find(value) for value in unique}


def _dominant_ratio(values: Iterable[str]) -> float:
    normalized = [
        normalize_collection_text(value)
        for value in values
        if normalize_collection_text(value)
    ]
    if not normalized:
        return 0.0
    return max(Counter(normalized).values()) / len(normalized)


def _candidate_score(
    items: Sequence[Mapping[str, Any]],
) -> tuple[float, dict[str, Any]]:
    marker_ratio = sum(bool(item.get("has_issue_marker")) for item in items) / len(
        items
    )
    periodical_ratio = sum(
        normalize_collection_text(item.get("work_type"))
        in {"newsarticle", "periodical"}
        or any(
            "newspaper" in normalize_collection_text(value)
            or "periodical" in normalize_collection_text(value)
            or "journal" in normalize_collection_text(value)
            for value in item.get("genres") or []
        )
        for item in items
    ) / len(items)
    publisher_ratio = _dominant_ratio(
        publisher for item in items for publisher in (item.get("publishers") or [])
    )
    explicit_series_ratio = sum(bool(item.get("series_hints")) for item in items) / len(
        items
    )
    score = min(
        0.99,
        0.45
        + min(0.15, len(items) * 0.015)
        + marker_ratio * 0.15
        + periodical_ratio * 0.15
        + publisher_ratio * 0.08
        + explicit_series_ratio * 0.12,
    )
    return round(score, 4), {
        "marker_ratio": round(marker_ratio, 4),
        "periodical_ratio": round(periodical_ratio, 4),
        "publisher_ratio": round(publisher_ratio, 4),
        "explicit_series_ratio": round(explicit_series_ratio, 4),
    }


def _upsert_proposal(
    conn: Any,
    *,
    proposal_type: str,
    target_collection_id: int | None,
    title: str,
    score: float,
    evidence: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    now: str,
) -> tuple[int, str]:
    key = _proposal_key(
        proposal_type, target_collection_id, (str(item["md5"]) for item in items)
    )
    row = (
        conn.execute(
            text(
                """
            INSERT INTO library_collection_proposals (
                proposal_key, proposal_type, target_collection_id, proposed_title,
                normalized_title, status, detector_version, deterministic_score,
                evidence_json, created_at, updated_at
            ) VALUES (
                :proposal_key, :proposal_type, :target_collection_id, :proposed_title,
                :normalized_title, 'queued_validation', :detector_version, :score,
                CAST(:evidence_json AS JSONB), :created_at, :updated_at
            )
            ON CONFLICT (proposal_key) DO UPDATE SET
                proposed_title = CASE
                    WHEN library_collection_proposals.status IN ('approved', 'rejected')
                    THEN library_collection_proposals.proposed_title
                    ELSE EXCLUDED.proposed_title
                END,
                normalized_title = CASE
                    WHEN library_collection_proposals.status IN ('approved', 'rejected')
                    THEN library_collection_proposals.normalized_title
                    ELSE EXCLUDED.normalized_title
                END,
                deterministic_score = CASE
                    WHEN library_collection_proposals.status IN ('approved', 'rejected')
                    THEN library_collection_proposals.deterministic_score
                    ELSE EXCLUDED.deterministic_score
                END,
                evidence_json = CASE
                    WHEN library_collection_proposals.status IN ('approved', 'rejected')
                    THEN library_collection_proposals.evidence_json
                    ELSE EXCLUDED.evidence_json
                END,
                status = CASE
                    WHEN library_collection_proposals.status = 'superseded'
                    THEN 'queued_validation'
                    ELSE library_collection_proposals.status
                END,
                superseded_at = CASE
                    WHEN library_collection_proposals.status = 'superseded' THEN NULL
                    ELSE library_collection_proposals.superseded_at
                END,
                updated_at = EXCLUDED.updated_at
            RETURNING proposal_id, status
            """
            ),
            {
                "proposal_key": key,
                "proposal_type": proposal_type,
                "target_collection_id": target_collection_id,
                "proposed_title": title,
                "normalized_title": normalize_collection_text(title),
                "detector_version": DETECTOR_VERSION,
                "score": score,
                "evidence_json": json.dumps(dict(evidence), ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            },
        )
        .mappings()
        .one()
    )
    proposal_id = int(row["proposal_id"])
    changed_input = bool(
        conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM library_collection_proposal_items current
                    JOIN jsonb_to_recordset(CAST(:items AS JSONB))
                         AS incoming(md5 TEXT, input_hash TEXT)
                      ON incoming.md5 = current.md5
                    WHERE current.proposal_id = :proposal_id
                      AND current.input_hash <> incoming.input_hash
                )
                """
            ),
            {
                "proposal_id": proposal_id,
                "items": json.dumps(
                    [
                        {"md5": item["md5"], "input_hash": item["input_hash"]}
                        for item in items
                    ]
                ),
            },
        ).scalar_one()
    )
    if changed_input and row["status"] not in {"approved", "rejected"}:
        conn.execute(
            text(
                """
                UPDATE library_collection_proposals
                SET status='queued_validation', gemini_collection_verdict=NULL,
                    gemini_canonical_name=NULL, gemini_confidence=NULL,
                    gemini_rationale=NULL, updated_at=:now
                WHERE proposal_id=:proposal_id
                """
            ),
            {"proposal_id": proposal_id, "now": now},
        )
        conn.execute(
            text(
                """
                UPDATE library_collection_proposal_items
                SET gemini_verdict=NULL, gemini_confidence=NULL,
                    gemini_rationale=NULL, gemini_model=NULL,
                    prompt_version=NULL, validated_at=NULL
                WHERE proposal_id=:proposal_id
                """
            ),
            {"proposal_id": proposal_id},
        )
        row = {**row, "status": "queued_validation"}
    for item in items:
        conn.execute(
            text(
                """
                INSERT INTO library_collection_proposal_items (
                    proposal_id, md5, input_hash, deterministic_score, evidence_json
                ) VALUES (
                    :proposal_id, :md5, :input_hash, :score, CAST(:evidence_json AS JSONB)
                )
                ON CONFLICT (proposal_id, md5) DO UPDATE SET
                    input_hash = EXCLUDED.input_hash,
                    deterministic_score = EXCLUDED.deterministic_score,
                    evidence_json = EXCLUDED.evidence_json
                """
            ),
            {
                "proposal_id": proposal_id,
                "md5": item["md5"],
                "input_hash": item["input_hash"],
                "score": score,
                "evidence_json": json.dumps(
                    {"title_core": item["title_core"]}, ensure_ascii=False
                ),
            },
        )
    return proposal_id, str(row["status"])


def discover_collections(
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Refresh metadata features and create path-independent review proposals."""
    policy = CollectionEligibilityPolicy()
    now = _utc_now()
    engine, config_source = create_runtime_engine()
    counters = Counter()
    generated_keys: set[str] = set()
    try:
        with engine.begin() as conn:
            _set_search_path(conn)
            rows = conn.execute(
                text(
                    """
                    SELECT md5, schema_org
                    FROM metadata
                    WHERE schema_org IS NOT NULL
                    ORDER BY md5
                    """
                )
            ).mappings()
            features: list[dict[str, Any]] = []
            for row in rows:
                if should_stop():
                    counters["stopped"] = 1
                    break
                counters["scanned"] += 1
                md5 = str(row.get("md5") or "")
                schema_obj = row.get("schema_org")
                eligibility = policy.evaluate(schema_obj)
                if isinstance(schema_obj, Mapping) and _title(schema_obj):
                    feature = build_document_features(md5, schema_obj)
                else:
                    fallback_hash = hashlib.sha256(
                        repr(schema_obj).encode("utf-8")
                    ).hexdigest()
                    feature = {
                        "md5": md5,
                        "input_hash": fallback_hash,
                        "title": "",
                        "normalized_title": "",
                        "title_core": "",
                        "work_type": "",
                        "publication_date": "",
                        "publication_year": None,
                        "issue_number": "",
                        "publishers": [],
                        "authors": [],
                        "genres": [],
                        "description": "",
                        "series_hints": [],
                        "has_issue_marker": False,
                    }
                feature["eligible"] = eligibility.eligible
                feature["exclusion_reason"] = (
                    eligibility.reason if not eligibility.eligible else ""
                )
                features.append(feature)
                counters["eligible" if eligibility.eligible else "excluded"] += 1
                counters[f"excluded_{eligibility.reason}"] += int(
                    not eligibility.eligible
                )
                if on_progress and counters["scanned"] % 1000 == 0:
                    on_progress(dict(counters))

                conn.execute(
                    text(
                        """
                        INSERT INTO library_collection_document_features (
                            md5, input_hash, eligible, exclusion_reason, title,
                            normalized_title, title_core, work_type, publication_date,
                            publication_year, issue_number, publishers_json, authors_json,
                            genres_json, description, series_hints_json, has_issue_marker,
                            created_at, updated_at
                        ) VALUES (
                            :md5, :input_hash, :eligible, :exclusion_reason, :title,
                            :normalized_title, :title_core, :work_type, :publication_date,
                            :publication_year, :issue_number, CAST(:publishers AS JSONB),
                            CAST(:authors AS JSONB), CAST(:genres AS JSONB), :description,
                            CAST(:series_hints AS JSONB), :has_issue_marker, :created_at, :updated_at
                        )
                        ON CONFLICT (md5) DO UPDATE SET
                            input_hash = EXCLUDED.input_hash,
                            eligible = EXCLUDED.eligible,
                            exclusion_reason = EXCLUDED.exclusion_reason,
                            title = EXCLUDED.title,
                            normalized_title = EXCLUDED.normalized_title,
                            title_core = EXCLUDED.title_core,
                            work_type = EXCLUDED.work_type,
                            publication_date = EXCLUDED.publication_date,
                            publication_year = EXCLUDED.publication_year,
                            issue_number = EXCLUDED.issue_number,
                            publishers_json = EXCLUDED.publishers_json,
                            authors_json = EXCLUDED.authors_json,
                            genres_json = EXCLUDED.genres_json,
                            description = EXCLUDED.description,
                            series_hints_json = EXCLUDED.series_hints_json,
                            has_issue_marker = EXCLUDED.has_issue_marker,
                            updated_at = CASE
                                WHEN library_collection_document_features.input_hash <> EXCLUDED.input_hash
                                  OR library_collection_document_features.eligible <> EXCLUDED.eligible
                                THEN EXCLUDED.updated_at
                                ELSE library_collection_document_features.updated_at
                            END
                        """
                    ),
                    {
                        **feature,
                        "publishers": json.dumps(
                            feature["publishers"], ensure_ascii=False
                        ),
                        "authors": json.dumps(feature["authors"], ensure_ascii=False),
                        "genres": json.dumps(feature["genres"], ensure_ascii=False),
                        "series_hints": json.dumps(
                            feature["series_hints"], ensure_ascii=False
                        ),
                        "created_at": now,
                        "updated_at": now,
                    },
                )

            if should_stop():
                return {
                    "available": True,
                    "error": None,
                    "config_source": config_source,
                    "stopped": True,
                    **dict(counters),
                }

            conn.execute(
                text(
                    """
                    DELETE FROM library_collection_document_features f
                    WHERE NOT EXISTS (SELECT 1 FROM metadata m WHERE m.md5 = f.md5)
                    """
                )
            )
            canonical_members = set(
                conn.execute(text("SELECT md5 FROM library_collection_items"))
                .scalars()
                .all()
            )

            matched_rows = (
                conn.execute(
                    text(
                        """
                    WITH match_values AS (
                        SELECT f.*, value.normalized_value AS match_value
                        FROM library_collection_document_features f
                        CROSS JOIN LATERAL (
                            SELECT f.title_core AS normalized_value
                            UNION
                            SELECT jsonb_array_elements_text(f.series_hints_json)
                        ) value
                        WHERE value.normalized_value <> ''
                    )
                    SELECT DISTINCT ON (f.md5)
                           f.md5, f.input_hash, f.title, f.title_core,
                           s.collection_id, c.title AS collection_title,
                           similarity(f.match_value, s.normalized_value) AS score
                    FROM match_values f
                    JOIN LATERAL (
                        SELECT collection_id, normalized_value
                        FROM library_collection_signatures s
                        WHERE f.match_value % s.normalized_value
                        ORDER BY similarity(f.match_value, s.normalized_value) DESC
                        LIMIT 1
                    ) s ON TRUE
                    JOIN library_collections c ON c.collection_id = s.collection_id
                    LEFT JOIN library_collection_items i ON i.md5 = f.md5
                    WHERE f.eligible = TRUE
                      AND f.title_core <> ''
                      AND i.md5 IS NULL
                      AND similarity(f.match_value, s.normalized_value) >= 0.72
                    ORDER BY f.md5, score DESC
                    """
                    )
                )
                .mappings()
                .all()
            )
            matched_md5: set[str] = set()
            by_collection: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for row in matched_rows:
                item = dict(row)
                by_collection[int(row["collection_id"])].append(item)
                matched_md5.add(str(row["md5"]))
            for collection_id, items in by_collection.items():
                score = min(float(item["score"]) for item in items)
                evidence = {
                    "match": "approved_signature",
                    "minimum_similarity": round(score, 4),
                }
                proposal_id, status = _upsert_proposal(
                    conn,
                    proposal_type="attach_to_collection",
                    target_collection_id=collection_id,
                    title=str(items[0]["collection_title"]),
                    score=score,
                    evidence=evidence,
                    items=items,
                    now=now,
                )
                generated_keys.add(
                    _proposal_key(
                        "attach_to_collection",
                        collection_id,
                        (item["md5"] for item in items),
                    )
                )
                counters["attachment_proposals"] += int(status == "queued_validation")
                counters["attachment_items"] += len(items)

            eligible_cores = [
                str(feature["title_core"])
                for feature in features
                if feature["eligible"]
            ]
            near_core = cluster_near_title_cores(eligible_cores)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for feature in features:
                if not feature["eligible"] or not feature["title_core"]:
                    continue
                if feature["md5"] in canonical_members or feature["md5"] in matched_md5:
                    continue
                grouped[
                    f"title:{near_core.get(feature['title_core'], feature['title_core'])}"
                ].append(feature)
                for hint in feature.get("series_hints") or []:
                    if hint and hint != feature["title_core"]:
                        grouped[f"series:{hint}"].append(feature)
            for core, items in grouped.items():
                group_value = core.split(":", 1)[1]
                if len(items) < 2 or len(group_value) < 4:
                    continue
                score, evidence = _candidate_score(items)
                if (
                    max(
                        evidence["marker_ratio"],
                        evidence["periodical_ratio"],
                        evidence["publisher_ratio"],
                        evidence["explicit_series_ratio"],
                    )
                    < 0.2
                ):
                    counters["incoherent_groups"] += 1
                    continue
                preferred_title = (
                    group_value
                    if core.startswith("series:")
                    else Counter(str(item["title"]) for item in items).most_common(1)[
                        0
                    ][0]
                )
                _proposal_id, status = _upsert_proposal(
                    conn,
                    proposal_type="new_collection",
                    target_collection_id=None,
                    title=preferred_title,
                    score=score,
                    evidence={
                        "match": "explicit_series"
                        if core.startswith("series:")
                        else "title_core",
                        "normalized_value": group_value,
                        **evidence,
                    },
                    items=items,
                    now=now,
                )
                key = _proposal_key(
                    "new_collection", None, (item["md5"] for item in items)
                )
                generated_keys.add(key)
                counters["new_collection_proposals"] += int(
                    status == "queued_validation"
                )
                counters["new_collection_items"] += len(items)

            conn.execute(
                text(
                    """
                        UPDATE library_collection_proposals
                        SET status = 'superseded', superseded_at = :now, updated_at = :now
                        WHERE detector_version = :detector_version
                          AND status IN ('queued_validation', 'review_ready')
                          AND NOT (proposal_key = ANY(:keys))
                    """
                ),
                {
                    "now": now,
                    "detector_version": DETECTOR_VERSION,
                    "keys": list(generated_keys),
                },
            )

            conn.execute(
                text(
                    """
                    INSERT INTO library_collection_events (action, payload_json, created_at)
                    VALUES ('proposals.discovered', :payload_json, :created_at)
                    """
                ),
                {
                    "payload_json": json.dumps(dict(counters), ensure_ascii=False),
                    "created_at": now,
                },
            )
        result = {
            "available": True,
            "error": None,
            "config_source": config_source,
            **dict(counters),
        }
        if on_progress:
            on_progress(result)
        return result
    except Exception as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "config_source": config_source,
            **dict(counters),
        }
    finally:
        dispose_runtime_engine(engine)


__all__ = [
    "AdaptiveBatchSizer",
    "CollectionEligibilityPolicy",
    "DETECTOR_VERSION",
    "LEGAL_GENRE_BLACKLIST",
    "PROMPT_VERSION",
    "build_document_features",
    "cluster_near_title_cores",
    "discover_collections",
    "normalize_collection_text",
    "parse_validation_response",
    "title_core",
]
