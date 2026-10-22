"""Load pinned public runtime facts, not the private evidence that established them."""
import hashlib
import json
from pathlib import Path

from p006_converter import distribution
from p006_converter.upstream_contracts import ContractValidationError, ValidatedContracts

SCHEMA = "fx5-q-converter.runtime-facts.v1"


def load_facts(root=None):
    path = (root or Path(__file__).with_name("resources") / "public-runtime") / "facts.json"
    if not distribution.PUBLIC_RELEASE or not distribution.RUNTIME_FACTS_SHA256:
        raise ContractValidationError("Public runtime facts require an explicit public build")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ContractValidationError("Public runtime facts missing or oversized")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != distribution.RUNTIME_FACTS_SHA256:
        raise ContractValidationError("Public runtime facts checksum mismatch")
    value = json.loads(data)
    if (set(value) != {"schema", "version", "source", "target", "records", "finding_codes", "authority_manifest_sha256"}
            or value["schema"] != SCHEMA or len(value["records"]) != 42
            or value["finding_codes"] != ["MINING_REQUIRED", "SOURCE_GLOBAL_ORDER_NOT_SERIALIZED"]):
        raise ContractValidationError("Public runtime facts schema mismatch")
    from p006_app.branding import application_version
    version = application_version()
    if value["version"] != version:
        raise ContractValidationError("Public runtime facts version mismatch")
    return value


def validate_runtime_facts():
    value = load_facts()
    schema = {"$defs": {"finding": {"properties": {"finding_code": {"enum": value["finding_codes"]}}}}}
    return ValidatedContracts(value["source"], value["target"], {}, tuple(value["records"]), {},
                              {"schemas/neutral-ir-1.0.0.schema.json": json.dumps(schema).encode()})
