"""Document cleanup planning and guarded execution tests."""

from __future__ import annotations

import pytest

from app.modules.library.document_cleanup import (
    build_isbn_cleanup_decisions,
    cleanup_reasons,
)
from app.modules.library.document_cleanup_service import (
    cleanup_target_path,
    prepare_document_cleanup,
)
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


def test_cleanup_reasons_share_document_sync_format_policy() -> None:
    assert cleanup_reasons(
        language="tat",
        mime_type="image/vnd.djvu",
        source_path="/books/book.djvu",
    ) == []
    assert cleanup_reasons(
        language="tat",
        mime_type="application/octet-stream",
        source_path="/books/book.docx",
    ) == []
    assert cleanup_reasons(
        language="tat",
        mime_type="video/x-ms-wmv",
        source_path="/media/movie.wmv",
    ) == ["non_document"]


def test_cleanup_target_preserves_hierarchy_below_source_root() -> None:
    assert cleanup_target_path(
        "/neurotatarlar/kitaplar/filtered_out",
        reason="corrupted",
        source_root_path="/neurotatarlar/kitaplar/monocorpus",
        source_path=(
            "/neurotatarlar/kitaplar/monocorpus/"
            "__Библиотека/Башкорт/Китап.pdf"
        ),
    ) == (
        "/neurotatarlar/kitaplar/filtered_out/corrupted/"
        "__Библиотека/Башкорт/Китап.pdf"
    )


def test_cleanup_target_rejects_source_outside_configured_root() -> None:
    with pytest.raises(ValueError, match="outside configured document root"):
        cleanup_target_path(
            "/filtered",
            reason="corrupted",
            source_root_path="/documents",
            source_path="/other/book.pdf",
        )

    with pytest.raises(ValueError, match="outside the document source root"):
        cleanup_target_path(
            "/documents/filtered",
            reason="corrupted",
            source_root_path="/documents",
            source_path="/documents/book.pdf",
        )


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
        assert overwrite is True
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
            {
                "md5": "d" * 32,
                "mime_type": "application/pdf",
                "ya_path": "/books/d.pdf",
                "ya_resource_id": "d-resource",
                "language": "tat",
                "full": True,
                "schema_org": {"name": "D", "isbn": "9781861972712"},
            },
            {
                "md5": "e" * 32,
                "mime_type": "application/pdf",
                "ya_path": "/books/e.pdf",
                "ya_resource_id": "e-resource",
                "language": "tat",
                "full": False,
                "schema_org": {"name": "E", "isbn": "9781861972712"},
            },
            {
                "md5": "f" * 32,
                "mime_type": "application/zip",
                "ya_path": "/books/f.zip",
                "ya_resource_id": "f-resource",
                "language": "tat",
                "full": True,
                "schema_org": {"name": "F"},
            },
        ]

    def enqueue_cleanup(self, payload):  # noqa: ANN001
        self.plans.append(dict(payload))
        return len(self.plans), True

    def is_cleanup_suppressed(self, _payload):  # noqa: ANN001
        return False

    def upsert_isbn_review(self, **payload):  # noqa: ANN003
        self.reviews.append(dict(payload))
        return len(self.reviews), True


def test_preparation_only_writes_plans_and_ambiguous_reviews() -> None:
    repository = _PlanningRepository()

    summary = prepare_document_cleanup(
        repository=repository,
        filtered_out_path="/filtered",
        source_root_path="/books",
    )

    assert summary["plans_created"] == 2
    assert summary["isbn_reviews_created"] == 2
    assert summary["planned_by_reason"] == {
        "duplicate_isbn": 0,
        "non_document": 1,
        "non_tatar": 1,
    }
    assert summary["planned_moves"] == {
        "total": 2,
        "by_isbn": 0,
        "by_language": 1,
        "by_non_document_format": 1,
    }
    assert summary["isbn_analysis"] == {
        "duplicate_groups": 2,
        "auto_resolved_groups": 0,
        "books_planned_to_move": 0,
        "review_groups": 2,
        "books_awaiting_review": 4,
    }
    assert summary["isbn_auto_resolved_groups"] == 0
    assert summary["isbn_review_groups"] == 2
    assert summary["isbn_review_candidates"] == 4
    assert repository.plans[0]["reason"] == "non_tatar"
    assert repository.plans[0]["target_path"].startswith(
        "/filtered/non_tatar/"
    )
    assert repository.reviews[0]["isbn"] == "9780306406157"


def test_preparation_does_not_recreate_explicitly_canceled_plan() -> None:
    repository = _PlanningRepository()
    repository.list_documents_for_planning = lambda: [
        {
            "md5": "f" * 32,
            "mime_type": "application/zip",
            "ya_path": "/books/f.zip",
            "ya_resource_id": "f-resource",
            "language": "tat",
            "full": True,
            "schema_org": {"name": "F"},
        }
    ]
    repository.is_cleanup_suppressed = lambda _payload: True

    summary = prepare_document_cleanup(
        repository=repository,
        filtered_out_path="/filtered",
        source_root_path="/books",
    )

    assert summary["plans_created"] == 0
    assert summary["plans_suppressed"] == 1
    assert summary["planned_non_document"] == 0
    assert repository.plans == []


def test_preparation_queues_windows_shortcut_as_non_document() -> None:
    repository = _PlanningRepository()
    repository.list_documents_for_planning = lambda: [
        {
            "md5": "8" * 32,
            "mime_type": "application/x-ms-shortcut",
            "ya_path": "/library/2015/47/47 - Ярлык.lnk",
            "ya_resource_id": "shortcut-resource",
            "language": None,
            "full": True,
            "schema_org": None,
        }
    ]

    summary = prepare_document_cleanup(
        repository=repository,
        filtered_out_path="/filtered",
        source_root_path="/library",
    )

    assert summary["planned_non_document"] == 1
    assert repository.plans[0]["reason"] == "non_document"
    assert repository.plans[0]["source_path"].endswith("47 - Ярлык.lnk")
