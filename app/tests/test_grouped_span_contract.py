"""Existing grouped X/Y span contract. Synthetic operands only."""
from __future__ import annotations

import unittest

from converter.bit_devices import parse_digit_designation, validate_digit_width
from converter.xy_mapping import (
    Bounds,
    MappingError,
    Profile,
    RangeMapper,
    Reference,
    Rule,
    UnmappedPolicy,
    effective_bounds,
    fixed_bounds,
    source_span,
)


class GroupedSpanContractTests(unittest.TestCase):
    def test_radix_profiles_have_no_capacity_ceiling(self) -> None:
        self.assertIs(Profile.Q, Profile.Q06UDV)
        self.assertEqual(16, Profile.Q.radix)
        self.assertEqual(8, Profile.FX5U.radix)
        for profile in (Profile.Q, Profile.FX5U):
            bounds = fixed_bounds(profile)
            self.assertIsNone(bounds.x)
            self.assertIsNone(bounds.y)
            self.assertEqual(bounds, effective_bounds(profile, Bounds(x=1, y=1)))
        family, start, end, width = source_span("K8XFFFF", Profile.Q, Bounds(x=1, y=1))
        self.assertEqual(("X", 0xFFFF, 0xFFFF + 31, 32), (family, start, end, width))

    def test_grouped_widths_stay_continuous(self) -> None:
        for digits in range(1, 9):
            for family_name in ("X", "Y"):
                family, start, end, width = source_span(f"K{digits}{family_name}0", Profile.Q)
                self.assertEqual(family_name, family)
                self.assertEqual(0, start)
                self.assertEqual(4 * digits, width)
                self.assertEqual(start + width - 1, end)

    def test_digit_parser_separates_grouped_devices_from_plain_constants(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero"):
            parse_digit_designation("K0X0")
        with self.assertRaisesRegex(ValueError, "nine"):
            parse_digit_designation("K9X0")
        self.assertIsNone(parse_digit_designation("K10"))
        parsed = parse_digit_designation("K1X8")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(16, parsed.radix)
        self.assertEqual(8, parsed.address)
        self.assertEqual(4, parsed.bit_count)
        self.assertEqual(11, parsed.last_address)
        with self.assertRaisesRegex(ValueError, "octal"):
            parse_digit_designation("K1X8", "FX5")
        for digits in range(1, 5):
            group = parse_digit_designation(f"K{digits}X0")
            assert group is not None
            validate_digit_width(group, 16)
            validate_digit_width(group, 32)
        for digits in range(5, 9):
            group = parse_digit_designation(f"K{digits}X0")
            assert group is not None
            with self.assertRaisesRegex(ValueError, "exceeds"):
                validate_digit_width(group, 16)
            validate_digit_width(group, 32)

    def test_fx5_source_span_rejects_octal_digit_eight(self) -> None:
        with self.assertRaisesRegex(MappingError, "octal"):
            source_span("K1X8", Profile.FX5U)
        self.assertEqual(("X", 8, 11, 4), source_span("K1X8", Profile.Q))

    def test_overlapping_sources_and_duplicate_targets_are_rejected(self) -> None:
        with self.assertRaisesRegex(MappingError, "overlapping source rules"):
            RangeMapper(
                source=Profile.Q,
                target=Profile.FX5U,
                rules=(Rule("a", "X0", 8, "X0"), Rule("b", "X4", 8, "X20")),
                unmapped=UnmappedPolicy.HOLD,
            )
        with self.assertRaisesRegex(MappingError, "duplicate target assignment"):
            RangeMapper(
                source=Profile.Q,
                target=Profile.FX5U,
                rules=(Rule("a", "X0", 4, "X0"), Rule("b", "X10", 4, "X0")),
                unmapped=UnmappedPolicy.HOLD,
            )

    def test_span_crossing_a_rule_boundary_is_rejected(self) -> None:
        mapper = RangeMapper(
            source=Profile.Q,
            target=Profile.FX5U,
            rules=(Rule("short", "X0", 8, "X0"),),
            unmapped=UnmappedPolicy.NUMERIC_IDENTITY,
        )
        with self.assertRaisesRegex(MappingError, "operand span crosses mapping boundary"):
            mapper.project(Reference("K4X0", "row:1"))

    def test_continuous_span_inside_one_rule_keeps_width_and_target_radix(self) -> None:
        mapper = RangeMapper(
            source=Profile.Q,
            target=Profile.FX5U,
            rules=(Rule("wide", "X0", 32, "X0"),),
            unmapped=UnmappedPolicy.HOLD,
        )
        wide = mapper.project(Reference("K4X0", "row:1"))
        self.assertEqual("K4X0", wide.target)
        self.assertEqual(16, wide.width)
        self.assertEqual(("wide",), wide.rule_ids)
        shifted = mapper.project(Reference("K1X8", "row:2"))
        self.assertEqual("K1X10", shifted.target)
        self.assertEqual(4, shifted.width)
        self.assertEqual(("wide",), shifted.rule_ids)

    def test_preflight_rejects_distinct_sources_on_one_target(self) -> None:
        mapper = RangeMapper(
            source=Profile.Q,
            target=Profile.FX5U,
            rules=(Rule("moved", "X10", 1, "X0"),),
            unmapped=UnmappedPolicy.NUMERIC_IDENTITY,
        )
        with self.assertRaisesRegex(MappingError, "target alias"):
            mapper.preflight_audit((Reference("X10", "a"), Reference("X0", "b")))
