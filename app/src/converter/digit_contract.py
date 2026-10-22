"""Kn syntax checks, separate from P004 lifecycle and mapping approval."""

from __future__ import annotations

import re

from converter.bit_devices import parse_digit_designation, validate_digit_width


def digit_operand_form(value: str) -> str | None:
    """Never let a digit-designated device impersonate a literal K constant."""
    try:
        group = parse_digit_designation(value)
    except ValueError:
        return "INVALID_DIGIT"
    return f"Kn{group.family}" if group is not None else None


def digit_operand_errors(instruction: str, operands: tuple[str, ...]) -> tuple[str, ...]:
    """Validate syntax and the explicit MOV data widths without approving a form.

    Indexed groups retain their existing independent lifecycle gate.  Removing
    their index here checks only the base digit count and device type.
    """
    widths = {"MOV": 16, "MOVP": 16, "DMOV": 32, "DMOVP": 32}
    errors = []
    for index, value in enumerate(operands):
        plain = re.sub(r"Z[0-9]+$", "", value)
        try:
            group = parse_digit_designation(plain)
            if group is None:
                continue
            if instruction in widths:
                validate_digit_width(group, widths[instruction])
        except ValueError as error:
            errors.append(f"operand[{index}]: {error}")
    return tuple(errors)
