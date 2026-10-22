"""Typed, value-independent source meaning used before Q projection.

This module deliberately has no GX Works2/Q imports.  It records what the
GX3 serialized source said, while ``core.Instruction`` remains the legacy
CSV-facing adapter.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Iterable


class OperandKind(str, Enum):
    DEVICE = "device"
    LITERAL = "literal"
    INDIRECT = "indirect"
    INDEXED = "indexed"
    STRING = "string"
    EMPTY = "empty"


@dataclass(frozen=True)
class TypedOperand:
    """One rendered operand plus its source-level class and semantic shape."""

    rendered: str
    kind: OperandKind
    device_family: str = ""
    width: str = ""
    signedness: str = ""
    indexed: bool = False
    digit_count: int = 0


@dataclass(frozen=True)
class SerializedOperation:
    """Descriptor-complete operation before its graph/order projection."""

    family: str
    marker: str
    descriptors: tuple[str, ...]
    pulse: bool = False
    pulse_kind: str = "none"
    inverse: bool = False
    width: str = ""
    signedness: str = ""
    variant: str = ""

    @property
    def signature(self) -> str:
        """A value-independent grammar signature, stable across device values."""
        marker = re.sub(r"\d+", "#", self.marker)
        descriptors = tuple(re.sub(r"\d+", "#", item) for item in self.descriptors)
        return _signature_digest((self.family, marker, descriptors, self.pulse, self.pulse_kind, self.inverse, self.width, self.signedness, self.variant))


@dataclass(frozen=True)
class NeutralSourceInstruction:
    """Canonical source instruction with provenance, independent of target CPU."""

    operation: SerializedOperation
    operands: tuple[TypedOperand, ...]
    source_step: int
    sequence: int
    block_id: str = ""
    note: str = ""
    line_statement: str = ""

    @property
    def feature_signature(self) -> str:
        return _signature_digest((
            self.operation.signature,
            tuple(
                (operand.kind.value, operand.device_family, operand.width, operand.signedness, operand.indexed)
                + ((operand.digit_count,) if operand.digit_count else ())
                for operand in self.operands
            ),
            bool(self.note), bool(self.line_statement),
        ))


@dataclass(frozen=True)
class DecodeCoverage:
    """Exactly partitioned source-row coverage for one program or project."""

    direct_parser_blocks: int = 0
    direct_parser_rows: int = 0
    official_replay_blocks: int = 0
    official_replay_rows: int = 0
    oracle_recovery_blocks: int = 0
    oracle_recovery_rows: int = 0
    opaque_blocks: int = 0
    opaque_rows: int = 0
    feature_signatures: tuple[tuple[str, int], ...] = ()

    @property
    def expected_rows(self) -> int:
        return self.direct_parser_rows + self.official_replay_rows + self.oracle_recovery_rows + self.opaque_rows

    def as_report(self) -> dict[str, object]:
        return {
            "direct_parser": {"blocks": self.direct_parser_blocks, "rows": self.direct_parser_rows},
            "official_replay": {"blocks": self.official_replay_blocks, "rows": self.official_replay_rows},
            "oracle_recovery": {"blocks": self.oracle_recovery_blocks, "rows": self.oracle_recovery_rows},
            "opaque": {"blocks": self.opaque_blocks, "rows": self.opaque_rows},
            "expected_rows": self.expected_rows,
            "feature_signatures": [{"signature": value, "rows": rows} for value, rows in self.feature_signatures],
        }


def classify_operand(rendered: str, *, width: str = "", signedness: str = "") -> TypedOperand:
    if not rendered:
        return TypedOperand(rendered, OperandKind.EMPTY, width=width, signedness=signedness)
    if rendered.startswith('"'):
        return TypedOperand(rendered, OperandKind.STRING, width=width, signedness=signedness)
    if re.fullmatch(r"K-?\d+|H[0-9A-F]+|-?\d+", rendered):
        return TypedOperand(rendered, OperandKind.LITERAL, width=width, signedness=signedness)
    indirect = rendered.startswith("@")
    value = rendered[1:] if indirect else rendered
    family = re.match(r"(?:K\d+)?(U\d+\\G|[A-Z]+)", value)
    indexed = bool(re.search(r"Z\d+$", value))
    digit = re.match(r"K([1-8])(?=[A-Z])", value)
    grouped_family = re.match(r"K[1-8](SM|SB|[XYMLFBS])(?=[0-9A-F])", value)
    return TypedOperand(
        rendered,
        OperandKind.INDIRECT if indirect else (OperandKind.INDEXED if indexed else OperandKind.DEVICE),
        grouped_family.group(1) if grouped_family else (family.group(1) if family else ""),
        width,
        signedness,
        indexed=indexed,
        digit_count=int(digit.group(1)) if digit else 0,
    )


def coverage_from_rows(
    rows: Iterable[NeutralSourceInstruction], *, direct_blocks: int, opaque_blocks: int = 0, opaque_rows: int = 0
) -> DecodeCoverage:
    values = tuple(rows)
    signatures = Counter(row.feature_signature for row in values)
    return DecodeCoverage(
        direct_parser_blocks=direct_blocks,
        direct_parser_rows=len(values),
        opaque_blocks=opaque_blocks,
        opaque_rows=opaque_rows,
        feature_signatures=tuple(sorted(signatures.items())),
    )


def _signature_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
