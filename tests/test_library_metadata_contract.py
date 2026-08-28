"""Strict schema.org metadata quality contracts."""

from __future__ import annotations

from app.modules.library.metadata_contract import (
    CONTRACT_VERSION,
    metadata_contract_issues,
    reshape_english_contributor_roles,
)


def _book(**overrides):
    payload = {
        "@context": "https://schema.org",
        "@type": "Book",
        "name": "Китап",
        "inLanguage": "tt-Cyrl",
        "description": "Бу китап татар халкы тарихы турында сөйли.",
        "genre": ["History"],
        "audience": {
            "@type": "Audience",
            "audienceType": "General public",
        },
        "datePublished": "1998",
    }
    payload.update(overrides)
    return payload


def _codes(payload) -> set[str]:
    return {str(issue["code"]) for issue in metadata_contract_issues(payload)}


def test_contract_accepts_document_language_description_and_english_facets() -> None:
    assert CONTRACT_VERSION == "schema-org.v1"
    assert metadata_contract_issues(_book()) == []


def test_contract_rejects_english_description_for_cyrillic_document() -> None:
    assert "description_script_mismatch" in _codes(
        _book(description="A history of the Tatar people and their literature.")
    )


def test_contract_accepts_english_description_for_english_document() -> None:
    assert metadata_contract_issues(
        _book(
            name="A Book",
            inLanguage="en",
            description="A concise history of publishing.",
        )
    ) == []


def test_contract_rejects_non_english_discovery_facets() -> None:
    assert "genre_not_english" in _codes(_book(genre=["тарих"]))
    assert "audience_not_english" in _codes(
        _book(
            audience={
                "@type": "Audience",
                "audienceType": "укучылар",
            }
        )
    )


def test_contract_rejects_legacy_schema_shapes() -> None:
    issues = _codes(
        _book(
            audience="General public",
            bookEdition=2,
            accessModeSufficient=["textual"],
            contributor=[
                {"@type": "Person", "name": "Editor", "role": "editor"}
            ],
        )
    )

    assert {
        "audience_shape",
        "book_edition_shape",
        "access_mode_sufficient_shape",
        "contributor_role_shape",
    } <= issues


def test_contract_accepts_schema_org_role_relationship() -> None:
    payload = _book(
        contributor=[
            {
                "@type": "Role",
                "roleName": "Proofreader",
                "contributor": {"@type": "Person", "name": "A. Example"},
            }
        ]
    )

    assert metadata_contract_issues(payload) == []


def test_english_roles_are_reshaped_but_non_english_roles_require_reextract() -> None:
    updated, changed, requires_reextract = reshape_english_contributor_roles(
        _book(
            contributor=[
                {"@type": "Person", "name": "A. Editor", "role": "editor"},
                {
                    "@type": "Person",
                    "name": "B. Proofreader",
                    "role": "proofreader",
                },
            ]
        )
    )

    assert changed is True
    assert requires_reextract is False
    assert updated["editor"] == [{"@type": "Person", "name": "A. Editor"}]
    assert updated["contributor"] == [
        {
            "@type": "Role",
            "roleName": "proofreader",
            "contributor": {"@type": "Person", "name": "B. Proofreader"},
        }
    ]

    _updated, _changed, requires_reextract = reshape_english_contributor_roles(
        _book(
            contributor=[
                {"@type": "Person", "name": "C. Example", "role": "мөхәррир"}
            ]
        )
    )
    assert requires_reextract is True


def test_contract_requires_schema_org_defined_term_sets() -> None:
    legacy = _book(
        about=[
            {
                "@type": "DefinedTerm",
                "termCode": "894.36",
                "inDefinedTermSet": "DDC",
            }
        ]
    )
    assert "defined_term_set_shape" in _codes(legacy)

    valid = _book(
        about=[
            {
                "@type": "DefinedTerm",
                "termCode": "894.36",
                "inDefinedTermSet": {
                    "@type": "DefinedTermSet",
                    "name": "DDC",
                },
            }
        ]
    )
    assert metadata_contract_issues(valid) == []
