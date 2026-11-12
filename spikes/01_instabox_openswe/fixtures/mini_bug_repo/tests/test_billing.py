"""Baseline tests that already pass before the agent edits the fixture repo."""

import pytest

from mini_ledger.billing import apply_discount_cents, compute_subtotal_cents


def test_compute_subtotal_cents() -> None:
    assert compute_subtotal_cents([(2, 350), (1, 199)]) == 899
    assert compute_subtotal_cents([]) == 0


def test_compute_subtotal_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        compute_subtotal_cents([(-1, 100)])
    with pytest.raises(ValueError):
        compute_subtotal_cents([(True, 100)])  # type: ignore[list-item]


def test_apply_discount_exact_multiples() -> None:
    assert apply_discount_cents(1000, 0) == 1000
    assert apply_discount_cents(1000, 10) == 900
    assert apply_discount_cents(1000, 100) == 0


def test_apply_discount_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError):
        apply_discount_cents(-1, 10)
    with pytest.raises(ValueError):
        apply_discount_cents(100, -5)
