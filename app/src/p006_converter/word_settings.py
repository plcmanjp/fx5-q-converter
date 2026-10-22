"""Strict, versioned opt-in for Q ZR to FX5 R decimal-number preservation.

This setting is independent of X/Y mapping and confers no target R capacity or
instruction-form approval. Callers must preflight every affected reference.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from p006_converter.xy_mapping import MappingError, Profile


SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 4096
_TOP_KEYS = frozenset({"schema_version", "source_profile", "target_profile", "zr_to_r"})


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MappingError(f"duplicate word settings field: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class WordDeviceSettings:
    source: Profile
    target: Profile
    zr_to_r: bool

    def __post_init__(self) -> None:
        if self.source is not Profile.Q or self.target is not Profile.FX5U:
            raise MappingError("word settings require Q to FX5U direction")
        if type(self.zr_to_r) is not bool:
            raise MappingError("zr_to_r must be a boolean")

    @classmethod
    def from_json(
        cls, payload: bytes, *, expected_source: Profile, expected_target: Profile,
    ) -> WordDeviceSettings:
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SETTINGS_BYTES:
            raise MappingError("word settings size or type is invalid")
        if payload.startswith(b"\xef\xbb\xbf"):
            raise MappingError("word settings must be UTF-8 without BOM")
        try:
            data = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_unique_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(MappingError(f"invalid number: {value}")),
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise MappingError("word settings JSON is invalid") from error
        if not isinstance(data, dict) or data.keys() != _TOP_KEYS:
            raise MappingError("unexpected word settings fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
            raise MappingError("unsupported word settings schema version")
        try:
            source = Profile(data["source_profile"])
            target = Profile(data["target_profile"])
        except (TypeError, ValueError) as error:
            raise MappingError("unsupported word settings profile") from error
        if source is not expected_source or target is not expected_target:
            raise MappingError("word settings direction/profile mismatch")
        return cls(source, target, data["zr_to_r"])

    def to_json(self) -> bytes:
        payload = (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_profile": self.source.value,
                    "target_profile": self.target.value,
                    "zr_to_r": self.zr_to_r,
                },
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n"
        ).encode("utf-8")
        return payload

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json()).hexdigest()
