"""FX5 text projection and manufacturer ceilings, separate from allocation.

JY997D55401-AG, printed page 85, is the ceiling authority. Passing this
check does not prove the current project's memory allocation, module layout,
instruction support or indexed/variable-length access. Device checks never
remap addresses; closed dummy projections separately retain source review.
"""
from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable

CPUS = ("FX5U", "FX5UC", "FX5UJ", "FX5S")
MANUAL = "https://www.mitsubishielectric.com/dl/fa/document/manual/plcf/jy997d55401/jy997d55401ag.pdf"
CLOCK_ALIASES = {"DATERD": "TRD", "DATERDP": "TRDP", "DATEWR": "TWR", "DATEWRP": "TWRP"}
_COMMON = {"X": 0o1777, "Y": 0o1777, "S": 4095, "D": 7999, "R": 32767}
_LARGE = {"M": 32767, "B": 0x7FFF, "SB": 0x7FFF, "F": 32767,
          "T": 1023, "ST": 1023, "C": 1023, "LC": 1023, "L": 32767,
          "W": 0x7FFF, "SW": 0x7FFF}
_UJ = {"M": 7679, "B": 0x7FF, "SB": 0x7FF, "F": 127,
       "T": 511, "ST": 15, "C": 255, "LC": 63, "L": 7679,
       "W": 0x3FF, "SW": 0x3FF}
_DEVICE = re.compile(
    r"(?:K([1-8]))?(ZR|SM|SD|ST|LC|SB|SW|ER|[XYMBFSTCDLRWZ])"
    r"([0-9A-F]+)(?:\.([0-9A-F]))?((?:ZZ|LZ|Z)[0-9]+)?\Z", re.I)
_MODULE = re.compile(r"U([0-9A-F]+)\\G([0-9]+)(?:\.([0-9A-F]))?((?:ZZ|LZ|Z)[0-9]+)?\Z", re.I)
MODULE_MANUAL = "https://dl.mitsubishielectric.com/dl/fa/document/manual/plcf/sh082452eng/sh082452engp.pdf"
MODULE_NUMBER_MANUAL = "https://dl.mitsubishielectric.com/dl/fa/document/manual/plc_fx/sh082703eng/sh082703engb.pdf"
_MODULE_NUMBER_MAX = {"FX5U": 0x10, "FX5UC": 0x10, "FX5UJ": 0x8, "FX5S": 0}
FALLBACK_MANUAL = "https://dl.mitsubishielectric.com/dl/fa/document/manual/plc_fx/sh082703eng/sh082703engc.pdf"
MODULE_DUMMY_FALLBACK = "FX5_MODULE_DUMMY_FALLBACK"


def validate_cpu(cpu: str) -> str:
    if cpu not in CPUS:
        raise ValueError("target CPU must be FX5U, FX5UC, FX5UJ or FX5S")
    return cpu


def project_instruction(opcode: str, operands: list[str]) -> dict[str, str]:
    if opcode in CLOCK_ALIASES:
        if len(operands) != 1:
            raise ValueError(f"{opcode} requires one seven-word device operand")
        return {"target_kind": "instruction", "target_opcode": CLOCK_ALIASES[opcode],
                "projection": "CLOCK_BINARY_7_WORDS"}
    if opcode == "NOPLF":
        if operands:
            raise ValueError("NOPLF print separator must have no operands")
        return {"target_kind": "statement", "target_opcode": "",
                "target_text": "NOPLF (print page separator)",
                "projection": "PRINT_SEPARATOR_TO_EXPLANATION_STATEMENT"}
    return {"target_kind": "instruction", "target_opcode": opcode, "projection": "SOURCE_OPCODE"}


