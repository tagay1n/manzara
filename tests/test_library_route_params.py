"""Tests for reusable library route query helpers."""

from __future__ import annotations

import app.library_route_params as params


def test_parse_csv_tokens_strips_and_drops_empty_items() -> None:
    assert params.parse_csv_tokens(" a, ,b ,, c ") == ["a", "b", "c"]
    assert params.parse_csv_tokens("") == []


def test_query_helpers_define_expected_bounds() -> None:
    page = params.q_page()
    assert page.default == 1
    assert any(getattr(item, "ge", None) == 1 for item in page.metadata)

    page_size = params.q_page_size(default=25, max_value=100)
    assert page_size.default == 25
    assert any(getattr(item, "ge", None) == 1 for item in page_size.metadata)
    assert any(getattr(item, "le", None) == 100 for item in page_size.metadata)

    non_negative = params.q_non_negative(default=0)
    assert non_negative.default == 0
    assert any(getattr(item, "ge", None) == 0 for item in non_negative.metadata)

    limit = params.q_limit(default=80, minimum=1, maximum=300)
    assert limit.default == 80
    assert any(getattr(item, "ge", None) == 1 for item in limit.metadata)
    assert any(getattr(item, "le", None) == 300 for item in limit.metadata)
