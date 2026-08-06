from app.modules.library.collection_detection import CollectionEligibilityPolicy


def test_collection_review_policy_keeps_periodicals_and_excludes_law() -> None:
    policy = CollectionEligibilityPolicy()

    assert policy.evaluate(
        {"@type": "NewsArticle", "name": "Газета", "genre": "Newspaper"}
    ).eligible
    assert not policy.evaluate(
        {"@type": "Book", "name": "Карар", "genre": "Legal document"}
    ).eligible
