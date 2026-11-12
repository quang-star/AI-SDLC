"""Minimal billing helpers used as the Spike 01 Phase 2 verification fixture."""

from __future__ import annotations

from collections.abc import Sequence


def compute_subtotal_cents(items: Sequence[tuple[int, int]]) -> int:
    """Return the sum of ``qty * unit_cents`` for non-negative line items."""
    total = 0
    for qty, unit_cents in items:
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
            raise ValueError(f"qty must be a non-negative int, got {qty!r}")
        if isinstance(unit_cents, bool) or not isinstance(unit_cents, int) or unit_cents < 0:
            raise ValueError(f"unit_cents must be a non-negative int, got {unit_cents!r}")
        total += qty * unit_cents
    return total


def apply_discount_cents(subtotal_cents: int, discount_percent: int) -> int:
    """Apply an integer percentage discount (0..100 inclusive), rounding half-cent discounts up.

    Args:
        subtotal_cents: Non-negative integer subtotal in cents.
        discount_percent: Integer percentage in ``0..100`` inclusive.

    Returns:
        Payable amount in cents after subtracting ``(subtotal_cents * discount_percent + 50) // 100``.
    """
    if isinstance(subtotal_cents, bool) or not isinstance(subtotal_cents, int) or subtotal_cents < 0:
        raise ValueError(f"subtotal_cents must be a non-negative int, got {subtotal_cents!r}")
    if isinstance(discount_percent, bool) or not isinstance(discount_percent, int) or discount_percent < 0:
        raise ValueError(f"discount_percent must be in 0..100, got {discount_percent!r}")

    discount = (subtotal_cents * discount_percent) // 100
    return subtotal_cents - discount
