from app.modules.library.collections import _build_collection_review_payload


def _schema(
    *,
    name: str,
    published: str | None,
    issue: str | None,
    publisher: str,
    work_type: str = "NewsArticle",
) -> dict:
    payload = {
        "name": name,
        "@type": work_type,
        "publisher": {"name": publisher},
        "genre": ["Newspaper"],
        "numberOfPages": 4,
    }
    if published:
        payload["datePublished"] = published
    if issue:
        payload["about"] = [
            {
                "inDefinedTermSet": "issueNumber",
                "termCode": issue,
            }
        ]
    return payload


def test_collection_review_prioritizes_consistency_and_outliers() -> None:
    collection = {
        "collection_id": 7,
        "title": "Газета",
        "status": "suggested",
        "include_in_library": 1,
        "confidence": 0.94,
        "item_count": 3,
        "heuristics_json": {
            "key_mode": "parent+stem",
            "parent": "/library/Газета",
            "stem": "газета",
            "marker_ratio": 1.0,
        },
        "metadata_template_json": {},
    }
    rows = [
        {
            "md5": "a" * 32,
            "item_title": "Газета",
            "item_hint": "/library/Газета/issue-1.pdf",
            "signal_json": {"parent": "/library/Газета", "has_issue_marker": True},
            "lib": True,
            "schema_org": _schema(
                name="Газета",
                published="1955-01-01",
                issue="1",
                publisher="Publisher A",
            ),
        },
        {
            "md5": "b" * 32,
            "item_title": "Газета",
            "item_hint": "/library/Газета/issue-2.pdf",
            "signal_json": {"parent": "/library/Газета", "has_issue_marker": True},
            "lib": True,
            "schema_org": _schema(
                name="Газета",
                published="1955-02-01",
                issue="2",
                publisher="Publisher A",
            ),
        },
        {
            "md5": "c" * 32,
            "item_title": "Башка исем",
            "item_hint": "/library/Other/book.pdf",
            "signal_json": {"parent": "/library/Other", "has_issue_marker": False},
            "lib": False,
            "schema_org": _schema(
                name="Башка исем",
                published=None,
                issue=None,
                publisher="Publisher B",
                work_type="Book",
            ),
        },
    ]

    payload = _build_collection_review_payload(collection, rows)

    assert payload["safety"]["approval_mutates_documents"] is False
    assert payload["summary"]["item_count"] == 3
    assert payload["summary"]["included_count"] == 2
    assert payload["summary"]["date_coverage"]["count"] == 2
    assert payload["summary"]["date_range"] == {
        "earliest": "1955-01-01",
        "latest": "1955-02-01",
    }
    assert payload["summary"]["issue_number_coverage"]["count"] == 2
    assert payload["consistency"]["title"]["dominant"] == "Газета"
    assert payload["consistency"]["publisher"]["dominant"] == "Publisher A"
    assert payload["outliers_total"] == 1
    assert payload["outliers"][0]["md5"] == "c" * 32
    assert set(payload["outliers"][0]["reasons"]) >= {
        "title_mismatch",
        "parent_mismatch",
    }
    assert len(payload["samples"]) == 3
