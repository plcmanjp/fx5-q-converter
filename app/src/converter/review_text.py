"""Pure, lossless viewing renderers for Converter's existing in-memory CSV rows.

The CSV owner supplies its actual headers and final field values. The supported
layout is checked before rendering; a reordered schema fails closed. This module neither
projects comments, assigns POU ownership, parses CSV files, nor publishes files.
The conversion publisher remains responsible for validation and no-clobber I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import json
from pathlib import PurePath
from typing import Literal, TypeVar


_Path = TypeVar("_Path", bound=PurePath)
_ImportState = Literal["PLANNED", "WITHHELD", "NOT_REQUESTED"]
_Category = Literal["common", "comment", "pou"]
_Row = tuple[str, ...]
_COMMENT_COLUMNS = (
    "Device Name", "Q Device Name", "Source Comment", "GX Works2 Comment",
    "Source CP949 Bytes", "GX Works2 CP949 Bytes", "Status",
)
_INVALID_COLUMNS = (
    "sequence", "source_step", "instruction", "operand", "error", "finding_code",
    "mining_tracks", "candidate",
)
_COMMENT_LABELS = (
    "원본 주소", "대상 주소", "원문", "GX Works2 출력문", "원문 CP949 바이트",
    "출력문 CP949 바이트", "처리 상태",
)
_INVALID_LABELS = (
    "Sequence", "원본 Step", "명령", "첫 피연산자", "오류 사유", "Finding Code",
    "Mining Tracks", "Candidate",
)
_IMPORT_DESCRIPTIONS = {
    "PLANNED": "정상 완료 시 가져오기 CSV에 포함",
    "WITHHELD": "가져오기 CSV 발행 보류",
    "NOT_REQUESTED": "가져오기 CSV 출력 미요청",
}
_STATUS_DESCRIPTIONS = {
    "EXACT": "원문 유지",
    "NORMALIZED": "줄바꿈 또는 끝 공백 등의 정규화",
    "TRUNCATED": "길이 제한에 맞춘 축약",
    "APPROVED_OVERRIDE": "승인된 대체 문구 적용",
}
# Policy code points classify text; they are not product display strings.
_PROHIBITED_PUNCTUATION = frozenset(map(chr, (0x2014, 0x2013, 0x2015, 0x00B7, 0x30FB, 0x2022)))
_CONTEXT_FIELDS = (
    ("source_operation", "원본 전체 명령"),
    ("source_note", "원본 Note"),
    ("source_line_statement", "원본 Line Statement"),
    ("source_pi_statement", "원본 PI Statement"),
    ("fallback_operation", "치환 전체 명령"),
    ("publication", "발행 제한"),
    ("runtime_action", "실행 제한"),
)


@dataclass(frozen=True)
class FindingContext:
    """Caller-verified attribution, bound to one full finding and its occurrence.

    ``data_row`` is the 1-based CSV data-row ordinal, not source Step. The
    caller must derive additional fields from an exact source-record binding.
    Candidate spelling or a matching Step alone is not that evidence.
    """

    data_row: int
    finding: _Row
    category: _Category = "common"
    program: str = ""
    source_operation: str | None = None
    source_note: str | None = None
    source_line_statement: str | None = None
    source_pi_statement: str | None = None
    fallback_operation: str | None = None
    publication: str | None = None
    runtime_action: str | None = None


def companion_text_path(csv_path: _Path) -> _Path:
    """Derive a name only; the publisher must check all collisions before I/O."""
    return csv_path.with_suffix(".txt")


def _enclosed_glyph(codepoint: int) -> bool:
    return (
        0x2460 <= codepoint <= 0x24FF
        or 0x2776 <= codepoint <= 0x2793
        or 0x3200 <= codepoint <= 0x32FF
        or 0x1F100 <= codepoint <= 0x1F1FF
    )


def _display_string(value: str) -> str:
    """Keep Korean readable while retaining the JSON-string inverse exactly."""
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("review text fields must contain Unicode scalar values")
    quoted = json.dumps(value, ensure_ascii=False)
    trailing_spaces = len(value) - len(value.rstrip(" "))
    if trailing_spaces:
        quoted = quoted[:-1 - trailing_spaces] + "\\u0020" * trailing_spaces + '"'
    return "".join(
        json.dumps(character, ensure_ascii=True)[1:-1]
        if (
            not character.isprintable()
            or character in _PROHIBITED_PUNCTUATION
            or _enclosed_glyph(ord(character))
        )
        else character
        for character in quoted
    )


def _validated_table(
    rows: Iterable[Sequence[str]], headers: Sequence[str], expected_columns: _Row,
) -> tuple[_Row, tuple[_Row, ...]]:
    if isinstance(headers, (str, bytes)) or not isinstance(headers, Sequence):
        raise ValueError("CSV headers must be a sequence of field names")
    columns = tuple(headers)
    width = len(expected_columns)
    if (
        len(columns) != width
        or any(not isinstance(column, str) or not column for column in columns)
        or columns != expected_columns
    ):
        raise ValueError("review text does not support this CSV header layout")
    materialized: list[_Row] = []
    for number, row in enumerate(rows, 1):
        if (
            isinstance(row, (str, bytes))
            or not isinstance(row, Sequence)
            or len(row) != width
            or any(not isinstance(value, str) for value in row)
        ):
            raise ValueError(f"CSV data row {number} must contain exactly {width} string fields")
        materialized.append(tuple(row))
    return columns, tuple(materialized)


def _preamble(title: str, columns: _Row) -> list[str]:
    return [
        title,
        "열람용이다. GX Works2 가져오기 파일이 아니다. 파일 존재나 진단 0건은 PLC 실행 승인이 아니다.",
        "이 문서는 발행 계획을 표시한다. 부분 발행 또는 실패한 결과는 가져오지 않는다.",
        "원장 열: [" + ", ".join(_display_string(column) for column in columns) + "]",
        "표시값은 JSON 문자열이다. 따옴표 안의 내용을 원문으로 해석하며 escape는 표시용이다.",
        r"개행은 \n 또는 \r, 탭은 \t, 역슬래시는 \\, 끝 공백은 \u0020으로 구분한다.",
        "CSV 데이터 행 번호는 헤더를 제외한 1부터 시작한다. CSV의 물리적 줄 번호와 다를 수 있다.",
        "",
    ]


def _append_row(lines: list[str], number: int, labels: _Row, row: _Row) -> None:
    lines.append(f"[CSV 데이터 행 {number}]")
    for label, value in zip(labels, row, strict=True):
        lines.append(f"  {label}: {_display_string(value)}")


def _encode(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def comment_review_text(
    rows: Iterable[Sequence[str]], *, headers: Sequence[str], excluded_status: str,
    import_csv_state: _ImportState,
) -> bytes:
    """Render the exact seven-field rows already prepared for the comment CSV.

    ``excluded_status`` comes from the converter's existing status constant.
    Import state is a publication plan, never inferred from projected text.
    """
    columns, items = _validated_table(rows, headers, _COMMENT_COLUMNS)
    if not isinstance(excluded_status, str) or not excluded_status or "|" in excluded_status:
        raise ValueError("excluded_status must be one nonempty status token")
    if not isinstance(import_csv_state, str) or import_csv_state not in _IMPORT_DESCRIPTIONS:
        raise ValueError("unknown comment import publication plan")
    status_sets = [tuple(dict.fromkeys(row[6].split("|"))) for row in items]
    excluded_count = sum(excluded_status in statuses for statuses in status_sets)
    counts = Counter(status for statuses in status_sets for status in statuses)
    lines = _preamble("코멘트 검토 원장 (열람용)", columns)
    lines.extend([
        f"전체 코멘트: {len(items)}건",
        f"대상 포함: {len(items) - excluded_count}건",
        f"대상 제외: {excluded_count}건",
        f"대상 포함 항목의 처리: {_IMPORT_DESCRIPTIONS[import_csv_state]}",
        "빈 바이트 수는 계산 불가이며 0바이트와 다르다. 기존 원장의 계산값을 그대로 표시한다.",
        "상태별 집계: 복합 상태는 중복 집계한다. 한 행의 같은 상태는 한 번만 집계한다.",
    ])
    for status, count in sorted(counts.items()):
        description = (
            "가져오기 대상에서 제외"
            if status == excluded_status else _STATUS_DESCRIPTIONS.get(status, "설명 미확인, 원장 값 보존")
        )
        lines.append(f"  {_display_string(status)}: {count}건 ({description})")
    if not items:
        lines.append("코멘트 0건")
    review_rows = [str(number) for number, statuses in enumerate(status_sets, 1) if statuses != ("EXACT",)]
    lines.append("EXACT 이외 상태의 CSV 데이터 행:")
    if review_rows:
        for offset in range(0, len(review_rows), 24):
            lines.append("  " + ", ".join(review_rows[offset:offset + 24]))
    else:
        lines.append("  없음")
    lines.append("")
    for number, (row, statuses) in enumerate(zip(items, status_sets, strict=True), 1):
        _append_row(lines, number, _COMMENT_LABELS, row)
        disposition = (
            "대상 제외, 가져오기 CSV에 포함되지 않음"
            if excluded_status in statuses else _IMPORT_DESCRIPTIONS[import_csv_state]
        )
        lines.extend([f"  가져오기 계획: {disposition}", ""])
    return _encode(lines)


def _validated_contexts(
    contexts: Iterable[FindingContext], items: tuple[_Row, ...],
) -> dict[int, FindingContext]:
    bound: dict[int, FindingContext] = {}
    for context in contexts:
        if not isinstance(context, FindingContext):
            raise ValueError("invalid finding context type")
        number = context.data_row
        if type(number) is not int or not 1 <= number <= len(items) or number in bound:
            raise ValueError("finding context must identify one unique existing CSV data row")
        if not isinstance(context.finding, tuple) or context.finding != items[number - 1]:
            raise ValueError("finding context does not match the complete CSV data row")
        if context.category not in ("common", "comment", "pou") or not isinstance(context.program, str):
            raise ValueError("invalid finding context category or program")
        if (context.category == "pou") != bool(context.program):
            raise ValueError("only POU context requires a nonempty verified program")
        for attribute, _label in _CONTEXT_FIELDS:
            value = getattr(context, attribute)
            if value is not None and not isinstance(value, str):
                raise ValueError("finding context details must be strings or None")
        if context.fallback_operation is not None and (
            not context.fallback_operation or not context.publication or not context.runtime_action
        ):
            raise ValueError("fallback context must preserve publication and runtime restrictions")
        bound[number] = context
    return bound


def _group_title(category: str, program: str) -> str:
    if category == "pou":
        return f"POU: {_display_string(program)}"
    return "코멘트" if category == "comment" else "프로젝트 공통 또는 귀속 미확인"


def invalid_review_text(
    rows: Iterable[Sequence[str]], *, headers: Sequence[str],
    contexts: Iterable[FindingContext] = (),
) -> bytes:
    """Preserve every diagnostic; unattributed rows remain common without guessing."""
    columns, items = _validated_table(rows, headers, _INVALID_COLUMNS)
    bound = _validated_contexts(contexts, items)
    groups: dict[tuple[str, str], list[int]] = {}
    for number in range(1, len(items) + 1):
        context = bound.get(number)
        group = (context.category, context.program) if context is not None else ("common", "")
        groups.setdefault(group, []).append(number)
    ranks = {"common": 0, "comment": 1, "pou": 2}
    group_order = sorted(groups, key=lambda group: (ranks[group[0]], group[1]))
    lines = _preamble("전체 변환 진단 (열람용)", columns)
    lines.extend([
        f"전체 진단: {len(items)}건",
        "분류는 호출자가 확인한 연결만 사용한다. 연결이 없는 항목은 귀속 미확인으로 보존한다.",
        "더미 치환은 원래 기능의 구현이나 PLC 실행 승인이 아니다.",
    ])
    if not items:
        lines.append("검토 필요 항목 0건")
    for category, program in group_order:
        lines.append(f"{_group_title(category, program)}: {len(groups[(category, program)])}건")
    lines.append("Finding Code별 집계:")
    for code, count in sorted(Counter(row[5] for row in items).items()):
        lines.append(f"  {_display_string(code)}: {count}건")
    lines.append("")
    for category, program in group_order:
        lines.extend([f"[{_group_title(category, program)}]", ""])
        for number in groups[(category, program)]:
            _append_row(lines, number, _INVALID_LABELS, items[number - 1])
            context = bound.get(number)
            if context is not None:
                for attribute, label in _CONTEXT_FIELDS:
                    value = getattr(context, attribute)
                    if value is not None:
                        lines.append(f"  {label}: {_display_string(value)}")
            lines.append("")
    return _encode(lines)