def device_check(token: str, cpu: str, *, width: int = 1) -> dict[str, Any] | None:
    """Check a statically spelled base/span; None means not a parsed device.

    Decimal devices never absorb a dot-bit suffix into the family (D1432.F
    remains D). Dynamic indexing remains an explicit unverified condition.
    """
    validate_cpu(cpu)
    if module := _MODULE.fullmatch(token):
        unit = int(module[1], 16)
        maximum = _MODULE_NUMBER_MAX[cpu]
        invalid = not 1 <= unit <= maximum
        return {"device": token, "family": "U", "code": "TARGET_MODULE_NUMBER_UNSUPPORTED" if invalid else "TARGET_MODULE_REVIEW",
                "state": "BLOCKED" if invalid else "REVIEW", "indexed": bool(module[4]), "dot_bit": module[3],
                "module_number": unit, "module_radix": 16, "module_minimum": 1, "module_maximum": maximum,
                "module_access_supported": bool(maximum), "buffer_address": int(module[2]),
                "authority": MODULE_MANUAL, "module_number_authority": MODULE_NUMBER_MANUAL,
                "reason": (f'{cpu}는 모듈 접근 장치를 지원하지 않습니다.' if maximum == 0 else
                           f'{cpu} 모듈 번호는 U1..U{maximum:X}(16진수)이며 {token}의 번호는 지원 밖입니다. '
                           '모듈 배치와 버퍼 의미를 추정하여 번호를 변경하지 않습니다.' if invalid else
                           "번호 범위 안이어도 실제 모듈 구성과 버퍼 의미는 미검증입니다.")}
    if prefix := re.match(r"@?U([0-9A-F]+)\\G", token, re.I):
        unsupported = not 1 <= int(prefix[1], 16) <= _MODULE_NUMBER_MAX[cpu]
        return {"device": token, "family": "U", "code": "TARGET_MODULE_NUMBER_UNSUPPORTED" if unsupported else "TARGET_MODULE_SYNTAX_UNVERIFIED", "state": "BLOCKED",
                "indexed": bool(re.search(r"(?:ZZ|LZ|Z)", token, re.I)), "syntax": "UNVERIFIED",
                "authority": MODULE_MANUAL, "reason": "FX5 모듈 피연산자 형식은 미검증이며 지원 밖 번호는 변경하지 않습니다."}
    match = _DEVICE.fullmatch(token)
    if match is None:
        return None
    group, family, digits, bit, index = match.groups()
    family = family.upper()
    radix = 8 if family in {"X", "Y"} else 16 if family in {"B", "SB", "W", "SW"} else 10
    result: dict[str, Any] = {"device": token, "family": family, "dot_bit": bit,
                              "indexed": bool(index), "radix": radix}
    if (group and family not in {"X", "Y", "M", "L", "B", "SB", "F", "S"}
            or bit is not None and family not in {"D", "R", "W", "SW", "ZR", "SD"}):
        return {**result, "code": "TARGET_DEVICE_SYNTAX_REVIEW", "state": "REVIEW"}
    if not isinstance(width, int) or isinstance(width, bool) or width < 1:
        raise ValueError("device width must be a positive integer")
    if width > 1 and (group or bit is not None or family in {"X", "Y", "M", "L", "B", "SB", "F", "S"}):
        return {**result, "code": "TARGET_ACCESS_SPAN_REVIEW", "state": "REVIEW"}
    try:
        number = int(digits, radix)
    except ValueError:
        return {**result, "code": "TARGET_DEVICE_SYNTAX_REVIEW", "state": "REVIEW"}
    if family == "ZR":
        return {**result, "code": "TARGET_ZR_UNSUPPORTED", "state": "BLOCKED"}
    limits = {**_COMMON, **(_UJ if cpu == "FX5UJ" else _LARGE)}
    if family not in limits:
        return {**result, "code": "TARGET_DEVICE_REVIEW", "state": "REVIEW"}
    # K digit groups contain four bits each; other widths are explicit words.
    end = number + (int(group) * 4 if group else width) - 1
    ceiling = limits[family]
    result.update(base=number, end=end, ceiling=ceiling)
    if end > ceiling:
        return {**result, "code": "TARGET_DEVICE_CEILING", "state": "BLOCKED"}
    if index:
        return {**result, "code": "TARGET_INDEX_REVIEW", "state": "REVIEW"}
    return {**result, "code": "WITHIN_MANUFACTURER_CEILING", "state": "CANDIDATE"}


