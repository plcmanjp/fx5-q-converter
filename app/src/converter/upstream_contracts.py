"""Pinned public decoder adapter and runtime contract types."""
from __future__ import annotations
from dataclasses import dataclass
import importlib, json, sys, tempfile, threading
from pathlib import Path, PurePosixPath
from typing import Any
_SOURCE_LOADER_LOCK = threading.Lock()
_SOURCE_MODULE_PREFIXES = ("gx3_core", "gx3_fx5_profile", "gx3_fx5_parser_toolkit")
class ContractValidationError(ValueError):
    """Raised when an upstream snapshot is incomplete or does not match its pin."""


@dataclass(frozen=True)
class ValidatedContracts:
    """Validated, evidence-only upstream facts available to a future Converter adapter."""

    source_contract: dict[str, Any]
    target_contract: dict[str, Any]
    target_adapter_contract: dict[str, Any]
    full_target_records: tuple[dict[str, Any], ...]
    provenance: dict[str, dict[str, Any]]
    source_payloads: dict[str, bytes]


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _json_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractValidationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def build_source_neutral_ir(
    source: Path,
    *,
    source_bytes: bytes | None = None,
    contracts: ValidatedContracts | None = None,
    public_overlay: Any | None = None,
) -> dict[str, Any]:
    """Execute pinned code; keep legacy authority and public code identities separate.

    The default path preserves the accepted legacy decoder for regression and
    rollback. Current product callers explicitly supply a validated public overlay.
    """
    validated = contracts or validate_vendored_contracts()
    from converter.distribution import PUBLIC_RELEASE
    if PUBLIC_RELEASE and public_overlay is None:
        _fail("Public releases require the pinned public decoder")
    source_payloads = validated.source_payloads if public_overlay is None else public_overlay.source_payloads
    expected_commit = (validated.provenance["source"]["source_commit"] if public_overlay is None
                       else public_overlay.public_provenance["commit"])
    production_prefix = "production-src/src/"
    production = {
        path[len(production_prefix):]: body
        for path, body in source_payloads.items()
        if path.startswith(production_prefix)
    }
    if not production or "gx3_fx5_parser_toolkit/ir.py" not in production:
        _fail("P002 production source payload is incomplete")
    with tempfile.TemporaryDirectory(prefix="converter-p002-adapter-") as temporary:
        root = Path(temporary)
        source_root = root / "production-src" / "src"
        (root / "production-src" / "PACKAGE-MANIFEST.json").parent.mkdir(parents=True, exist_ok=True)
        (root / "production-src" / "PACKAGE-MANIFEST.json").write_text(
            json.dumps({"source_commit": expected_commit}, sort_keys=True),
            encoding="utf-8",
        )
        for relative, body in production.items():
            target = source_root / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        decoder_input = source
        if source_bytes is not None:
            decoder_input = root / "input.gx3"
            decoder_input.write_bytes(source_bytes)
        with _SOURCE_LOADER_LOCK:
            collisions = [
                name for name in sys.modules
                if any(name == prefix or name.startswith(prefix + ".") for prefix in _SOURCE_MODULE_PREFIXES)
            ]
            if collisions:
                _fail("P002 isolated decoder namespace is already occupied")
            sys.path.insert(0, str(source_root))
            try:
                module = importlib.import_module("gx3_fx5_parser_toolkit.ir")
                module_path = Path(module.__file__).resolve()
                if source_root.resolve() not in module_path.parents:
                    _fail("P002 decoder resolved outside the vendored namespace")
                decoder = getattr(module, "build_neutral_ir", None)
                if not callable(decoder):
                    _fail("P002 Neutral IR decoder entrypoint is not callable")
                value = decoder(decoder_input)
            finally:
                if sys.path and sys.path[0] == str(source_root):
                    sys.path.pop(0)
                for name in tuple(sys.modules):
                    if any(name == prefix or name.startswith(prefix + ".") for prefix in _SOURCE_MODULE_PREFIXES):
                        sys.modules.pop(name, None)
    if not isinstance(value, dict):
        _fail("P002 Neutral IR decoder returned a non-object")
    if (
        value.get("schema_name") != "plcman.gx3.neutral-ir"
        or value.get("schema_version") != "1.0.0"
        or value.get("producer", {}).get("package") != "gx3-fx5-parser-toolkit"
        or value.get("producer", {}).get("source_commit") != expected_commit
        or (public_overlay is not None and value.get("producer", {}).get("version") != "0.4.0")
        or value.get("profile", {}).get("detector_status") != "SUPPORTED"
    ):
        _fail("P002 Neutral IR result is incompatible or unsupported")
    schema = _json_bytes(
        validated.source_payloads.get("schemas/neutral-ir-1.0.0.schema.json", b""),
        "P002 Neutral IR schema",
    )
    allowed_findings = schema.get("$defs", {}).get("finding", {}).get("properties", {}).get("finding_code", {}).get("enum")
    findings = value.get("findings")
    pous = value.get("pous")
    if (
        allowed_findings != ["MINING_REQUIRED", "SOURCE_GLOBAL_ORDER_NOT_SERIALIZED"]
        or not isinstance(findings, list)
        or not isinstance(pous, list)
    ):
        _fail("P002 Neutral IR finding schema is incompatible")
    emitted = [*findings]
    for pou in pous:
        if not isinstance(pou, dict) or not isinstance(pou.get("findings"), list):
            _fail("P002 Neutral IR POU finding inventory is invalid")
        emitted.extend(pou["findings"])
    if any(
        not isinstance(finding, dict) or finding.get("finding_code") not in allowed_findings
        for finding in emitted
    ):
        _fail("P002 Neutral IR emitted finding is outside the declared schema")
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ContractValidationError("P002 Neutral IR result is not JSON-safe") from error
    return value


def validate_vendored_contracts(root=None):
    if root is not None:
        _fail("Private contract archives are not supported in public builds")
    from converter.runtime_facts import validate_runtime_facts
    return validate_runtime_facts()
