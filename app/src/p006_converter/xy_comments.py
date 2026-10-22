"""Read-only X/Y comment impact projection; target CSV authority is separate.

The caller supplies source scope/language coverage and verified CPU ceilings.
This module preserves source comment state and never serializes target comments.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from p006_converter.xy_mapping import Bounds, MappingError, Reference, _parse_plain
from p006_converter.xy_settings import MappingPreview, MappingSettings, preview


@dataclass(frozen=True)
class CommentRecord:
    device: str
    locator: str
    scope: str
    language: str
    text: str | None

    @property
    def text_state(self) -> str:
        return "ABSENT" if self.text is None else ("EMPTY" if self.text == "" else "VALUE")


@dataclass(frozen=True)
class CommentImpact:
    source_device: str
    target_device: str
    locator: str
    scope: str
    language: str
    text: str | None
    rule_ids: tuple[str, ...]

    @property
    def text_state(self) -> str:
        return "ABSENT" if self.text is None else ("EMPTY" if self.text == "" else "VALUE")


@dataclass(frozen=True)
class UnsupportedComment:
    locator: str
    scope: str
    language: str


@dataclass(frozen=True)
class CommentImpactPreview:
    mapping: MappingPreview
    impacts: tuple[CommentImpact, ...]
    unsupported: tuple[UnsupportedComment, ...]
    source_slots: frozenset[tuple[str, str]]

    def require_address_preflight(
        self, *, settings: MappingSettings, input_sha256: str,
        source_bounds: Bounds, target_bounds: Bounds,
        source_slots: frozenset[tuple[str, str]],
    ) -> None:
        self.mapping.require_publishable(settings=settings, input_sha256=input_sha256,
                                         source_bounds=source_bounds, target_bounds=target_bounds)
        if self.source_slots != source_slots:
            raise MappingError("comment coverage preflight is stale")
        if self.unsupported:
            locations = ", ".join(item.locator for item in self.unsupported)
            raise MappingError(f"unsupported comment scope/language at {locations}")


def _coverage_set(values: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    if not isinstance(values, frozenset) or not values or any(
        not isinstance(item, tuple) or len(item) != 2 or
        any(not isinstance(part, str) or not part for part in item)
        for item in values
    ):
        raise MappingError("verified source scope/language pairs are required")
    return values


def preview_comment_impacts(
    settings: MappingSettings, *, source_bounds: Bounds, target_bounds: Bounds,
    input_sha256: str, program: Iterable[Reference], comments: Iterable[CommentRecord],
    source_slots: frozenset[tuple[str, str]],
) -> CommentImpactPreview:
    """Include comment-only keys while retaining text, provenance and source scope."""
    slots = _coverage_set(source_slots)
    program_rows = tuple(program)
    comment_rows = tuple(comments)
    for item in comment_rows:
        if not isinstance(item, CommentRecord) or not isinstance(item.locator, str) or not item.locator:
            raise MappingError("comment locator is required")
        if not isinstance(item.scope, str) or not item.scope or not isinstance(item.language, str) or not item.language:
            raise MappingError(f"comment scope/language is invalid at {item.locator}")
        if item.text is not None and not isinstance(item.text, str):
            raise MappingError(f"comment text state is invalid at {item.locator}")
        _parse_plain(item.device, settings.source, source_bounds)
    mapping = preview(settings, source_bounds=source_bounds, target_bounds=target_bounds,
                      input_sha256=input_sha256, program=program_rows,
                      comments=(Reference(item.device, item.locator) for item in comment_rows))
    unsupported = tuple(UnsupportedComment(item.locator, item.scope, item.language)
                        for item in comment_rows if (item.scope, item.language) not in slots)
    impacts: list[CommentImpact] = []
    occupied: dict[tuple[str, str, str, int], str] = {}
    held = Counter((item.source, item.locator) for item in mapping.unmapped)
    projections = iter(mapping.projections)
    rows = ([(None, item) for item in program_rows]
            + [(item, Reference(item.device, item.locator)) for item in comment_rows])
    for item, reference in rows:
        key = (reference.spelling, reference.locator)
        if held[key]:
            held[key] -= 1
            continue
        projection = next(projections)
        if item is None:
            continue
        target = _parse_plain(projection.target, settings.target, target_bounds)
        key = (item.scope, item.language, target.family, target.number)
        if key in occupied:
            raise MappingError(f"duplicate comment target key at {occupied[key]} and {item.locator}")
        occupied[key] = item.locator
        impacts.append(CommentImpact(item.device, projection.target, item.locator, item.scope,
                                     item.language, item.text, projection.rule_ids))
    return CommentImpactPreview(mapping, tuple(impacts), unsupported, slots)
