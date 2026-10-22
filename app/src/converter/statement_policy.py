"""Target-only Line Statement policy; source text and raw type stay immutable."""
from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
import os

IN_PLC = "in_plc"
PRESERVE_SOURCE = "preserve_source"
POLICIES = (IN_PLC, PRESERVE_SOURCE)
SETTINGS_SCHEMA = "plcman.p006.statement-policy.v1"
MIGRATION_NOTICE = "Statement 기본값은 In PLC입니다. 이전 유형을 유지하려면 원본 유형 유지를 선택하세요."
NOPLF_BODY = "NOPLF (print page separator)"
# SH-080780ENG-AF section 9.2.1 caps Q line statements at 64 characters.
# FX5 longer text has no accepted native proof here; this is a review boundary,
# not a claim that FX5 has the same manufacturer ceiling or memory allocation.
REVIEW_CHARACTER_LIMIT = 64


def validate(value):
    if value not in POLICIES:
        raise ValueError("unknown Statement policy")
    return value


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate Statement settings key")
        result[key] = value
    return result


@dataclass(frozen=True)
class StatementSettings:
    policy: str = IN_PLC
    migration_notice: str = ""

    def __post_init__(self):
        validate(self.policy)

    @classmethod
    def from_json(cls, payload):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= 4096 or payload.startswith(b"\xef\xbb\xbf"):
            raise ValueError("invalid Statement settings bytes")
        obj = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if (not isinstance(obj, dict) or obj.get("schema") != SETTINGS_SCHEMA
                or set(obj) not in ({"schema"}, {"schema", "statement_policy"})):
            raise ValueError("invalid Statement settings schema")
        return cls(validate(obj.get("statement_policy", IN_PLC)),
                   MIGRATION_NOTICE if "statement_policy" not in obj else "")

    def to_json(self):
        return (json.dumps({"schema": SETTINGS_SCHEMA, "statement_policy": self.policy},
                           sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    @property
    def digest(self):
        return hashlib.sha256(self.to_json()).hexdigest()

    def save_new(self, path):
        """Explicit action only; no overwrite, link traversal or rollback delete."""
        from . import core
        target = core._reject_link_like_ancestors(Path(path), "new Statement settings")
        parent = target.parent
        parent_path = os.path.normcase(os.path.abspath(parent.resolve()))
        identity = core._object_identity(os.stat(parent, follow_symlinks=False))
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            expected = os.path.normcase(os.path.abspath(target))
            if core._handle_final_path(descriptor) != expected:
                raise ValueError("Statement settings final path differs")
            remaining = memoryview(self.to_json())
            while remaining:
                count = os.write(descriptor, remaining)
                if count <= 0:
                    raise OSError("Statement settings write made no progress")
                remaining = remaining[count:]
            os.fsync(descriptor)
            if (core._handle_final_path(descriptor) != expected
                    or os.path.normcase(os.path.abspath(parent.resolve())) != parent_path
                    or core._object_identity(os.stat(parent, follow_symlinks=False)) != identity):
                raise ValueError("Statement settings destination changed; partial file retained")
        finally:
            os.close(descriptor)


def source_fields(body, subtype, raw_type, evidence):
    """Called only after a reader has bound raw bytes/type/body to its row."""
    if not isinstance(body, str) or subtype not in {"i", "s"} or not raw_type or not evidence:
        raise ValueError("Statement source body/type binding is missing")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {"source_statement_body": body, "source_statement_type": subtype,
            "source_statement_raw_type": raw_type,
            "source_statement_evidence": {**evidence, "raw_type": raw_type, "subtype": subtype, "body_sha256": digest},
            "source_statement_body_sha256": digest}


def _bound(record, body, subtype, input_sha256):
    proof = record.get("source_statement_evidence", {})
    if not isinstance(proof, dict):
        return False
    raw_type = record.get("source_statement_raw_type")
    if (proof.get("raw_type") != raw_type or proof.get("subtype") != subtype
            or proof.get("locator") != record.get("locator")
            or proof.get("body_sha256") != record.get("source_statement_body_sha256")
            or not re.fullmatch(r"[0-9a-f]{64}", proof.get("input_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", proof.get("source_digest", ""))):
        return False
    if (input_sha256 is not None and proof["input_sha256"] != input_sha256
            or record.get("source_digest") is not None and proof["source_digest"] != record["source_digest"]):
        return False
    if raw_type in {"i", "s"}:
        return raw_type == subtype
    raw = proof.get("raw_text_hex", "")
    try:
        payload = bytes.fromhex(raw)
        value = int(raw_type, 16)
    except (ValueError, TypeError):
        return False
    if value != (1 if subtype == "s" else (len(payload) + 5) // 2):
        return False
    for encoding in ("cp949", "cp1252"):
        try:
            if payload == body.encode(encoding):
                return True
        except UnicodeEncodeError:
            continue
    return False


def project(record, policy=IN_PLC, *, generated=False, target_profile="FX5U", input_sha256=None):
    validate(policy)
    body = NOPLF_BODY if generated else record.get("source_statement_body")
    subtype = None if generated else record.get("source_statement_type")
    result = {"statement_policy": policy, "target_kind": "statement", "target_opcode": "",
              "source_statement_type": subtype, "target_statement_body": body,
              "target_statement_type": None, "statement_generated": generated,
              "statement_state": "HELD", "target_text": record.get("text", ""),
              "statement_reason": "STATEMENT_SOURCE_TYPE_UNVERIFIED"}
    if not generated:
        if (subtype not in {"i", "s"} or not isinstance(body, str)
                or not _bound(record, body, subtype, input_sha256)
                or record.get("source_statement_body_sha256") != hashlib.sha256(body.encode("utf-8")).hexdigest()
                or record.get("text") != ("*" if subtype == "s" else "") + body):
            return result
    target = "i" if policy == IN_PLC else "s" if generated else subtype
    result.update(target_statement_type=target, target_text=("*" if target == "s" else "") + body,
                  statement_reason="GENERATED_PRINT_EXPLANATION" if generated else
                  "PERIPHERAL_TO_IN_PLC" if subtype != target else "SOURCE_TYPE_RETAINED")
    if target == "i" and body.startswith("*"):
        result["statement_reason"] = "STATEMENT_IN_PLC_LEADING_STAR_ESCAPE_UNVERIFIED"
        return result
    if body.startswith("[Title]") and not (target_profile == "Q06UDV" and subtype == target == "i"):
        result["statement_reason"] = "STATEMENT_TITLE_TARGET_FORMAT_UNVERIFIED"
        return result
    if not body or any(ord(c) < 32 for c in body):
        result["statement_reason"] = "STATEMENT_TARGET_CONTROL_OR_EMPTY_TEXT_UNVERIFIED"
        return result
    if len(body) > REVIEW_CHARACTER_LIMIT:
        result["statement_reason"] = ("STATEMENT_Q_CHARACTER_LIMIT_EXCEEDED" if target_profile == "Q06UDV"
                                      else "STATEMENT_FX5_LONG_TEXT_UNVERIFIED")
        return result
    try:
        body.encode("cp949")
    except UnicodeEncodeError:
        result["statement_reason"] = "STATEMENT_TARGET_ENCODING_UNVERIFIED"
        return result
    result["statement_state"] = "SERIALIZED_CANDIDATE"
    return result


def summary(records, policy):
    rows = [r for r in records if r.get("target_kind") == "statement"]
    source = [r for r in rows if not r.get("statement_generated")]
    return {"policy": validate(policy), "source_statement_count": len(source),
            "source_noplf_count": sum(r.get("kind") == "instruction" and r.get("opcode") == "NOPLF" for r in records),
            "source_types": {(v or "unknown"): sum(r.get("source_statement_type") == v for r in source) for v in ("i", "s", None)},
            "generated_noplf_statement_count": sum(bool(r.get("statement_generated")) for r in rows),
            "target_types": {(v or "unknown"): sum(r.get("target_statement_type") == v for r in rows) for v in ("i", "s", None)},
            "type_changed_count": sum(r.get("source_statement_type") != r.get("target_statement_type")
                                      and r.get("source_statement_type") in {"i", "s"} for r in source),
            "held_count": sum(r.get("statement_state") == "HELD" for r in rows),
            "target_serialization": "CANDIDATE_ONLY", "official_gx_saved_reopened": "NOT_RUN_FOR_THIS_OUTPUT",
            "plc_preservation": "NOT_RUN"}
