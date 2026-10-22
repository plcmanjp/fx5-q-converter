"""Fail-closed reader for a versioned, published GX3 FX5 parser wheel overlay.

The overlay contains public production source resources only. It never
replaces the accepted private source and target authority archives.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import stat
from typing import Any
import zipfile


OVERLAY_SCHEMA = "plcman.p006.public-upstream-overlay.v1"
PUBLIC_PACKAGE = "gx3-fx5-parser-toolkit"
PUBLIC_VERSION = "0.4.0"
PUBLIC_COMMIT = "d3dd42b2ca13bbc24f0016e258efb48d50b0eb7f"
PUBLIC_TREE = "99721fee59932e37edb8c027c410f8c3ddefe504"
APPROVED_WHEEL_BYTES = 81659
APPROVED_WHEEL_SHA256 = "b38c04fc2648c1dcb2feed08b4237d91a777633cdf8888284abb629c84bd8dcf"
APPROVED_SCHEMA_SHA256 = "b42b578be8b10190c4a7e8ff7dc52dda05b1e30cd1a21a03b16b92a51eb4f6ed"
EXPECTED_CONSUMER_BASELINE = "2cc317e6cac6fb3d8633b9d7506368fd0e1f6fd5"
EXPECTED_CONSUMER_VERSION = "2.1.3"
MAX_WHEEL_BYTES = 16 * 1024 * 1024
MAX_WHEEL_ENTRIES = 256
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_RATIO = 1_000

_WHEEL_PATH = "wheel/d3dd42b2ca13bbc24f0016e258efb48d50b0eb7f/gx3_fx5_parser_toolkit-0.4.0-py3-none-any.whl"
_AUTHORITY_PATH = "upstream-contracts/manifest.json"
_EXPECTED_PUBLIC_PAYLOADS = frozenset({
    "gx3_core/__init__.py", "gx3_core/archive.py", "gx3_core/models.py", "gx3_core/xml.py",
    "gx3_fx5_profile/__init__.py", "gx3_fx5_profile/config.py", "gx3_fx5_profile/decoder.py", "gx3_fx5_profile/detector.py",
    "gx3_fx5_profile/opcode_signatures.py", "gx3_fx5_profile/topology.py",
    "gx3_fx5_parser_toolkit/__init__.py", "gx3_fx5_parser_toolkit/_provenance.json",
    "gx3_fx5_parser_toolkit/acceptance.py", "gx3_fx5_parser_toolkit/cli.py",
    "gx3_fx5_parser_toolkit/ir.py", "gx3_fx5_parser_toolkit/official_export.py",
    "gx3_fx5_parser_toolkit/projections.py",
    "gx3_fx5_parser_toolkit/schemas/neutral-ir-1.0.0.schema.json",
})
_EXPECTED_AUTHORITY_PINS = {
    "source": {
        "kind": "source", "local_path": "p002-fx5u-neutral-source-contract-1.1.9.zip",
        "bundle_version": "1.1.9", "source_commit": "5443beb01cc30f6827fd474ab4d147edc3525313",
        "byte_count": 509343,
        "sha256": "189f76d3d3e566227ab356f0e615a26fbd6a3ee008ee01d4f4172afb9c39a8b7",
    },
    "target": {
        "kind": "target", "local_path": "p004-q06udv-target-contract-1.8.7.zip",
        "bundle_version": "1.8.7", "source_commit": "5443beb01cc30f6827fd474ab4d147edc3525313",
        "byte_count": 20708675,
        "sha256": "7e3803554be291269ea94c54bbbed2167621c96c88af20ec00740018e4f391a5",
    },
}


class PublicUpstreamValidationError(ValueError):
    """Raised when a public overlay is missing, unsafe, or not pinned."""


@dataclass(frozen=True)
class ValidatedPublicUpstream:
    manifest: dict[str, Any]
    public_provenance: dict[str, str]
    authority_pins: tuple[dict[str, Any], ...]
    source_payloads: dict[str, bytes]


def _fail(message: str) -> None:
    raise PublicUpstreamValidationError(message)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(value: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicUpstreamValidationError(f"{label} is not UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        _fail(f"{label} must be an object")
    return parsed


def default_public_overlay_root() -> Path:
    return Path(__file__).with_name("resources") / "public-upstream"


def default_authority_root() -> Path:
    return Path(__file__).with_name("resources") / "upstream-contracts"


def _regular(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        _fail(f"{label} is missing")
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        _fail(f"{label} must be a regular local file")


def _safe_name(name: str) -> bool:
    """Accept one unambiguous, relative POSIX regular-file archive name."""
    if not name or "\\" in name or ":" in name or name.startswith("/"):
        return False
    if "//" in name or name.startswith("./") or name.endswith("/"):
        return False
    return all(part not in {"", ".", ".."} for part in name.split("/"))


def _validate_zip_entries(infos: list[zipfile.ZipInfo]) -> None:
    if not infos or len(infos) > MAX_WHEEL_ENTRIES:
        _fail("public wheel entry count is invalid")
    names = [item.filename for item in infos]
    if len(names) != len(set(names)) or len({name.casefold() for name in names}) != len(names):
        _fail("public wheel has duplicate or case-colliding entries")
    total = 0
    for info in infos:
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if (
            not _safe_name(info.filename)
            or info.is_dir()
            or kind not in {0, stat.S_IFREG}
            or info.file_size > MAX_ENTRY_BYTES
            or (info.file_size and info.compress_size == 0)
            or (info.compress_size and info.file_size / info.compress_size > MAX_RATIO)
        ):
            _fail("public wheel contains an unsafe entry")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        _fail("public wheel uncompressed size exceeds limit")


def _validate_payload_inventory(entries: dict[str, bytes]) -> None:
    present = {
        name for name in entries
        if name.startswith(("gx3_core/", "gx3_fx5_profile/", "gx3_fx5_parser_toolkit/"))
        and (name.endswith(".py") or name.endswith(".json"))
    }
    if present != _EXPECTED_PUBLIC_PAYLOADS:
        _fail("public wheel production resource inventory differs")


def _read_wheel(path: Path) -> dict[str, bytes]:
    _regular(path, "public wheel")
    if path.stat().st_size != APPROVED_WHEEL_BYTES or path.stat().st_size > MAX_WHEEL_BYTES:
        _fail("public wheel byte count differs from approved artifact")
    body = path.read_bytes()
    if _sha256(body) != APPROVED_WHEEL_SHA256:
        _fail("public wheel SHA-256 differs from approved artifact")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as error:
        raise PublicUpstreamValidationError("public wheel is not a ZIP") from error
    with archive:
        infos = archive.infolist()
        _validate_zip_entries(infos)
        entries = {info.filename: archive.read(info) for info in infos}
    _validate_payload_inventory(entries)
    return entries


def _contracts_from_manifest(manifest: dict[str, Any], label: str) -> tuple[dict[str, Any], ...]:
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list) or len(contracts) != 2:
        _fail(f"{label} contract inventory is invalid")
    results: list[dict[str, Any]] = []
    for kind in ("source", "target"):
        matching = [item for item in contracts if isinstance(item, dict) and item.get("kind") == kind]
        expected = _EXPECTED_AUTHORITY_PINS[kind]
        if len(matching) != 1 or any(matching[0].get(field) != value for field, value in expected.items()):
            _fail(f"{label} {kind} contract differs from accepted pin")
        results.append(dict(expected))
    return tuple(results)


def _validate_actual_authority(authority_root: Path) -> tuple[bytes, tuple[dict[str, Any], ...]]:
    if authority_root.is_symlink() or not authority_root.is_dir():
        _fail("actual authority root must be a local directory")
    manifest_path = authority_root / "manifest.json"
    _regular(manifest_path, "actual authority manifest")
    manifest_body = manifest_path.read_bytes()
    pins = _contracts_from_manifest(_json(manifest_body, "actual authority manifest"), "actual authority")
    for pin in pins:
        archive = authority_root / pin["local_path"]
        _regular(archive, f"actual {pin['kind']} authority archive")
        if archive.stat().st_size != pin["byte_count"] or _sha256_file(archive) != pin["sha256"]:
            _fail(f"actual {pin['kind']} authority archive differs from accepted pin")
    return manifest_body, pins


def _validate_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    required = {"schema", "consumer", "public", "wheel", "authority_manifest", "authority_pins"}
    if set(manifest) != required or manifest.get("schema") != OVERLAY_SCHEMA:
        _fail("public overlay manifest schema or fields are invalid")
    if manifest["consumer"] != {"baseline": EXPECTED_CONSUMER_BASELINE, "version": EXPECTED_CONSUMER_VERSION}:
        _fail("consumer baseline or version differs from accepted overlay consumer")
    if manifest["public"] != {
        "repository": "plcmanjp/gx-re-toolkit", "commit": PUBLIC_COMMIT, "tree": PUBLIC_TREE,
        "package": PUBLIC_PACKAGE, "version": PUBLIC_VERSION,
    }:
        _fail("published public provenance is invalid")
    expected_wheel = {"path": _WHEEL_PATH, "bytes": APPROVED_WHEEL_BYTES, "sha256": APPROVED_WHEEL_SHA256}
    if manifest["wheel"] != expected_wheel:
        _fail("public wheel pin differs from approved artifact")
    authority = manifest["authority_manifest"]
    if not isinstance(authority, dict) or set(authority) != {"path", "sha256"} or authority["path"] != _AUTHORITY_PATH or not isinstance(authority["sha256"], str) or len(authority["sha256"]) != 64:
        _fail("private authority manifest pin is invalid")
    if not isinstance(manifest["authority_pins"], list):
        _fail("private authority source/target pins are invalid")
    pins = _contracts_from_manifest({"contracts": manifest["authority_pins"]}, "overlay authority")
    return expected_wheel, pins


def validate_public_upstream(root: Path | None = None, *, authority_root: Path | None = None) -> ValidatedPublicUpstream:
    """Validate a public overlay against the actual accepted authority root."""
    unresolved = root or default_public_overlay_root()
    if unresolved.is_symlink() or not unresolved.is_dir():
        _fail("public overlay root must be a local directory")
    base = unresolved.resolve()
    from converter.distribution import PUBLIC_RELEASE
    if PUBLIC_RELEASE and authority_root is None:
        from converter.runtime_facts import load_facts
        facts = load_facts()
        replica_path = base / _AUTHORITY_PATH
        _regular(replica_path, "public authority digest manifest")
        actual_manifest = replica_path.read_bytes()
        if _sha256(actual_manifest) != facts["authority_manifest_sha256"]:
            _fail("public runtime and authority digest manifest differ")
        actual_pins = _contracts_from_manifest(_json(actual_manifest, "authority digest manifest"), "public runtime")
    else:
        actual_manifest, actual_pins = _validate_actual_authority(authority_root or default_authority_root())
    manifest_path = base / "manifest.json"
    _regular(manifest_path, "public overlay manifest")
    manifest = _json(manifest_path.read_bytes(), "public overlay manifest")
    _, pins = _validate_manifest(manifest)
    if pins != actual_pins:
        _fail("overlay authority pins differ from actual accepted authority")
    replica_path = base / _AUTHORITY_PATH
    _regular(replica_path, "private authority manifest replica")
    replica = replica_path.read_bytes()
    if replica != actual_manifest or _sha256(replica) != manifest["authority_manifest"]["sha256"]:
        _fail("private authority manifest replica differs from actual authority")
    entries = _read_wheel(base / _WHEEL_PATH)
    try:
        metadata = entries["gx3_fx5_parser_toolkit-0.4.0.dist-info/METADATA"].decode("utf-8")
    except (KeyError, UnicodeDecodeError) as error:
        raise PublicUpstreamValidationError("public wheel metadata is missing or invalid") from error
    if "Name: gx3-fx5-parser-toolkit" not in metadata or "Version: 0.4.0" not in metadata:
        _fail("public wheel metadata package or version differs")
    provenance = _json(entries["gx3_fx5_parser_toolkit/_provenance.json"], "public wheel provenance")
    if provenance != {"format": "gx-re-toolkit.source.v1", "source_commit": PUBLIC_COMMIT, "source_tree": PUBLIC_TREE}:
        _fail("public wheel provenance differs")
    schema_name = "gx3_fx5_parser_toolkit/schemas/neutral-ir-1.0.0.schema.json"
    if _sha256(entries[schema_name]) != APPROVED_SCHEMA_SHA256:
        _fail("public wheel schema SHA-256 differs from approved artifact")
    schema = _json(entries[schema_name], "public wheel schema")
    if schema.get("$id") != "https://github.com/plcmanjp/001-plc-workspace/neutral-ir-1.0.0.schema.json":
        _fail("public wheel Neutral IR schema differs")
    payloads = {"production-src/src/" + name: entries[name] for name in sorted(_EXPECTED_PUBLIC_PAYLOADS)}
    return ValidatedPublicUpstream(
        manifest, {"commit": PUBLIC_COMMIT, "tree": PUBLIC_TREE, "wheel_sha256": APPROVED_WHEEL_SHA256},
        pins, payloads,
    )
