from __future__ import annotations

import csv
import importlib
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Protocol, Sequence

from converter.review_text import companion_text_path


APP_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = APP_ROOT.parent
BUILD_VERSION = os.environ.get("CONVERTER_BUILD_VERSION", "source")
DEFAULT_CONFIG = {
    "language": "ko",
    "log_directory": "logs",
    "log_max_bytes": 1_048_576,
    "log_backup_count": 7,
    "queue_poll_ms": 100,
}


def application_title(title: str, build_version: str = BUILD_VERSION) -> str:
    return f"{title} {build_version}"

TEXT = {
    "ko": {
        "title": "FX5/Q CSV Converter",
        "input": "FX5 → Q: GX Works3 파일 (.gx3)",
        "output": "출력 폴더",
        "browse_file": "파일 선택",
        "browse_folder": "폴더 선택",
        "preflight": "사전 점검",
        "convert": "변환 실행",
        "cancel": "취소",
        "open_folder": "결과 폴더 열기",
        "open_invalid": "진단 TXT 열기",
        "open_comment_review": "코멘트 TXT 열기",
        "review_text_fallback": "열람용 TXT가 없어 기존 CSV를 엽니다: {path}",
        "review_text_missing": "열람용 TXT와 대응 CSV가 생성되지 않았거나 없습니다: {path}",
        "output_comment_review_text": "코멘트 검토 TXT (열람용)",
        "output_invalid_text": "전체 진단 TXT (열람용)",
        "copy_log": "로그 복사",
        "open_log": "로그 파일 열기",
        "boundary": "지원 경계",
        "log": "결과 및 오류 로그",
        "findings": "변환 차단 상세",
        "finding_filter": "분류 필터",
        "filter_all": "전체",
        "filter_q_target": "Q 대상 차단",
        "filter_general": "일반 변환 오류",
        "summary_total": "전체 Q 대상",
        "summary_unsupported": "Q 미지원",
        "summary_general": "일반 오류",
        "program": "프로그램",
        "source_step": "source step",
        "instruction": "명령",
        "operands": "전체 피연산자",
        "finding_code": "finding_code",
        "reason": "차단 원인",
        "required_action": "필요 조치",
        "unsupported_warning_summary": (
            "경고: TARGET_UNSUPPORTED finding {count}건에 안전성이 입증된 진단 폴백이 없습니다. "
            "해당 POU의 정상 Q CSV 발행을 중단하고 review-only로 보존했습니다. 다운로드하거나 RUN하지 마십시오."
        ),
        "unsupported_warning_withheld": (
            "경고: TARGET_UNSUPPORTED finding 중 {fallback_count}건은 DIAGNOSTIC_FALLBACK_UNAVAILABLE이며, "
            "{withheld_count}건도 Q CSV 발행을 보류했습니다. 모든 대상 POU는 review-only입니다."
        ),
        "unsupported_warning_official": (
            "경고: TARGET_UNSUPPORTED finding {count}건을 미쓰비시 공식 dummy fallback으로 검토용 CSV에 발행했습니다. "
            "정상 Q CSV는 발행하지 않습니다. 이 파일은 offline review-only이며 실행 동등 mapping이 아닙니다. A-PLC compatibility를 검증하고 다운로드하거나 RUN하지 마십시오."
        ),
        "unsupported_warning_official_withheld": (
            "경고: TARGET_UNSUPPORTED finding 중 {official_count}건은 공식 dummy fallback 검토용 CSV로 발행했고 "
            "{withheld_count}건은 검토용 CSV도 발행하지 않았습니다. 정상 Q CSV는 모두 보류되며 다운로드하거나 RUN하지 마십시오."
        ),
        "unsupported_warning_historical": (
            "경고: 과거 YFFFF 센티널 원장이 감지됐습니다. YFFFF는 GX Works2 Q06UDV에서 거부된 이력이며 "
            "현재 변환 결과로 간주하지 않습니다. 다시 변환해야 합니다."
        ),
        "unsupported_warning_no_fallback": "경고: TARGET_UNSUPPORTED finding {count}건의 Q CSV 발행을 보류했습니다.",
        "comment_filter_warning": (
            "코멘트 대상 필터: 원본 {total}건 중 GX Works2 import용 {import_rows}건을 발행하고 "
            "Q06UDV 비호환 디바이스 {excluded}건은 comment-review.csv에 보존했습니다. 열람용 TXT의 발행 여부는 결과 목록에서 확인하세요."
        ),
        "output_normalized_source": "소스 정규화 CSV (FX X/Y 원본 표기 보존, GX Works2 import 금지)",
        "output_q_target": "GX Works2 대상 import CSV (프로그램별 Q target, X/Y source spelling identity)",
        "output_not_published": "발행 없음",
        "partial_complete": (
            "변환 부분 완료: 처리 {processed}행, 검토 필요 {invalid}건. "
            "발행된 파일과 보류 사유는 conversion-report.json에서 확인할 수 있다."
        ),
        "no_normal_q": (
            "정상 Q CSV 0개: 처리 {processed}행, 검토 필요 {invalid}건. "
            "검토용 CSV {import_review}개, TXT만 생성 {ledger_only}개이며 정상 변환 완료가 아니다. "
            "검토 전용 산출물은 DO_NOT_DOWNLOAD_OR_RUN이다."
        ),
        "complete_with_review": (
            "POU 처리 완료(검토 필요): 처리 {processed}행, 검토 필요 {invalid}건. "
            "정상 Q {normal}개, 검토용 CSV {import_review}개, TXT만 생성 {ledger_only}개이며 "
            "review-only 산출물은 DO_NOT_DOWNLOAD_OR_RUN입니다."
        ),
        "import_complete": "가져오기 파일 생성 완료: 정상 Q {normal}개.",
        "import_complete_review": "가져오기 파일 생성 완료 (검토 필요): 정상 Q {normal}개, 검토용 CSV {import_review}개. DO_NOT_DOWNLOAD_OR_RUN.",
        "import_complete_fallback": (
            "가져오기 파일 생성 완료 (폴백 {fallback_count}건, 수정 필요): "
            "정상 Q {normal}개, 검토용 CSV {import_review}개. DO_NOT_DOWNLOAD_OR_RUN."
        ),
        "import_incomplete": (
            "일부 가져오기 파일 미생성: {missing}. 정상 Q {normal}개, "
            "검토용 CSV {import_review}개, TXT만 생성 {ledger_only}개. "
            "검토 필요 {invalid}건. DO_NOT_DOWNLOAD_OR_RUN."
        ),
        "action_q_mapping": "승인된 Q 명령 및 피연산자 치환과 전체 왕복 검증이 필요합니다.",
        "action_diagnostic_fallback_unavailable": "안전성과 무부작용이 입증된 쓰기 가능 진단 폴백이 없습니다. 정상 Q CSV는 발행하지 않으며 review-only 결과를 검토하십시오.",
        "action_official_dummy_fallback": "공식 SM/SD dummy로 복구된 offline review CSV입니다. 원본 명령을 검토하고 A-PLC compatibility를 확인하며 다운로드하거나 RUN하지 마십시오.",
        "action_yffff_historical": "과거 YFFFF 센티널은 GX Works2 Q06UDV에서 거부됐습니다. 현재 결과로 승인하지 말고 재변환하십시오.",
        "action_unknown_fallback": "알 수 없는 fallback marker입니다. 결과를 사용하지 말고 현재 변환기로 재변환하십시오.",
        "action_source_rebuild": "원본을 보존하고 GX3 복제본을 GX Works3에서 연 뒤 Convert > Rebuild All을 실행하고 저장한 파일로 다시 변환하십시오.",
        "action_general": "원본, 변환 규칙 또는 인코딩을 확인하고 승인해야 합니다.",
        "idle": "GX3 파일을 놓거나 선택하면 입력 경로와 기본 출력 폴더가 설정됩니다.",
        "running": "작업 중",
        "confirm_exit": "실행하지 않은 선택 또는 진행 중인 작업이 있습니다. 종료하시겠습니까?",
        "confirm_replace_title": "기존 작업본 삭제 확인",
        "confirm_replace": (
            "선택한 프로젝트 결과 폴더가 이미 있습니다.\n\n"
            "대상 폴더: {path}\n\n"
            "[예]를 누르면 이 폴더의 기존 작업본을 모두 삭제하고 계속합니다.\n"
            "기존 작업본은 새 변환 성공 전까지 안전 격리됩니다.\n"
            "[아니오]를 누르면 아무것도 변경하지 않고 취소합니다.\n\n"
            "계속할까요?"
        ),
        "replace_cancelled": "기존 작업본을 유지하고 작업을 취소했습니다.",
        "replace_cleared": "기존 작업본을 안전 격리하고 결과 경로를 비웠습니다: {path}",
        "replace_retained": "이전 작업본을 보존했습니다: {path}",
        "replace_cleanup_failed": "새 변환은 완료됐지만 이전 작업본 정리에 실패해 보존했습니다: {path} ({error})",
        "replace_failed": "기존 프로젝트 결과 폴더를 삭제할 수 없습니다: {error}",
    },
    "en": {
        "title": "FX5/Q CSV Converter",
        "input": "FX5 to Q: GX Works3 file (.gx3)",
        "output": "Output folder",
        "browse_file": "Choose file",
        "browse_folder": "Choose folder",
        "preflight": "Preflight",
        "convert": "Convert",
        "cancel": "Cancel",
        "open_folder": "Open result folder",
        "open_invalid": "Open diagnostic TXT",
        "open_comment_review": "Open comment TXT",
        "review_text_fallback": "Viewing TXT is unavailable. Opening the existing CSV: {path}",
        "review_text_missing": "Neither the viewing TXT nor its CSV was generated or is available: {path}",
        "output_comment_review_text": "Comment review TXT (view only)",
        "output_invalid_text": "All diagnostics TXT (view only)",
        "copy_log": "Copy log",
        "open_log": "Open log file",
        "boundary": "Support boundary",
        "log": "Result and error log",
        "findings": "Conversion blocker details",
        "finding_filter": "Category filter",
        "filter_all": "All",
        "filter_q_target": "Q target blockers",
        "filter_general": "General conversion errors",
        "summary_total": "All Q target",
        "summary_unsupported": "Q unsupported",
        "summary_general": "General errors",
        "program": "Program",
        "source_step": "Source step",
        "instruction": "Instruction",
        "operands": "All operands",
        "finding_code": "finding_code",
        "reason": "Blocking reason",
        "required_action": "Required action",
        "unsupported_warning_summary": (
            "Warning: {count} TARGET_UNSUPPORTED finding(s) have no proven-safe diagnostic fallback. "
            "Normal Q CSV publication for each affected POU was blocked and preserved as review-only. Do not download or run."
        ),
        "unsupported_warning_withheld": (
            "Warning: {fallback_count} TARGET_UNSUPPORTED finding(s) are DIAGNOSTIC_FALLBACK_UNAVAILABLE and "
            "{withheld_count} finding(s) also withhold Q CSV publication. Every affected POU is review-only."
        ),
        "unsupported_warning_official": (
            "Warning: {count} TARGET_UNSUPPORTED finding(s) were emitted only to review CSV with the Mitsubishi official dummy fallback. "
            "No normal Q CSV is published. This file is offline review-only and not an execution-equivalent mapping. Verify A-PLC compatibility and do not download or run."
        ),
        "unsupported_warning_official_withheld": (
            "Warning: {official_count} TARGET_UNSUPPORTED finding(s) were emitted to review CSV with the official dummy fallback and "
            "{withheld_count} finding(s) also withhold review CSV publication. All normal Q CSV is withheld; do not download or run."
        ),
        "unsupported_warning_historical": (
            "Warning: a historical YFFFF sentinel ledger was detected. YFFFF was rejected by GX Works2 Q06UDV and is not "
            "treated as a current conversion result. Reconvert the source."
        ),
        "unsupported_warning_no_fallback": "Warning: {count} TARGET_UNSUPPORTED finding(s) withhold Q CSV publication.",
        "comment_filter_warning": (
            "Comment target filter: published {import_rows} of {total} source comments for GX Works2 import; "
            "preserved {excluded} Q06UDV-incompatible device comment(s) in comment-review.csv. Check the output list for its viewing TXT."
        ),
        "output_normalized_source": (
            "Source-normalized CSV (preserves FX X/Y source spelling; do not import into GX Works2)"
        ),
        "output_q_target": "GX Works2 target import CSV (per-program Q target, X/Y source spelling identity)",
        "output_not_published": "not published",
        "partial_complete": (
            "Partial conversion: processed {processed} row(s), {invalid} finding(s) require review. "
            "Check conversion-report.json for published files and reasons for withheld output."
        ),
        "no_normal_q": (
            "No normal Q CSV: processed {processed} row(s), {invalid} finding(s). "
            "Review CSV {import_review}, TXT only {ledger_only}; normal conversion is not complete. "
            "Review-only artifacts are DO_NOT_DOWNLOAD_OR_RUN."
        ),
        "complete_with_review": (
            "POU processing complete (review required): processed {processed} row(s), "
            "{invalid} finding(s) require review. Normal Q {normal}, review CSV {import_review}, "
            "TXT only {ledger_only}; review-only artifacts are DO_NOT_DOWNLOAD_OR_RUN."
        ),
        "import_complete": "Import files generated: normal Q {normal}.",
        "import_complete_review": "Import files generated (review required): normal Q {normal}, review CSV {import_review}. DO_NOT_DOWNLOAD_OR_RUN.",
        "import_complete_fallback": (
            "Import files generated ({fallback_count} fallback rows, modification required): "
            "normal Q {normal}, review CSV {import_review}. DO_NOT_DOWNLOAD_OR_RUN."
        ),
        "import_incomplete": (
            "Some import files were not generated: {missing}. Normal Q {normal}, "
            "review CSV {import_review}, TXT only {ledger_only}. "
            "{invalid} findings require review. DO_NOT_DOWNLOAD_OR_RUN."
        ),
        "action_q_mapping": "Provide an approved Q instruction and operand replacement, then complete full round-trip validation.",
        "action_diagnostic_fallback_unavailable": "No writable diagnostic fallback has proven safety and absence of side effects. No normal Q CSV is published; review the review-only result.",
        "action_official_dummy_fallback": "This is an offline review CSV recovered with official SM/SD dummy devices. Review the source instruction, verify A-PLC compatibility, and do not download or run.",
        "action_yffff_historical": "The historical YFFFF sentinel was rejected by GX Works2 Q06UDV. Do not approve it as a current result; reconvert the source.",
        "action_unknown_fallback": "The fallback marker is unknown. Do not use the result; reconvert it with the current converter.",
        "action_source_rebuild": "Preserve the original, open a GX3 copy in GX Works3, run Convert > Rebuild All, save it, and reconvert the saved copy.",
        "action_general": "Review and approve the source, conversion rule, or encoding.",
        "idle": "Drop or choose a GX3 file to set the input path and default output folder.",
        "running": "Working",
        "confirm_exit": "There is an unexecuted selection or running job. Exit?",
        "confirm_replace_title": "Delete existing work?",
        "confirm_replace": (
            "The selected project result folder already exists.\n\n"
            "Target folder: {path}\n\n"
            "Choose Yes to delete all existing work in this folder and continue.\n"
            "Existing work is safely quarantined until the new conversion succeeds.\n"
            "Choose No to cancel without changing anything.\n\n"
            "Continue?"
        ),
        "replace_cancelled": "Kept the existing work and cancelled the operation.",
        "replace_cleared": "Safely quarantined the existing work and cleared the result path: {path}",
        "replace_retained": "Preserved the previous work: {path}",
        "replace_cleanup_failed": "The new conversion completed, but previous work cleanup failed and was retained: {path} ({error})",
        "replace_failed": "Could not delete the existing project result folder: {error}",
    },
}


