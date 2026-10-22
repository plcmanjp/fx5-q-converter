"""Versioned X/Y mapping settings and read-only impact preflight.

The settings carry no CPU address authority. The v1 Q06UDV wire token denotes
the Q hexadecimal X/Y address profile, not an identified CPU. A caller supplies
optional verified project bounds and an immutable input hash for every preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable
from .module_mapping import validate_rules

from converter.xy_mapping import (
    Bounds, MappingError, Profile, Projection, RangeMapper, Reference, Rule, TargetAlias,
    UnmappedAddressError, UnmappedPolicy, _OPERAND, _parse_plain,
)


SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 64 * 1024
MAX_RULES = 256
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_TOP_KEYS = frozenset({"schema_version", "source_profile", "target_profile", "unmapped_policy", "rules"})
_RULE_KEYS = frozenset({"rule_id", "enabled", "source_start", "count", "target_start"})


def _exact_keys(value: object, keys: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or value.keys() != keys:
        raise MappingError(f"unexpected {label} fields")
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise MappingError(f"duplicate settings field: {key}")
        result[key] = value
    return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _input_hash(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MappingError("input snapshot SHA-256 is required")
    return value.lower()


@dataclass(frozen=True)
class MappingSettings:
    source: Profile
    target: Profile
    unmapped: UnmappedPolicy
    rules: tuple[Rule, ...]
    module_rules: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        try:
            validate_rules(self.module_rules)
        except ValueError as error:
            raise MappingError(str(error)) from error
        if self.module_rules and (self.source is not Profile.Q or self.target is not Profile.FX5U):
            raise MappingError("모듈 매핑은 Q에서 FX5 방향만 지원합니다")
        if not isinstance(self.source, Profile) or not isinstance(self.target, Profile) or self.source == self.target:
            raise MappingError("invalid direction profiles")
        if not isinstance(self.unmapped, UnmappedPolicy):
            raise MappingError("unknown unmapped policy")
        if not isinstance(self.rules, tuple):
            raise MappingError("rules must be an immutable tuple")
        if len(self.rules) > MAX_RULES:
            raise MappingError("too many mapping rules")
        identifiers: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, Rule):
                raise MappingError("invalid mapping rule")
            if not isinstance(rule.rule_id, str) or not rule.rule_id or len(rule.rule_id) > 64:
                raise MappingError("invalid rule ID")
            if rule.rule_id in identifiers:
                raise MappingError(f"duplicate rule ID: {rule.rule_id}")
            identifiers.add(rule.rule_id)
            if type(rule.enabled) is not bool or type(rule.count) is not int or rule.count <= 0:
                raise MappingError(f"invalid enabled/count fields: {rule.rule_id}")
            if not isinstance(rule.source_start, str) or not isinstance(rule.target_start, str):
                raise MappingError(f"invalid address fields: {rule.rule_id}")
            try:
                rule.rule_id.encode("utf-8")
                rule.source_start.encode("utf-8")
                rule.target_start.encode("utf-8")
            except UnicodeError as error:
                raise MappingError(f"invalid Unicode in rule: {rule.rule_id}") from error

    @classmethod
    def from_json(
        cls, payload: bytes, *, expected_source: Profile, expected_target: Profile,
    ) -> MappingSettings:
        if not isinstance(payload, bytes) or len(payload) > MAX_SETTINGS_BYTES or not payload:
            raise MappingError("settings size or type is invalid")
        if payload.startswith(b"\xef\xbb\xbf"):
            raise MappingError("settings must be UTF-8 without BOM")
        try:
            obj = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs,
                             parse_constant=lambda value: (_ for _ in ()).throw(MappingError(f"invalid number: {value}")))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise MappingError("settings JSON is invalid") from error
        version = obj.get("schema_version") if isinstance(obj, dict) else None
        data = _exact_keys(obj, _TOP_KEYS | {"module_rules"} if version == 2 else _TOP_KEYS, "settings")
        if type(data["schema_version"]) is not int or data["schema_version"] not in (1, 2):
            raise MappingError("unsupported settings schema version")
        try:
            source = Profile(data["source_profile"])
            target = Profile(data["target_profile"])
            policy = UnmappedPolicy(data["unmapped_policy"])
        except (TypeError, ValueError) as error:
            raise MappingError("unsupported profile or unmapped policy") from error
        if source != expected_source or target != expected_target:
            raise MappingError("settings direction/profile mismatch")
        if not isinstance(data["rules"], list) or len(data["rules"]) > MAX_RULES:
            raise MappingError("invalid rules list")
        rules: list[Rule] = []
        for item in data["rules"]:
            row = _exact_keys(item, _RULE_KEYS, "rule")
            rules.append(Rule(row["rule_id"], row["source_start"], row["count"],
                              row["target_start"], row["enabled"]))
        modules = data.get("module_rules", [])
        if not isinstance(modules, list) or any(not isinstance(row, list) or len(row) != 2 for row in modules):
            raise MappingError("invalid module rules list")
        return cls(source, target, policy, tuple(rules), tuple(tuple(row) for row in modules))

    def to_json(self) -> bytes:
        data = {
            "schema_version": 2 if self.module_rules else SCHEMA_VERSION,
            "source_profile": self.source.value,
            "target_profile": self.target.value,
            "unmapped_policy": self.unmapped.value,
            "rules": [
                {"rule_id": r.rule_id, "enabled": r.enabled, "source_start": r.source_start,
                 "count": r.count, "target_start": r.target_start}
                for r in self.rules
            ],
        }
        if self.module_rules:
            data["module_rules"] = [list(row) for row in self.module_rules]
        payload = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MAX_SETTINGS_BYTES:
            raise MappingError("settings size limit exceeded")
        return payload

    @property
    def digest(self) -> str:
        return _sha256_bytes(self.to_json())

    def mapper(self, *, source_bounds: Bounds | None = None, target_bounds: Bounds | None = None,
               allow_target_aliases: bool = False) -> RangeMapper:
        return RangeMapper(source=self.source, target=self.target,
                           source_bounds=source_bounds, target_bounds=target_bounds,
                           rules=self.rules, unmapped=self.unmapped,
                           allow_target_aliases=allow_target_aliases)

    def reversed_candidate(self, *, source_bounds: Bounds | None = None, target_bounds: Bounds | None = None) -> MappingSettings:
        if self.module_rules:
            raise MappingError("모듈 매핑의 자동 역변환은 지원하지 않습니다")
        self.mapper(source_bounds=target_bounds, target_bounds=source_bounds).reversed_candidate(
            source_bounds=source_bounds, target_bounds=target_bounds)
        candidate = tuple(Rule(r.rule_id, r.target_start, r.count, r.source_start, r.enabled)
                          for r in self.rules)
        return MappingSettings(self.target, self.source, UnmappedPolicy.HOLD, candidate)


@dataclass(frozen=True)
class UnmappedDiagnostic:
    source: str
    locator: str
    width: int


@dataclass(frozen=True)
class MappingPreview:
    projections: tuple[Projection, ...]
    unmapped: tuple[UnmappedDiagnostic, ...]
    program_occurrences: int
    comment_rows: int
    mapped_unique_points: int
    unmapped_unique_points: int
    input_sha256: str
    settings_digest: str
    bounds_digest: str
    target_aliases: tuple[TargetAlias, ...] = ()
    allow_target_aliases: bool = False

    @property
    def publishable(self) -> bool:
        return not self.unmapped

    def require_publishable(
        self, *, settings: MappingSettings, input_sha256: str,
        source_bounds: Bounds | None = None, target_bounds: Bounds | None = None,
        allow_target_aliases: bool = False,
    ) -> None:
        self.require_current(settings=settings, input_sha256=input_sha256,
                             source_bounds=source_bounds, target_bounds=target_bounds,
                             allow_target_aliases=allow_target_aliases)
        if self.unmapped:
            raise MappingError(f"mapping preflight has {len(self.unmapped)} held references")

    def require_current(
        self, *, settings: MappingSettings, input_sha256: str,
        source_bounds: Bounds | None = None, target_bounds: Bounds | None = None,
        allow_target_aliases: bool = False,
    ) -> None:
        if (self.settings_digest != settings.digest or self.input_sha256 != _input_hash(input_sha256)
                or self.bounds_digest != _bounds_digest(source_bounds, target_bounds)
                or type(allow_target_aliases) is not bool
                or self.allow_target_aliases != allow_target_aliases):
            raise MappingError("mapping preflight is stale")


def _bounds_digest(source: Bounds | None, target: Bounds | None) -> str:
    source, target = source or Bounds(), target or Bounds()
    return _sha256_bytes(json.dumps([source.x, source.y, target.x, target.y], separators=(",", ":")).encode("ascii"))


def preview(
    settings: MappingSettings, *, source_bounds: Bounds | None = None, target_bounds: Bounds | None = None,
    input_sha256: str, program: Iterable[Reference], comments: Iterable[Reference],
    allow_target_aliases: bool = False,
) -> MappingPreview:
    """Inspect all source operands and comment keys without modifying any file."""
    source_hash = _input_hash(input_sha256)
    bounds_hash = _bounds_digest(source_bounds, target_bounds)
    program_rows = tuple(program)
    comment_rows = tuple(comments)
    mapper = settings.mapper(source_bounds=source_bounds, target_bounds=target_bounds,
                             allow_target_aliases=allow_target_aliases)
    ready: list[Reference] = []
    held: list[UnmappedDiagnostic] = []
    held_points: set[tuple[str, int]] = set()
    for reference in (*program_rows, *comment_rows):
        try:
            mapper.project(reference)
        except UnmappedAddressError:
            match = _OPERAND.fullmatch(reference.spelling.upper())
            assert match is not None
            width = 4 * int(match.group(1)[1]) if match.group(1) else 1
            source = _parse_plain(match.group(2) + match.group(3), settings.source, source_bounds)
            held.append(UnmappedDiagnostic(reference.spelling, reference.locator, width))
            held_points.update((source.family, source.number + offset) for offset in range(width))
        else:
            ready.append(reference)
    audit = mapper.preflight_audit(ready)
    projected = audit.projections
    mapped: set[tuple[str, int]] = set()
    unmapped: set[tuple[str, int]] = set()
    for item in projected:
        match = _OPERAND.fullmatch(item.source.upper())
        assert match is not None
        source = _parse_plain(match.group(2) + match.group(3), settings.source, source_bounds)
        group = mapped if item.rule_ids else unmapped
        group.update((source.family, source.number + offset) for offset in range(item.width))
    return MappingPreview(projected, tuple(held), len(program_rows), len(comment_rows), len(mapped),
                          len(unmapped | held_points), source_hash, settings.digest, bounds_hash,
                          audit.target_aliases, allow_target_aliases)
