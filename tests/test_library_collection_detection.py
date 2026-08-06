from __future__ import annotations

import pytest

from app.modules.library.collection_detection import (
    AdaptiveBatchSizer,
    CollectionEligibilityPolicy,
    build_document_features,
    cluster_near_title_cores,
    parse_validation_response,
)
from app.modules.library.collection_validation import build_validation_prompt


def test_legislation_is_excluded_even_without_genre() -> None:
    policy = CollectionEligibilityPolicy()

    result = policy.evaluate({"@type": "Legislation", "name": "Карар"})

    assert result.eligible is False
    assert result.reason == "excluded_work_type"


@pytest.mark.parametrize(
    "genre",
    [
        "Governmental decree",
        "Legal document",
        "Administrative regulation",
        "Карар",
        "Норматив-хокукый акт",
        {"name": "Рәсми документ"},
        ["History", "Law"],
    ],
)
def test_legal_genres_are_excluded_across_schema_shapes(genre: object) -> None:
    policy = CollectionEligibilityPolicy()

    result = policy.evaluate({"@type": "Book", "name": "Document", "genre": genre})

    assert result.eligible is False
    assert result.reason == "excluded_genre"


def test_broad_non_legal_genres_remain_eligible() -> None:
    policy = CollectionEligibilityPolicy()

    for genre in (
        "Politics",
        "History",
        "Official gazette",
        "Instructions",
        "Legal thriller",
    ):
        assert policy.evaluate(
            {"@type": "Book", "name": "Document", "genre": genre}
        ).eligible


def test_feature_extraction_never_uses_source_path() -> None:
    schema = {
        "@type": "NewsArticle",
        "name": "Ватаным Татарстан № 12, 2024",
        "genre": ["Newspaper"],
        "publisher": {"name": "Татмедиа"},
        "datePublished": "2024-03-01",
    }

    feature = build_document_features("a" * 32, schema)

    assert feature["title_core"] == "ватаным татарстан"
    assert feature["publication_year"] == 2024
    serialized = repr(feature).lower()
    assert "path" not in serialized
    assert "folder" not in serialized


def test_near_title_clustering_joins_single_character_ocr_variants() -> None:
    clusters = cluster_near_title_cores(
        ["darelfonyn университет", "darelfonyun университет", "башка журнал"]
    )

    assert clusters["darelfonyn университет"] == clusters["darelfonyun университет"]
    assert clusters["башка журнал"] != clusters["darelfonyn университет"]


def test_adaptive_batch_sizer_halves_and_recovers() -> None:
    sizer = AdaptiveBatchSizer(initial_size=20)

    assert sizer.record_failure("gemini-3-flash-preview") == 10
    assert sizer.record_failure("gemini-3-flash-preview") == 5
    assert sizer.record_success("gemini-3-flash-preview") == 5
    assert sizer.record_success("gemini-3-flash-preview") == 5
    assert sizer.record_success("gemini-3-flash-preview") == 10
    assert sizer.size_for("gemini-3.6-flash") == 20


def test_validation_response_requires_exact_requested_md5_set() -> None:
    requested = ["a" * 32, "b" * 32]
    payload = {
        "is_named_collection": True,
        "canonical_name": "Ватаным Татарстан",
        "confidence": 0.95,
        "rationale": "Recurring newspaper",
        "documents": [
            {
                "md5": requested[0],
                "verdict": "belongs",
                "confidence": 0.9,
                "rationale": "Matching title",
            }
        ],
    }

    with pytest.raises(ValueError, match="exactly once"):
        parse_validation_response(payload, requested_md5s=requested)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_named_collection", "false"),
        ("confidence", "0.9"),
        ("confidence", 1.1),
    ],
)
def test_validation_response_rejects_coerced_or_out_of_range_group_values(
    field: str, value: object
) -> None:
    md5 = "a" * 32
    payload = {
        "is_named_collection": True,
        "canonical_name": "Collection",
        "confidence": 0.9,
        "rationale": "Evidence",
        "documents": [
            {
                "md5": md5,
                "verdict": "belongs",
                "confidence": 0.9,
                "rationale": "Evidence",
            }
        ],
    }
    payload[field] = value

    with pytest.raises(ValueError):
        parse_validation_response(payload, requested_md5s=[md5])


def test_validation_response_rejects_string_item_confidence() -> None:
    md5 = "a" * 32
    payload = {
        "is_named_collection": True,
        "canonical_name": "Collection",
        "confidence": 0.9,
        "rationale": "Evidence",
        "documents": [
            {
                "md5": md5,
                "verdict": "belongs",
                "confidence": "0.9",
                "rationale": "Evidence",
            }
        ],
    }

    with pytest.raises(ValueError):
        parse_validation_response(payload, requested_md5s=[md5])


def test_validation_prompt_defines_named_series_and_never_contains_paths() -> None:
    prompt = build_validation_prompt(
        {
            "proposal_type": "new_collection",
            "proposed_title": "Ватаным Татарстан",
            "evidence_json": {"title_core": "ватаным татарстан"},
        },
        [
            {
                "md5": "a" * 32,
                "title": "Ватаным Татарстан №1",
                "work_type": "NewsArticle",
                "publication_date": "2024-01-01",
                "issue_number": "1",
                "publishers_json": ["Татмедиа"],
                "authors_json": [],
                "genres_json": ["Newspaper"],
                "description": "Газета саны",
            }
        ],
    )

    assert "explicitly named recurring publication" in prompt
    assert "does_not_belong" in prompt
    assert "source paths are intentionally unavailable" in prompt.lower()
    assert "/folder" not in prompt
