"""Metadata quality audit and invalidation behavior."""

from app.modules.library.metadata_quality import assess_metadata


def _book(**overrides):
    payload = {
        "@context": "https://schema.org",
        "@type": "Book",
        "name": "Китап",
        "genre": ["History"],
    }
    payload.update(overrides)
    return payload


def test_assessment_repairs_english_roles_without_reextracting() -> None:
    decision = assess_metadata(
        _book(
            contributor=[
                {"@type": "Person", "name": "A. Example", "role": "editor"}
            ]
        )
    )

    assert decision.status == "resolved"
    assert decision.changed is True
    assert decision.schema_org["editor"] == [
        {"@type": "Person", "name": "A. Example"}
    ]


def test_assessment_invalidates_non_english_roles_and_preserves_payload() -> None:
    original = _book(
        contributor=[
            {"@type": "Person", "name": "A. Example", "role": "мөхәррир"}
        ]
    )
    decision = assess_metadata(original)

    assert decision.status == "invalid"
    assert decision.changed is False
    assert decision.schema_org == original
    assert "role_not_english" in {issue["code"] for issue in decision.issues}


def test_assessment_invalidates_language_and_schema_shape_problems() -> None:
    decision = assess_metadata(
        _book(
            inLanguage="tt-Cyrl",
            description="An English description for a Tatar document.",
            audience="укучылар",
        )
    )

    assert decision.status == "invalid"
    assert {issue["code"] for issue in decision.issues} >= {
        "description_script_mismatch",
        "audience_shape",
    }
