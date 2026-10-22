"""Fail-closed Q-family GXW to FX5U GX Works3 CSV candidate conversion.

The pinned public reader is a source observation. Generated CSV remains a
candidate until a disposable GX Works3 project import, rebuild, and re-export
has been checked. No source project is modified.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from p006_converter.core import _open_input_snapshot, _reject_link_like_ancestors
from p006_converter.xy_mapping import Bounds, MappingError, Profile, Reference, fixed_bounds, source_span
from p006_converter.xy_settings import MappingSettings, preview
from p006_converter.word_settings import WordDeviceSettings
from p006_converter.fx5_target import CLOCK_ALIASES


READER_COMMIT = "45ff30bfef5a151032ad009dc2101006206070cc"
READER_SHA256 = "7ad6749ffda658899b16450f29222c7ee52dcfd3ad4fca0862c76adae05fd23e"
READER_FILE = "45ff30bfef5a151032ad009dc2101006206070cc/gxw_parser_toolkit-0.1.0-py3-none-any.whl"
SCHEMA = "plcman.p006.q-to-fx5-candidate.v2"
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_COMMENT_ENTRIES = 100_000
_PLAIN = re.compile(r"(M|D)([0-9]+)\Z")
_IO = re.compile(r"(?:K[1-8])?[XY][0-9A-F]+\Z")
_STATIC_LB = re.compile(r"(?:(L)([0-9]+)|(B)([0-9A-F]+))\Z")
_WORD_BIT_D = re.compile(r"D([0-9]+)\.([0-9A-F])\Z")
_STATIC_W = re.compile(r"W([0-9A-F]+)\Z")
_STATIC_Z = re.compile(r"Z([0-9]+)\Z")
_STATIC_T = re.compile(r"T([0-9]+)\Z")
_STATIC_ZR = re.compile(r"ZR([0-9]+)\Z")
_WORD_BIT_ZR = re.compile(r"ZR([0-9]+)\.([0-9A-F])\Z")
_STATIC_R = re.compile(r"R([0-9]+)\Z")
_NESTING = re.compile(r"N([0-9]+)\Z")
_WORD_BIT_R = re.compile(r"R([0-9]+)\.([0-9A-F])\Z")
_VERIFIED_SM = frozenset({"SM400", "SM401", "SM402", "SM410", "SM411", "SM412", "SM413"})
_K = re.compile(r"K-?[0-9]+\Z")
_H = re.compile(r"H[0-9A-F]+\Z")
_COMPARE = frozenset(prefix + relation for prefix in ("LD", "AND", "OR", "LDD", "ANDD", "ORD")
                     for relation in ("=", "<>", ">", ">=", "<", "<="))
_DWORD_COMPARE = frozenset(op for op in _COMPARE if op.startswith(("LDD", "ANDD", "ORD")))
_ARITHMETIC = frozenset({"+", "-", "*", "D+", "D-", "D/"})
_PULSE_CONTACT = frozenset({"LDP", "LDF", "LDPI", "LDFI", "ANDP", "ANDF", "ANDPI", "ANDFI", "ORP", "ORF", "ORPI", "ORFI"})
_PULSE_COIL = frozenset({"PLS", "PLF"})
_PULSE_DATA = frozenset({"MOVP", "DMOVP", "INCP", "DINCP"})
_BLOCK = frozenset({"BMOV", "BMOVP", "FMOV", "FMOVP"})
_DATA = _COMPARE | _ARITHMETIC | {"MOV", "DMOV", "INC", "DNEG", "SFL", "E+", "DECO",
                                  "BTOW", "DMAX", "DMIN", *CLOCK_ALIASES} | _PULSE_DATA | _BLOCK
_DWORD = _DWORD_COMPARE | {"DMOV", "DMOVP", "DINCP", "D+", "D-", "D/", "DNEG", "E+"}
_ARITY = {
    "LD": 1, "LDI": 1, "AND": 1, "ANI": 1, "OR": 1, "ORI": 1,
    **dict.fromkeys(_PULSE_CONTACT, 1), **dict.fromkeys(_PULSE_COIL, 1),
    "MEP": 0, "MEF": 0,
    "OUT": 1, "SET": 1, "RST": 1,
    "ANB": 0, "ORB": 0, "MPS": 0, "MRD": 0, "MPP": 0, "INV": 0,
    "MOV": 2, "DMOV": 2, "MOVP": 2, "DMOVP": 2, "BCD": 2,
    "DNEG": 1, "SFL": 2, "E+": 3, "DECO": 3, "FF": 1,
    "BTOW": 3, "DMAX": 3, "DMIN": 3, **dict.fromkeys(CLOCK_ALIASES, 1),
    "FOR": 1, "NEXT": 0, "MC": 2, "MCR": 1,
    **dict.fromkeys(_BLOCK, 3), "INCP": 1, "DINCP": 1,
    **dict.fromkeys(_COMPARE, 2), **dict.fromkeys(_ARITHMETIC, 3), "INC": 1,
    "END": 0,
}
_TWO_OR_THREE_D_ARITHMETIC = frozenset({"D+", "D-"})
_EXPECTED_ACCESS = {
    "DNEG": ("both",), "SFL": ("both", "read"),
    "E+": ("read", "read", "write"),
    "DECO": ("read", "write", "read"),
    "BTOW": ("read", "write", "read"),
    "DMAX": ("read", "write", "read"),
    "DMIN": ("read", "write", "read"),
    "DATERD": ("write",), "DATERDP": ("write",),
    "DATEWR": ("read",), "DATEWRP": ("read",), "FF": ("write",),
    "FOR": ("read",), "MC": ("read", "write"), "MCR": ("read",),
}


def _valid_arity(opcode: str, count: int, operand_tokens: tuple[str, ...] | None = None) -> bool:
    """Keep the two-operand in-place D form separate from other arithmetic."""
    return (count == _ARITY[opcode]
            or (opcode in _TWO_OR_THREE_D_ARITHMETIC and count == 2)
            or (opcode == "+" and count == 2 and operand_tokens is not None
                and _K.fullmatch(operand_tokens[0]) is not None
                and _STATIC_Z.fullmatch(operand_tokens[1]) is not None))


_CONTACT = frozenset({"LD", "LDI", "AND", "ANI", "OR", "ORI"})
_COIL = frozenset({"OUT", "SET", "RST"})
_OPEN_CONTACT = _CONTACT | _PULSE_CONTACT | _COMPARE | {"INV", "MEP", "MEF", "ANB", "ORB"}


def _static_z_expected_access(opcode: str, position: int, count: int) -> str | None:
    """Verified static Z roles; + destination reads and writes at runtime."""
    if opcode in {"MOV", "DMOV"} and count == 2:
        return "read" if position == 0 else "write"
    if opcode == "INC" and position == 0 and count == 1:
        return "both"
    if opcode == "-" and count == 3 and position in {0, 2}:
        return "read" if position == 0 else "write"
    if opcode == "+" and count == 2 and position == 1:
        return "write"
    if opcode == "*" and count == 3 and position in {0, 2}:
        return "read" if position == 0 else "write"
    return None


def _static_z_word_span(opcode: str, position: int) -> int:
    """DMOV reads/writes two adjacent words; word multiplication writes two."""
    return 2 if opcode == "DMOV" or opcode == "*" and position == 2 else 1


def _reader_pou_rows(reader: Any, body: bytes, comments: dict[str, str]):
    if getattr(reader, "POU_TEXT_ENCODING_API", 0) == 1:
        return reader.pou_rows(body, comments, text_encoding="auto")
    return reader.pou_rows(body, comments)


def _build_source_ir(ir_module: Any, reader: Any, source: Path):
    if getattr(reader, "POU_TEXT_ENCODING_API", 0) == 1:
        return ir_module.build(source, text_encoding="auto")
    return ir_module.build(source)


class ReverseConversionError(ValueError):
    """The source or target candidate cannot be projected without guesswork."""

    def __init__(self, code: str, *context: str, pou: str | None = None,
                 source_locator: str | None = None, operand: str | None = None):
        self.code = code
        self.context = context
        self.pou = pou
        self.source_locator = source_locator
        self.operand = operand
        super().__init__(":".join((code, *context)))


@dataclass(frozen=True)
class QDiagnostic:
    code: str
    severity: str
    blocking_scope: str
    pou: str | None
    source_locator: str | None
    operand: str | None
    reason: str
    action: str
    detail: str = ""


@dataclass(frozen=True)
class QProgramReference:
    pou: str
    source_locator: str
    operand_index: int
    raw_token: str
    opcode: str
    status: str


@dataclass(frozen=True)
class QCommentReference:
    device: str
    source_locator: str
    text: str


@dataclass(frozen=True)
class QSourceAnalysis:
    source_sha256: str | None
    source_cpu_identity: str
    pous: tuple[str, ...]
    program_references: tuple[QProgramReference, ...]
    comment_references: tuple[QCommentReference, ...]
    diagnostics: tuple[QDiagnostic, ...]
    inspection_scope: tuple[str, ...]
    uninspected_scope: tuple[str, ...]
    source_cpu_observation: str | None = None


def _cpu_observation(source: Path, reader: Any) -> str | None:
    """Best-effort reader metadata, never CPU compatibility or identity proof."""
    inspect = getattr(reader, "project_info", None)
    if inspect is None:
        return None
    _name, text = inspect(str(source))
    if isinstance(text, str) and re.fullmatch(r"QCPU \(Q mode\)(?: Q[0-9]{2}[A-Z0-9]{1,8})?", text):
        return text
    return None


def _unaddressed_module_labels(warning: Any) -> int | None:
    if (isinstance(warning, tuple) and len(warning) == 2
            and warning[0] == "UnaddressedModuleLabels"
            and type(warning[1]) is int and warning[1] > 0):
        return warning[1]
    return None


@dataclass(frozen=True)
class QPlan:
    analysis: QSourceAnalysis
    required_settings: tuple[str, ...]
    diagnostics: tuple[QDiagnostic, ...]
    publishable: bool
    outputs: Mapping[str, bytes] | None
    report: Mapping[str, Any] | None


def _q_diagnostic(code: str, *, scope: str = "project", pou: str | None = None,
                  locator: str | None = None, operand: str | None = None,
                  detail: str = "") -> QDiagnostic:
    actions = {
        "Q_SOURCE_INVALID": "일반 GX Works2 .gxw 원본 파일을 선택하세요.",
        "Q_READER_UNKNOWN": "GX Works2에서 해당 POU 원문을 확인하세요.",
        "POU_TEXT_UNSUPPORTED": "원본의 Statement 또는 Note를 검토하세요.",
        "Q_NOTE_TYPE_UNVERIFIED": "원본 Note의 문자, 형식 및 직전 명령 부착을 확인하세요.",
        "Q_TEXT_BINDING_UNVERIFIED": "원본 텍스트 행과 명령 위치의 결속을 확인하세요.",
        "CONTROL_STRUCTURE_UNVERIFIED": "FOR/NEXT 및 MC/MCR의 짝, 중첩, 제어 경계를 원본에서 확인하세요.",
        "CONTROL_NESTING_UNSUPPORTED": "MC/MCR N0..N14의 정규 표기와 중첩 순서를 확인하세요.",
        "CONTROL_COIL_ALIAS_UNVERIFIED": "MC 대상 M의 다른 쓰기 또는 중복 사용을 확인하세요.",
        "OPCODE_UNSUPPORTED": "해당 명령이 지원 범위에 있는지 확인하세요.",
        "ARITY_UNSUPPORTED": "원본 명령의 피연산자 수를 확인하세요.",
        "OPERAND_UNCLASSIFIED": "원본 피연산자의 의미를 확인하세요.",
        "Q_COMMENT_BINDING_UNVERIFIED": "디바이스 코멘트의 주소 연결을 확인하세요. 프로그램 진단과 별도로 변환을 차단합니다.",
        "Q_MODULE_COMMENTS_UNRESOLVED": "주소를 확정하지 못한 모듈 코멘트가 있습니다. 공식 코멘트 CSV와 대조하세요.",
        "PROJECT_IDENTIFIER_REQUIRED": "프로젝트 식별자를 확인하세요.",
        "FX5_DEVICE_BOUNDS_REQUIRED": "입력한 FX5 M/D 상한이 0 이상의 정수인지 확인하세요.",
        "XY_SETTINGS_REQUIRED": "원본에서 사용하는 X/Y 주소의 대상 배정을 확인하세요.",
        "XY_SOURCE_RANGE_INVALID": "원본 X/Y 주소와 전체 접근 구간을 확인하세요. 원문은 검토용 CSV에 보존됩니다.",
    }
    return QDiagnostic(code, "error", scope, pou, locator, operand,
                       code, actions.get(code, "원본과 검증된 변환 설정을 확인하세요."),
                       detail)


def _source_operand_code(token: str, opcode: str, position: int, *, operand_count: int | None = None,
                         operand_tokens: tuple[str, ...] | None = None) -> str | None:
    """Classify known source forms without applying any target address ceiling."""
    if opcode == "+" and operand_count == 2:
        if position == 0 and _K.fullmatch(token):
            return None if -32768 <= int(token[1:]) <= 32767 else "CONSTANT_RANGE_UNSUPPORTED"
        if position == 0:
            return "DEVICE_TYPE_UNSUPPORTED"
        # The only verified in-place + form here is K16 -> static Z.
        match = _STATIC_Z.fullmatch(token)
        return (None if match and str(int(match.group(1))) == match.group(1)
                else "DEVICE_TYPE_UNSUPPORTED")
    z_match = _STATIC_Z.fullmatch(token)
    if z_match:
        if str(int(z_match.group(1))) != z_match.group(1):
            return "DEVICE_TYPE_UNSUPPORTED"
        tokens = operand_tokens or ()
        if opcode in {"MOV", "DMOV"} and position == 1 and len(tokens) == 2:
            source = tokens[0]
            return (None if (opcode == "MOV" and _K.fullmatch(source))
                    or (_PLAIN.fullmatch(source) and source.startswith("D"))
                    else "DEVICE_TYPE_UNSUPPORTED")
        if opcode in {"MOV", "DMOV"} and position == 0 and len(tokens) == 2:
            target = tokens[1]
            return (None if _PLAIN.fullmatch(target) and target.startswith("D")
                    else "DEVICE_TYPE_UNSUPPORTED")
        if opcode == "*" and position in {0, 2} and len(tokens) == 3:
            return (None if _STATIC_Z.fullmatch(tokens[0]) and _K.fullmatch(tokens[1])
                    and _STATIC_Z.fullmatch(tokens[2]) else "DEVICE_TYPE_UNSUPPORTED")
        if opcode == "INC" and position == 0 and len(tokens) == 1:
            return None
        if opcode == "-" and position == 0 and len(tokens) == 3:
            return (None if _K.fullmatch(tokens[1]) and _STATIC_Z.fullmatch(tokens[2])
                    else "DEVICE_TYPE_UNSUPPORTED")
        if opcode == "-" and position == 2 and len(tokens) == 3:
            return (None if ((_PLAIN.fullmatch(tokens[0]) and tokens[0].startswith("D")
                             or _STATIC_Z.fullmatch(tokens[0]))
                    and _K.fullmatch(tokens[1])) else "DEVICE_TYPE_UNSUPPORTED")
        return "DEVICE_TYPE_UNSUPPORTED"
    if opcode == "FOR":
        if not _K.fullmatch(token):
            return "OPERAND_UNSUPPORTED"
        return None if 1 <= int(token[1:]) <= 32767 else "CONSTANT_RANGE_UNSUPPORTED"
    if opcode in {"MC", "MCR"}:
        if position == 0:
            match = _NESTING.fullmatch(token)
            return (None if match and str(int(match.group(1))) == match.group(1)
                    and 0 <= int(match.group(1)) <= 14 else "CONTROL_NESTING_UNSUPPORTED")
        match = _PLAIN.fullmatch(token)
        return (None if match and match.group(1) == "M" and str(int(match.group(2))) == match.group(2)
                else "DEVICE_TYPE_UNSUPPORTED")
    if opcode == "FF":
        return None if position == 0 and ((_PLAIN.fullmatch(token) and token.startswith("M"))
                                           or _STATIC_LB.fullmatch(token)) else "DEVICE_TYPE_UNSUPPORTED"
    if opcode in CLOCK_ALIASES:
        return None if (token.startswith("D") and _PLAIN.fullmatch(token)) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
    if opcode in {"DNEG", "E+"}:
        return None if (token.startswith("D") and _PLAIN.fullmatch(token)) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
    if opcode in {"BTOW", "DMAX", "DMIN"}:
        if position < 2:
            return None if (token.startswith("D") and _PLAIN.fullmatch(token)) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
        if not _K.fullmatch(token):
            return "OPERAND_UNSUPPORTED"
        return None if 1 <= int(token[1:]) <= 32767 else "CONSTANT_RANGE_UNSUPPORTED"
    if opcode == "SFL":
        if position == 0:
            return None if (token.startswith("D") and _PLAIN.fullmatch(token)) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
        if not _K.fullmatch(token):
            return "OPERAND_UNSUPPORTED"
        return None if 0 <= int(token[1:]) <= 15 else "CONSTANT_RANGE_UNSUPPORTED"
    if opcode == "DECO":
        if position == 0:
            return None if (token.startswith("D") and _PLAIN.fullmatch(token)) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
        if position == 1:
            return None if _PLAIN.fullmatch(token) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
        if not _K.fullmatch(token):
            return "OPERAND_UNSUPPORTED"
        upper = 4 if operand_tokens is not None and len(operand_tokens) > 1 and operand_tokens[1].startswith(("D", "ZR")) else 8
        return None if 1 <= int(token[1:]) <= upper else "CONSTANT_RANGE_UNSUPPORTED"
    if opcode == "BCD":
        # Q and FX5 share this word-sized form for D and mapped ZR/R.
        return None if (token.startswith("D") and _PLAIN.fullmatch(token)) or _STATIC_ZR.fullmatch(token) else "DEVICE_TYPE_UNSUPPORTED"
    if opcode in _PULSE_CONTACT:
        return None if (_PLAIN.fullmatch(token) and token.startswith("M")) or re.fullmatch(r"[XY][0-9A-F]+", token) else "DEVICE_TYPE_UNSUPPORTED"
    if opcode in _PULSE_COIL:
        return None if (_PLAIN.fullmatch(token) and token.startswith("M")) or re.fullmatch(r"Y[0-9A-F]+", token) else "IO_ROLE_UNSUPPORTED"
    if _STATIC_LB.fullmatch(token):
        return None if opcode in _CONTACT | _COIL and position == 0 else "DEVICE_TYPE_UNSUPPORTED"
    if _WORD_BIT_D.fullmatch(token) or _WORD_BIT_ZR.fullmatch(token):
        return None if opcode in _CONTACT | {"OUT", "SET", "RST"} and position == 0 else "DEVICE_TYPE_UNSUPPORTED"
    if token in _VERIFIED_SM:
        return None if opcode in _CONTACT and position == 0 else "DEVICE_TYPE_UNSUPPORTED"
    if _STATIC_T.fullmatch(token):
        return None if opcode in _CONTACT and position == 0 else "DEVICE_TYPE_UNSUPPORTED"
    if opcode in _BLOCK:
        if position == 2:
            if not _K.fullmatch(token):
                return "OPERAND_UNSUPPORTED"
            return None if 1 <= int(token[1:]) <= 65535 else "CONSTANT_RANGE_UNSUPPORTED"
        if position == 0 and opcode in {"FMOV", "FMOVP"} and (_K.fullmatch(token) or _H.fullmatch(token)):
            value = int(token[1:], 16 if token.startswith("H") else 10)
            lower, upper = ((0, 65535) if token.startswith("H") else (-32768, 32767))
            return None if lower <= value <= upper else "CONSTANT_RANGE_UNSUPPORTED"
        return None if ((_PLAIN.fullmatch(token) and token.startswith("D"))
                        or _STATIC_ZR.fullmatch(token) or _STATIC_W.fullmatch(token)) else "DEVICE_TYPE_UNSUPPORTED"
    if _STATIC_ZR.fullmatch(token):
        return None if opcode in _DATA else "DEVICE_TYPE_UNSUPPORTED"
    if _STATIC_W.fullmatch(token):
        return None if opcode in _DATA else "DEVICE_TYPE_UNSUPPORTED"
    match = _PLAIN.fullmatch(token)
    if match:
        family = match.group(1)
        if (family == "D" and opcode in _CONTACT | _COIL) or (family == "M" and opcode in _DATA):
            return "DEVICE_TYPE_UNSUPPORTED"
        return None
    if _IO.fullmatch(token):
        if opcode not in _CONTACT | _COIL or (opcode in _COIL and token.startswith(("X", "K"))):
            return "IO_ROLE_UNSUPPORTED"
        return None
    arithmetic_source_count = (1 if opcode in _TWO_OR_THREE_D_ARITHMETIC and operand_count == 2 else 2)
    constant_source = (opcode in _COMPARE or (opcode in _ARITHMETIC and position < arithmetic_source_count)
                       or (opcode in {"MOV", "DMOV", "MOVP", "DMOVP"} and position == 0))
    if (_K.fullmatch(token) or _H.fullmatch(token)) and constant_source:
        bits = 32 if opcode in _DWORD else 16
        value = int(token[1:], 16 if token.startswith("H") else 10)
        lower, upper = ((0, (1 << bits) - 1) if token.startswith("H")
                        else (-(1 << (bits - 1)), (1 << (bits - 1)) - 1))
        return None if lower <= value <= upper else "CONSTANT_RANGE_UNSUPPORTED"
    return "OPERAND_UNSUPPORTED"


def _word_projection(token: str) -> str:
    """Map only canonical, static Q ZR spelling to the same-numbered FX5 R."""
    match = _STATIC_ZR.fullmatch(token) or _WORD_BIT_ZR.fullmatch(token)
    if match is None:
        raise ReverseConversionError("WORD_DYNAMIC_OR_MODIFIED_UNSUPPORTED", token)
    digits = match.group(1)
    if str(int(digits)) != digits:
        raise ReverseConversionError("WORD_SOURCE_ALIAS", token)
    bit = f".{match.group(2)}" if match.re.groups == 2 else ""
    return f"R{digits}{bit}"


def _word_preflight(occurrences: list[dict[str, Any]], comments: dict[str, str],
                    word_settings: WordDeviceSettings | None) -> tuple[dict[str, str], dict[str, Any]]:
    """Bind all ZR references before any candidate byte is generated."""
    program = [(operand["raw_token"], f'{occurrence["provenance"]["source_locator"]}:operand:{position}')
               for occurrence in occurrences for position, operand in enumerate(occurrence["operands"])]
    comment = [(device, f"comment:{device}") for device in comments]
    zr_program = [(token, locator) for token, locator in program if token.startswith("ZR")]
    zr_comment = [(token, locator) for token, locator in comment if token.startswith("ZR")]
    if not zr_program and not zr_comment:
        return {}, {"state": "NO_ZR_REFERENCES" if word_settings else "NOT_REQUESTED",
                    **({"settings_sha256": word_settings.digest} if word_settings else {})}
    if word_settings is None or not word_settings.zr_to_r:
        raise ReverseConversionError("WORD_SETTINGS_REQUIRED")
    native_r = next(((token, locator) for token, locator in (*program, *comment)
                     if re.match(r"R[0-9]", token)), None)
    if native_r is not None:
        raise ReverseConversionError("Q_R_BANK_ALIAS_UNVERIFIED", native_r[0],
                                     source_locator=native_r[1], operand=native_r[0])
    mapped: dict[str, str] = {}
    aliases: dict[int, str] = {}
    for token, locator in (*zr_program, *zr_comment):
        try:
            target = _word_projection(token)
        except ReverseConversionError as error:
            raise ReverseConversionError(error.code, *error.context,
                                         source_locator=locator, operand=token) from error
        number = int((_STATIC_R.fullmatch(target) or _WORD_BIT_R.fullmatch(target)).group(1))
        source_base = token.split(".", 1)[0]
        previous = aliases.setdefault(number, source_base)
        if previous != source_base:
            raise ReverseConversionError("WORD_SOURCE_ALIAS", token,
                                         source_locator=locator, operand=token)
        if locator in mapped:
            raise ReverseConversionError("WORD_LOCATOR_COLLISION", locator,
                                         source_locator=locator, operand=token)
        mapped[locator] = target
    return mapped, {"state": "PREFLIGHT_OK", "settings_sha256": word_settings.digest,
                    "program_occurrences": len(zr_program), "comment_rows": len(zr_comment),
                    "mapped_unique_points": len(aliases), "target_r_capacity": "NOT_CHECKED",
                    "dynamic_zr": "HOLD", "gx_import": "NOT_RUN"}


def analyze_q_source(source: Path, *, cancel_check: Callable[[], bool] | None = None) -> QSourceAnalysis:
    """Read a bounded, no-follow Q snapshot without requiring target settings.

    The pinned reader receives a temporary copy of the exact captured bytes.
    No conversion artifact is published. An incomplete inspection is explicit.
    """
    source = Path(source)
    if cancel_check is not None and cancel_check():
        raise ReverseConversionError("Q_CONVERSION_CANCELLED")
    if source.suffix.lower() != ".gxw":
        return QSourceAnalysis(None, "UNVERIFIED", (), (), (),
                               (_q_diagnostic("Q_SOURCE_INVALID"),), (), ("source",))
    snapshot = None
    try:
        _reject_link_like_ancestors(source, "Q source")
        snapshot = _open_input_snapshot(source)
        if len(snapshot.body) > MAX_INPUT_BYTES:
            raise ReverseConversionError("Q_SOURCE_INVALID")
        ir_module, reader = _reader_modules()
        with tempfile.TemporaryDirectory(prefix=".p006-q-analysis-") as scratch:
            copy = Path(scratch) / "snapshot.gxw"
            copy.write_bytes(snapshot.body)
            diagnostics = []
            uninspected = ["target ceilings", "target mapping", "GX import validation"]
            comment_problem = None
            try:
                _preflight_comment_directory(copy, reader)
            except ReverseConversionError as error:
                if error.code != "Q_COMMENT_BINDING_UNVERIFIED":
                    raise
                comment_problem = error.code
            ir = _build_source_ir(ir_module, reader, copy)
            if ir.get("schema_name") != "plcman.gxw.reference-ir" or ir.get("schema_version") != 1:
                raise ReverseConversionError("Q_IR_SCHEMA_MISMATCH")
            if ir.get("input", {}).get("sha256") != snapshot.sha256:
                raise ReverseConversionError("Q_IR_INCOMPLETE_OR_CHANGED")
            streams = reader.load_all_substreams(str(copy))
            pous, comments, registry = reader.collect_pous(streams)
            if comments:
                pairs, warning = reader.device_comment_pairs(streams)
                excluded_labels = _unaddressed_module_labels(warning)
                if excluded_labels is not None:
                    diagnostics.append(_q_diagnostic("Q_MODULE_COMMENTS_UNRESOLVED", detail=str(excluded_labels)))
                    uninspected.append("module comment addresses")
                if ((warning is not None and excluded_labels is None)
                        or len(pairs) != len({device for device, _ in pairs})):
                    comment_problem = "Q_COMMENT_BINDING_UNVERIFIED"
                elif {device: value for device, value in pairs if value} != comments:
                    comment_problem = "Q_COMMENT_BINDING_MISMATCH"
                else:
                    comments = dict(pairs)
            elif streams:
                pairs, warning = reader.device_comment_pairs(streams)
                if warning is None:
                    comments = dict(pairs)
                elif warning != "NoCommentStream":
                    comment_problem = "Q_COMMENT_BINDING_UNVERIFIED"
            if comment_problem is not None:
                diagnostics.append(_q_diagnostic(comment_problem))
                uninspected.append("comment binding")
                comments = {}  # Never expose uncertain pairs as editable references.
            if not pous:
                raise ReverseConversionError("NO_POU")
            names = [name for name in registry[2] if name in pous] if registry else []
            names += sorted(set(pous) - set(names))
            rows_by_pou = {}
            control_digests: dict[str, str] = {}
            for name in names:
                if cancel_check is not None and cancel_check():
                    raise ReverseConversionError("Q_CONVERSION_CANCELLED")
                store, body, _score = pous[name]
                rows, unknown_instructions, unknown_devices = _reader_pou_rows(reader, body, comments)
                rows_by_pou[name] = (rows, str(store))
                control_digests[name] = hashlib.sha256(reader.program_section(body)).hexdigest()
                if unknown_instructions or unknown_devices:
                    diagnostics.append(_q_diagnostic("Q_READER_UNKNOWN", scope="pou", pou=name))
                    uninspected.append(f"{name}: unknown reader rows")
                if any(row[0] in {"__STMT__", "__NOTE__"} for row in rows):
                    try:
                        _validated_pou_text(rows, body, comments, reader, str(store), name,
                                            ir_module.split_operands)
                    except ReverseConversionError as error:
                        diagnostics.append(_q_diagnostic(error.code, scope="row", pou=name,
                                                         locator=error.source_locator, detail=str(error)))
            occurrences = ir.get("occurrences", [])
            if sum(sum(row[0] not in {"__STMT__", "__NOTE__"} for row in rows)
                   for rows, _ in rows_by_pou.values()) != len(occurrences):
                raise ReverseConversionError("Q_IR_ROW_MISMATCH")
            try:
                _control_preflight(rows_by_pou, ir_module.split_operands, occurrences, control_digests)
            except ReverseConversionError as error:
                diagnostics.append(_q_diagnostic(error.code, scope="row", pou=error.pou,
                                                 locator=error.source_locator, operand=error.operand,
                                                 detail=str(error)))
            references = []
            index = 0
            for name in names:
                if cancel_check is not None and cancel_check():
                    raise ReverseConversionError("Q_CONVERSION_CANCELLED")
                rows, store = rows_by_pou[name]
                for row in rows:
                    if row[0] in {"__STMT__", "__NOTE__"}:
                        continue  # Already diagnosed; reference IR excludes text rows.
                    occurrence = occurrences[index]
                    index += 1
                    locator = occurrence["provenance"]["source_locator"]
                    opcode = occurrence["record"]["opcode"]
                    operands = occurrence["operands"]
                    if (occurrence["pou"]["name"] != name or
                            occurrence["provenance"]["source_store"] != store or
                            opcode != row[0] or [item["raw_token"] for item in operands] != ir_module.split_operands(row[1])):
                        raise ReverseConversionError("Q_IR_CONTENT_MISMATCH")
                    if opcode not in _ARITY and opcode not in {"__STMT__", "__NOTE__"}:
                        diagnostics.append(_q_diagnostic("OPCODE_UNSUPPORTED", scope="row", pou=name,
                                                         locator=locator, detail=opcode))
                    elif opcode in _ARITY and not _valid_arity(
                            opcode, len(operands), tuple(item["raw_token"] for item in operands)):
                        diagnostics.append(_q_diagnostic("ARITY_UNSUPPORTED", scope="row", pou=name,
                                                         locator=locator, detail=opcode))
                    operand_tokens = tuple(item["raw_token"] for item in operands)
                    for position, operand in enumerate(operands):
                        token = operand["raw_token"]
                        status = operand.get("status", "unknown")
                        references.append(QProgramReference(name, locator, position, token, opcode, status))
                        if re.fullmatch(r"(?:K[1-8])?[XY][0-9A-Fa-f]+", token):
                            try:
                                source_span(token, Profile.Q)
                            except MappingError as error:
                                diagnostics.append(_q_diagnostic("XY_SOURCE_RANGE_INVALID", scope="row",
                                                                 pou=name, locator=f"{locator}:operand:{position}",
                                                                 operand=token, detail=str(error)))
                        if status != "decoded" or operand.get("access") == "unknown":
                            diagnostics.append(_q_diagnostic("OPERAND_UNCLASSIFIED", scope="row", pou=name,
                                                             locator=locator, operand=token))
                        elif (_STATIC_Z.fullmatch(token)
                              and (expected := _static_z_expected_access(opcode, position, len(operands))) is not None
                              and operand.get("access") != expected):
                            diagnostics.append(_q_diagnostic("IO_ROLE_UNSUPPORTED", scope="row", pou=name,
                                                             locator=locator, operand=token))
                        elif (opcode in _EXPECTED_ACCESS and _valid_arity(opcode, len(operands), operand_tokens)
                              and operand.get("access") != _EXPECTED_ACCESS[opcode][position]):
                            diagnostics.append(_q_diagnostic("IO_ROLE_UNSUPPORTED", scope="row", pou=name,
                                                             locator=locator, operand=token))
                        elif opcode in _ARITY and _valid_arity(opcode, len(operands), operand_tokens):
                            code = _source_operand_code(token, opcode, position,
                                                        operand_count=len(operands),
                                                        operand_tokens=operand_tokens)
                            if code is not None:
                                diagnostics.append(_q_diagnostic(code, scope="row", pou=name,
                                                                 locator=locator, operand=token))
            if ir.get("analysis", {}).get("state") != "COMPLETE":
                diagnostics.append(_q_diagnostic("Q_IR_INCOMPLETE_OR_CHANGED"))
                uninspected.append("incomplete reference IR")
            for device in comments:
                if re.fullmatch(r"[XY][0-9A-Fa-f]+", device):
                    try:
                        source_span(device, Profile.Q)
                    except MappingError as error:
                        diagnostics.append(_q_diagnostic("XY_SOURCE_RANGE_INVALID", scope="comment",
                                                         locator=f"comment:{device}", operand=device,
                                                         detail=str(error)))
            if any(ref.raw_token.startswith("ZR") for ref in references) or any(device.startswith("ZR") for device in comments):
                uninspected.append("ZR target mapping and R device capacity")
            if any(_STATIC_Z.fullmatch(ref.raw_token) for ref in references):
                uninspected.append("Z target allocation and indexed use")
            snapshot.verify()
            return QSourceAnalysis(snapshot.sha256, "UNVERIFIED", tuple(names), tuple(references),
                                   tuple(QCommentReference(device, f"comment:{device}", value)
                                         for device, value in sorted(comments.items())),
                                   tuple(diagnostics), ("source bytes", "POU registry", "program rows",
                                                       "reference IR") + (() if comment_problem else ("comment binding",)),
                                   tuple(uninspected), _cpu_observation(copy, reader))
    except (OSError, RuntimeError, ReverseConversionError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, ReverseConversionError) and error.code == "Q_CONVERSION_CANCELLED":
            raise
        code = error.code if isinstance(error, ReverseConversionError) else "Q_SOURCE_INVALID"
        return QSourceAnalysis(snapshot.sha256 if snapshot is not None else None, "UNVERIFIED", (), (), (),
                               (_q_diagnostic(code, detail=type(error).__name__),),
                               ("source bytes",) if snapshot is not None else (),
                               ("POU registry", "program rows", "comment binding", "reference IR",
                                "target ceilings", "target mapping", "GX import validation"))
    finally:
        if snapshot is not None:
            snapshot.close()


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def plan_q_to_fx5(source: Path, *, project_identifier: str | None = None,
                  max_m: int | None = None, max_d: int | None = None,
                  settings: MappingSettings | None = None, q_io_bounds: Bounds | None = None,
                  fx5_io_bounds: Bounds | None = None,
                  word_settings: WordDeviceSettings | None = None,
                  cancel_check: Callable[[], bool] | None = None) -> QPlan:
    """Expose source-first requirements, then the existing byte-identical plan."""
    analysis = analyze_q_source(source, cancel_check=cancel_check)
    if analysis.source_sha256 is None:
        return QPlan(analysis, (), analysis.diagnostics, False, None, None)
    required = []
    diagnostics = list(analysis.diagnostics)
    if project_identifier is None:
        required.append("project_identifier")
        diagnostics.append(_q_diagnostic("PROJECT_IDENTIFIER_REQUIRED"))
    elif not project_identifier or any(ch in project_identifier for ch in "\r\n\t\x00"):
        diagnostics.append(_q_diagnostic("PROJECT_IDENTIFIER_INVALID"))
    has_xy = any(ref.raw_token.startswith(("X", "Y", "K1X", "K2X", "K3X", "K4X", "K5X", "K6X", "K7X", "K8X",
                                          "K1Y", "K2Y", "K3Y", "K4Y", "K5Y", "K6Y", "K7Y", "K8Y"))
                 for ref in analysis.program_references) or any(ref.device.startswith(("X", "Y"))
                                                         for ref in analysis.comment_references)
    if has_xy and settings is None:
        required.append("settings")
        diagnostics.append(_q_diagnostic("XY_SETTINGS_REQUIRED"))
    has_zr = any(ref.raw_token.startswith("ZR") for ref in analysis.program_references) or any(
        ref.device.startswith("ZR") for ref in analysis.comment_references)
    if has_zr and (word_settings is None or not word_settings.zr_to_r):
        required.append("word_settings")
        diagnostics.append(_q_diagnostic("WORD_SETTINGS_REQUIRED"))
    if required or diagnostics:
        return QPlan(analysis, tuple(required), tuple(diagnostics), False, None, None)
    try:
        outputs, report = _plan(Path(source), project_identifier=project_identifier, max_m=max_m, max_d=max_d,
                                settings=settings, q_io_bounds=q_io_bounds, fx5_io_bounds=fx5_io_bounds,
                                word_settings=word_settings,
                                cancel_check=cancel_check)
    except (ReverseConversionError, MappingError, OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, ReverseConversionError) and error.code == "Q_CONVERSION_CANCELLED":
            raise
        structured = isinstance(error, ReverseConversionError)
        locator = error.source_locator if structured else getattr(error, "locator", None)
        operand = error.operand if structured else getattr(error, "operand", None)
        diagnostic = _q_diagnostic(error.code if structured else "Q_PLAN_BLOCKED",
                                   scope=("row" if locator else
                                          "pou" if structured and error.pou else "project"),
                                   pou=error.pou if structured else None,
                                   locator=locator,
                                   operand=operand,
                                   detail=str(error))
        return QPlan(analysis, (), (diagnostic,), False, None, None)
    if report["source_sha256"] != analysis.source_sha256:
        diagnostic = _q_diagnostic("Q_SOURCE_CHANGED")
        return QPlan(analysis, (), (diagnostic,), False, None, None)
    return QPlan(analysis, (), (), True, _freeze(outputs), _freeze(report))


def _reader_modules():
    wheel = Path(__file__).with_name("resources") / "q-reader" / READER_FILE
    if not wheel.is_file() or wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != READER_SHA256:
        raise ReverseConversionError("Q_READER_PIN_MISMATCH")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as error:
        raise ReverseConversionError("Q_READER_WHEEL_INVALID") from error
    if not {"gxw_reference_ir.py", "gxw_ladder_reader.py", "gxw_pou_devmap.py"}.issubset(names):
        raise ReverseConversionError("Q_READER_MODULES_MISSING")
    # The wheel is data in the portable bundle. Python's ZIP importer reads it
    # directly, and the pin is checked before any module from it is imported.
    path = str(wheel)
    if path not in sys.path:
        sys.path.insert(0, path)
    import gxw_reference_ir  # type: ignore[import-not-found]
    import gxw_ladder_reader  # type: ignore[import-not-found]
    for module in (gxw_reference_ir, gxw_ladder_reader):
        if not str(getattr(module, "__file__", "")).startswith(path):
            raise ReverseConversionError("Q_READER_MODULE_ORIGIN_MISMATCH")
    return gxw_reference_ir, gxw_ladder_reader


def _csv(rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerows(rows)
    return b"\xff\xfe" + output.getvalue().encode("utf-16le")


def _safe_leaf(value: str) -> str:
    if not value or value in {".", ".."} or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", value) is None:
        raise ReverseConversionError("UNSAFE_POU_NAME", value, pou=value)
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                          *(f"LPT{i}" for i in range(1, 10))}:
        raise ReverseConversionError("RESERVED_POU_NAME", value, pou=value)
    return value


def _count_first_pass_comment_directory(data: bytes, codes: set[int], stop_after: int) -> int:
    """Count reader directory runs without allocating one string per device."""
    total = 0
    index = 0
    last = len(data) - 10
    while index <= last:
        if (data[index] in codes and data[index + 1] == 0 and data[index + 4] == 0
                and data[index + 5] == 0):
            count = int.from_bytes(data[index + 6:index + 10], "little")
            if 1 <= count <= 65535:
                total += count
                if total > stop_after:
                    return total
                index += 10
                continue
        index += 1
    return total


def _preflight_comment_directory(source: Path, reader: Any) -> None:
    """Bound reader directory work before expanding source-bound comment pairs."""
    streams = reader.load_all_substreams(str(source))
    structured = getattr(reader, "_structured_comment_section", None)
    if structured is not None:
        try:
            if any(structured(body) is not None for body in streams.values()):
                reader.device_comment_pairs(streams)
                return
        except ValueError as error:
            code = ("Q_COMMENT_DIRECTORY_OVERSIZE" if "budget" in str(error)
                    else "Q_COMMENT_BINDING_UNVERIFIED")
            raise ReverseConversionError(code) from error
    best_comments = 0
    best_directory = 0
    best_body: bytes | None = None
    total_directory = 0
    codes = set(reader.CMT_DIR_DEVICE)
    for body in streams.values():
        start, comments = reader._longest_comment_run(body)
        count = len(comments)
        if count > MAX_COMMENT_ENTRIES:
            raise ReverseConversionError("Q_COMMENT_BLOCK_OVERSIZE")
        if count < 2:
            continue
        # Legacy pools use the same pre-text boundary as the pinned reader.
        directory_count = _count_first_pass_comment_directory(
            (reader._comment_directory_region(body) if hasattr(reader, "_comment_directory_region") else body[:start]),
            codes, MAX_COMMENT_ENTRIES - total_directory)
        total_directory += directory_count
        if total_directory > MAX_COMMENT_ENTRIES:
            raise ReverseConversionError("Q_COMMENT_DIRECTORY_OVERSIZE")
        if directory_count and count > best_comments:
            best_comments = count
            best_directory = directory_count
            best_body = body
    if best_body is not None and best_directory != best_comments:
        # Q GXW also has high-address and U/G records. The pinned reader
        # limits unified expansion to MAX_COMMENT_ENTRIES and accepts only a
        # unique directory matching the comment block length.
        try:
            unified = reader.unified_directory(best_body)
        except ValueError as error:
            raise ReverseConversionError("Q_COMMENT_DIRECTORY_OVERSIZE") from error
        if len(unified) != best_comments or len(set(unified)) != len(unified):
            verify_split = getattr(reader, "verified_split_comment_chains", None)
            if verify_split is None or verify_split(best_body, unified) is None:
                raise ReverseConversionError("Q_COMMENT_BINDING_UNVERIFIED")


def _validate_operand(token: str, opcode: str, position: int, *, max_m: int | None, max_d: int | None,
                      operand_count: int | None = None,
                      operand_tokens: tuple[str, ...] | None = None) -> None:
    # Legacy bounds arguments have no effect on text conversion.
    code = _source_operand_code(token, opcode, position, operand_count=operand_count,
                                operand_tokens=operand_tokens)
    if code is not None:
        raise ReverseConversionError(code, token)


def _target_word(token: str) -> tuple[str, int]:
    if token.startswith("W") and _STATIC_W.fullmatch(token):
        return "W", int(token[1:], 16)
    match = re.fullmatch(r"(D|R)([0-9]+)", token)
    if match is None:
        raise ReverseConversionError("DEVICE_TYPE_UNSUPPORTED", token)
    return match.group(1), int(match.group(2))


def _validate_block_span(opcode: str, operands: list[str], *, max_d: int | None) -> None:
    """Legacy hook; operands/counts are checked without device capacity limits."""
    return None


def _validate_counted_span(opcode: str, operands: list[str], *,
                           max_m: int | None, max_d: int | None) -> None:
    """Validate every addressed word or bit of the narrow counted forms."""
    count = int(operands[2][1:])
    source_token = operands[0]
    target_token = operands[1]
    if opcode == "DECO":
        if target_token.startswith(("D", "R")) and _target_word(source_token) == _target_word(target_token):
            raise ReverseConversionError("SOURCE_DESTINATION_OVERLAP_UNVERIFIED", target_token)
        return
    source_width = count if opcode == "BTOW" else 2 * count
    target_width = (count + 1) // 2 if opcode == "BTOW" else 4
    source_family, source_first = _target_word(source_token)
    target_family, target_first = _target_word(target_token)
    if (source_family == target_family and source_first <= target_first + target_width - 1
            and target_first <= source_first + source_width - 1):
        raise ReverseConversionError("SOURCE_DESTINATION_OVERLAP_UNVERIFIED", target_token)


def _render_occurrence(occurrence: dict[str, Any], *, max_m: int | None, max_d: int | None,
                       mapped: dict[str, str]) -> tuple[str, list[str]]:
    opcode = occurrence["record"]["opcode"]
    locator = occurrence["provenance"]["source_locator"]
    pou = occurrence.get("pou", {}).get("name")
    if opcode not in _ARITY:
        raise ReverseConversionError("OPCODE_UNSUPPORTED", locator, opcode,
                                     pou=pou, source_locator=locator)
    operands = occurrence["operands"]
    operand_tokens = tuple(operand["raw_token"] for operand in operands)
    if not _valid_arity(opcode, len(operands), operand_tokens):
        raise ReverseConversionError("ARITY_UNSUPPORTED", locator, opcode,
                                     pou=pou, source_locator=locator)
    result: list[str] = []
    for position, operand in enumerate(operands):
        token = operand["raw_token"]
        if operand["status"] != "decoded" or operand["access"] == "unknown":
            raise ReverseConversionError("OPERAND_UNCLASSIFIED", locator, token,
                                         pou=pou, source_locator=locator, operand=token)
        if opcode in _EXPECTED_ACCESS and operand["access"] != _EXPECTED_ACCESS[opcode][position]:
            raise ReverseConversionError("IO_ROLE_UNSUPPORTED", locator, token,
                                         pou=pou, source_locator=locator, operand=token)
        z_access = _static_z_expected_access(opcode, position, len(operands)) if _STATIC_Z.fullmatch(token) else None
        if z_access is not None and operand["access"] != z_access:
            raise ReverseConversionError("IO_ROLE_UNSUPPORTED", locator, token,
                                         pou=pou, source_locator=locator, operand=token)
        try:
            _validate_operand(token, opcode, position, max_m=max_m, max_d=max_d,
                              operand_count=len(operands), operand_tokens=operand_tokens)
        except ReverseConversionError as error:
            raise ReverseConversionError(error.code, *error.context, pou=pou,
                                         source_locator=locator, operand=token) from error
        target = mapped.get(f"{locator}:operand:{position}", token)
        if token.startswith("ZR"):
            if target == token:
                raise ReverseConversionError("WORD_SETTINGS_REQUIRED", locator, token,
                                             pou=pou, source_locator=locator, operand=token)
            if target != _word_projection(token):
                raise ReverseConversionError("WORD_MAPPING_INVALID", locator, token,
                                             pou=pou, source_locator=locator, operand=token)
        result.append(target)
    if opcode in _BLOCK:
        try:
            _validate_block_span(opcode, result, max_d=max_d)
        except ReverseConversionError as error:
            raise ReverseConversionError(error.code, *error.context, pou=pou,
                                         source_locator=locator, operand=error.context[0]) from error
    if opcode in {"DECO", "BTOW", "DMAX", "DMIN"}:
        try:
            _validate_counted_span(opcode, result, max_m=max_m, max_d=max_d)
        except ReverseConversionError as error:
            raise ReverseConversionError(error.code, *error.context, pou=pou,
                                         source_locator=locator, operand=error.context[0]) from error
    return CLOCK_ALIASES.get(opcode, opcode), result


def _validated_pou_text(rows: list[tuple[str, str, str]], body: bytes, comments: dict[str, str],
                        reader: Any, store: str, pou: str,
                        split_operands: Callable[[str], list[str]]) -> dict[int, tuple[str, str]]:
    """Bind printable text to its row and an immediately preceding instruction."""
    note_count = sum(row[0] == "__NOTE__" for row in rows)
    framed_text = getattr(reader, "POU_TEXT_ENCODING_API", 0) == 1
    typed_notes = None
    if note_count:
        method = getattr(reader, "typed_note_records", None)
        if method is None:
            raise ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", pou, pou=pou)
        try:
            typed_notes = (method(body, comments, framed_types=True, text_encoding="auto")
                           if framed_text else method(body, comments, ascii_types=True))
        except (TypeError, ValueError) as error:
            raise ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", pou, pou=pou) from error
        if not isinstance(typed_notes, (list, tuple)) or len(typed_notes) != note_count:
            raise ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", pou, pou=pou)
    notes = iter(typed_notes or ())
    result: dict[int, tuple[str, str]] = {}
    last_instruction: tuple[str, list[str]] | None = None
    previous_kind: str | None = None
    ended = False
    for index, (opcode, raw, _comment) in enumerate(rows):
        locator = f"_hdb/{store}:row:{index}"
        if opcode not in {"__STMT__", "__NOTE__"}:
            last_instruction = (opcode, split_operands(raw))
            previous_kind = "instruction"
            if opcode == "END":
                ended = True
            continue
        code = "POU_TEXT_UNSUPPORTED" if opcode == "__STMT__" else "Q_NOTE_TYPE_UNVERIFIED"
        if (ended or not isinstance(raw, str) or not raw or raw.startswith("*")
                or any(not (ord(char) >= 0x20 and ord(char) != 0x7F if framed_text else
                            0x20 <= ord(char) <= 0x7E
                            or (opcode == "__STMT__" and char == "\u2014")) for char in raw)):
            raise ReverseConversionError(code, pou, pou=pou, source_locator=locator)
        if opcode == "__STMT__":
            result[index] = ("statement", raw)
            previous_kind = "statement"
            continue
        note = next(notes)
        attachment = note.get("attachment") if isinstance(note, dict) else None
        encoded_length = len(raw)
        if framed_text:
            try:
                if not isinstance(note, dict) or not isinstance(note.get("raw_text_hex"), str):
                    raise ValueError("raw Note bytes missing")
                encoded = bytes.fromhex(note["raw_text_hex"])
                if (reader._decode_pou_text(encoded, "auto") != raw
                        or type(note.get("encoded_length")) is not int
                        or note["encoded_length"] != len(encoded) or not 1 <= len(encoded) <= 251):
                    raise ValueError("raw Note byte binding differs")
                encoded_length = len(encoded)
            except (AttributeError, TypeError, ValueError, UnicodeError) as error:
                raise ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", pou, pou=pou,
                                             source_locator=locator) from error
        expected_raw_type = (1 if note.get("subtype") == "s" else (encoded_length + 5) // 2
                             ) if isinstance(note, dict) else None
        if ((not framed_text and len(raw) > 32)
                or previous_kind != "instruction" or last_instruction is None
                or (not framed_text and last_instruction[0] != "MOV") or not isinstance(note, dict)
                or note.get("text") != raw or note.get("subtype") not in {"i", "s"}
                or re.fullmatch(r"0x[0-9a-f]{2}", str(note.get("raw_type"))) is None
                or int(note["raw_type"], 16) != expected_raw_type
                or not isinstance(attachment, dict)
                or attachment.get("instruction") != last_instruction[0]
                or attachment.get("operands") != last_instruction[1]):
            raise ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", pou, pou=pou, source_locator=locator)
        result[index] = ("note", ("*" if note["subtype"] == "s" else "") + raw)
        previous_kind = "note"
    return result


def _source_rows(source: Path, reader: Any, *,
                 split_operands: Callable[[str], list[str]] | None = None,
                 text_by_pou: dict[str, dict[int, tuple[str, str]]] | None = None,
                 digest_by_pou: dict[str, str] | None = None
                 ) -> tuple[dict[str, tuple[list[tuple[str, str, str]], str]], dict[str, str]]:
    streams = reader.load_all_substreams(str(source))
    pous, comments, registry = reader.collect_pous(streams)
    if comments:
        pairs, warning = reader.device_comment_pairs(streams)
        excluded_labels = _unaddressed_module_labels(warning)
        if excluded_labels is not None:
            raise ReverseConversionError("Q_MODULE_COMMENTS_UNRESOLVED", str(excluded_labels))
        if warning is not None or len(pairs) != len({device for device, _text in pairs}):
            raise ReverseConversionError("Q_COMMENT_BINDING_UNVERIFIED")
        if {device: text for device, text in pairs if text} != comments:
            raise ReverseConversionError("Q_COMMENT_BINDING_MISMATCH")
        comments = dict(pairs)
    elif streams:
        pairs, warning = reader.device_comment_pairs(streams)
        if warning is None:
            comments = dict(pairs)
        elif warning != "NoCommentStream":
            raise ReverseConversionError("Q_COMMENT_BINDING_UNVERIFIED")
    if not pous:
        raise ReverseConversionError("NO_POU")
    names = [name for name in registry[2] if name in pous] if registry else []
    names += sorted(set(pous) - set(names))
    result = {}
    for name in names:
        store, body, _score = pous[name]
        rows, unknown_instructions, unknown_devices = _reader_pou_rows(reader, body, comments)
        if unknown_instructions or unknown_devices:
            raise ReverseConversionError("Q_READER_UNKNOWN", name, pou=name)
        if any(row[0] in {"__STMT__", "__NOTE__"} for row in rows):
            if split_operands is None:
                raise ReverseConversionError("Q_TEXT_BINDING_UNVERIFIED", name, pou=name)
            validated = _validated_pou_text(rows, body, comments, reader, str(store), name, split_operands)
            if text_by_pou is not None:
                text_by_pou[name] = validated
        if digest_by_pou is not None:
            digest_by_pou[name] = hashlib.sha256(reader.program_section(body)).hexdigest()
        result[name] = (rows, str(store))
    return result, comments


def _control_preflight(rows_by_pou: dict[str, tuple[list[tuple[str, str, str]], str]],
                       split_operands: Callable[[str], list[str]],
                       occurrences: list[dict[str, Any]],
                       digest_by_pou: dict[str, str] | None = None) -> dict[str, int]:
    """Validate only the narrow, paired Q control forms before rendering."""
    totals = {"for": 0, "next": 0, "mc": 0, "mcr": 0}
    if not any(row[0] in {"FOR", "NEXT", "MC", "MCR"}
               for rows, _store in rows_by_pou.values() for row in rows):
        return totals
    mc_targets: dict[int, str] = {}
    occurrence_index = 0
    boundary = {"END", "FEND", "RET", "SRET", "GOEND", "I", "IRET"}
    for name, (rows, store) in rows_by_pou.items():
        has_control = any(row[0] in {"FOR", "NEXT", "MC", "MCR"} for row in rows)
        for_stack: list[str] = []
        mc_stack: list[tuple[int, str]] = []
        previous_opcode: str | None = None
        for index, (opcode, raw, _comment) in enumerate(rows):
            if opcode in {"__STMT__", "__NOTE__"}:
                continue
            locator = f"_hdb/{store}:row:{index}"
            if occurrence_index >= len(occurrences):
                raise ReverseConversionError("Q_IR_ROW_MISMATCH", pou=name, source_locator=locator)
            occurrence = occurrences[occurrence_index]
            occurrence_index += 1
            if has_control:
                provenance = occurrence["provenance"]
                record = occurrence["record"]
                if (occurrence["pou"]["name"] != name or str(provenance["source_store"]) != store
                        or provenance.get("source_locator") != locator
                        or (digest_by_pou is not None
                            and provenance.get("source_digest") != digest_by_pou[name])
                        or record.get("sequence") != index or record["opcode"] != opcode
                        or [item["raw_token"] for item in occurrence["operands"]] != split_operands(raw)):
                    raise ReverseConversionError("Q_IR_CONTENT_MISMATCH", pou=name, source_locator=locator)
            if opcode in boundary and (for_stack or mc_stack):
                raise ReverseConversionError("CONTROL_STRUCTURE_UNVERIFIED", opcode, pou=name,
                                             source_locator=locator)
            if opcode not in {"FOR", "NEXT", "MC", "MCR"}:
                previous_opcode = opcode
                continue
            operands = split_operands(raw)
            if not _valid_arity(opcode, len(operands)):
                raise ReverseConversionError("ARITY_UNSUPPORTED", opcode, pou=name,
                                             source_locator=locator)
            for position, token in enumerate(operands):
                code = _source_operand_code(token, opcode, position)
                if code is not None:
                    raise ReverseConversionError(code, token, pou=name,
                                                 source_locator=locator, operand=token)
            if opcode == "FOR":
                if mc_stack or len(for_stack) >= 16 or previous_opcode in _OPEN_CONTACT:
                    raise ReverseConversionError("CONTROL_STRUCTURE_UNVERIFIED", opcode, pou=name,
                                                 source_locator=locator)
                for_stack.append(locator)
                totals["for"] += 1
            elif opcode == "NEXT":
                if not for_stack or mc_stack or previous_opcode in _OPEN_CONTACT:
                    raise ReverseConversionError("CONTROL_STRUCTURE_UNVERIFIED", opcode, pou=name,
                                                 source_locator=locator)
                for_stack.pop()
                totals["next"] += 1
            elif opcode == "MC":
                level = int(operands[0][1:])
                next_opcode = next((next_row[0] for next_row in rows[index + 1:]
                                    if next_row[0] not in {"__STMT__", "__NOTE__"}), None)
                if (for_stack or level != (mc_stack[-1][0] + 1 if mc_stack else 0)
                        or next_opcode not in {"LD", "LDI"}):
                    raise ReverseConversionError("CONTROL_STRUCTURE_UNVERIFIED", opcode, pou=name,
                                                 source_locator=locator)
                target = operands[1]
                number = int(target[1:])
                if number in mc_targets:
                    raise ReverseConversionError("CONTROL_COIL_ALIAS_UNVERIFIED", target, pou=name,
                                                 source_locator=locator, operand=target)
                mc_targets[number] = locator
                mc_stack.append((level, locator))
                totals["mc"] += 1
            else:
                level = int(operands[0][1:])
                if (for_stack or not mc_stack or level != mc_stack[-1][0]
                        or previous_opcode in _OPEN_CONTACT):
                    raise ReverseConversionError("CONTROL_STRUCTURE_UNVERIFIED", opcode, pou=name,
                                                 source_locator=locator)
                mc_stack.pop()
                totals["mcr"] += 1
            previous_opcode = opcode
        if for_stack or mc_stack:
            raise ReverseConversionError("CONTROL_STRUCTURE_UNVERIFIED", name, pou=name,
                                         source_locator=(for_stack[-1] if for_stack else mc_stack[-1][1]))
    for occurrence in occurrences:
        if occurrence["record"]["opcode"] == "MC":
            continue
        operands = occurrence["operands"]
        for position, operand in enumerate(operands):
            token = operand["raw_token"]
            match = _PLAIN.fullmatch(token)
            if not match or match.group(1) != "M" or operand.get("access") not in {"write", "both"}:
                continue
            first = int(match.group(2))
            last = first
            if occurrence["record"]["opcode"] == "DECO" and position == 1 and len(operands) == 3:
                count = operands[2]["raw_token"]
                if _K.fullmatch(count) and 1 <= int(count[1:]) <= 8:
                    last = first + (1 << int(count[1:])) - 1
            if any(first <= number <= last for number in mc_targets):
                raise ReverseConversionError("CONTROL_COIL_ALIAS_UNVERIFIED", token,
                                             pou=occurrence["pou"]["name"],
                                             source_locator=occurrence["provenance"]["source_locator"],
                                             operand=token)
    return totals


def _project_comments(comments: dict[str, str], mapped: dict[str, str], *,
                      max_m: int | None, max_d: int | None) -> list[list[str]]:
    """Preserve source-bound typed comments without a hardware capacity gate."""
    rows = []
    seen: set[tuple[Any, ...]] = set()
    for source_device, value in sorted(comments.items()):
        target = mapped.get(f"comment:{source_device}", source_device)
        module = re.fullmatch(r"U([0-9A-F]+)\\G([0-9]+)(?:\.([0-9A-F]))?", target)
        plain = re.fullmatch(r"(SM|SD|ST|ZR|M|D|L|R|Z|T|C|F|N|P)([0-9]+)(?:\.([0-9A-F]))?", target)
        hexadecimal = re.fullmatch(r"(SB|SW|B|W|U)([0-9A-F]+)(?:\.([0-9A-F]))?", target)
        xy = re.fullmatch(r"([XY])([0-7]+)", target)
        if module:
            unit, digits, bit = module.groups()
            number = int(digits)
            alias = str(number) != digits
            identity = "U\\G", int(unit, 16), number, bit
        elif plain:
            family, digits, bit = plain.groups()
            number = int(digits)
            alias = str(number) != digits
            identity = family, number, bit
        elif hexadecimal:
            family, digits, bit = hexadecimal.groups()
            number = int(digits, 16)
            canonical = format(number, "X")
            alias = digits != canonical and not (canonical[0] in "ABCDEF" and digits == "0" + canonical)
            identity = family, number, bit
        elif xy and f"comment:{source_device}" in mapped:
            family, digits = xy.groups()
            number = int(digits, 8)
            alias = format(number, "o") != digits
            identity = family, number, None
        else:
            raise ReverseConversionError("FX5_COMMENT_DEVICE_UNSUPPORTED", source_device,
                                         source_locator=f"comment:{source_device}", operand=source_device)
        if alias:
            raise ReverseConversionError("FX5_COMMENT_DEVICE_ALIAS", source_device,
                                         source_locator=f"comment:{source_device}", operand=source_device)
        if identity in seen:
            raise ReverseConversionError("FX5_COMMENT_TARGET_ALIAS", source_device,
                                         source_locator=f"comment:{source_device}", operand=source_device)
        seen.add(identity)
        if not isinstance(value, str) or any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in value):
            raise ReverseConversionError("FX5_COMMENT_TEXT_UNSUPPORTED", source_device,
                                         source_locator=f"comment:{source_device}", operand=source_device)
        rows.append([target, value])
    return rows


def _plan(source: Path, *, project_identifier: str, max_m: int | None, max_d: int | None,
          settings: MappingSettings | None, q_io_bounds: Bounds | None,
          fx5_io_bounds: Bounds | None,
          word_settings: WordDeviceSettings | None = None,
          cancel_check: Callable[[], bool] | None = None) -> tuple[dict[str, bytes], dict[str, Any]]:
    if settings and settings.module_rules:
        raise ReverseConversionError("MODULE_MAPPING_TEXT_REQUIRED")
    if cancel_check is not None and cancel_check():
        raise ReverseConversionError("Q_CONVERSION_CANCELLED")
    if source.suffix.lower() != ".gxw" or not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_INPUT_BYTES:
        raise ReverseConversionError("Q_SOURCE_INVALID")
    if not project_identifier or any(ch in project_identifier for ch in "\r\n\t\x00"):
        raise ReverseConversionError("PROJECT_IDENTIFIER_INVALID")
    if any(value is not None and (type(value) is not int or value < 0) for value in (max_m, max_d)):
        raise ReverseConversionError("FX5_DEVICE_BOUNDS_REQUIRED")
    reported_max_m, reported_max_d = max_m, max_d
    max_m = max_d = None
    snapshot = source.read_bytes()
    source_sha = hashlib.sha256(snapshot).hexdigest()
    ir_module, reader = _reader_modules()
    _preflight_comment_directory(source, reader)
    ir = _build_source_ir(ir_module, reader, source)
    if ir.get("schema_name") != "plcman.gxw.reference-ir" or ir.get("schema_version") != 1:
        raise ReverseConversionError("Q_IR_SCHEMA_MISMATCH")
    if ir["input"]["sha256"] != source_sha or ir["analysis"]["state"] != "COMPLETE":
        raise ReverseConversionError("Q_IR_INCOMPLETE_OR_CHANGED")
    text_by_pou: dict[str, dict[int, tuple[str, str]]] = {}
    digest_by_pou: dict[str, str] = {}
    rows_by_pou, comments = _source_rows(source, reader, split_operands=ir_module.split_operands,
                                         text_by_pou=text_by_pou, digest_by_pou=digest_by_pou)
    cpu_observation = _cpu_observation(source, reader)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
        raise ReverseConversionError("Q_SOURCE_CHANGED")
    occurrences = ir["occurrences"]
    if sum(sum(row[0] not in {"__STMT__", "__NOTE__"} for row in rows)
           for rows, _ in rows_by_pou.values()) != len(occurrences):
        raise ReverseConversionError("Q_IR_ROW_MISMATCH")
    control_report = _control_preflight(rows_by_pou, ir_module.split_operands, occurrences, digest_by_pou)
    mapped: dict[str, str] = {}
    mapping_report: dict[str, Any] = {"state": "NOT_REQUESTED"}
    io_refs = [Reference(operand["raw_token"], occurrence["provenance"]["source_locator"] + f":operand:{position}")
               for occurrence in occurrences for position, operand in enumerate(occurrence["operands"])
               if operand["raw_token"].startswith(("X", "Y", "K1X", "K2X", "K3X", "K4X", "K5X", "K6X", "K7X", "K8X",
                                                    "K1Y", "K2Y", "K3Y", "K4Y", "K5Y", "K6Y", "K7Y", "K8Y"))]
    comment_refs = [Reference(device, f"comment:{device}") for device in comments if device.startswith(("X", "Y"))]
    if io_refs or comment_refs:
        if settings is None:
            raise ReverseConversionError("XY_SETTINGS_REQUIRED")
        if settings.source is not Profile.Q or settings.target is not Profile.FX5U:
            raise ReverseConversionError("XY_DIRECTION_MISMATCH")
        result = preview(settings, source_bounds=q_io_bounds or Bounds(), target_bounds=fx5_io_bounds or Bounds(),
                         input_sha256=source_sha, program=io_refs, comments=comment_refs)
        result.require_publishable(settings=settings, input_sha256=source_sha,
                                   source_bounds=q_io_bounds or Bounds(), target_bounds=fx5_io_bounds or Bounds())
        mapped = {projection.locator: projection.target for projection in result.projections}
        mapping_report = {"state": "PREFLIGHT_OK", "settings_sha256": settings.digest,
                          "program_occurrences": result.program_occurrences, "comment_rows": result.comment_rows,
                          "mapped_unique_points": result.mapped_unique_points,
                          "unmapped_unique_points": result.unmapped_unique_points}
    elif settings is not None:
        settings.mapper(source_bounds=q_io_bounds or Bounds(), target_bounds=fx5_io_bounds or Bounds())
        mapping_report = {"state": "NO_XY_REFERENCES", "settings_sha256": settings.digest}
    word_mapped, word_mapping_report = _word_preflight(occurrences, comments, word_settings)
    if set(mapped).intersection(word_mapped):
        raise ReverseConversionError("MAPPING_LOCATOR_COLLISION")
    mapped.update(word_mapped)
    outputs: dict[str, bytes] = {}
    ordered_pous = list(rows_by_pou)
    leaves = [_safe_leaf(name) + ".csv" for name in ordered_pous]
    if len({leaf.casefold() for leaf in [*leaves, "comment.csv", "COMMENT.review.csv", "report.json"]}) != len(leaves) + 3:
        raise ReverseConversionError("OUTPUT_NAME_COLLISION")
    occurrence_index = 0
    statement_count = 0
    note_count = 0
    for name in ordered_pous:
        if cancel_check is not None and cancel_check():
            raise ReverseConversionError("Q_CONVERSION_CANCELLED")
        source_rows, source_store = rows_by_pou[name]
        table = [[project_identifier], ["Module Type Information:", "FX5CPU FX5U"],
                 ["Step No.", "Line Statement", "Instruction", "I/O (Device)", "Blank", "P/I Statement", "Note"]]
        last_instruction_row: int | None = None
        for row_index, source_row in enumerate(source_rows):
            text = text_by_pou.get(name, {}).get(row_index)
            if text is not None:
                if text[0] == "statement":
                    table.append(["", text[1], "", "", "", "", ""])
                    statement_count += 1
                else:
                    if last_instruction_row is None:
                        raise ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", name, pou=name,
                                                     source_locator=f"_hdb/{source_store}:row:{row_index}")
                    table[last_instruction_row][6] = text[1]
                    note_count += 1
                continue
            if source_row[0] in {"__STMT__", "__NOTE__"}:
                raise ReverseConversionError("Q_TEXT_BINDING_UNVERIFIED", name, pou=name,
                                             source_locator=f"_hdb/{source_store}:row:{row_index}")
            if occurrence_index >= len(occurrences):
                raise ReverseConversionError("Q_IR_ROW_MISMATCH")
            occurrence = occurrences[occurrence_index]
            occurrence_index += 1
            provenance = occurrence["provenance"]
            if (occurrence["pou"]["name"] != name or str(provenance["source_store"]) != source_store
                    or (text_by_pou.get(name) and (
                        provenance.get("source_locator") != f"_hdb/{source_store}:row:{row_index}"
                        or provenance.get("source_digest") != digest_by_pou[name]
                        or occurrence["record"].get("sequence") != row_index))):
                raise ReverseConversionError("Q_IR_PROVENANCE_MISMATCH")
            if occurrence["record"]["opcode"] != source_row[0] or [operand["raw_token"] for operand in occurrence["operands"]] != ir_module.split_operands(source_row[1]):
                raise ReverseConversionError("Q_IR_CONTENT_MISMATCH")
            opcode, operands = _render_occurrence(occurrence, max_m=max_m, max_d=max_d, mapped=mapped)
            # Candidate steps are import addresses, not a claimed GX Works3
            # re-export equivalence. GX import/rebuild must confirm them.
            table.append([str((occurrence["record"]["sequence"]) * 2), "", opcode,
                          operands[0] if operands else "", "", "", ""])
            for token in operands[1:]:
                table.append(["", "", "", token, "", "", ""])
            last_instruction_row = len(table) - 1
        if not source_rows or source_rows[-1][0] != "END":
            raise ReverseConversionError("POU_END_MISSING", name, pou=name)
        outputs[f"{name}.csv"] = _csv(table)
    if occurrence_index != len(occurrences):
        raise ReverseConversionError("Q_IR_ROW_MISMATCH")
    comment_rows = [[project_identifier], ["Device Name", "Comment"]]
    for device, value in sorted(comments.items()):
        if not isinstance(value, str):
            raise ReverseConversionError("COMMENT_UNSUPPORTED", device,
                                         source_locator=f"comment:{device}", operand=device)
        comment_rows.append([mapped.get(f"comment:{device}", device), value])
    # Keep the mapped review table separate from the GX import candidate.
    outputs["COMMENT.review.csv"] = _csv(comment_rows)
    if comments:
        # Header and encoding match the GX Works3 FX5U/528 Common Device Comment
        # English A/B import and re-export corpus. Source language and this
        # product's own import lifecycle remain independent acceptance gates.
        target_comments = _project_comments(comments, mapped, max_m=max_m, max_d=max_d)
        outputs["comment.csv"] = _csv([[project_identifier], ["Device Name", "English"], *target_comments])
    report = {"schema": SCHEMA, "direction": "Q_TO_FX5U", "source_sha256": source_sha,
              "source_address_profile": "Q_XY_HEX",
              "source_cpu_identity": "UNVERIFIED",
              "source_cpu_observation": cpu_observation,
              "reader_commit": READER_COMMIT, "reader_wheel_sha256": READER_SHA256,
              "source_analysis": ir["analysis"]["state"], "candidate": "GX_WORKS3_IMPORT_REBUILD_REEXPORT_REQUIRED",
              "official_validation": "NOT_RUN", "production_adoption": "NOT_GRANTED",
              "pous": ordered_pous, "occurrences": len(occurrences), "comments": len(comments),
              "statements": statement_count, "notes": note_count,
              "control_structure": {"state": "PREFLIGHT_OK" if any(control_report.values()) else "NOT_PRESENT",
                                    **control_report, "gx_rebuild": "NOT_RUN"},
              "comment_output": "GX_WORKS3_COMMON_ENGLISH_IMPORT_CANDIDATE" if comments else "NO_COMMENTS",
              "comment_source_language": "DEFAULT_SLOT_UNPROVEN" if comments else "NOT_APPLICABLE",
              "comment_source_binding": "EXACT_PAIRS_NO_WARNING" if comments else "NO_COMMENTS",
              "outputs_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in outputs.items()},
              "provided_fx5_bounds": {"m_max": reported_max_m, "d_max": reported_max_d},
              "xy_mapping": mapping_report, "word_device_mapping": word_mapping_report}
    report["fixed_xy_bounds"] = {"source": {"profile": Profile.Q.value,
                                               "x_max": fixed_bounds(Profile.Q).x,
                                               "y_max": fixed_bounds(Profile.Q).y},
                                  "target": {"profile": Profile.FX5U.value,
                                               "x_max": fixed_bounds(Profile.FX5U).x,
                                               "y_max": fixed_bounds(Profile.FX5U).y}}
    static_lb_program = [operand["raw_token"] for occurrence in occurrences
                         for operand in occurrence["operands"]
                         if _STATIC_LB.fullmatch(operand["raw_token"])]
    static_lb_comments = [device for device in comments if _STATIC_LB.fullmatch(device)]
    static_lb_families = sorted({token[0] for token in (*static_lb_program, *static_lb_comments)})
    if static_lb_families:
        report["static_lb_target_review"] = {
            "status": "NOT_CHECKED",
            "families_present": static_lb_families,
            "program_occurrences": len(static_lb_program),
            "comment_rows": len(static_lb_comments),
            "latch_region": "UNVERIFIED" if "L" in static_lb_families else "NOT_APPLICABLE",
            "link_refresh_allocation": "UNVERIFIED" if "B" in static_lb_families else "NOT_APPLICABLE",
            "target_device_capacity": "NOT_CHECKED",
        }
    verified_sm_program = [operand["raw_token"] for occurrence in occurrences
                           for operand in occurrence["operands"]
                           if operand["raw_token"] in _VERIFIED_SM]
    verified_sm_comments = [device for device in comments if device in _VERIFIED_SM]
    if verified_sm_program or verified_sm_comments:
        report["verified_system_relay_subset"] = {
            "status": "DOCUMENTED_FUNCTION_SUBSET",
            "devices_present": sorted(set((*verified_sm_program, *verified_sm_comments))),
            "program_occurrences": len(verified_sm_program),
            "comment_rows": len(verified_sm_comments),
            "program_scope": "BASIC_READ_CONTACTS_ONLY",
            "coil_writes": "NOT_SUPPORTED",
            "program_execution_schedule": "NOT_CHECKED",
            "target_comment_import": "NOT_RUN",
        }
    static_w_program = [operand["raw_token"] for occurrence in occurrences
                        for operand in occurrence["operands"]
                        if _STATIC_W.fullmatch(operand["raw_token"])]
    static_w_comments = [device for device in comments if _STATIC_W.fullmatch(device)]
    if static_w_program or static_w_comments:
        report["static_w_target_review"] = {
            "status": "NOT_CHECKED",
            "program_occurrences": len(static_w_program),
            "comment_rows": len(static_w_comments),
            "link_register_refresh_allocation": "UNVERIFIED",
            "target_device_capacity": "NOT_CHECKED",
        }
    static_z_program = [operand["raw_token"] for occurrence in occurrences
                        for operand in occurrence["operands"]
                        if _STATIC_Z.fullmatch(operand["raw_token"])]
    if static_z_program:
        z_spans = [{"source_locator": occurrence["provenance"]["source_locator"],
                    "opcode": occurrence["record"]["opcode"], "operand_position": position,
                    "device": operand["raw_token"], "ir_access": operand["access"],
                    "word_count": _static_z_word_span(occurrence["record"]["opcode"], position),
                    "last_device": "Z" + str(int(operand["raw_token"][1:])
                        + _static_z_word_span(occurrence["record"]["opcode"], position) - 1)}
                   for occurrence in occurrences for position, operand in enumerate(occurrence["operands"])
                   if _STATIC_Z.fullmatch(operand["raw_token"])]
        plus_in_place = sum(occurrence["record"]["opcode"] == "+" and len(occurrence["operands"]) == 2
                            and _STATIC_Z.fullmatch(occurrence["operands"][1]["raw_token"]) is not None
                            for occurrence in occurrences)
        report["static_z_target_review"] = {
            "status": "NOT_CHECKED", "program_operand_occurrences": len(static_z_program),
            "word_width": "16_AND_32_BIT" if any(span["word_count"] == 2 for span in z_spans) else "16_BIT_ONLY",
            "operand_word_spans": z_spans, "source_spelling": "PRESERVED",
            "two_operand_plus_occurrences": plus_in_place,
            "two_operand_plus_ir_destination_access": "WRITE" if plus_in_place else "NOT_APPLICABLE",
            "two_operand_plus_runtime_effect": "READ_MODIFY_WRITE" if plus_in_place else "NOT_APPLICABLE",
            "target_index_register_allocation": "NOT_CHECKED",
            "target_device_capacity": "NOT_CHECKED", "dynamic_index_projection": "NOT_SUPPORTED",
        }
    static_t_program = [operand["raw_token"] for occurrence in occurrences
                        for operand in occurrence["operands"]
                        if _STATIC_T.fullmatch(operand["raw_token"])]
    if static_t_program:
        report["static_timer_target_review"] = {
            "status": "NOT_CHECKED",
            "program_occurrences": len(static_t_program),
            "timer_timebase": "UNVERIFIED",
            "timer_mode": "UNVERIFIED",
            "timer_coil": "NOT_SUPPORTED",
            "target_device_capacity": "NOT_CHECKED",
        }
    provided_checks = {"m": False, "d": False}
    if settings is not None:
        provided_checks.update(dict.fromkeys(("q_x", "q_y", "fx5_x", "fx5_y"), False))
    provided_checks.update({family.lower(): False for family in static_lb_families})
    if static_w_program or static_w_comments:
        provided_checks["w"] = False
    if static_z_program:
        provided_checks["z"] = False
    if static_t_program:
        provided_checks["t"] = False
    if word_mapped:
        provided_checks["r"] = False
    if not all(provided_checks.values()):
        report["project_bounds_check"] = {"state": "PARTIAL_CHECK" if any(provided_checks.values()) else "NOT_CHECKED",
                                          "provided": provided_checks}
    report["device_capacity_policy"] = {"state": "NOT_APPLIED", "blocking": False,
                                         "scope": "HARDWARE_INDEPENDENT_TEXT_CONVERSION"}
    outputs["report.json"] = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return outputs, report


def convert_q_to_fx5(source: Path, output_dir: Path, *, project_identifier: str,
                     max_m: int | None = None, max_d: int | None = None, settings: MappingSettings | None = None,
                     q_io_bounds: Bounds | None = None, fx5_io_bounds: Bounds | None = None,
                     word_settings: WordDeviceSettings | None = None,
                     expected_source_sha256: str | None = None,
                     cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Publish a complete candidate directory once, after full input preflight."""
    if output_dir.exists() or output_dir.is_symlink() or not output_dir.parent.is_dir() or output_dir.parent.is_symlink():
        raise ReverseConversionError("OUTPUT_DIRECTORY_MUST_BE_NEW")
    outputs, report = _plan(source, project_identifier=project_identifier, max_m=max_m, max_d=max_d,
                            settings=settings, q_io_bounds=q_io_bounds, fx5_io_bounds=fx5_io_bounds,
                            word_settings=word_settings,
                            cancel_check=cancel_check)
    if expected_source_sha256 is not None and report["source_sha256"] != expected_source_sha256:
        raise ReverseConversionError("Q_SOURCE_CHANGED_SINCE_PLAN")
    if cancel_check is not None and cancel_check():
        raise ReverseConversionError("Q_CONVERSION_CANCELLED")
    with tempfile.TemporaryDirectory(prefix=".p006-qfx5-", dir=output_dir.parent) as scratch:
        staging = Path(scratch) / "candidate"
        staging.mkdir()
        for leaf, body in outputs.items():
            if cancel_check is not None and cancel_check():
                raise ReverseConversionError("Q_CONVERSION_CANCELLED")
            (staging / leaf).write_bytes(body)
        if not source.is_file() or source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != report["source_sha256"]:
            raise ReverseConversionError("Q_SOURCE_CHANGED_BEFORE_PUBLICATION")
        if output_dir.exists() or output_dir.is_symlink():
            raise ReverseConversionError("OUTPUT_DIRECTORY_EXISTS")
        if cancel_check is not None and cancel_check():
            raise ReverseConversionError("Q_CONVERSION_CANCELLED")
        os.rename(staging, output_dir)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Q-family GXW to FX5U GX Works3 CSV candidate")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="new directory for candidate artifacts")
    parser.add_argument("--project-identifier", required=True)
    parser.add_argument("--fx5-max-m", type=int, help="optional verified inclusive M device ceiling")
    parser.add_argument("--fx5-max-d", type=int, help="optional user-defined inclusive D device ceiling; omitted bounds remain unverified")
    parser.add_argument("--xy-settings", type=Path, help="versioned Q to FX5U X/Y mapping JSON (legacy Q06UDV token supported)")
    parser.add_argument("--word-settings", type=Path, help="versioned Q ZR to FX5U R mapping JSON")
    parser.add_argument("--q-max-x", type=int)
    parser.add_argument("--q-max-y", type=int)
    parser.add_argument("--fx5-max-x", type=int)
    parser.add_argument("--fx5-max-y", type=int)
    args = parser.parse_args(argv)
    try:
        settings = (MappingSettings.from_json(args.xy_settings.read_bytes(), expected_source=Profile.Q,
                                              expected_target=Profile.FX5U) if args.xy_settings else None)
        word_settings = (WordDeviceSettings.from_json(args.word_settings.read_bytes(), expected_source=Profile.Q,
                                                      expected_target=Profile.FX5U) if args.word_settings else None)
        q_bounds = (Bounds(args.q_max_x, args.q_max_y) if args.q_max_x is not None or args.q_max_y is not None else None)
        fx_bounds = (Bounds(args.fx5_max_x, args.fx5_max_y) if args.fx5_max_x is not None or args.fx5_max_y is not None else None)
        report = convert_q_to_fx5(args.input, args.output_dir, project_identifier=args.project_identifier,
                                  max_m=args.fx5_max_m, max_d=args.fx5_max_d, settings=settings,
                                  word_settings=word_settings,
                                  q_io_bounds=q_bounds, fx5_io_bounds=fx_bounds)
    except (ReverseConversionError, MappingError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "HOLD", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "CANDIDATE", "report": str(args.output_dir / "report.json"),
                      "occurrences": report["occurrences"]}, ensure_ascii=False))
    return 0