def project_module_fallback(opcode: str, operands: list[str], cpu: str) -> dict[str, Any] | None:
    """Closed review shapes for proven-invalid module numbers; never calculate index meaning."""
    validate_cpu(cpu)
    modules = [_MODULE.fullmatch(token) for token in operands]
    unsupported = [bool(m and not 1 <= int(m[1], 16) <= _MODULE_NUMBER_MAX[cpu]) for m in modules]
    if not any(unsupported):
        return None

    def word(token: str, bits: int = 16, *, allow_group: bool = False) -> bool:
        module = _MODULE.fullmatch(token)
        if module:
            return module[3] is None and int(module[2]) <= 65535
        if re.fullmatch(r"(?:D|R|SD)[0-9]+(?:(?:ZZ|LZ|Z)[0-9]+)?", token, re.I):
            return True
        if re.fullmatch(r"W[0-9A-F]+(?:(?:ZZ|LZ|Z)[0-9]+)?", token, re.I):
            return True
        group = re.fullmatch(r"K([1-8])(SB|[XYMLBF])([0-9A-F]+)(?:(?:ZZ|LZ|Z)[0-9]+)?", token, re.I)
        if group:
            if not allow_group or int(group[1]) > bits // 4:
                return False
            family = group[2].upper()
            radix = 8 if family in {"X", "Y"} else 16 if family in {"B", "SB"} else 10
            try:
                int(group[3], radix)
            except ValueError:
                return False
            return True
        if re.fullmatch(r"K-?[0-9]+", token, re.I):
            return -(1 << (bits - 1)) <= int(token[1:]) < (1 << (bits - 1))
        if re.fullmatch(r"H[0-9A-F]+", token, re.I):
            return int(token[1:], 16) < (1 << bits)
        return False

    indexed = any(re.search(r"(?:ZZ|LZ|Z)[0-9]+\Z", token, re.I) for token in operands)
    replacement = list(operands)
    kind = ""
    if opcode == "OUT" and len(operands) == 1 and modules[0] and modules[0][3] is not None and int(modules[0][2]) <= 65535:
        target_opcode, replacement, kind = "OUT", ["SM4095"], "BIT_OUTPUT"
    elif opcode in {"MOV", "DMOV"} and len(operands) == 2 and all(word(token, 32 if opcode == "DMOV" else 16, allow_group=True) for token in operands):
        # A destination must be a word device, never a literal constant.
        if re.fullmatch(r"K-?[0-9]+|H[0-9A-F]+", operands[1], re.I):
            return None
        if opcode == "DMOV" or indexed:
            target_opcode, replacement, kind = "OUT", ["SM4095"], "WHOLE_ACTION"
        else:
            target_opcode, kind = opcode, "WORD_DEVICE"
            replacement = ["SD4095" if held else token
                           for token, held in zip(operands, unsupported, strict=True)]
    elif (comparison := re.fullmatch(r"(LD|AND|OR)(?:<>|>=|<=|=|>|<)", opcode)) and len(operands) == 2 and all(word(token) for token in operands):
        target_opcode, replacement, kind = comparison[1], ["SM4095"], "WHOLE_COMPARISON"
    else:
        return None
    # Every accepted shape above has a fixed known arity; no adjacent word or
    # computed index is used for the dummy. Source semantics remain unaccepted.
    return {"code": MODULE_DUMMY_FALLBACK, "kind": kind, "target_opcode": target_opcode,
            "target_operands": replacement, "indexed_source": indexed,
            "publication": "REVIEW_ONLY", "runtime_action": "DO_NOT_DOWNLOAD_OR_RUN",
            "compatibility": "SOURCE_FUNCTION_NOT_IMPLEMENTED", "authority": FALLBACK_MANUAL,
            "reason": f'{cpu}에서 사용할 수 없는 모듈 참조입니다. 실제 인덱스/장치 계산을 수행하지 않은 검토용 대체이며 원래 기능은 구현되지 않았습니다.'}


def comment_partition(comments: Iterable[dict[str, Any]], cpu: str) -> dict[str, Any]:
    """Keep the full order and isolate candidates from import-stopping rows."""
    candidates: dict[str, list[dict[str, Any]]] = {}
    retained = []
    assessments = []
    families: Counter[str] = Counter()
    first_blocked = None
    for row_index, comment in enumerate(comments):
        check = device_check(comment["target_device"], cpu)
        if comment.get('mapping_state') == 'RETAIN_SOURCE_UNMAPPED':
            check = {**(check or {}), 'device': comment['device'],
                     'family': (check or {}).get('family', 'UNRESOLVED'),
                     'code': 'TARGET_COMMENT_MAPPING_HELD', 'state': 'REVIEW'}
        if check is None:
            check = {"device": comment["target_device"], "family": "UNRESOLVED",
                     "code": "TARGET_DEVICE_REVIEW", "state": "REVIEW"}
        entry = {"row_index": row_index, "locator": comment["locator"], **check}
        assessments.append(entry)
        families[check["family"]] += 1
        if check["state"] == "CANDIDATE" and comment["text"] is not None:
            bucket = check["family"] + ("-bit" if check.get("dot_bit") is not None else "")
            candidates.setdefault(bucket, []).append(comment)
        else:
            retained.append(comment)
            if first_blocked is None:
                first_blocked = entry
    return {"candidates": candidates, "retained": retained, "assessments": assessments,
            "family_counts": dict(sorted(families.items())), "first_non_candidate": first_blocked}


def compare_comment_rows(expected: list[tuple[str, str]], actual: list[tuple[str, str]]) -> dict[str, Any]:
    """Round-trip evidence distinguishes an exact prefix from arbitrary loss."""
    missing, extra = Counter(expected) - Counter(actual), Counter(actual) - Counter(expected)
    prefix = len(actual) < len(expected) and expected[:len(actual)] == actual and not extra
    prefix_multiset = (len(actual) < len(expected) and not extra
                       and Counter(expected[:len(actual)]) == Counter(actual))
    return {"expected_count": len(expected), "actual_count": len(actual),
            "missing_count": sum(missing.values()), "extra_count": sum(extra.values()),
            "state": "MATCH" if not missing and not extra else "MISMATCH",
            "exact_prefix": prefix,
            "input_prefix_multiset": prefix_multiset,
            "first_unimported": {"row_index": len(actual), "device": expected[len(actual)][0]}
            if prefix_multiset else None}
