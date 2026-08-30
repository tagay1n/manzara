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
        _book(contributor=[{"@type": "Person", "name": "A. Example", "role": "editor"}])
    )

    assert decision.status == "resolved"
    assert decision.changed is True
    assert decision.schema_org["editor"] == [{"@type": "Person", "name": "A. Example"}]


def test_assessment_resolves_metadata_without_title() -> None:
    payload = _book()
    payload.pop("name")

    decision = assess_metadata(payload)

    assert decision.status == "resolved"
    assert decision.issues == ()


def test_assessment_invalidates_non_english_roles_and_preserves_payload() -> None:
    original = _book(
        contributor=[{"@type": "Person", "name": "A. Example", "role": "мөхәррир"}]
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
        "audience_not_english",
    }


def test_assessment_resolves_valid_yanalif_description() -> None:
    decision = assess_metadata(
        _book(
            name="Janalif kitabь",
            inLanguage="tt-Latn-x-yanalif",
            description=(
                "Bu əsər kolxoz eşceləreneꞑ tormьşь turьnda sөjli həm "
                "praktik kyrsətmələr birə."
            ),
        )
    )

    assert decision.status == "resolved"
    assert decision.issues == ()


def test_assessment_preserves_zamanalif_variant_tag() -> None:
    decision = assess_metadata(
        _book(
            name="Tatar orfografiyäse",
            inLanguage="tt-Latn-x-zaman-alif",
            description="Äsär zamança Tatar yazuı qağidälären añlata.",
        )
    )

    assert decision.status == "resolved"
    assert decision.schema_org["inLanguage"] == "tt-Latn-x-zaman-alif"
    assert decision.changed is False


def test_assessment_repairs_legacy_zamanalif_variant_tag() -> None:
    decision = assess_metadata(
        _book(
            name="Tatar orfografiyäse",
            inLanguage="tt-Latn-x-zamanalif",
            description="Äsär zamança Tatar yazuı qağidälären añlata.",
        )
    )

    assert decision.status == "resolved"
    assert decision.schema_org["inLanguage"] == "tt-Latn-x-zaman-alif"
    assert decision.changed is True


def test_assessment_losslessly_repairs_legacy_json_ld_shapes() -> None:
    decision = assess_metadata(
        _book(
            audience="General public",
            suggestedMinAge=12,
            bookEdition=2,
            accessMode="textual",
            accessModeSufficient=["textual", "visual"],
            about=[
                {
                    "@type": "DefinedTerm",
                    "termCode": "821.512.145",
                    "inDefinedTermSet": "UDC",
                }
            ],
        )
    )

    assert decision.status == "resolved"
    assert decision.changed is True
    assert decision.schema_org["audience"] == {
        "@type": "PeopleAudience",
        "audienceType": "General public",
        "suggestedMinAge": 12,
    }
    assert "suggestedMinAge" not in decision.schema_org
    assert decision.schema_org["bookEdition"] == "2"
    assert decision.schema_org["accessMode"] == ["textual"]
    assert decision.schema_org["accessModeSufficient"] == [
        {
            "@type": "ItemList",
            "itemListElement": ["textual", "visual"],
        }
    ]
    assert decision.schema_org["about"][0]["inDefinedTermSet"] == {
        "@type": "DefinedTermSet",
        "name": "UDC",
    }


def test_assessment_persists_safe_repairs_while_retaining_semantic_issue() -> None:
    decision = assess_metadata(
        _book(
            genre=["тарих"],
            about=[
                {
                    "@type": "DefinedTerm",
                    "termCode": "900",
                    "inDefinedTermSet": "DDC",
                }
            ],
        )
    )

    assert decision.status == "invalid"
    assert decision.changed is True
    assert {issue["code"] for issue in decision.issues} == {"genre_not_english"}
    assert decision.schema_org["genre"] == ["тарих"]
    assert decision.schema_org["about"][0]["inDefinedTermSet"]["name"] == "DDC"


def test_age_only_audience_is_preserved_without_inventing_a_label() -> None:
    decision = assess_metadata(_book(suggestedMinAge="16"))

    assert decision.status == "resolved"
    assert decision.schema_org["audience"] == {
        "@type": "PeopleAudience",
        "suggestedMinAge": 16,
    }


def test_assessment_promotes_generic_work_with_book_evidence() -> None:
    decision = assess_metadata(
        {
            "@context": "https://schema.org",
            "@type": "CreativeWork",
            "name": "A generic catalogue record",
            "isbn": ["9785298021098"],
            "numberOfPages": 240,
        }
    )

    assert decision.status == "resolved"
    assert decision.changed is True
    assert decision.schema_org["@type"] == "Book"
    assert decision.schema_org["numberOfPages"] == 240


def test_assessment_prunes_book_fields_from_explicit_non_book() -> None:
    decision = assess_metadata(
        {
            "@context": "https://schema.org",
            "@type": "Legislation",
            "name": "A statute",
            "datePublished": "2020",
            "numberOfPages": 12,
            "bookEdition": "2",
            "isbn": ["9785298021098"],
            "illustrator": [{"@type": "Person", "name": "Example"}],
        }
    )

    assert decision.status == "resolved"
    assert decision.changed is True
    assert set(decision.schema_org).isdisjoint(
        {"numberOfPages", "bookEdition", "isbn", "illustrator"}
    )


def test_assessment_repairs_exact_inline_author_relationship() -> None:
    decision = assess_metadata(
        _book(author=[{"@type": "Person", "name": "A. Example", "role": "editor"}])
    )

    assert decision.status == "resolved"
    assert "author" not in decision.schema_org
    assert decision.schema_org["editor"] == [
        {"@type": "Person", "name": "A. Example"}
    ]