@dataclass(frozen=True)
class PilotRequest:
    input_path: Path
    output_directory: Path


@dataclass(frozen=True)
class OutputPaths:
    normalized: Path
    q_import: Path
    comment: Path
    comment_review: Path
    report: Path
    invalid: Path
    q_imports: tuple[Path, ...] = ()
    partial_reviews: tuple[Path, ...] = ()
    mining_reviews: tuple[Path, ...] = ()
    typed_mining_reviews: tuple[Path, ...] = ()
    source_recovered_reviews: tuple[Path, ...] = ()
    structural_reviews: tuple[Path, ...] = ()
    import_reviews: tuple[Path, ...] = ()

    @property
    def comment_review_text(self) -> Path:
        return companion_text_path(self.comment_review)

    @property
    def invalid_text(self) -> Path:
        return companion_text_path(self.invalid)

    def all(self) -> tuple[Path, ...]:
        q_paths = self.q_imports or (self.q_import,)
        return (
            self.normalized,
            *q_paths,
            *self.partial_reviews,
            *self.mining_reviews,
            *self.typed_mining_reviews,
            *self.source_recovered_reviews,
            *self.structural_reviews,
            *(path for path in self.import_reviews if path not in q_paths),
            self.comment,
            self.comment_review,
            self.comment_review_text,
            self.report,
            self.invalid,
            self.invalid_text,
        )

    def diagnostics(self) -> tuple[Path, ...]:
        return (
            self.normalized, self.comment, self.comment_review, self.comment_review_text,
            self.report, self.invalid, self.invalid_text,
        )


@dataclass(frozen=True)
class PreflightReport:
    supported: bool
    summary: str
    details: tuple[str, ...] = ()
    partial: bool = False


RESULT_SPLIT_ORIENT = "vertical"


def format_preflight_boundary(report: PreflightReport) -> str:
    """Keep the support boundary compact; details belong in the scrollable log."""
    prefix = "부분 변환 예정" if report.partial else ("통과" if report.supported else "차단")
    return f"{prefix}: {report.summary}"


def format_preflight_log(report: PreflightReport) -> str:
    """Return the complete preflight record, including every detail line."""
    message = f"사전 점검 {format_preflight_boundary(report)}"
    if report.details:
        message += "\n" + "\n".join(report.details)
    return message


def create_vertical_result_split(parent: tk.Misc, *panes: tk.Widget) -> ttk.Panedwindow:
    """Attach findings and log panes to one vertically resizable split."""
    split = parent if isinstance(parent, ttk.Panedwindow) else ttk.Panedwindow(parent, orient=RESULT_SPLIT_ORIENT)
    for pane in panes:
        split.add(pane, weight=1)
    return split


