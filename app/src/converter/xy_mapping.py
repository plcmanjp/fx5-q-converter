"""Pure, fail-closed X/Y range projection for an explicitly selected direction.

Address capacity belongs to the chosen PLC, not this text converter. This module
never infers physical I/O assignment or edits a source operand/comment body.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable


class MappingError(ValueError):
    """An address, rule, or complete preflight cannot be projected safely."""

    def __init__(self, message: str, *, locator: str | None = None,
                 operand: str | None = None) -> None:
        super().__init__(message)
        self.locator = locator
        self.operand = operand


class UnmappedAddressError(MappingError):
    """A valid X/Y point has no rule while the selected policy is HOLD."""


class Profile(str, Enum):
    """X/Y address radix, not a verified physical CPU identity.

    The Q address profile retains the v1 Q06UDV wire value so existing mapping
    files and their canonical SHA-256 digests remain byte-for-byte compatible.
    """

    Q = "Q06UDV"
    Q06UDV = "Q06UDV"  # Legacy API alias for the same hexadecimal address profile.
    FX5U = "FX5U"

    @property
    def radix(self) -> int:
        return 16 if self is Profile.Q else 8

    @property
    def display_name(self) -> str:
        return "Q" if self is Profile.Q else "FX5U"


class UnmappedPolicy(str, Enum):
    HOLD = "HOLD"
    SOURCE_SPELLING_IDENTITY = "SOURCE_SPELLING_IDENTITY"
    NUMERIC_IDENTITY = "NUMERIC_IDENTITY"


@dataclass(frozen=True)
class Bounds:
    """Legacy configuration values, retained for file/API compatibility only."""

    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        if any(value is not None and (type(value) is not int or value < 0)
               for value in (self.x, self.y)):
            raise MappingError("project ceilings must be nonnegative integers or omitted")

    def maximum(self, family: str) -> int | None:
        return self.x if family == "X" else self.y


def fixed_bounds(profile: Profile) -> Bounds:
    """Legacy API: radix profiles impose no CPU/device capacity ceiling."""
    if not isinstance(profile, Profile):
        raise MappingError("unknown X/Y profile")
    return Bounds()


def effective_bounds(profile: Profile, additional: Bounds | None = None) -> Bounds:
    """Accept legacy bounds structurally without imposing device capacity."""
    if additional is not None and not isinstance(additional, Bounds):
        raise MappingError("invalid project ceilings")
    return fixed_bounds(profile)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    source_start: str
    count: int
    target_start: str
    enabled: bool = True


@dataclass(frozen=True)
class Reference:
    spelling: str
    locator: str


@dataclass(frozen=True)
class Projection:
    source: str
    target: str
    rule_ids: tuple[str, ...]
    locator: str
    width: int


@dataclass(frozen=True)
class TargetAlias:
    target: str
    sources: tuple[str, ...]
    locators: tuple[str, ...]


@dataclass(frozen=True)
class MappingAudit:
    projections: tuple[Projection, ...]
    target_aliases: tuple[TargetAlias, ...]


@dataclass(frozen=True)
class _Address:
    family: str
    number: int
    spelling: str
    width: int


_PLAIN = re.compile(r"([XY])([0-9A-Fa-f]+)\Z", re.ASCII)
_OPERAND = re.compile(r"(K[1-8])?([XY])([0-9A-Fa-f]+)(.*)\Z", re.ASCII)


def _check_end(number: int, family: str, profile: Profile, bounds: Bounds | None,
               context: str) -> None:
    if number < 0:
        raise MappingError(f"{context} has a negative address")


def _parse_plain(spelling: str, profile: Profile, bounds: Bounds | None) -> _Address:
    if not isinstance(spelling, str):
        raise MappingError("X/Y address must be text")
    match = _PLAIN.fullmatch(spelling.upper())
    if match is None:
        raise MappingError(f"unsupported X/Y address: {spelling}")
    family, digits = match.groups()
    if profile is Profile.FX5U and any(d not in "01234567" for d in digits):
        raise MappingError(f"invalid FX5U octal address: {spelling}")
    number = int(digits, profile.radix)
    _check_end(number, family, profile, bounds, f"address {spelling}")
    return _Address(family, number, spelling, 1)


def _format(family: str, number: int, profile: Profile) -> str:
    if profile is Profile.Q:
        return f"{family}{number:X}"
    return f"{family}{number:o}"


def source_span(spelling: str, profile: Profile, bounds: Bounds | None = None) -> tuple[str, int, int, int]:
    """Validate an unmapped literal X/Y operand and its full grouped width."""
    if not isinstance(spelling, str):
        raise MappingError("X/Y operand must be text")
    match = _OPERAND.fullmatch(spelling.upper())
    if match is None:
        raise MappingError(f"unsupported X/Y operand: {spelling}")
    group, family, digits, suffix = match.groups()
    if suffix:
        raise MappingError(f"dynamic or modified X/Y operand: {spelling}")
    width = 4 * int(group[1]) if group else 1
    effective = effective_bounds(profile, bounds)
    first = _parse_plain(family + digits, profile, None)
    end = first.number + width - 1
    _check_end(end, family, profile, effective,
               f"operand span {spelling} ({_format(family, first.number, profile)}.."
               f"{_format(family, end, profile)})")
    return family, first.number, end, width


class RangeMapper:
    """One source-to-target projection with whole-set collision preflight."""

    def __init__(
        self, *, source: Profile, target: Profile, source_bounds: Bounds | None = None,
        target_bounds: Bounds | None = None, rules: Iterable[Rule], unmapped: UnmappedPolicy,
        allow_target_aliases: bool = False,
    ) -> None:
        if not isinstance(source, Profile) or not isinstance(target, Profile):
            raise MappingError("unknown source or target profile")
        if source == target:
            raise MappingError("source and target profiles must differ")
        source_bounds = effective_bounds(source, source_bounds)
        target_bounds = effective_bounds(target, target_bounds)
        if not isinstance(unmapped, UnmappedPolicy):
            raise MappingError("unknown unmapped policy")
        if type(allow_target_aliases) is not bool:
            raise MappingError("target alias policy must be a boolean")
        self.source, self.target = source, target
        self.source_bounds, self.target_bounds = source_bounds, target_bounds
        self.unmapped = unmapped
        self.allow_target_aliases = allow_target_aliases
        self.rules = tuple(rule for rule in rules if rule.enabled)
        self._source_ranges: list[tuple[str, int, int, int, Rule]] = []
        seen_ids: set[str] = set()
        target_ranges: list[tuple[str, int, int]] = []
        for rule in self.rules:
            if not rule.rule_id or rule.rule_id in seen_ids:
                raise MappingError(f"empty or duplicate rule ID: {rule.rule_id}")
            seen_ids.add(rule.rule_id)
            if type(rule.count) is not int or rule.count <= 0:
                raise MappingError(f"count must be a positive decimal integer: {rule.rule_id}")
            first = _parse_plain(rule.source_start, source, None)
            destination = _parse_plain(rule.target_start, target, None)
            if first.family != destination.family:
                raise MappingError(f"X/Y family change is unsupported: {rule.rule_id}")
            source_end = first.number + rule.count - 1
            target_end = destination.number + rule.count - 1
            try:
                _check_end(source_end, first.family, source, source_bounds,
                           f"source rule {rule.rule_id} ({_format(first.family, first.number, source)}.."
                           f"{_format(first.family, source_end, source)})")
                _check_end(target_end, first.family, target, target_bounds,
                           f"target rule {rule.rule_id} ({_format(first.family, destination.number, target)}.."
                           f"{_format(first.family, target_end, target)})")
            except MappingError as error:
                raise MappingError(str(error), operand=rule.source_start) from error
            if any(family == first.family and first.number <= end and start <= source_end
                   for family, start, end, _target, _rule in self._source_ranges):
                raise MappingError(f"overlapping source rules: {rule.rule_id}")
            if not allow_target_aliases and any(family == first.family and destination.number <= end and start <= target_end
                   for family, start, end in target_ranges):
                raise MappingError(f"duplicate target assignment: {rule.rule_id}")
            self._source_ranges.append((first.family, first.number, source_end, destination.number, rule))
            target_ranges.append((first.family, destination.number, target_end))

    def _project_one(self, address: _Address) -> tuple[str, int, str | None]:
        for family, start, end, target, rule in self._source_ranges:
            if family == address.family and start <= address.number <= end:
                number = target + address.number - start
                return _format(address.family, number, self.target), number, rule.rule_id
        if self.unmapped is UnmappedPolicy.HOLD:
            raise UnmappedAddressError(f"unmapped X/Y address: {address.spelling}")
        if self.unmapped is UnmappedPolicy.NUMERIC_IDENTITY:
            spelling = _format(address.family, address.number, self.target)
        else:
            spelling = address.spelling
        # Validate the complete target span once all points are projected, so
        # a grouped operand reports both ends rather than only the first miss.
        target = _parse_plain(spelling, self.target, None)
        return spelling, target.number, None

    def project(self, reference: Reference) -> Projection:
        try:
            return self._project_reference(reference)
        except MappingError as error:
            # Keep the subtype used by HOLD preview while retaining the exact
            # source row and spelling for an actionable GUI diagnostic.
            raise type(error)(str(error), locator=reference.locator,
                              operand=reference.spelling) from error

    def _project_reference(self, reference: Reference) -> Projection:
        if not reference.locator:
            raise MappingError("source locator is required")
        if not isinstance(reference.spelling, str):
            raise MappingError(f"X/Y operand must be text at {reference.locator}")
        match = _OPERAND.fullmatch(reference.spelling.upper())
        if match is None:
            raise MappingError(f"unsupported X/Y operand at {reference.locator}: {reference.spelling}")
        group, family, digits, suffix = match.groups()
        if suffix:
            raise MappingError(f"dynamic or modified X/Y operand at {reference.locator}")
        try:
            _family, start, _end, width = source_span(reference.spelling, self.source, self.source_bounds)
        except MappingError as error:
            raise MappingError(f"source at {reference.locator}: {error}") from error
        first = _Address(family, start, reference.spelling[len(group or ""):], 1)
        targets: list[int] = []
        rule_ids: list[str | None] = []
        first_spelling = ""
        for offset in range(width):
            address = first if offset == 0 else _Address(
                family, first.number + offset, _format(family, first.number + offset, self.source), 1)
            spelling, number, rule_id = self._project_one(address)
            if not first_spelling:
                first_spelling = spelling
            targets.append(number)
            rule_ids.append(rule_id)
        if (None in rule_ids and any(rule_id is not None for rule_id in rule_ids)) or any(
            number != targets[0] + offset for offset, number in enumerate(targets)
        ):
            raise MappingError(f"operand span crosses mapping boundary at {reference.locator}")
        _check_end(targets[-1], family, self.target, self.target_bounds,
                   f"target operand span at {reference.locator} "
                   f"({_format(family, targets[0], self.target)}.."
                   f"{_format(family, targets[-1], self.target)})")
        if not any(rule_ids) and self.unmapped is UnmappedPolicy.SOURCE_SPELLING_IDENTITY:
            return Projection(reference.spelling, reference.spelling, (), reference.locator, width)
        return Projection(reference.spelling, (group or "") + first_spelling,
                          tuple(dict.fromkeys(rule_id for rule_id in rule_ids if rule_id is not None)),
                          reference.locator, width)

    def preflight(self, references: Iterable[Reference]) -> tuple[Projection, ...]:
        """Inspect program operands and comment keys together before publication."""
        return self.preflight_audit(references).projections

    def preflight_audit(self, references: Iterable[Reference]) -> MappingAudit:
        """Keep every occurrence and report many-to-one points in text mode."""
        projected: list[Projection] = []
        occupied: dict[tuple[str, int], tuple[str, int]] = {}
        alias_sources: dict[tuple[str, int], dict[tuple[str, int], set[str]]] = {}
        for reference in references:
            item = self.project(reference)
            match = _OPERAND.fullmatch(item.source.upper())
            assert match is not None
            source = _parse_plain(match.group(2) + match.group(3), self.source, self.source_bounds)
            target_match = _OPERAND.fullmatch(item.target.upper())
            assert target_match is not None
            target = _parse_plain(target_match.group(2) + target_match.group(3), self.target, self.target_bounds)
            for offset in range(item.width):
                source_key = (source.family, source.number + offset)
                target_key = (target.family, target.number + offset)
                prior = occupied.get(target_key)
                if prior is not None and prior != source_key and not self.allow_target_aliases:
                    raise MappingError(f"target alias at {reference.locator}: {item.target}",
                                       locator=reference.locator, operand=reference.spelling)
                occupied[target_key] = source_key
                alias_sources.setdefault(target_key, {}).setdefault(source_key, set()).add(item.locator)
            projected.append(item)
        aliases = tuple(TargetAlias(
            _format(family, number, self.target),
            tuple(_format(source_family, source_number, self.source)
                  for source_family, source_number in sorted(sources)),
            tuple(sorted({locator for locators in sources.values() for locator in locators})),
        ) for (family, number), sources in sorted(alias_sources.items()) if len(sources) > 1)
        return MappingAudit(tuple(projected), aliases)

    def reversed_candidate(self, *, source_bounds: Bounds | None = None,
                           target_bounds: Bounds | None = None) -> RangeMapper:
        """Reverse explicit one-to-one ranges; revalidate against new CPU bounds."""
        rules = (Rule(rule.rule_id, rule.target_start, rule.count, rule.source_start) for rule in self.rules)
        return RangeMapper(source=self.target, target=self.source, source_bounds=source_bounds,
                           target_bounds=target_bounds, rules=rules, unmapped=UnmappedPolicy.HOLD)
