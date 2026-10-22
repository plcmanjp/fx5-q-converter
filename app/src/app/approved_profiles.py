"""Pinned, source-bound address profiles for background preparation workers.

The registry grants address reuse only. Neither a matching source nor CPU
ceiling checks grant GX lifecycle or field acceptance. Callers must discard
stale worker results, apply both settings together, and obtain confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from converter.device_relocation import DeviceRelocationSettings
from converter.fx5_target import device_check, validate_cpu
from converter.xy_mapping import Profile
from converter.xy_settings import MappingSettings


REGISTRY_SHA256 = "17a15e0d34eec3068cbf7bcb09d75ad1a9d4be811384d9d126442102c8a45e25"
RESOURCE_ROOT = Path(__file__).absolute().parent / "resources" / "approved-profiles"
MAX_REGISTRY_BYTES = 64 * 1024
MAX_PROFILE_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ApprovedProfileError(ValueError):
    """The approved bundle is unavailable or fails its integrity contract."""


@dataclass(frozen=True)
class ApprovedProfile:
    profile_id: str
    display_name: str
    source_sha256: str
    direction: str
    target_cpu: str
    compatible_cpus: tuple[str, ...]
    validation_scope: str
    settings: MappingSettings
    relocation_settings: DeviceRelocationSettings
    settings_source_path: str
    settings_source_sha256: str
    settings_digest: str
    relocation_settings_source_path: str
    relocation_settings_source_sha256: str
    relocation_settings_digest: str
    registry_source_path: str
    registry_source_sha256: str

    @property
    def native_validation(self) -> str:
        return "NOT_VERIFIED_BY_PROFILE_LOADER"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ApprovedProfileError("duplicate approved profile JSON key")
        result[key] = value
    return result


def _keys(value, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ApprovedProfileError("unexpected approved profile fields")


def _identity(value) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _check_ancestors(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ApprovedProfileError("approved profile path must not traverse links or reparse points")


def _read_verified(path: Path, limit: int, expected_sha256: str, expected_size: int | None = None) -> bytes:
    """Read bounded pinned bytes from one handle, checking its final path too."""
    from converter.core import _handle_final_path

    _check_ancestors(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
        raise ApprovedProfileError("approved profile must be a bounded regular file")
    if expected_size is not None and before.st_size != expected_size:
        raise ApprovedProfileError("approved profile size mismatch")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        intended = os.path.normcase(os.path.abspath(path))
        if (_identity(before) != _identity(opened) or _handle_final_path(descriptor) != intended or
                os.path.normcase(os.path.abspath(path.resolve())) != intended):
            raise ApprovedProfileError("approved profile changed while opening")
        body = bytearray()
        while chunk := os.read(descriptor, min(65536, limit + 1 - len(body))):
            body.extend(chunk)
            if len(body) > limit:
                raise ApprovedProfileError("approved profile exceeds byte limit")
        _check_ancestors(path)
        if (_identity(opened) != _identity(os.fstat(descriptor)) or
                _identity(opened) != _identity(path.lstat()) or
                _handle_final_path(descriptor) != intended or
                os.path.normcase(os.path.abspath(path.resolve())) != intended or len(body) != opened.st_size):
            raise ApprovedProfileError("approved profile changed while reading")
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ApprovedProfileError("approved profile checksum mismatch")
        return bytes(body)
    finally:
        os.close(descriptor)


def _resource_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ApprovedProfileError("invalid approved profile resource path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise ApprovedProfileError("approved profile resource escapes its root")
    path = root.joinpath(*parts.parts)
    _check_ancestors(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ApprovedProfileError("approved profile resource escapes its root")
    return path


def _validate_registry(payload: bytes) -> list[dict]:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ApprovedProfileError("approved profile registry must be UTF-8 without BOM")
    registry = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(
                              ApprovedProfileError("invalid approved profile JSON number")))
    _keys(registry, {"schema_version", "profiles"})
    if type(registry["schema_version"]) is not int or registry["schema_version"] != 1:
        raise ApprovedProfileError("unsupported approved profile registry version")
    profiles = registry["profiles"]
    if not isinstance(profiles, list) or len(profiles) > 64:
        raise ApprovedProfileError("invalid approved profile count")
    ids, sources = set(), set()
    for entry in profiles:
        _keys(entry, {"id", "display_name", "source_sha256", "direction", "target_cpus",
                      "validation_scope", "xy", "relocation"})
        for key in ("id", "display_name", "source_sha256"):
            if not isinstance(entry[key], str) or not entry[key] or len(entry[key]) > 256:
                raise ApprovedProfileError("invalid approved profile identity")
        if not _SHA.fullmatch(entry["source_sha256"]) or entry["direction"] != "Q_TO_FX5":
            raise ApprovedProfileError("invalid approved source or direction")
        if entry["id"] in ids or entry["source_sha256"] in sources:
            raise ApprovedProfileError("duplicate approved profile identity")
        ids.add(entry["id"])
        sources.add(entry["source_sha256"])
        cpus = entry["target_cpus"]
        if not isinstance(cpus, list) or not cpus or any(not isinstance(cpu, str) for cpu in cpus):
            raise ApprovedProfileError("invalid approved CPU list")
        if len(cpus) != len(set(cpus)) or entry["validation_scope"] != "ADDRESS_CEILINGS_ONLY":
            raise ApprovedProfileError("invalid approved validation scope")
        for cpu in cpus:
            validate_cpu(cpu)
        for key in ("xy", "relocation"):
            item = entry[key]
            _keys(item, {"path", "sha256", "size"})
            if (not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"]) or
                    type(item["size"]) is not int or not 0 < item["size"] <= MAX_PROFILE_BYTES):
                raise ApprovedProfileError("invalid approved resource identity")
    return profiles


def _check_target_spans(settings: MappingSettings, relocation: DeviceRelocationSettings, cpu: str) -> None:
    for rule in settings.rules:
        if rule.enabled:
            family, digits = rule.target_start[0], rule.target_start[1:]
            last = int(digits, settings.target.radix) + rule.count - 1
            finding = device_check(f"{family}{last:o}", cpu)
            if not finding or finding["state"] != "CANDIDATE":
                raise ApprovedProfileError("approved I/O mapping exceeds target CPU ceiling")
    for rule in relocation.rules:
        finding = device_check(f"{rule.target_family}{rule.target_start + rule.count - 1}", cpu)
        if not finding or finding["state"] != "CANDIDATE":
            raise ApprovedProfileError("approved relocation exceeds target CPU ceiling")


def load_approved_profile(source_sha256: str, direction: str, target_cpu: str,
                          complete_text: bool = True) -> ApprovedProfile | None:
    """Return the entire verified profile or None for an inapplicable source.

    File I/O is synchronous: invoke this API from the preparation worker.
    Invalid inputs or a damaged matching bundle raise ApprovedProfileError.
    No settings are applied by this function.
    """
    if not isinstance(source_sha256, str) or not _SHA.fullmatch(source_sha256):
        raise ApprovedProfileError("source identity requires an exact SHA-256")
    if type(complete_text) is not bool:
        raise ApprovedProfileError("complete_text must be a boolean")
    try:
        validate_cpu(target_cpu)
        if direction != "Q_TO_FX5" or not complete_text:
            return None
        root = RESOURCE_ROOT.absolute()
        registry_path = root / "registry.json"
        entries = _validate_registry(_read_verified(registry_path, MAX_REGISTRY_BYTES, REGISTRY_SHA256))
        entry = next((item for item in entries if item["source_sha256"] == source_sha256), None)
        if entry is None or target_cpu not in entry["target_cpus"]:
            return None
        paths, bodies = {}, {}
        for key in ("xy", "relocation"):
            item = entry[key]
            paths[key] = _resource_path(root, item["path"])
            bodies[key] = _read_verified(paths[key], MAX_PROFILE_BYTES, item["sha256"], item["size"])
        settings = MappingSettings.from_json(bodies["xy"], expected_source=Profile.Q, expected_target=Profile.FX5U)
        settings.mapper(allow_target_aliases=True)
        relocation = DeviceRelocationSettings.from_json(bodies["relocation"])
        if relocation.source_sha256 != source_sha256:
            raise ApprovedProfileError("approved relocation source does not match registry")
        for cpu in entry["target_cpus"]:
            _check_target_spans(settings, relocation, cpu)
        # Validate both assets together once more before returning any settings.
        _read_verified(registry_path, MAX_REGISTRY_BYTES, REGISTRY_SHA256)
        for key in paths:
            item = entry[key]
            _read_verified(paths[key], MAX_PROFILE_BYTES, item["sha256"], item["size"])
        return ApprovedProfile(
            entry["id"], entry["display_name"], source_sha256, direction, target_cpu,
            tuple(entry["target_cpus"]), entry["validation_scope"], settings, relocation,
            str(paths["xy"]), entry["xy"]["sha256"], settings.digest,
            str(paths["relocation"]), entry["relocation"]["sha256"], relocation.digest,
            str(registry_path), REGISTRY_SHA256)
    except ApprovedProfileError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, RuntimeError) as error:
        raise ApprovedProfileError(f"approved profile cannot be loaded: {error}") from error