@dataclass(frozen=True)
class ConversionResult:
    processed_rows: int
    invalid_rows: int
    outputs: OutputPaths


Q_TARGET_FINDING_CODES = frozenset({"TARGET_UNSUPPORTED"})
FINDING_FILTER_ALL = "ALL"
FINDING_FILTER_Q_TARGET = "Q_TARGET"
FINDING_FILTER_GENERAL = "GENERAL"


@dataclass(frozen=True)
class Finding:
    program: str
    source_step: str
    instruction: str
    operands: str
    finding_code: str
    reason: str
    action_key: str
    fallback: str = ""

    @property
    def classification(self) -> str:
        return FINDING_FILTER_Q_TARGET if self.finding_code in Q_TARGET_FINDING_CODES else FINDING_FILTER_GENERAL


@dataclass(frozen=True)
class FindingSummary:
    q_target: int
    target_unsupported: int
    general: int


@dataclass(frozen=True)
class BlockedPresentation:
    error: "PilotBlockedError"
    findings: tuple[Finding, ...] = ()
    parse_error: str = ""


def _candidate_fields(candidate: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in candidate.split(";"):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _action_key(finding_code: str) -> str:
    if finding_code == "TARGET_UNSUPPORTED":
        return "action_q_mapping"
    if finding_code == "SOURCE_REBUILD_REQUIRED":
        return "action_source_rebuild"
    return "action_general"


def parse_invalid_findings(path: Path, invalid_header: tuple[str, ...] | None = None) -> tuple[Finding, ...]:
    """Parse the core invalid ledger into a UI-neutral finding model."""
    if invalid_header is None:
        core = importlib.import_module("converter.core")
        invalid_header = tuple(core.INVALID_HEADER)
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != tuple(invalid_header):
            raise ValueError("invalid CSV header does not match core.INVALID_HEADER")
        findings: list[Finding] = []
        for row in reader:
            candidate = _candidate_fields(row["candidate"])
            operands = candidate.get("operands", row["operand"])
            findings.append(
                Finding(
                    program=candidate.get("program", ""),
                    source_step=row["source_step"],
                    instruction=row["instruction"],
                    operands=operands,
                    finding_code=row["finding_code"],
                    reason=row["error"],
                    action_key=_action_key(row["finding_code"]),
                    fallback=candidate.get("diagnostic_fallback", candidate.get("fallback", "")),
                )
            )
    return tuple(findings)


def summarize_findings(findings: tuple[Finding, ...]) -> FindingSummary:
    unsupported = sum(finding.finding_code == "TARGET_UNSUPPORTED" for finding in findings)
    general = sum(finding.classification == FINDING_FILTER_GENERAL for finding in findings)
    return FindingSummary(unsupported, unsupported, general)


def filter_findings(findings: tuple[Finding, ...], category: str) -> tuple[Finding, ...]:
    if category == FINDING_FILTER_ALL:
        return findings
    if category in {FINDING_FILTER_Q_TARGET, FINDING_FILTER_GENERAL}:
        return tuple(finding for finding in findings if finding.classification == category)
    return tuple(finding for finding in findings if finding.finding_code == category)


def format_unsupported_warning(findings: tuple[Finding, ...], text: dict[str, str]) -> str:
    """Format one deterministic warning record for all unsupported Q instructions."""
    unsupported = tuple(finding for finding in findings if finding.finding_code == "TARGET_UNSUPPORTED")
    if not unsupported:
        return ""
    unavailable_markers = {"DIAGNOSTIC_FALLBACK_UNAVAILABLE"}
    official_markers = {"OFFICIAL_DUMMY_FALLBACK"}
    historical_markers = {
        "YFFFF_SENTINEL_NOT_HARDWARE_MAPPING",
        "YFFFF_SENTINEL_PLANNED_NOT_HARDWARE_MAPPING",
    }
    fallback_count = sum(finding.fallback in unavailable_markers for finding in unsupported)
    official_count = sum(finding.fallback in official_markers for finding in unsupported)
    historical_count = sum(finding.fallback in historical_markers for finding in unsupported)
    if official_count == len(unsupported):
        lines = [text["unsupported_warning_official"].format(count=len(unsupported))]
    elif official_count:
        lines = [
            text["unsupported_warning_official_withheld"].format(
                official_count=official_count,
                withheld_count=len(unsupported) - official_count,
            )
        ]
    elif fallback_count == len(unsupported):
        lines = [text["unsupported_warning_summary"].format(count=len(unsupported))]
    elif fallback_count == 0 and historical_count:
        lines = [text["unsupported_warning_historical"]]
    elif fallback_count == 0:
        lines = [text["unsupported_warning_no_fallback"].format(count=len(unsupported))]
    else:
        lines = [
            text["unsupported_warning_withheld"].format(
                fallback_count=fallback_count,
                withheld_count=len(unsupported) - fallback_count,
            )
        ]
    for index, finding in enumerate(unsupported, start=1):
        if finding.fallback in official_markers:
            action_key = "action_official_dummy_fallback"
        elif finding.fallback in unavailable_markers:
            action_key = "action_diagnostic_fallback_unavailable"
        elif finding.fallback in historical_markers:
            action_key = "action_yffff_historical"
        elif finding.fallback:
            action_key = "action_unknown_fallback"
        else:
            action_key = finding.action_key
        lines.append(
            f"[{index}] {text['program']}={finding.program} | "
            f"{text['source_step']}={finding.source_step} | "
            f"{text['instruction']}={finding.instruction} | "
            f"{text['operands']}={finding.operands} | "
            f"{text['finding_code']}={finding.finding_code} | "
            f"{text['reason']}={finding.reason} | "
            f"{text['required_action']}={text[action_key]}"
        )
    return "\n".join(lines)


class PilotBlockedError(RuntimeError):
    """A fail-closed conversion finding, optionally accompanied by invalid.csv."""

    def __init__(self, message: str, invalid_output: Path | None = None) -> None:
        super().__init__(message)
        self.invalid_output = invalid_output


class PilotPartialPublishError(RuntimeError):
    """A failed conversion that left operator-reviewable partial outputs."""

    def __init__(self, message: str, partial_outputs: tuple[Path, ...]) -> None:
        super().__init__(message)
        self.partial_outputs = partial_outputs


ProgressCallback = Callable[[int, str], None]


class ConverterBackend(Protocol):
    def preflight(self, request: PilotRequest) -> PreflightReport: ...

    def convert(
        self,
        request: PilotRequest,
        progress: ProgressCallback,
        cancel_event: threading.Event,
    ) -> ConversionResult: ...


def load_config(path: Path | None = None) -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    config_path = path or APP_ROOT / "assets" / "pilot-config.json"
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("pilot config must be a JSON object")
        config.update(loaded)
    if config["language"] not in TEXT:
        raise ValueError("language must be 'ko' or 'en'")
    return config


def configured_log_path(config: dict[str, object]) -> Path:
    configured = Path(str(config["log_directory"]))
    log_directory = configured if configured.is_absolute() else APP_ROOT / configured
    return log_directory / "pilot-ui.log"


def configure_logging(config: dict[str, object]) -> Path:
    log_path = configured_log_path(config)
    log_directory = log_path.parent
    log_directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=int(config["log_max_bytes"]),
        backupCount=int(config["log_backup_count"]),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("converter.pilot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(handler)
    return log_path


def copy_text_to_clipboard(
    text: str,
    clear: Callable[[], None],
    append: Callable[[str], None],
) -> int:
    """Copy an exact screen-log snapshot through injectable clipboard operations."""
    clear()
    append(text)
    return len(text)


def format_output_paths(
    outputs: OutputPaths,
    *,
    published_only: bool = False,
    text: dict[str, str] | None = None,
) -> str:
    labels = text or TEXT["ko"]
    q_label = labels["output_q_target"]
    not_published = labels["output_not_published"]
    q_paths = outputs.q_imports or (outputs.q_import,)
    if published_only:
        q_paths = tuple(path for path in q_paths if path.is_file() and path not in outputs.import_reviews)
    q_text = "\n".join(f"{q_label}: {path}" for path in q_paths) or f"{q_label}: {not_published}"
    review_paths = outputs.partial_reviews
    if published_only:
        review_paths = tuple(path for path in review_paths if path.is_file())
    review_text = "\n".join(f"미변환 요소 TXT (GX Works2 import 금지): {path}" for path in review_paths)
    if not review_text:
        review_text = "미변환 요소 TXT (GX Works2 import 금지): 발행 없음"
    mining_paths = outputs.mining_reviews
    if published_only:
        mining_paths = tuple(path for path in mining_paths if path.is_file())
    mining_text = "\n".join(
        f"마이닝 검토 CSV (MINING_REVIEW_ONLY, 다운로드 및 RUN 금지): {path}"
        for path in mining_paths
    )
    if not mining_text:
        mining_text = "마이닝 검토 CSV (MINING_REVIEW_ONLY, 다운로드 및 RUN 금지): 발행 없음"

    typed_mining_paths = outputs.typed_mining_reviews
    if published_only:
        typed_mining_paths = tuple(path for path in typed_mining_paths if path.is_file())
    typed_mining_text = "\n".join(
        f"타입 확인 마이닝 검토 CSV (TYPED_MINING_REVIEW_ONLY, 다운로드/RUN 금지): {path}"
        for path in typed_mining_paths
    )
    if not typed_mining_text:
        typed_mining_text = "타입 확인 마이닝 검토 CSV (TYPED_MINING_REVIEW_ONLY, 다운로드/RUN 금지): 발행 없음"

    source_recovered_paths = outputs.source_recovered_reviews
    if published_only:
        source_recovered_paths = tuple(path for path in source_recovered_paths if path.is_file())
    source_recovered_text = "\n".join(
        f"Source recovery review CSV (SOURCE_RECOVERED_REVIEW_ONLY, DO_NOT_DOWNLOAD_OR_RUN): {path}"
        for path in source_recovered_paths
    ) or "Source recovery review CSV (SOURCE_RECOVERED_REVIEW_ONLY, DO_NOT_DOWNLOAD_OR_RUN): not published"
    structural_paths = outputs.structural_reviews
    if published_only:
        structural_paths = tuple(path for path in structural_paths if path.is_file())
    structural_text = "\n".join(
        f"Structural review CSV (STRUCTURAL_RESULT_DUMMY_FALLBACK, DO_NOT_DOWNLOAD_OR_RUN): {path}"
        for path in structural_paths
    ) or "Structural review CSV (STRUCTURAL_RESULT_DUMMY_FALLBACK, DO_NOT_DOWNLOAD_OR_RUN): not published"
    import_paths = outputs.import_reviews
    if published_only:
        import_paths = tuple(path for path in import_paths if path.is_file())
    import_text = "\n".join(
        f"검토용 CSV (REVIEW_ONLY, DO_NOT_DOWNLOAD_OR_RUN): {path}" for path in import_paths
    ) or "검토용 CSV (REVIEW_ONLY, DO_NOT_DOWNLOAD_OR_RUN): not published"

    def diagnostic(label: str, path: Path) -> str:
        if published_only and not path.is_file():
            return f"{label}: {not_published}"
        return f"{label}: {path}"

    return (
        f"{diagnostic(labels['output_normalized_source'], outputs.normalized)}\n"
        f"{q_text}\n"
        f"{review_text}\n"
        f"{mining_text}\n"
        f"{typed_mining_text}\n"
        f"{source_recovered_text}\n"
        f"{structural_text}\n"
        f"{import_text}\n"
        f"{diagnostic('코멘트 CSV', outputs.comment)}\n"
        f"{diagnostic('코멘트 검토 CSV', outputs.comment_review)}\n"
        f"{diagnostic(labels['output_comment_review_text'], outputs.comment_review_text)}\n"
        f"{diagnostic('변환 보고서', outputs.report)}\n"
        f"{diagnostic('invalid CSV', outputs.invalid)}\n"
        f"{diagnostic(labels['output_invalid_text'], outputs.invalid_text)}"
    )


def format_comment_filter_warning(report_path: Path, text: dict[str, str]) -> str:
    """Describe target-device exclusions recorded by the conversion report."""
    if not report_path.is_file():
        return ""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        comments = report["comments"]
        excluded = int(comments["excluded_target_incompatible_devices"])
        total = int(comments["total"])
        import_rows = int(comments["import_rows"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return ""
    if excluded <= 0:
        return ""
    return text["comment_filter_warning"].format(
        total=total,
        import_rows=import_rows,
        excluded=excluded,
    )


def format_completion_summary(
    report_path: Path,
    processed: int,
    invalid: int,
    text: dict[str, str],
) -> str:
    """Separate complete artifact handling from normal-Q semantic acceptance."""
    fallback = text["partial_complete"].format(processed=processed, invalid=invalid)
    if not report_path.is_file():
        return fallback
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        publication = report["status"]["publication"]
        summary = report["publication_summary"]
        if publication not in {"COMPLETE_NORMAL", "COMPLETE_WITH_REVIEW"}:
            return fallback
        if summary["all_pous_have_artifacts"] is not True:
            return fallback
        counts = tuple(summary[key] for key in (
            "normal_q_pou_count", "import_review_pou_count", "ledger_only_pou_count",
        ))
        if any(type(value) is not int or value < 0 for value in counts):
            return fallback
        normal, import_review, ledger_only = counts
        if normal + import_review + ledger_only == 0:
            return fallback
        missing_names = [
            item["name"] for item in report.get("programs", [])
            if not item.get("q_output") and not item.get("import_review_output")
        ]
        if ledger_only and (
            len(missing_names) != ledger_only
            or any(not isinstance(name, str) or not name.strip() for name in missing_names)
            or len(set(missing_names)) != len(missing_names)
        ):
            return fallback
        missing = ", ".join(missing_names)
        fallback_count = report.get("diagnostic_fallback", {}).get("official_dummy_fallback_count", 0)
        if type(fallback_count) is not int or fallback_count < 0:
            return fallback
        key = "import_incomplete" if ledger_only else (
            ("import_complete_fallback" if fallback_count else "import_complete_review")
            if import_review else "import_complete"
        )
        return text[key].format(
            processed=processed, invalid=invalid, normal=normal,
            import_review=import_review, ledger_only=ledger_only,
            missing=missing, fallback_count=fallback_count,
        )
    except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError, OSError):
        return fallback


INPUT_EXTENSION_ERROR = "입력 파일 확장자는 .gx3이어야 합니다."
INPUT_FILE_ERROR = "입력 파일이 존재하는 일반 파일이어야 합니다."
INPUT_SINGLE_ERROR = "GX3 파일 하나만 선택할 수 있습니다."
DROP_PARSE_ERROR = "드롭한 경로를 해석할 수 없습니다."
OUTPUT_NOT_DIRECTORY_ERROR = "출력 폴더가 존재하는 디렉터리여야 합니다."
OUTPUT_NOT_WRITABLE_ERROR = "출력 폴더에 쓰기 권한이 없습니다."
OUTPUT_PARENT_ERROR = "출력 폴더를 만들 수 있는 상위 폴더가 없습니다."
SAME_SOURCE_FOLDER_ERROR = "원본 GX3와 같은 폴더는 출력 폴더로 사용할 수 없습니다."


def review_view_path(csv_path: Path) -> tuple[Path | None, str]:
    """Prefer an existing viewing companion without changing legacy results."""
    text_path = companion_text_path(csv_path)
    if text_path.is_file():
        return text_path, ""
    if csv_path.is_file():
        return csv_path, "review_text_fallback"
    return None, "review_text_missing"


def normalize_user_path(value: str) -> Path:
    """Normalize a pasted Windows path without changing filesystem identity."""
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    if os.name == "nt":
        normalized = normalized.replace("/", os.sep).replace("\\", os.sep)
    return Path(os.path.normpath(normalized))


def resolve_application_directory(
    *,
    frozen: bool | None = None,
    executable: Path | None = None,
) -> Path:
    """Return the portable or source application directory used for default output."""
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        exe = Path(sys.executable) if executable is None else executable
        return exe.resolve().parent
    return APP_ROOT


def default_output_directory(input_path: Path, app_dir: Path | None = None) -> Path:
    """Return `<application_directory>/result/<input stem>` without creating it."""
    base = app_dir if app_dir is not None else resolve_application_directory()
    return base.resolve() / "result" / input_path.stem


def nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def parse_drop_payload(
    payload: str,
    splitlist: Callable[[str], Sequence[str]],
) -> tuple[tuple[str, ...], str]:
    """Parse a Tk DND_FILES payload with the injected Tcl splitlist."""
    try:
        items = tuple(str(item) for item in splitlist(payload))
    except Exception:
        return (), DROP_PARSE_ERROR
    if not items:
        return (), DROP_PARSE_ERROR
    return items, ""


def selected_gx3_from_paths(raw_paths: Sequence[str]) -> tuple[Path | None, str]:
    """Accept exactly one existing regular .gx3 file and return its resolved path."""
    if len(raw_paths) != 1:
        return None, INPUT_SINGLE_ERROR
    path = normalize_user_path(raw_paths[0])
    if path.suffix.lower() != ".gx3":
        return None, INPUT_EXTENSION_ERROR
    if not path.is_file():
        return None, INPUT_FILE_ERROR
    return path.resolve(), ""


def validate_request(request: PilotRequest) -> tuple[str, ...]:
    errors: list[str] = []
    source = request.input_path
    destination = request.output_directory
    if source.suffix.lower() != ".gx3":
        errors.append(INPUT_EXTENSION_ERROR)
    if not source.is_file():
        errors.append(INPUT_FILE_ERROR)
    if destination.is_dir():
        if not os.access(destination, os.W_OK):
            errors.append(OUTPUT_NOT_WRITABLE_ERROR)
    elif destination.exists():
        errors.append(OUTPUT_NOT_DIRECTORY_ERROR)
    else:
        parent = nearest_existing_parent(destination)
        if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK):
            errors.append(OUTPUT_PARENT_ERROR)
    if source.is_file() and source.resolve().parent == destination.resolve():
        errors.append(SAME_SOURCE_FOLDER_ERROR)
    return tuple(errors)


def build_output_paths(request: PilotRequest) -> OutputPaths:
    return OutputPaths(
        normalized=request.output_directory / "normalized.csv",
        q_import=request.output_directory / "q-import.csv",
        comment=request.output_directory / "comment.csv",
        comment_review=request.output_directory / "comment-review.csv",
        report=request.output_directory / "conversion-report.json",
        invalid=request.output_directory / "invalid.csv",
    )


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if callable(isjunction) and isjunction(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)
    except OSError:
        return True


def validate_clearable_output_directory(request: PilotRequest, app_dir: Path | None = None) -> None:
    """Fail closed unless the exact selected output tree is safe to remove."""
    errors = validate_request(request)
    if errors:
        raise ValueError("; ".join(errors))
    target = request.output_directory
    if not target.is_dir():
        raise ValueError("삭제 대상이 존재하는 디렉터리가 아닙니다.")
    if target.parent == target:
        raise ValueError("드라이브 또는 공유 루트는 삭제할 수 없습니다.")
    if _is_link_like(target):
        raise ValueError("심볼릭 링크 또는 정션 폴더는 삭제할 수 없습니다.")
    expected = default_output_directory(request.input_path, app_dir)
    if target.resolve() != expected.resolve():
        raise ValueError(f"자동 삭제는 기본 프로젝트 결과 폴더만 허용합니다: {expected}")
    source = request.input_path.resolve()
    resolved_target = target.resolve()
    if resolved_target == source or resolved_target in source.parents:
        raise ValueError("원본 GX3를 포함하는 상위 폴더는 삭제할 수 없습니다.")

    current = target
    expected_root = expected.parent.resolve()
    while True:
        if current.exists() and _is_link_like(current):
            raise ValueError(f"링크형 경로를 통과하므로 삭제할 수 없습니다: {current}")
        if current.resolve() == expected_root:
            break
        if current.parent == current:
            raise ValueError("프로젝트 결과 루트 경계를 확인할 수 없습니다.")
        current = current.parent

    pending = [target]
    while pending:
        current = pending.pop()
        try:
            entries = tuple(os.scandir(current))
        except OSError as error:
            raise ValueError(f"삭제 대상 폴더를 검사할 수 없습니다: {error}") from error
        for entry in entries:
            entry_path = Path(entry.path)
            if _is_link_like(entry_path):
                raise ValueError(f"링크형 항목이 포함되어 삭제할 수 없습니다: {entry_path}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry_path)


def quarantine_existing_output_directory(request: PilotRequest, app_dir: Path | None = None) -> Path:
    """Atomically move the exact validated output aside until conversion succeeds."""
    validate_clearable_output_directory(request, app_dir)
    target = request.output_directory
    quarantine = target.parent / f".converter-previous-{target.name}-{uuid.uuid4().hex}"
    if quarantine.exists():
        raise ValueError(f"안전 격리 경로가 이미 있습니다: {quarantine}")
    target.replace(quarantine)
    return quarantine


def _with_core_q_outputs(
    core: object,
    request: PilotRequest,
    outputs: OutputPaths,
    *,
    project: object | None = None,
) -> OutputPaths:
    planner = getattr(core, "planned_q_outputs", None)
    if not callable(planner):
        return outputs
    if project is None:
        q_imports = tuple(planner(request.input_path, outputs.q_import))
    else:
        q_imports = tuple(planner(request.input_path, outputs.q_import, project=project))
    if not q_imports:
        raise ValueError("core returned an empty Q output plan")
    review_planner = getattr(core, "planned_review_outputs", None)
    if callable(review_planner):
        partial_reviews = tuple(review_planner(request.input_path, outputs.q_import, project=project))
    else:
        partial_reviews = tuple(path.with_name(f"{path.stem}.txt") for path in q_imports)
    mining_planner = getattr(core, "planned_mining_review_outputs", None)
    if callable(mining_planner):
        mining_reviews = tuple(mining_planner(request.input_path, outputs.q_import, project=project))
    else:
        mining_reviews = tuple(path.with_name(f"{path.stem}.mining-review.csv") for path in q_imports)
    typed_mining_planner = getattr(core, "planned_typed_mining_review_outputs", None)
    if callable(typed_mining_planner):
        typed_mining_reviews = tuple(typed_mining_planner(request.input_path, outputs.q_import, project=project))
    else:
        typed_mining_reviews = tuple(path.with_name(f"{path.stem}.typed-mining-review.csv") for path in q_imports)
    source_recovered_planner = getattr(core, "planned_source_recovered_review_outputs", None)
    if callable(source_recovered_planner):
        source_recovered_reviews = tuple(source_recovered_planner(request.input_path, outputs.q_import, project=project))
    else:
        source_recovered_reviews = tuple(path.with_name(f"{path.stem}.source-recovered-review.csv") for path in q_imports)
    structural_planner = getattr(core, "planned_structural_review_outputs", None)
    if callable(structural_planner):
        structural_reviews = tuple(structural_planner(request.input_path, outputs.q_import, project=project))
    else:
        structural_reviews = tuple(path.with_name(f"{path.stem}.structural-review.csv") for path in q_imports)
    import_planner = getattr(core, "planned_import_review_outputs", None)
    import_reviews = tuple(import_planner(request.input_path, outputs.q_import, project=project)) if callable(import_planner) else ()
    return OutputPaths(
        outputs.normalized,
        q_imports[0],
        outputs.comment,
        outputs.comment_review,
        outputs.report,
        outputs.invalid,
        q_imports,
        partial_reviews,
        mining_reviews,
        typed_mining_reviews,
        source_recovered_reviews,
        structural_reviews,
        import_reviews,
    )


class CoreBackend:
    """Lazy adapter around the converter core; tkinter remains independently testable."""

    def _core(self):
        return importlib.import_module("converter.core")

    def preflight(self, request: PilotRequest) -> PreflightReport:
        errors = list(validate_request(request))
        outputs = build_output_paths(request)
        existing = [str(path) for path in outputs.diagnostics() if path.exists()]
        if existing:
            errors.append("기존 결과 파일 덮어쓰기를 차단했습니다: " + ", ".join(existing))
        if errors:
            return PreflightReport(False, "기본 입력 검증 실패", tuple(errors))
        try:
            core = self._core()
            project = core.read_gx3(request.input_path)
            outputs = _with_core_q_outputs(core, request, outputs, project=project)
            existing = [str(path) for path in outputs.all() if path.exists()]
            if existing:
                return PreflightReport(
                    False,
                    "기본 입력 검증 실패",
                    ("기존 결과 파일 덮어쓰기를 차단했습니다: " + ", ".join(existing),),
                )
            core._validate_output_paths(request.input_path, outputs.all())
            validator = getattr(core, "validate_project", None)
            invalid = validator(project) if callable(validator) else core.validate_records(project.instructions)
            comment_invalid = core.validate_comments(project.comments)
            invalid.extend(comment_invalid)
            if any(row[5] == "COMMENT_ENCODING_REQUIRED" for row in comment_invalid):
                truncated_comments = 0
            else:
                projections = core.project_comments(project.comments)
                truncated_comments = sum(projection.truncated for projection in projections)
        except Exception as error:
            return PreflightReport(False, "GX3 구조 또는 지원 경계 점검 실패", (str(error),))
        if invalid:
            details = tuple(" | ".join(row) for row in invalid[:20])
            if len(invalid) > 20:
                details += (f"추가 오류 {len(invalid) - 20}건",)
            return PreflightReport(
                True,
                f"검토 필요 항목 {len(invalid)}건. 전체 검사 후 차단 finding이 없는 POU만 발행",
                details,
                partial=True,
            )
        return PreflightReport(
            True,
            f"지원 범위 통과: {len(project.instructions)}행, 코멘트 {len(project.comments)}건, 프로그램 {project.program_name}",
            (
                "파일 내부 관계와 구조 계약을 통과한 단일 또는 복수 POU Ladder 지원",
                f"GX Works2 CP949 32바이트 초과 코멘트 {truncated_comments}건은 안전하게 축약하고 원문 검토 CSV와 열람용 TXT에 보존",
                "검증된 root DB의 모든 영어 표시 디바이스 코멘트를 함께 출력",
                "파일명이 아니라 미해석 구조, 명령, 피연산자만 해당 범위에서 격리",
            ),
        )

    def convert(
        self,
        request: PilotRequest,
        progress: ProgressCallback,
        cancel_event: threading.Event,
    ) -> ConversionResult:
        progress(5, "입력과 출력 경로 확인")
        errors = list(validate_request(request))
        outputs = build_output_paths(request)
        existing = [str(path) for path in outputs.diagnostics() if path.exists()]
        if existing:
            errors.append("기존 결과 파일 덮어쓰기를 차단했습니다: " + ", ".join(existing))
        if errors:
            raise PilotBlockedError("기본 입력 검증 실패: " + "; ".join(errors))
        if cancel_event.is_set():
            raise InterruptedError("변환 시작 전에 취소되었습니다.")
        progress(30, "기본 경로 검증 통과")
        core = self._core()
        try:
            outputs = _with_core_q_outputs(core, request, outputs)
        except core.ConversionError:
            pass
        planned_paths = outputs.all() if outputs.q_imports else outputs.diagnostics()
        existing = [str(path) for path in planned_paths if path.exists()]
        if existing:
            raise PilotBlockedError("기존 결과 파일 덮어쓰기를 차단했습니다: " + ", ".join(existing))
        progress(55, "변환 결과와 열람용 TXT 생성 중")
        try:
            processed, invalid = core.convert(
                request.input_path,
                request.input_path.stem,
                outputs.normalized,
                outputs.q_import,
                outputs.invalid,
                cancel_check=cancel_event.is_set,
                comment_output=outputs.comment,
                comment_review_output=outputs.comment_review,
                report_output=outputs.report,
            )
        except core.ConversionCancelled as error:
            raise InterruptedError(str(error)) from error
        except core.PartialPublishError as error:
            partial_outputs = tuple(path for path in outputs.all() if path.exists())
            raise PilotPartialPublishError(str(error), partial_outputs) from error
        except core.ConversionError as error:
            invalid_output = outputs.invalid if outputs.invalid.is_file() else None
            raise PilotBlockedError(str(error), invalid_output) from error
        planned_q = outputs.q_imports or (outputs.q_import,)
        if planned_q:
            published_normal_names, published_review_names = _published_csv_names(outputs)
            published_q = tuple(path for path in planned_q if path.is_file() and path.name in published_normal_names)
            published_reviews = tuple(path for path in outputs.partial_reviews if path.is_file())
            published_mining_reviews = tuple(path for path in outputs.mining_reviews if path.is_file())
            published_typed_mining_reviews = tuple(path for path in outputs.typed_mining_reviews if path.is_file())
            published_source_recovered_reviews = tuple(path for path in outputs.source_recovered_reviews if path.is_file())
            published_structural_reviews = tuple(path for path in outputs.structural_reviews if path.is_file())
            published_import_reviews = tuple(path for path in outputs.import_reviews if path.is_file() and path.name in published_review_names)
            outputs = OutputPaths(
                outputs.normalized,
                published_q[0] if published_q else outputs.q_import,
                outputs.comment,
                outputs.comment_review,
                outputs.report,
                outputs.invalid,
                published_q,
                published_reviews,
                published_mining_reviews,
                published_typed_mining_reviews,
                published_source_recovered_reviews,
                published_structural_reviews,
                published_import_reviews,
            )
        progress(100, "변환 부분 완료" if invalid else "변환 완료")
        return ConversionResult(processed, invalid, outputs)


def _published_csv_names(outputs: OutputPaths) -> tuple[set[str], set[str]]:
    """Classify only explicit, disjoint publication entries from the report."""
    try:
        report = json.loads(outputs.report.read_text(encoding="utf-8"))
        programs = report["programs"]
        if not isinstance(programs, list) or not programs:
            raise ValueError("Missing program publication entries")
        normal: set[str] = set()
        review: set[str] = set()
        program_names: set[str] = set()
        target_names: set[str] = set()
        planned = {path.name for path in (*(outputs.q_imports or (outputs.q_import,)), *outputs.import_reviews)}
        for item in programs:
            name, target = item["name"], item["target_name"]
            q_name, review_name = item["q_output"], item["import_review_output"]
            if (
                not isinstance(name, str) or not name or name in program_names
                or not isinstance(target, str) or not target or target in target_names
                or not isinstance(q_name, str) or not isinstance(review_name, str)
                or (q_name and review_name)
            ):
                raise ValueError("Ambiguous program publication")
            program_names.add(name)
            target_names.add(target)
            for output_name, destination in ((q_name, normal), (review_name, review)):
                if not output_name:
                    continue
                if (
                    output_name != f"{target}.csv" or Path(output_name).name != output_name
                    or output_name not in planned or output_name in normal or output_name in review
                    or not (outputs.report.parent / output_name).is_file()
                    or (destination is normal and item.get("status") != "CONVERTED")
                ):
                    raise ValueError("Invalid CSV publication entry")
                destination.add(output_name)
        existing = {path.name for path in (*(outputs.q_imports or (outputs.q_import,)), *outputs.import_reviews) if path.is_file()}
        if existing != normal | review:
            raise ValueError("Unlisted CSV publication")
        return normal, review
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError("Cannot classify published CSV without a valid conversion report") from error


def create_worker(target: Callable[[], None], name: str) -> threading.Thread:
    """Create a process-lifetime worker so shutdown cannot interrupt file publication."""
    return threading.Thread(target=target, name=name, daemon=False)


class PilotApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Tk,
        backend: ConverterBackend | None = None,
        config: dict[str, object] | None = None,
    ) -> None:
        self.config_data = config or load_config()
        self.text = TEXT[str(self.config_data["language"])]
        super().__init__(master, padding=16)
        self.master = master
        self.backend = backend or CoreBackend()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.started_at = 0.0
        self.last_output_directory: Path | None = None
        self.last_invalid_output: Path | None = None
        self.last_comment_review_output: Path | None = None
        self.last_conversion_result: ConversionResult | None = None
        self.last_operation_outcome = "IDLE"
        self.previous_output_backups: list[tuple[Path, Path]] = []
        self.findings: tuple[Finding, ...] = ()
        self.dirty = False
        self.close_after_worker = False
        self.logger = logging.getLogger("converter.pilot")
        self.log_path = configured_log_path(self.config_data)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value=self.text["idle"])
        self.progress_var = tk.IntVar(value=0)
        self.eta_var = tk.StringVar(value="ETA: -")
        self.boundary_var = tk.StringVar(
            value="승인된 원본 프로필만 변환합니다. 복수 POU 승인 원본은 프로그램별 Q CSV로 분리합니다."
        )
        self.finding_filter_var = tk.StringVar(value=self.text["filter_all"])
        self.finding_summary_vars = [tk.StringVar() for _ in range(3)]
        self.input_var.trace_add("write", self._mark_dirty)
        self.output_var.trace_add("write", self._mark_dirty)

        self._build()
        self.master.title(application_title(self.text["title"]))
        self.master.minsize(1024, 720)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(int(self.config_data["queue_poll_ms"]), self._poll_events)

    def _build(self) -> None:
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(8, weight=1)

        ttk.Label(self, text=self.text["input"]).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.input_entry = ttk.Entry(self, textvariable=self.input_var)
        self.input_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Button(self, text=self.text["browse_file"], command=self._choose_input, width=16).grid(row=1, column=2, ipady=8)

        ttk.Label(self, text=self.text["output"]).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 4))
        ttk.Entry(self, textvariable=self.output_var).grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Button(self, text=self.text["browse_folder"], command=self._choose_output, width=16).grid(row=3, column=2, ipady=8)

        action = ttk.Frame(self)
        action.grid(row=4, column=0, columnspan=3, sticky="ew", pady=14)
        for index in range(8):
            action.columnconfigure(index, weight=1)
        self.preflight_button = ttk.Button(action, text=self.text["preflight"], command=self._start_preflight)
        self.preflight_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=10)
        self.convert_button = ttk.Button(action, text=self.text["convert"], command=self._start_convert)
        self.convert_button.grid(row=0, column=1, sticky="ew", padx=6, ipady=10)
        self.cancel_button = ttk.Button(action, text=self.text["cancel"], command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=2, sticky="ew", padx=6, ipady=10)
        self.open_button = ttk.Button(action, text=self.text["open_folder"], command=self._open_folder, state="disabled")
        self.open_button.grid(row=0, column=3, sticky="ew", padx=(6, 0), ipady=10)
        self.open_invalid_button = ttk.Button(
            action, text=self.text["open_invalid"], command=self._open_invalid, state="disabled"
        )
        self.open_invalid_button.grid(row=0, column=4, sticky="ew", padx=(6, 0), ipady=10)
        self.open_comment_review_button = ttk.Button(
            action, text=self.text["open_comment_review"], command=self._open_comment_review, state="disabled"
        )
        self.open_comment_review_button.grid(row=0, column=5, sticky="ew", padx=(6, 0), ipady=10)
        self.reverse_button = ttk.Button(action, text="Q → FX5 후보", command=self._open_reverse)
        self.reverse_button.grid(row=0, column=6, sticky="ew", padx=(6, 0), ipady=10)
        self.forward_map_button = ttk.Button(action, text="FX5 → Q X/Y 매핑", command=self._open_forward_map)
        self.forward_map_button.grid(row=0, column=7, sticky="ew", padx=(6, 0), ipady=10)

        ttk.Label(self, textvariable=self.status_var, wraplength=650, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(self, textvariable=self.eta_var).grid(row=5, column=2, sticky="e")
        ttk.Progressbar(self, variable=self.progress_var, maximum=100).grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 12))

        boundary = ttk.LabelFrame(self, text=self.text["boundary"], padding=10)
        boundary.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(boundary, textvariable=self.boundary_var, wraplength=760, justify="left").grid(sticky="ew")

        self.result_split = ttk.Panedwindow(self, orient=RESULT_SPLIT_ORIENT)
        self.result_split.grid(row=8, column=0, columnspan=3, sticky="nsew")

        finding_frame = ttk.LabelFrame(self.result_split, text=self.text["findings"], padding=8)
        finding_frame.columnconfigure(0, weight=1)
        finding_frame.rowconfigure(2, weight=1)
        summary_frame = ttk.Frame(finding_frame)
        summary_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        summary_keys = ("summary_total", "summary_unsupported", "summary_general")
        for index, key in enumerate(summary_keys):
            summary_frame.columnconfigure(index, weight=1)
            ttk.Label(summary_frame, textvariable=self.finding_summary_vars[index], anchor="center", relief="ridge", padding=8).grid(
                row=0, column=index, sticky="ew", padx=(0 if index == 0 else 4, 0)
            )
            self.finding_summary_vars[index].set(f"{self.text[key]}: 0")
        filter_frame = ttk.Frame(finding_frame)
        filter_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(filter_frame, text=self.text["finding_filter"]).grid(row=0, column=0, padx=(0, 8))
        filter_values = (
            self.text["filter_all"], self.text["filter_q_target"], self.text["filter_general"],
            "TARGET_UNSUPPORTED",
        )
        self.finding_filter = ttk.Combobox(
            filter_frame, textvariable=self.finding_filter_var, values=filter_values, state="readonly", width=28
        )
        self.finding_filter.grid(row=0, column=1, sticky="w")
        self.finding_filter.bind("<<ComboboxSelected>>", self._apply_finding_filter)
        columns = ("program", "source_step", "instruction", "operands", "finding_code", "reason", "required_action")
        self.finding_table = ttk.Treeview(finding_frame, columns=columns, show="headings", height=7)
        widths = (90, 80, 80, 180, 170, 290, 300)
        for column, width in zip(columns, widths):
            self.finding_table.heading(column, text=self.text[column])
            self.finding_table.column(column, width=width, minwidth=70, stretch=column in {"operands", "reason", "required_action"})
        self.finding_table.grid(row=2, column=0, sticky="nsew")
        finding_scroll = ttk.Scrollbar(finding_frame, orient="vertical", command=self.finding_table.yview)
        finding_scroll.grid(row=2, column=1, sticky="ns")
        finding_horizontal = ttk.Scrollbar(finding_frame, orient="horizontal", command=self.finding_table.xview)
        finding_horizontal.grid(row=3, column=0, sticky="ew")
        self.finding_table.configure(
            yscrollcommand=finding_scroll.set,
            xscrollcommand=finding_horizontal.set,
        )

        log_frame = ttk.LabelFrame(self.result_split, text=self.text["log"], padding=8)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        log_actions = ttk.Frame(log_frame)
        log_actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(log_actions, text=self.text["copy_log"], command=self._copy_log).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(log_actions, text=self.text["open_log"], command=self._open_log_file).grid(row=0, column=1)
        create_vertical_result_split(self.result_split, finding_frame, log_frame)

    def _mark_dirty(self, *_args: object) -> None:
        self.dirty = True

    def _open_reverse(self) -> None:
        from app.reverse_dialog import ReverseDialog

        ReverseDialog(self.master)

    def _open_forward_map(self) -> None:
        from app.forward_map_dialog import ForwardMapDialog

        ForwardMapDialog(self.master)

    def _request(self) -> PilotRequest:
        return PilotRequest(normalize_user_path(self.input_var.get()), normalize_user_path(self.output_var.get()))

    def _report_input_selection_error(self, message: str) -> None:
        self.status_var.set(message)
        self._append_log(message, level=logging.ERROR)

    def _select_input_paths(self, raw_paths: Sequence[str]) -> None:
        path, error = selected_gx3_from_paths(raw_paths)
        if error or path is None:
            self._report_input_selection_error(error)
            return
        self.input_var.set(str(path))
        self.output_var.set(str(default_output_directory(path)))

    def _choose_input(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[("GX Works3", "*.gx3"), ("All files", "*.*")])
        if selected:
            self._select_input_paths((selected,))

    def _on_files_dropped(self, event: object) -> str:
        payload = str(getattr(event, "data", ""))
        widget = getattr(event, "widget", None)
        splitlist = getattr(getattr(widget, "tk", None), "splitlist", None)
        if splitlist is None:
            self._report_input_selection_error(DROP_PARSE_ERROR)
            return "copy"
        raw_paths, error = parse_drop_payload(payload, splitlist)
        if error:
            self._report_input_selection_error(error)
            return "copy"
        self._select_input_paths(raw_paths)
        return "copy"

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_var.set(selected)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.preflight_button.configure(state=state)
        self.convert_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _confirm_clear_existing_output(self, request: PilotRequest) -> bool:
        target = request.output_directory
        if not target.exists():
            return True
        try:
            validate_clearable_output_directory(request)
        except ValueError as error:
            message = self.text["replace_failed"].format(error=error)
            self.status_var.set(message)
            self._append_log(message, level=logging.ERROR)
            return False
        if not messagebox.askyesno(
            self.text["confirm_replace_title"],
            self.text["confirm_replace"].format(path=target.resolve()),
            default=messagebox.NO,
        ):
            message = self.text["replace_cancelled"]
            self.status_var.set(message)
            self._append_log(message)
            return False
        try:
            quarantine = quarantine_existing_output_directory(request)
        except (OSError, ValueError) as error:
            message = self.text["replace_failed"].format(error=error)
            self.status_var.set(message)
            self._append_log(message, level=logging.ERROR)
            return False
        backups = getattr(self, "previous_output_backups", None)
        if backups is None:
            self.previous_output_backups = []
        self.previous_output_backups.append((target, quarantine))
        self.last_output_directory = None
        self.last_invalid_output = None
        self.last_comment_review_output = None
        self.open_button.configure(state="disabled")
        self.open_invalid_button.configure(state="disabled")
        self.open_comment_review_button.configure(state="disabled")
        self._append_log(self.text["replace_cleared"].format(path=target))
        self._append_log(self.text["replace_retained"].format(path=quarantine))
        return True

    def _start_worker(self, operation: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        request = self._request()
        if not self._confirm_clear_existing_output(request):
            return
        backups_to_cleanup = tuple(
            backup
            for original, backup in getattr(self, "previous_output_backups", ())
            if original == request.output_directory
        )
        if operation == "convert":
            self.last_conversion_result = None
            self.last_operation_outcome = "RUNNING"
            self.last_invalid_output = None
            self.last_comment_review_output = None
            self.open_invalid_button.configure(state="disabled")
            self.open_comment_review_button.configure(state="disabled")
            self._show_findings(())
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.eta_var.set("ETA: -")
        self.started_at = time.monotonic()
        self._set_busy(True)
        if operation == "preflight":
            self._append_log("사전 점검을 시작했습니다.")
        else:
            self._append_log("변환을 시작했습니다. 원본 GX3는 읽기 전용입니다.")

        def work() -> None:
            try:
                if operation == "preflight":
                    report = self.backend.preflight(request)
                    self.events.put(("preflight", report))
                else:
                    result = self.backend.convert(request, self._post_progress, self.cancel_event)
                    removed: list[Path] = []
                    retained: list[tuple[Path, str]] = []
                    for backup in backups_to_cleanup:
                        try:
                            shutil.rmtree(backup)
                            removed.append(backup)
                        except OSError as cleanup_error:
                            retained.append((backup, str(cleanup_error)))
                    if backups_to_cleanup:
                        self.events.put(("backup_cleanup", (tuple(removed), tuple(retained))))
                    self.events.put(("converted", result))
            except InterruptedError as error:
                self.events.put(("cancelled", str(error)))
            except PilotPartialPublishError as error:
                self.events.put(("partial_publish", error))
            except PilotBlockedError as error:
                findings: tuple[Finding, ...] = ()
                parse_error = ""
                if error.invalid_output and error.invalid_output.is_file():
                    try:
                        findings = parse_invalid_findings(error.invalid_output)
                    except Exception as finding_error:
                        parse_error = str(finding_error)
                self.events.put(("blocked", BlockedPresentation(error, findings, parse_error)))
            except Exception as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("idle", None))

        self.worker = create_worker(work, f"converter-{operation}")
        self.worker.start()

    def _start_preflight(self) -> None:
        self._start_worker("preflight")

    def _start_convert(self) -> None:
        self._start_worker("convert")

    def _post_progress(self, percent: int, message: str) -> None:
        self.events.put(("progress", (percent, message)))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._append_log("취소를 요청했습니다. 현재 단계 경계에서 반영합니다.")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    percent, message = payload  # type: ignore[misc]
                    self.progress_var.set(int(percent))
                    self.status_var.set(str(message))
                    elapsed = time.monotonic() - self.started_at
                    eta = elapsed * (100 - int(percent)) / int(percent) if int(percent) else 0
                    self.eta_var.set(f"ETA: {eta:.1f}s" if percent else "ETA: -")
                elif kind == "backup_cleanup":
                    removed, retained = payload  # type: ignore[misc]
                    removed_set = set(removed)
                    self.previous_output_backups = [
                        pair for pair in getattr(self, "previous_output_backups", []) if pair[1] not in removed_set
                    ]
                    for path, error in retained:
                        self._append_log(
                            self.text["replace_cleanup_failed"].format(path=path, error=error),
                            level=logging.WARNING,
                        )
                elif kind == "preflight":
                    report = payload
                    assert isinstance(report, PreflightReport)
                    boundary_text = format_preflight_boundary(report)
                    self.boundary_var.set(boundary_text)
                    self.status_var.set(f"사전 점검 {boundary_text}")
                    self._append_log(format_preflight_log(report))
                elif kind == "converted":
                    result = payload
                    assert isinstance(result, ConversionResult)
                    self.last_conversion_result = result
                    self.last_operation_outcome = "CONVERTED"
                    self.last_output_directory = result.outputs.q_import.parent
                    self.open_button.configure(state="normal")
                    self.dirty = False
                    if review_view_path(result.outputs.invalid)[0] is not None:
                        self.last_invalid_output = result.outputs.invalid
                        self.open_invalid_button.configure(state="normal")
                    if review_view_path(result.outputs.comment_review)[0] is not None:
                        self.last_comment_review_output = result.outputs.comment_review
                        self.open_comment_review_button.configure(state="normal")
                    paths = format_output_paths(
                        result.outputs,
                        published_only=True,
                        text=self.text,
                    )
                    comment_filter_warning = format_comment_filter_warning(result.outputs.report, self.text)
                    if result.invalid_rows > 0:
                        summary = format_completion_summary(
                            result.outputs.report,
                            result.processed_rows,
                            result.invalid_rows,
                            self.text,
                        )
                        message = f"{summary}\n{paths}"
                        self.status_var.set(summary)
                        self.boundary_var.set(summary)
                        self._append_log(message, level=logging.WARNING)
                        if comment_filter_warning:
                            self._append_log(comment_filter_warning, level=logging.WARNING)
                        if result.outputs.invalid.is_file():
                            try:
                                findings = parse_invalid_findings(result.outputs.invalid)
                            except Exception as finding_error:
                                findings = ()
                                self._append_log(
                                    f"invalid CSV 상세 읽기 실패: {finding_error}",
                                    level=logging.ERROR,
                                )
                            self._show_findings(findings)
                            unsupported_warning = format_unsupported_warning(findings, self.text)
                            if unsupported_warning:
                                self._append_log(unsupported_warning, level=logging.WARNING)
                    elif comment_filter_warning:
                        summary = format_completion_summary(
                            result.outputs.report, result.processed_rows, result.invalid_rows, self.text,
                        ) + f" {comment_filter_warning}"
                        message = f"{summary}\n{paths}"
                        self.status_var.set(summary)
                        self.boundary_var.set(summary)
                        self._append_log(message, level=logging.WARNING)
                    else:
                        summary = format_completion_summary(
                            result.outputs.report, result.processed_rows, result.invalid_rows, self.text,
                        )
                        self.status_var.set(summary)
                        self.boundary_var.set(summary)
                        self._append_log(f"{summary}\n{paths}")
                elif kind == "cancelled":
                    self.last_operation_outcome = "CANCELLED"
                    self.status_var.set(f"변환 취소: {payload}")
                    self._append_log(f"작업 취소: {payload}")
                elif kind == "partial_publish":
                    self.last_operation_outcome = "PARTIAL_PUBLISH"
                    partial = payload
                    assert isinstance(partial, PilotPartialPublishError)
                    message = f"변환 실패/부분 발행: {partial}"
                    self.status_var.set(message)
                    self.boundary_var.set(message)
                    self._append_log(message, level=logging.ERROR)
                    if partial.partial_outputs:
                        self.last_output_directory = partial.partial_outputs[0].parent
                        self.open_button.configure(state="normal")
                elif kind == "blocked":
                    self.last_operation_outcome = "BLOCKED"
                    presentation = payload
                    assert isinstance(presentation, BlockedPresentation)
                    blocked = presentation.error
                    invalid_line = f"\ninvalid CSV: {blocked.invalid_output}" if blocked.invalid_output else ""
                    self.status_var.set(f"변환 차단: {blocked}{invalid_line}")
                    self.boundary_var.set(f"변환 차단: {blocked}{invalid_line}")
                    self._append_log(f"변환 차단: {blocked}", level=logging.WARNING)
                    if blocked.invalid_output and blocked.invalid_output.is_file():
                        self.last_invalid_output = blocked.invalid_output
                        self.last_output_directory = blocked.invalid_output.parent
                        self.open_button.configure(state="normal")
                        self.open_invalid_button.configure(state="normal")
                        self._append_log(f"차단 보고서: {blocked.invalid_output}")
                    self._show_findings(presentation.findings)
                    unsupported_warning = format_unsupported_warning(presentation.findings, self.text)
                    if unsupported_warning:
                        self._append_log(unsupported_warning, level=logging.WARNING)
                    if presentation.parse_error:
                        self._append_log(f"invalid CSV 상세 읽기 실패: {presentation.parse_error}", level=logging.ERROR)
                elif kind == "error":
                    self.last_operation_outcome = "ERROR"
                    self.status_var.set(f"변환 실패: {payload}")
                    self._append_log(f"오류: {payload}", level=logging.ERROR)
                    self.boundary_var.set(f"변환 실패: {payload}")
                elif kind == "open_error":
                    self.status_var.set(f"열기 실패: {payload}")
                    self._append_log(f"열기 실패: {payload}", level=logging.ERROR)
                elif kind == "idle":
                    self._set_busy(False)
                    if self.close_after_worker:
                        self.master.destroy()
                        return
        except queue.Empty:
            pass
        self.after(int(self.config_data["queue_poll_ms"]), self._poll_events)

    def _append_log(self, message: str, level: int = logging.INFO) -> None:
        self.logger.log(level, message)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _copy_log(self) -> None:
        snapshot = self.log_text.get("1.0", "end-1c")
        copied = copy_text_to_clipboard(snapshot, self.master.clipboard_clear, self.master.clipboard_append)
        self.status_var.set(f"로그 복사 완료: {copied}자")

    def _show_findings(self, findings: tuple[Finding, ...]) -> None:
        self.findings = findings
        summary = summarize_findings(findings)
        values = (summary.q_target, summary.target_unsupported, summary.general)
        keys = ("summary_total", "summary_unsupported", "summary_general")
        for variable, key, value in zip(self.finding_summary_vars, keys, values):
            variable.set(f"{self.text[key]}: {value}")
        self._apply_finding_filter()

    def _apply_finding_filter(self, _event: object | None = None) -> None:
        labels = {
            self.text["filter_all"]: FINDING_FILTER_ALL,
            self.text["filter_q_target"]: FINDING_FILTER_Q_TARGET,
            self.text["filter_general"]: FINDING_FILTER_GENERAL,
        }
        category = labels.get(self.finding_filter_var.get(), self.finding_filter_var.get())
        for item in self.finding_table.get_children():
            self.finding_table.delete(item)
        for finding in filter_findings(self.findings, category):
            self.finding_table.insert("", "end", values=(
                finding.program, finding.source_step, finding.instruction, finding.operands,
                finding.finding_code, finding.reason, self.text[finding.action_key],
            ))

    def _start_open_path(self, path: Path, label: str) -> None:
        def open_path() -> None:
            try:
                if os.name == "nt":
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except Exception as error:
                self.events.put(("open_error", f"{label}: {error}"))

        threading.Thread(target=open_path, name="converter-open-path", daemon=True).start()

    def _open_folder(self) -> None:
        directory = self.last_output_directory
        if not directory:
            return
        self._start_open_path(directory, "결과 폴더")

    def _open_review(self, csv_path: Path | None, label: str) -> None:
        if csv_path is None:
            return
        path, notice = review_view_path(csv_path)
        if notice:
            self._append_log(self.text[notice].format(path=path or csv_path), level=logging.WARNING)
        if path is not None:
            self._start_open_path(path, label)

    def _open_invalid(self) -> None:
        self._open_review(self.last_invalid_output, self.text["open_invalid"])

    def _open_comment_review(self) -> None:
        self._open_review(self.last_comment_review_output, self.text["open_comment_review"])

    def _open_log_file(self) -> None:
        self._start_open_path(self.log_path, "로그 파일")

    def _on_close(self) -> None:
        from app.xy_settings_dialog import candidate_worker_running
        if candidate_worker_running(self.master):
            self.status_var.set('양방향 후보 작업 중이다. 완료 후 종료할 수 있다.')
            return
        running = bool(self.worker and self.worker.is_alive())
        if (running or self.dirty) and not messagebox.askyesno(self.text["title"], self.text["confirm_exit"]):
            return
        if running:
            self.cancel_event.set()
            self.close_after_worker = True
            self.status_var.set("취소 후 안전 종료 대기 중")
            self._append_log("종료를 요청했습니다. 작업 스레드 종료 후 창을 닫습니다.")
            return
        self.master.destroy()


def create_app_components(
    backend: ConverterBackend | None = None,
    config: dict[str, object] | None = None,
) -> tuple[tk.Tk, PilotApp]:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    root = TkinterDnD.Tk()
    app = PilotApp(root, backend=backend, config=config)
    for target in (root, app, app.input_entry):
        target.drop_target_register(DND_FILES)
        target.dnd_bind("<<Drop>>", app._on_files_dropped)
    return root, app


def create_app(
    backend: ConverterBackend | None = None,
    config: dict[str, object] | None = None,
) -> tk.Tk:
    if backend is not None:
        root, _app = create_app_components(backend=backend, config=config)
        return root
    from tkinterdnd2 import DND_FILES, TkinterDnD
    from app.unified_app import UnifiedApp

    root = TkinterDnD.Tk()
    app = UnifiedApp(root, config=config)
    for target in (root, app, app.source_entry):
        target.drop_target_register(DND_FILES)
        target.dnd_bind("<<Drop>>", app._on_files_dropped)
    return root
