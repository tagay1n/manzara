"""Document cleanup planning and guarded execution tests."""

from __future__ import annotations

import pytest

from app.modules.library.document_cleanup import (
    build_isbn_cleanup_decisions,
    cleanup_reasons,
)
from app.modules.library.document_cleanup_service import prepare_document_cleanup
from app.modules.maintenance.document_cleanup_executor import (
    execute_yandex_cleanup,
)


def test_cleanup_reasons_detect_non_tatar_and_non_document_content() -> None:
    assert cleanup_reasons(language="rus", mime_type="application/pdf") == [
        "non_tatar"
    ]
    assert cleanup_reasons(language="tat", mime_type="application/zip") == [
        "non_document"
    ]
    assert cleanup_reasons(language="tt", mime_type="application/pdf") == []
    assert cleanup_reasons(language="tt-Cyrl", mime_type="application/pdf") == []
    assert cleanup_reasons(
        language="tt-Latn-x-zamanalif",
        mime_type="application/octet-stream",
        source_path="/books/book.pdf",
    ) == []


def test_isbn_cleanup_keeps_only_unambiguously_complete_document() -> None:
    decisions = build_isbn_cleanup_decisions(
        [
            {
                "md5": "a" * 32,
                "isbn": ["978-0-306-40615-7"],
                "full": True,
                "mime_type": "application/pdf",
            },
            {
                "md5": "b" * 32,
                "isbn": ["9780306406157"],
                "full": False,
                "mime_type": "application/pdf",
            },
        ]
    )

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.requires_review is False
    assert decision.keep_md5s == ("a" * 32,)
    assert decision.remove_md5s == ("b" * 32,)


def test_isbn_cleanup_keeps_ambiguous_complete_documents_for_review() -> None:
    decisions = build_isbn_cleanup_decisions(
        [
            {
                "md5": "a" * 32,
                "isbn": ["9780306406157"],
                "full": True,
                "mime_type": "application/pdf",
            },
            {
                "md5": "b" * 32,
                "isbn": ["9780306406157"],
                "full": True,
                "mime_type": "application/pdf",
            },
        ]
    )

    assert len(decisions) == 1
    assert decisions[0].requires_review is True
    assert decisions[0].keep_md5s == ()
    assert decisions[0].remove_md5s == ()


class _FakeYaDisk:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.moved: list[tuple[str, str]] = []

    def remove(self, path: str, permanently: bool = False) -> None:
        assert permanently is True
        self.removed.append(path)

    def move(self, source: str, target: str, overwrite: bool = False) -> None:
        assert overwrite is False
        self.moved.append((source, target))


def test_yandex_cleanup_refuses_unpersisted_or_non_executable_plan() -> None:
    yadisk = _FakeYaDisk()

    with pytest.raises(ValueError, match="persisted cleanup_id"):
        execute_yandex_cleanup(
            {"action": "delete", "status": "planned", "source_path": "/a.pdf"},
            yadisk=yadisk,
        )
    with pytest.raises(ValueError, match="planned or running"):
        execute_yandex_cleanup(
            {
                "cleanup_id": 7,
                "action": "delete",
                "status": "completed",
                "source_path": "/a.pdf",
            },
            yadisk=yadisk,
        )

    assert yadisk.removed == []
    assert yadisk.moved == []


def test_yandex_cleanup_executes_only_the_persisted_action() -> None:
    yadisk = _FakeYaDisk()

    execute_yandex_cleanup(
        {
            "cleanup_id": 8,
            "action": "delete",
            "status": "planned",
            "source_path": "/duplicate.pdf",
        },
        yadisk=yadisk,
    )
    execute_yandex_cleanup(
        {
            "cleanup_id": 9,
            "action": "move",
            "status": "running",
            "source_path": "/book.pdf",
            "target_path": "/filtered/book.pdf",
        },
        yadisk=yadisk,
    )

    assert yadisk.removed == ["/duplicate.pdf"]
    assert yadisk.moved == [("/book.pdf", "/filtered/book.pdf")]


class _PlanningRepository:
    def __init__(self) -> None:
        self.plans: list[dict] = []
        self.reviews: list[dict] = []

    def list_documents_for_planning(self):
        return [
            {
                "md5": "a" * 32,
                "mime_type": "application/pdf",
                "ya_path": "/books/a.pdf",
                "ya_resource_id": "a-resource",
                "language": "rus",
                "full": True,
                "schema_org": {"name": "A"},
            },
            {
                "md5": "b" * 32,
                "mime_type": "application/pdf",
                "ya_path": "/books/b.pdf",
                "ya_resource_id": "b-resource",
                "language": "tat",
                "full": True,
                "schema_org": {"name": "B", "isbn": "9780306406157"},
            },
            {
                "md5": "c" * 32,
                "mime_type": "application/pdf",
                "ya_path": "/books/c.pdf",
                "ya_resource_id": "c-resource",
                "language": "tat",
                "full": True,
                "schema_org": {"name": "C", "isbn": "9780306406157"},
            },
        ]

    def enqueue_cleanup(self, payload):  # noqa: ANN001
        self.plans.append(dict(payload))
        return len(self.plans), True

    def upsert_isbn_review(self, **payload):  # noqa: ANN003
        self.reviews.append(dict(payload))
        return len(self.reviews), True


def test_preparation_only_writes_plans_and_ambiguous_reviews() -> None:
    repository = _PlanningRepository()

    summary = prepare_document_cleanup(
        repository=repository,
        filtered_out_path="/filtered",
    )

    assert summary["plans_created"] == 1
    assert summary["isbn_reviews_created"] == 1
    assert repository.plans[0]["reason"] == "non_tatar"
    assert repository.plans[0]["target_path"].startswith(
        "/filtered/non_tatar/"
    )
    assert repository.reviews[0]["isbn"] == "9780306406157"
