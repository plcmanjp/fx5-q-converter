"""One Tk workflow for the existing FX5/Q conversion engines.

All source reads, conversion work and opening files run outside Tk's event
thread.  The widgets only consume immutable worker results tagged with a job
generation; a result from a prior selection cannot replace the current view.
"""
from __future__ import annotations

import os
import logging
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Mapping

from converter.xy_mapping import Bounds, Profile, fixed_bounds
from app.workflow_state import Outcome, SourceIdentity, Stage, StaleSourceError, WorkflowState


FX5_TO_Q = "FX5_TO_Q"
Q_TO_FX5 = "Q_TO_FX5"

LABELS = {
    "ko": {
        "title": "FX5/Q CSV 변환기", "source": "원본 파일", "choose": "파일 선택",
        "direction": "원본과 대상", "forward": "FX5 -> Q", "reverse": "Q -> FX5",
        "purpose": "작업 목적", "candidate_purpose": "FX5용 CSV 후보 만들기",
        "review_purpose": "Q 원본 검토용 CSV 추출",
        "complete_text": "전체 POU 변환 사용 (기본, 해제하면 제한된 이전 엔진 사용)",
        "flow": "1. 원본 파일 선택 → 2. 자동 준비 후 내용 확인 → 3. CSV 생성과 결과 확인",
        "simple_source": "1. 원본 파일 선택 (.gx3 / .gxw, 파일을 끌어놓아도 됩니다)",
        "simple_check": "2. 자동 준비 결과 확인", "simple_create": "3. CSV 생성",
        "advanced_show": "상세 설정 보기", "advanced_hide": "상세 설정 접기",
        "simple_confirm": "원본, 대상 CPU, 주소 방식과 저장 위치를 확인했습니다",
        "simple_summary": "원본: {source}\n방향: {direction} | 대상: {cpu} | 주소 방식: {mapping}",
        "prepare": "자동 준비 다시 하기", "prepared": "준비 완료. 위 내용을 확인하고 아래 확인란을 선택하세요.",
        "read_first": "먼저읽기 안내", "simple_result": "CSV 후보 생성 가능. GX 검증과 현장 수락은 미검증입니다.",
        "simple_created": "CSV 후보를 생성했습니다. GX 검증과 현장 수락은 미검증입니다.",
        "simple_fallback": "폴백 검토용입니다. 원래 기능을 수행하지 않습니다 (REVIEW).",
        "simple_hold": "보류 또는 가져오기 차단입니다. 진단에서 원인과 다음 조치를 확인하세요.",
        "simple_review": "검토가 필요한 결과입니다 (REVIEW). 진단과 원문 대조 자료를 확인하세요.",
        "relocation": "D/T/ZR 명시 배정 (Q -> FX5)",
        "relocation_load": "배정 JSON 선택", "relocation_clear": "배정 해제",
        "relocation_none": "D/T/ZR 배정표 미선택. 필요하면 원본에 결속된 승인 JSON을 선택하세요.",
        "relocation_applied": "적용 배정표: {name}\n{ranges}\n보류 코멘트: {held}개. 계획 점검에서 충돌과 대상 상한을 검사합니다.",
        "relocation_more": "외 {count}구간 (전체 {total}구간)",
        "relocation_conflict": "D/T/ZR 명시 배정과 ZR -> R 단독 설정은 함께 사용할 수 없습니다. 먼저 현재 설정을 해제하세요.",
        "identifier": "결과 이름", "output": "저장 위치", "folder": "폴더 선택",
        "summary": "요약", "usage": "원본 X/Y 사용 현황", "addresses": "주소 배정", "diagnostics": "진단", "details": "상세 설정",
        "usage_range": "원본 X/Y 구간", "usage_points": "실제 사용/구간 점수", "usage_width": "최대 접근 폭",
        "usage_program": "프로그램 참조", "usage_comments": "코멘트 참조", "usage_pous": "영향 POU", "usage_locators": "원본 위치",
        "usage_uninspected": "이 표는 X/Y만 표시합니다. M/D/L/B 등은 표 대상이 아닙니다. 판독하지 못한 범위: {count}건. 목록에 없는 주소를 미사용으로 간주하지 마세요.",
        "analyze": "원본 분석", "plan": "계획 점검", "publish": "CSV 생성",
        "publish_review": "Q 원본 검토 CSV 생성",
        "cancel": "취소", "confirm": "현재 대상 설정과 주소 배정을 확인했습니다",
        "idle": "원본을 선택하면 읽기 전용 분석을 시작합니다.",
        "unverified": "주소 공간 범위는 실제 장착 I/O나 사용 가능한 빈 주소를 뜻하지 않습니다. GX 및 현장 검증은 별도입니다.",
        "cpu_limits": "CPU와 대상 프로젝트 설정은 별도 확인이 필요합니다.", "project_limits": "주소 상한을 적용하지 않고 진수와 배정 충돌을 검사합니다.",
        "converter_limits": "변환기 지원: FX5U 및 Q의 검증된 입력 형식에 한정 (Q X/Y는 16진수, CPU 미확인)",
        "source_checked": "원본 분석 완료. 필요한 주소 배정을 확인한 뒤 계획을 점검하세요.",
        "source_review_checked": "Q 원본 분석 완료. 원본 검토 CSV 계획을 점검하세요.",
        "plan_ready": "계획 점검 완료. 현재 매핑을 확인하면 새 결과 폴더에 CSV를 생성합니다.",
        "review_plan_ready": "원본 검토 계획 점검 완료. Q 원본과 새 저장 위치를 확인한 뒤 검토 CSV를 생성하세요.",
        "settings": "주소 배정",
        "hold": "보류. 진단과 필요한 설정을 확인하세요.",
        "working": "작업 중입니다. 안전 지점에서 취소할 수 있습니다.",
        "cancel_requested": "취소 요청 중입니다. 현재 안전 지점을 기다립니다.",
        "cancelled": "작업이 취소됐습니다. 이미 생성된 파일은 결과 목록에서 확인하세요.",
        "stale": "원본 또는 작업이 바뀌었습니다. 원본 분석을 다시 실행하세요.",
        "settings_changed": "설정이 바뀌었습니다. 계획을 다시 점검하세요.",
        "closed": "진행 중인 작업이 끝나면 창을 닫습니다.",
        "failed": "작업을 완료하지 못했습니다. 진단을 확인하세요.",
        "completed": "파일 생성이 끝났습니다. GX 가져오기와 현장 검증은 별도입니다.",
        "completed_blocked": "대상 가져오기 차단 원인이 남아 있습니다. 파일별 분류와 가져오기 검토 보고서를 확인하세요.",
        "plan_blocked": "대상 가져오기 차단 항목이 남아 있습니다. 파일별 분류와 보고서를 확인하세요.",
        "plan_fallback": "폴백 적용 검토용 CSV 생성 가능 (REVIEW)",
        "fallback_notice": "{devices} 치환을 적용한 폴백 검토용입니다. 원래 기능을 수행하지 않습니다. POU별 원문 대조 자료를 확인하세요. 다운로드와 실행은 미수락입니다.",
        "completed_review": "Q 원본 검토 CSV를 생성했습니다. FX5 가져오기용 파일이 아닙니다.",
        "completed_after_cancel": "취소 요청 중 파일 생성이 끝났습니다. 생성 파일과 검증 상태를 확인하세요.",
        "open_folder": "결과 폴더 열기", "open_file": "선택 파일 열기",
        "files": "생성 파일", "guide": "GX 수동 가져오기 안내",
        "source_status": "원본 분석", "plan_status": "계획 점검", "csv_status": "CSV 생성",
        "gx_status": "GX 검증", "field_status": "현장 수락",
        "plan_candidate": "후보 생성 가능", "plan_review": "검토 항목 포함, 생성 가능 (REVIEW)",
        "plan_source_review": "검토용 생성 가능 (REVIEW)", "plan_hold": "보류",
        "bounds_status": "추가 프로젝트 범위 검사", "bounds_not_run": "계획 미실행",
        "bounds_not_reported": "검사 상태 보고 없음 (GX 수락 별도)",
        "bounds_no_unchecked": "보고서에 미검증 범위 표시 없음 (GX 수락 별도)",
        "bounds_not_checked": "추가 프로젝트 범위 미검증 (NOT_CHECKED, GX 수락 별도)",
        "bounds_partial": "추가 프로젝트 범위 부분 검증 (PARTIAL_CHECK, 미검증: {missing}, GX 수락 별도)",
        "bounds_partial_unknown": "추가 프로젝트 범위 부분 검증 (PARTIAL_CHECK, GX 수락 별도)",
        "bounds_unknown": "추가 프로젝트 범위 검사 상태 미확인 (GX 수락 별도)",
        "csv_created": "생성 완료 ({status})", "csv_partial": "부분 생성 (PARTIAL_PUBLISH)",
        "file_kind": "분류", "file_path": "파일 경로", "selected_file_path": "선택한 파일의 전체 경로",
        "summary_counts": "POU: {pous}개, 프로그램 참조: {program}건, 코멘트 참조: {comments}건",
        "summary_source_uninspected": "분석 시 미검사 범위: {count}건",
        "reverse_safety": "배선 확인과 현장 검증 필요. PLC 다운로드/운전 승인 아님.",
        "not_run": "미실행", "not_verified": "미검증", "mapping_policy": "정방향 주소 방식",
        "identity": "원본 주소 철자 유지", "range": "구간별 재배정", "numeric": "주소 값 보존 (진수 변환)",
        "max_m": "FX5 대상 M 마지막 허용 주소 (십진수, 선택)", "max_d": "FX5 대상 D 마지막 허용 주소 (십진수, 선택)",
        "bounds_note": "기종별 알려진 상한과 미지원 항목은 진단으로 표시합니다. 실제 프로젝트 메모리 할당과 하드웨어 설정은 GX에서 별도로 확인하세요.",
        "fixed_ranges": "X/Y 주소 표기: 원본 {source}, 대상 {target}. 진수와 배정 충돌을 검사하며 실제 I/O 배선은 별도 확인합니다.",
        "review_ranges": "{source}를 읽기 전용으로 검사합니다. 대상 배정은 적용하지 않습니다.",
        "output_hint": "결과 이름은 새 폴더, CSV의 프로젝트 식별 정보와 보고서에 사용됩니다. 저장 위치 아래 새 폴더를 만들며 기존 파일은 덮어쓰지 않습니다.",
        "show_related": "이 주소의 문제 보기", "back_to_address": "주소 배정으로 돌아가기",
        "technical_toggle": "기술 상세와 해시 보기", "policy_help": "주소 값 보존은 진수를 변환합니다 (FX5 X10 -> Q X8). 원본 주소 철자 유지는 표기를 그대로 사용합니다 (FX5 X10 -> Q X10). 구간별 재배정은 확인한 대상 주소로 옮깁니다.",
        "diagnostic_technical": "진단 기술 상세 보기",
        "technical": "기술 상세", "copy": "상세 복사", "reason": "이유", "action": "다음 조치",
        "applied_hash": "현재 작업에 적용한 설정 SHA-256", "file_hash": "불러온 원본 설정 파일 SHA-256",
        "code": "코드", "location": "원본 위치", "scope": "차단 범위",
        "filter_all": "전체 진단", "filter_address": "주소 {address} 관련 진단",
        "show_all": "전체 진단 보기",
        "no_data": "아직 분석 결과가 없습니다.", "pending": "미적용 주소 편집을 적용하거나 '배정 변경 취소'를 선택하세요.",
        "close_pending": "미적용 주소 편집이 있습니다. 버리고 종료하시겠습니까?",
        "partial": "파일 생성이 일부만 끝났습니다. 생성된 파일을 보존하고 진단을 확인하세요.",
    },
    "en": {
        "title": "FX5/Q CSV Converter", "source": "Source file", "choose": "Choose file",
        "direction": "Source and target", "forward": "FX5 -> Q", "reverse": "Q -> FX5",
        "purpose": "Work purpose", "candidate_purpose": "Create FX5 CSV candidate",
        "review_purpose": "Export Q source review CSV",
        "complete_text": "Use all-POU conversion (default; clear to use the limited earlier engine)",
        "flow": "1. Choose source → 2. Check automatic preparation → 3. Create CSV and inspect results",
        "simple_source": "1. Choose source (.gx3 / .gxw, or drop one file here)",
        "simple_check": "2. Check automatic preparation", "simple_create": "3. Create CSV",
        "advanced_show": "Show detailed settings", "advanced_hide": "Hide detailed settings",
        "simple_confirm": "I checked the source, target CPU, address policy and save location",
        "simple_summary": "Source: {source}\nDirection: {direction} | Target: {cpu} | Address policy: {mapping}",
        "prepare": "Prepare again", "prepared": "Ready. Check the summary and select the confirmation below.",
        "read_first": "Read first", "simple_result": "CSV candidate can be created. GX and field validation remain unverified.",
        "simple_created": "CSV candidate created. GX and field validation remain unverified.",
        "simple_fallback": "Fallback review output. It does not perform the original function (REVIEW).",
        "simple_hold": "On hold or import blocked. Check diagnostics for the reason and next action.",
        "simple_review": "Review is required (REVIEW). Check diagnostics and source comparison files.",
        "relocation": "Explicit D/T/ZR mapping (Q -> FX5)",
        "relocation_load": "Choose mapping JSON", "relocation_clear": "Clear mapping",
        "relocation_none": "No D/T/ZR mapping selected. Choose an approved source-bound JSON when needed.",
        "relocation_applied": "Applied mapping: {name}\n{ranges}\nHeld comments: {held}. Plan checking validates collisions and target ceilings.",
        "relocation_more": "and {count} more ranges ({total} total)",
        "relocation_conflict": "Explicit D/T/ZR mapping cannot be combined with the standalone ZR -> R setting. Clear the active setting first.",
        "identifier": "Result name", "output": "Save location", "folder": "Choose folder",
        "summary": "Summary", "usage": "Source X/Y usage", "addresses": "Address mapping", "diagnostics": "Diagnostics", "details": "Detailed settings",
        "usage_range": "Source X/Y range", "usage_points": "Used/range points", "usage_width": "Max access width",
        "usage_program": "Program refs", "usage_comments": "Comment refs", "usage_pous": "Affected POUs", "usage_locators": "Source locations",
        "usage_uninspected": "This table shows X/Y only. M/D/L/B and other devices are outside its scope. Uninspected scope: {count}. Do not treat unlisted addresses as unused.",
        "analyze": "Analyze source", "plan": "Check plan", "publish": "Create CSV",
        "publish_review": "Create Q source review CSV",
        "cancel": "Cancel", "confirm": "I confirmed the target settings and current address mapping",
        "idle": "Choose a source to start read-only analysis.",
        "unverified": "Address-space limits do not prove installed I/O or available unused points. GX and field validation remain separate.",
        "cpu_limits": "CPU and target project settings require separate confirmation.",
        "project_limits": "Address radix and mapping collisions are checked without device capacity limits.",
        "converter_limits": "Converter support: verified FX5U and Q input forms only (Q X/Y hexadecimal; CPU unverified)",
        "source_checked": "Source analysis complete. Check required mapping, then inspect the plan.",
        "source_review_checked": "Q source analysis complete. Check the source review CSV plan.",
        "plan_ready": "Plan checked. Confirm the mapping to create CSV in a new result folder.",
        "review_plan_ready": "Source review plan checked. Confirm the Q source and new save location, then create review CSV.",
        "settings": "Address mapping",
        "hold": "On hold. Check diagnostics and required settings.",
        "working": "Working. Cancellation is handled at safe points.",
        "cancel_requested": "Cancellation requested. Waiting for a safe point.",
        "cancelled": "Cancelled. Check the result list for any files already created.",
        "stale": "Source or work purpose changed. Analyze the source again.",
        "settings_changed": "Settings changed. Check the plan again.",
        "closed": "The window will close when the active job finishes.",
        "failed": "The job did not finish. Check diagnostics.",
        "completed": "Files created. GX import and field verification are separate.",
        "completed_blocked": "Target import remains blocked. Inspect each file's role and the import review report.",
        "plan_blocked": "Target import blockers remain. Inspect each file's role and report.",
        "plan_fallback": "Fallback import review CSV can be generated (REVIEW)",
        "fallback_notice": "Fallback review output uses {devices} substitution and does not perform the original function. Inspect the POU source comparison files. Download and operation remain unaccepted.",
        "completed_review": "Q source review CSV created. It is not for FX5 import.",
        "completed_after_cancel": "Files were created after cancellation was requested. Check the results and validation state.",
        "open_folder": "Open result folder", "open_file": "Open selected file",
        "files": "Created files", "guide": "Manual GX import guide",
        "source_status": "Source analysis", "plan_status": "Plan check", "csv_status": "CSV creation",
        "gx_status": "GX validation", "field_status": "Field acceptance",
        "plan_candidate": "Candidate can be generated",
        "plan_review": "Output includes items for review (REVIEW)",
        "plan_source_review": "Review-only output can be generated (REVIEW)", "plan_hold": "On hold",
        "bounds_status": "Additional project range checks", "bounds_not_run": "Plan not run",
        "bounds_not_reported": "Check status not reported (GX acceptance separate)",
        "bounds_no_unchecked": "Report lists no unchecked ranges (GX acceptance separate)",
        "bounds_not_checked": "Additional project ranges not checked (NOT_CHECKED; GX acceptance separate)",
        "bounds_partial": "Additional project ranges partly checked (PARTIAL_CHECK; unchecked: {missing}; GX acceptance separate)",
        "bounds_partial_unknown": "Additional project ranges partly checked (PARTIAL_CHECK; GX acceptance separate)",
        "bounds_unknown": "Additional project range check status unknown (GX acceptance separate)",
        "csv_created": "Created ({status})", "csv_partial": "Partially created (PARTIAL_PUBLISH)",
        "file_kind": "Category", "file_path": "File path", "selected_file_path": "Selected full file path",
        "summary_counts": "POUs: {pous}, program references: {program}, comment references: {comments}",
        "summary_source_uninspected": "Scope not inspected during analysis: {count}",
        "reverse_safety": "Hardware wiring and field validation are required. PLC download or operation is not approved.",
        "not_run": "NOT_RUN", "not_verified": "Unverified", "mapping_policy": "Forward address policy",
        "identity": "Preserve source address spelling", "range": "Remap ranges", "numeric": "Preserve address value (convert radix)",
        "max_m": "FX5 target M last allowed address (decimal, optional)", "max_d": "FX5 target D last allowed address (decimal, optional)",
        "bounds_note": "Known CPU ceilings and unsupported forms appear in diagnostics. Check actual project allocation and hardware settings in GX separately.",
        "fixed_ranges": "X/Y notation: source {source}, target {target}. Radix and mapping collisions are checked; verify actual I/O wiring separately.",
        "review_ranges": "{source} is checked read-only. Target mapping is not applied.",
        "output_hint": "The result name identifies the new folder, CSV project metadata and report. A new folder is created below the save location; existing files are preserved.",
        "show_related": "Show issues for this address", "back_to_address": "Back to address mapping",
        "technical_toggle": "Show technical details and hashes", "policy_help": "Preserve address value converts radix (FX5 X10 -> Q X8). Preserve source address spelling keeps the written address (FX5 X10 -> Q X10). Remap ranges uses confirmed target addresses.",
        "diagnostic_technical": "Show diagnostic technical details",
        "technical": "Technical details", "copy": "Copy details", "reason": "Reason", "action": "Next action",
        "applied_hash": "Applied settings SHA-256", "file_hash": "Loaded source settings file SHA-256",
        "code": "Code", "location": "Source location", "scope": "Blocking scope",
        "filter_all": "All diagnostics", "filter_address": "Diagnostics for address {address}",
        "show_all": "Show all diagnostics",
        "no_data": "No analysis yet.", "pending": "Apply address edits or select 'Discard mapping changes'.",
        "close_pending": "Discard unapplied address edits and close?",
        "partial": "Only some files were created. Preserve them and check diagnostics.",
    },
}


def suggest_direction(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower()
    return {".gx3": FX5_TO_Q, ".gxw": Q_TO_FX5}.get(suffix)


def suggest_identifier(path: str | Path) -> str:
    """Make an editable Windows-safe suggestion, not a CPU inference."""
    stem = Path(path).stem.strip().rstrip(". ")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    if not stem or stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem.upper()):
        stem = "project"
    return stem[:80].rstrip(". ") or "project"


def import_guide(direction: str, language: str) -> str:
    if language == "en":
        target = "GX Works2 Q" if direction == FX5_TO_Q else "GX Works3 FX5"
        return (f"Prepare a new or approved copy of the {target} project.\n"
                "Import each eligible program CSV. Confirm POU names, order and project settings.\n"
                "Back up common comments before importing: import may replace existing comments.\n"
                "Import eligible comment CSV separately. Rebuild all, save, reopen in a new process, "
                "and compare an official export. Manual steps do not mark GX validation as passed.")
    target = "GX Works2 Q" if direction == FX5_TO_Q else "GX Works3 FX5"
    return (f"{target}의 새 프로젝트 또는 승인된 복제본을 준비하세요.\n"
            "가져오기용 프로그램 CSV를 각각 가져오고 POU 이름, 실행 순서, 프로젝트 설정을 확인하세요.\n"
            "공통 코멘트 가져오기는 기존 내용을 교체할 수 있습니다. 먼저 백업과 보존 계획을 확인하세요.\n"
            "가져오기용 코멘트 CSV를 별도로 가져오세요. 전체 재빌드와 저장 후 새 프로세스로 다시 열고 "
            "공식 내보내기 결과와 대조하세요. 이 안내를 따른 사실만으로 GX 검증이 완료되지는 않습니다.")


def diagnostic_values(item: object) -> tuple[str, str, str, str, str]:
    return tuple(str(getattr(item, name, None) or "") for name in
                 ("code", "scope", "locator", "reason", "action"))


_DIAGNOSTIC_HELP = {
    "ko": {
        "XY_SOURCE_RANGE_INVALID": ("원본 X/Y 주소 또는 전체 접근 구간이 고정 범위를 벗어났습니다.", "원본 위치와 주소를 확인하세요. 원문은 검토 자료에 보존됩니다."),
        "XY_TARGET_RANGE_INVALID": ("배정한 대상 X/Y 주소 또는 전체 접근 구간이 고정 범위를 벗어났습니다.", "대상 시작 주소와 배정 구간을 수정한 뒤 계획을 다시 점검하세요."),
        "XY_PREFLIGHT_HOLD": ("X/Y 배정의 전체 참조 검사에서 문제가 발견됐습니다.", "관련 주소와 진단 상세를 확인한 뒤 배정을 수정하세요."),
        "XY_SETTINGS_REQUIRED": ("사용 중인 X/Y 주소에 대상 배정이 필요합니다.", "주소 배정에서 대상 시작을 확인하고 현재 작업에 적용하세요."),
        "DEVICE_RANGE_INVALID": ("주소 또는 접근 구간이 지원 범위를 벗어났습니다.", "원본 위치와 대상 배정을 확인하세요. 범위를 늘려 통과시킬 수 없습니다."),
        "OPCODE_UNSUPPORTED": ("지원하지 않는 명령이 있습니다.", "원본 명령과 지원 범위를 확인하세요."),
        "OPERAND_UNCLASSIFIED": ("피연산자의 의미를 판독하지 못했습니다.", "원본 위치에서 피연산자를 확인하세요."),
        "Q_READER_UNKNOWN": ("원본 POU의 일부를 판독하지 못했습니다.", "GX Works2에서 해당 원문을 확인하세요."),
        "Q_TYPED_MINING_REVIEW": ("명령 구조는 읽었지만 대상 동작의 대응을 확인하지 못했습니다.", "마이닝 검토용 파일과 원문을 대조하세요. 가져오기용으로 확정된 결과가 아닙니다."),
        "APPROVED_PROFILE_INVALID": ("내장 승인 배정표의 무결성을 확인하지 못했습니다.", "진단 상세를 보존하고 정상 클라이언트를 사용하세요. 배정표 없이 자동 진행하지 않습니다."),
        "Q_COMMENT_BINDING_UNVERIFIED": ("코멘트와 주소의 연결을 확인하지 못했습니다.", "공식 코멘트 자료와 원본 주소를 대조하세요."),
        "Q_PLAN_BLOCKED": ("Q 변환 계획을 만들지 못했습니다.", "기술 상세에서 원인을 확인하고 원본 또는 주소 배정을 수정하세요."),
        "XY_UNMAPPED": ("사용 중인 X/Y 주소에 대상 배정이 없습니다.", "해당 원본 구간에 대상 시작 주소를 배정하세요."),
        "WORD_SETTINGS_REQUIRED": ("ZR의 대상 대응 설정이 필요합니다.", "번호를 보존해 R로 옮기려면 상세 설정에서 ZR → R을 선택하세요."),
        "WORD_DYNAMIC_OR_MODIFIED_UNSUPPORTED": ("ZR의 인덱스 또는 변형 형식은 아직 지원하지 않습니다.", "해당 위치의 동적 주소 계산과 대상 구성을 확인하세요."),
        "Q_R_BANK_ALIAS_UNVERIFIED": ("Q의 R과 ZR이 함께 사용됩니다.", "RSET 블록 상태와 두 주소의 중첩 의미를 확인해야 합니다."),
        "WORD_SOURCE_ALIAS": ("ZR 주소에 숫자 별칭이 있습니다.", "같은 번호의 서로 다른 표기를 원본에서 확인하세요."),
        "WORD_DIRECTION_MISMATCH": ("ZR → R 설정은 Q → FX5 방향 전용입니다.", "현재 변환 방향에 맞는 설정을 선택하세요."),
        "PROJECT_IDENTIFIER_INVALID": ("결과 이름을 사용할 수 없습니다.", "결과 이름의 길이와 사용할 수 없는 문자를 확인하세요."),
        "SETTINGS_SOURCE_CHANGED": ("불러온 주소 설정 파일이 바뀌었습니다.", "설정을 다시 불러오고 현재 작업에 적용하세요."),
        "RELOCATION_SETTINGS_INVALID": ("D/T/ZR 배정표를 적용하지 못했습니다.", "원본에 결속된 승인 JSON과 기술 상세를 확인하고 다시 선택하세요."),
        "RELOCATION_SOURCE_MISMATCH": ("배정표의 원본 SHA와 현재 원본이 다릅니다.", "현재 원본을 다시 분석하고 해당 원본의 배정표를 선택하세요."),
        "RELOCATION_WORD_CONFLICT": ("D/T/ZR 배정과 단독 ZR -> R 설정이 겹칩니다.", "한 종류의 설정만 적용하고 계획을 다시 점검하세요."),
        "SOURCE_READ_FAILED": ("원본 파일을 읽지 못했습니다.", "원본 파일과 접근 권한을 확인하세요."),
        "SOURCE_ANALYSIS_FAILED": ("원본 분석을 완료하지 못했습니다.", "원본 형식과 진단 상세를 확인하세요."),
        "PLAN_FAILED": ("계획 점검을 완료하지 못했습니다.", "입력과 배정을 확인한 뒤 계획을 다시 점검하세요."),
        "INPUT_CHANGED": ("원본 파일 내용 또는 설정이 바뀌었습니다.", "원본을 다시 분석하고 계획을 점검하세요."),
        "PUBLICATION_FAILED": ("CSV 생성을 완료하지 못했습니다.", "기존 생성 파일을 보존하고 결과 폴더와 진단을 확인하세요."),
    },
    "en": {
        "XY_SOURCE_RANGE_INVALID": ("A source X/Y address or full access span exceeds the fixed range.", "Check the source location and address. The original remains in review output."),
        "XY_TARGET_RANGE_INVALID": ("A mapped target X/Y address or full access span exceeds the fixed range.", "Correct the target start and range, then check the plan again."),
        "XY_PREFLIGHT_HOLD": ("Full X/Y reference checking found a mapping problem.", "Inspect the related address and details, then correct the mapping."),
        "XY_SETTINGS_REQUIRED": ("A used X/Y address needs a target mapping.", "Confirm its target start in address mapping and apply it to this work."),
        "DEVICE_RANGE_INVALID": ("An address or access span exceeds the supported range.", "Check the source location and target mapping. Raising the range cannot bypass this limit."),
        "OPCODE_UNSUPPORTED": ("An instruction is unsupported.", "Check the source instruction and supported forms."),
        "OPERAND_UNCLASSIFIED": ("The operand could not be interpreted.", "Inspect the operand at the source location."),
        "Q_READER_UNKNOWN": ("Part of a source POU could not be decoded.", "Inspect the original in GX Works2."),
        "Q_TYPED_MINING_REVIEW": ("The instruction structure was read, but its target behavior is unverified.", "Compare the mining review output with the source. It is not an approved import candidate."),
        "APPROVED_PROFILE_INVALID": ("The bundled approved mapping failed its integrity check.", "Preserve the details and use an intact client. Automatic preparation will not bypass the mapping."),
        "Q_COMMENT_BINDING_UNVERIFIED": ("A comment could not be bound to a verified address.", "Compare the official comments and source address."),
        "Q_PLAN_BLOCKED": ("The Q conversion plan could not be built.", "Inspect technical details, then correct the source or address mapping."),
        "XY_UNMAPPED": ("A used X/Y address has no target mapping.", "Assign a target start to the source range."),
        "WORD_SETTINGS_REQUIRED": ("ZR requires a target mapping.", "Enable ZR → R in detailed settings to preserve the decimal number."),
        "WORD_DYNAMIC_OR_MODIFIED_UNSUPPORTED": ("Indexed or modified ZR forms are unsupported.", "Check the dynamic address calculation and target configuration at this location."),
        "Q_R_BANK_ALIAS_UNVERIFIED": ("The Q source uses both R and ZR.", "Verify the RSET bank state and address alias semantics."),
        "WORD_SOURCE_ALIAS": ("ZR uses a numeric address alias.", "Inspect different spellings of the same source address."),
        "WORD_DIRECTION_MISMATCH": ("ZR → R is only valid for Q → FX5.", "Select settings for the current conversion direction."),
        "PROJECT_IDENTIFIER_INVALID": ("The result name cannot be used.", "Check its length and prohibited characters."),
        "SETTINGS_SOURCE_CHANGED": ("The loaded address settings file changed.", "Load the settings again and apply them to this work."),
        "RELOCATION_SETTINGS_INVALID": ("The D/T/ZR mapping could not be applied.", "Inspect the approved source-bound JSON and technical details, then select it again."),
        "RELOCATION_SOURCE_MISMATCH": ("The mapping source SHA differs from the current source.", "Analyze the current source and choose its mapping."),
        "RELOCATION_WORD_CONFLICT": ("D/T/ZR mapping overlaps the standalone ZR -> R setting.", "Apply one settings type and check the plan again."),
        "SOURCE_READ_FAILED": ("The source file could not be read.", "Check the source file and access permissions."),
        "SOURCE_ANALYSIS_FAILED": ("Source analysis did not finish.", "Check the source format and diagnostic details."),
        "PLAN_FAILED": ("Plan checking did not finish.", "Check inputs and mappings, then inspect the plan again."),
        "INPUT_CHANGED": ("Source contents or settings changed.", "Analyze the source and check the plan again."),
        "PUBLICATION_FAILED": ("CSV creation did not finish.", "Preserve existing output and inspect the result folder and diagnostics."),
    },
}


def display_diagnostic_values(item: object, language: str) -> tuple[str, str, str, str, str]:
    code, scope, location, reason, action = diagnostic_values(item)
    language = language if language in _DIAGNOSTIC_HELP else "ko"
    help_text = _DIAGNOSTIC_HELP[language].get(code)
    if help_text:
        reason, action = help_text
    elif not reason or reason == code or re.fullmatch(r"[A-Z0-9_]+|[A-Za-z]+(?:Error|Exception)", reason):
        reason = ("원본 또는 설정에서 확인이 필요한 항목이 있습니다." if language == "ko" else
                  "A source or setting requires inspection.")
        action = ("원본 위치와 진단 상세를 확인하세요." if language == "ko" else
                  "Inspect the source location and diagnostic details.")
    elif language == "en" and not action.isascii():
        action = "Inspect the source location and diagnostic details."
    pou = str(getattr(item, "pou", None) or "")
    if pou and pou not in location:
        location = f"{pou}: {location}" if location else pou
    scope_names = ({"project": "작업 전체", "source": "원본", "row": "원본 위치",
                    "comment": "코멘트", "pou": "POU"} if language == "ko" else
                   {"project": "Whole work", "source": "Source", "row": "Source location",
                    "comment": "Comment", "pou": "POU"})
    return code, scope_names.get(scope, scope), location, reason, action


def diagnostic_span_summary(item: object, language: str) -> str:
    """Show the reported span and effective ceiling without inventing provenance."""
    detail = str(getattr(item, "detail", "") or "")
    match = re.search(
        r"\(([XY][0-9A-F]+)\.\.([XY][0-9A-F]+)\) exceeds X/Y allowed ceiling "
        r"for (FX5U|Q): ([XY][0-9A-F]+)", detail, re.ASCII)
    if match is None:
        return ""
    first, last, profile, ceiling = match.groups()
    if first[0] != last[0] or first[0] != ceiling[0]:
        return ""
    if language == "en":
        return f"Access span {first} to {last}; {profile} checked end {ceiling}."
    return f"접근 구간 {first}~{last}; {profile} 검사 허용 끝 주소 {ceiling}."


def artifact_kind(path: Path, status: str, report: Mapping[str, Any] | None = None) -> str:
    """Use publication status and report metadata; a CSV suffix is insufficient."""
    name = path.name.lower()
    if report and path.suffix.lower() == ".csv" and status not in {"REVIEW_ONLY", "HOLD"}:
        for program in report.get("programs", ()):
            role = program.get("text_output_role")
            if program.get("text_output") == path.name and role in {
                    "IMPORT_REVIEW_CSV", "SOURCE_REVIEW_ONLY", "TYPED_MINING_REVIEW_ONLY"}:
                return role
    if "review" in name or "invalid" in name or status in {"REVIEW_ONLY", "HOLD"}:
        return "REVIEW_ONLY" if path.suffix.lower() == ".csv" else "DIAGNOSTIC"
    if path.suffix.lower() != ".csv":
        return "DIAGNOSTIC"
    if report:
        for group in report.get("comment_import", {}).get("groups", ()):
            if group.get("full_csv") == path.name:
                return group.get("full_csv_role", "SOURCE_REVIEW_ONLY")
        for program in report.get("programs", ()):
            if program.get("text_output") == path.name and program.get("text_output_role") == "SOURCE_REVIEW_ONLY":
                return "SOURCE_REVIEW_ONLY"
    if report and any(token in str(report.get("status", "")).upper() for token in ("REVIEW_ONLY", "HOLD")):
        return "REVIEW_ONLY"
    return "IMPORT_CANDIDATE"


def has_import_review(report: Mapping[str, Any]) -> bool:
    """Use declared fallback program roles rather than the overall blocker state."""
    return isinstance(report, Mapping) and any(program.get("text_output_role") == "IMPORT_REVIEW_CSV"
               for program in report.get("programs", ()))


def project_bounds_status(plan: object, language: str) -> str:
    """Show only the engine's explicit project-range check, never infer acceptance."""
    labels = LABELS.get(language, LABELS["ko"])
    report = getattr(getattr(plan, "engine_plan", None), "report", None)
    if not isinstance(report, Mapping):
        return labels["bounds_not_reported"]
    target = report.get("target_review")
    if isinstance(target, Mapping) and target.get("cpu"):
        return (f'{target["cpu"]} 제조사 상한 검토: {target.get("state", "REVIEW")}. '
                "실제 메모리 할당, 동적 주소와 전체 명령 접근 범위는 미검증입니다."
                if language == "ko" else
                f'{target["cpu"]} manufacturer ceiling review: {target.get("state", "REVIEW")}. '
                "Actual allocation, dynamic addresses and complete instruction access spans are unverified.")
    if "project_bounds_check" not in report:
        return labels["bounds_no_unchecked"]
    check = report["project_bounds_check"]
    if not isinstance(check, Mapping):
        return labels["bounds_unknown"]
    state = check.get("state")
    if state == "NOT_CHECKED":
        return labels["bounds_not_checked"]
    if state == "PARTIAL_CHECK":
        provided = check.get("provided")
        if isinstance(provided, Mapping):
            missing = [str(key).upper().replace("_", " ") for key, value in provided.items()
                       if value is False]
            if missing:
                return labels["bounds_partial"].format(missing=", ".join(missing))
        return labels["bounds_partial_unknown"]
    return labels["bounds_unknown"]


class UnifiedApp(ttk.Frame):
    """Single screen controller. Service methods are called only by workers."""

    def __init__(self, master: tk.Tk, *, service: object | None = None,
                 config: Mapping[str, Any] | None = None,
                 editor_factory: Callable[..., ttk.Frame] | None = None) -> None:
        from app.pilot_app import application_title, load_config
        from app import workflow_service
        from app.usage_editor import UsageEditor

        self.config_data = dict(config or load_config())
        self.language = str(self.config_data.get("language", "ko"))
        self.text = LABELS.get(self.language, LABELS["ko"])
        super().__init__(master, padding=12)
        self.master = master
        self.service = service if service is not None else workflow_service
        self.editor_factory = editor_factory or UsageEditor
        self.workflow = WorkflowState()
        self.events: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.generation = 0
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._worker_generation: int | None = None
        self._job_kind = ""
        self.closing = False
        self._suspend_change = False
        self._last_values: dict[str, str] = {}
        self.source_identity: SourceIdentity | None = None
        self.analysis: object | None = None
        self.plan: object | None = None
        self.publication: object | None = None
        self.result_dir: Path | None = None
        self.settings: object | None = None
        self.applied_settings_provenance: tuple[str | None, str | None] = (None, None)
        self.word_settings_provenance: tuple[str | None, str | None] = (None, None)
        self.relocation_settings: object | None = None
        self.relocation_settings_provenance: tuple[str | None, str | None] = (None, None)
        self._ticket = None
        self._retry_handle = None
        self._prepare_handle = None
        self._auto_prepare = False
        self._simple_session = False
        self.approved_profile = None
        self._profile_lookup_key = None
        self._publish_lock = False
        self._selected_group = None
        self._all_diagnostics: tuple[object, ...] = ()

        self.source_var = tk.StringVar()
        self.direction_var = tk.StringVar(value=FX5_TO_Q)
        self.direction_display_var = tk.StringVar(value=self.text["forward"])
        self.identifier_var = tk.StringVar()
        self.output_parent_var = tk.StringVar()
        self.policy_var = tk.StringVar(value="numeric")
        self.confirm_var = tk.BooleanVar(value=False)
        self.q_source_review_var = tk.BooleanVar(value=False)
        self.complete_text_var = tk.BooleanVar(value=True)
        self.statement_policy_var = tk.StringVar(value="in_plc")
        self.target_cpu_var = tk.StringVar(value="FX5U")
        self.zr_to_r_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value=self.text["idle"])
        self.summary_var = tk.StringVar(value=self.text["no_data"])
        self.simple_summary_var = tk.StringVar(value=self.text["idle"])
        self.result_notice_var = tk.StringVar(value=self.text["no_data"])
        self.advanced_open_var = tk.BooleanVar(value=False)
        self.source_status_var = tk.StringVar(value=self.text["not_run"])
        self.plan_status_var = tk.StringVar(value=self.text["not_run"])
        self.bounds_status_var = tk.StringVar(value=self.text["bounds_not_run"])
        self.product_var = tk.StringVar(value=self.text["not_run"])
        self.gx_var = tk.StringVar(value=self.text["not_verified"])
        self.field_var = tk.StringVar(value=self.text["not_verified"])
        self.mapping_info_var = tk.StringVar()
        self.relocation_info_var = tk.StringVar(value=self.text["relocation_none"])
        self.fixed_range_var = tk.StringVar()
        self.technical_open_var = tk.BooleanVar(value=False)
        self.diagnostic_filter_var = tk.StringVar(value=self.text["filter_all"])
        self.selected_file_path_var = tk.StringVar()
        self.bound_vars = {key: tk.StringVar() for key in ("max_m", "max_d")}
        self.max_m_var = self.bound_vars["max_m"]
        self.max_d_var = self.bound_vars["max_d"]
        self.mapping_mode_var = self.policy_var
        self._build()
        self.primary_button = self.action_button
        self.state = self.workflow
        for name, var in (("source", self.source_var), ("direction", self.direction_var),
                          ("identifier", self.identifier_var), ("policy", self.policy_var),
                          ("q_source_review", self.q_source_review_var),
                          ("complete_text", self.complete_text_var),
                            ("statement_policy", self.statement_policy_var),
                          ("target_cpu", self.target_cpu_var),
                          ("zr_to_r", self.zr_to_r_var),
                          ("output", self.output_parent_var),
                          *((key, value) for key, value in self.bound_vars.items())):
            self._last_values[name] = var.get()
            var.trace_add("write", lambda *_args, key=name, variable=var:
                          self._inputs_changed(key, variable))
        self.master.title(application_title(self.text["title"]))
        self.master.minsize(1024, 720)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.master.bind("<Return>", self._on_return)
        self.master.bind("<Escape>", self._on_escape)
        self._poll_handle = self.after(int(self.config_data.get("queue_poll_ms", 100)), self._poll)

    def _build(self) -> None:
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=3, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.step_source_label = ttk.Label(top, text=self.text["simple_source"])
        self.step_source_label.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 5))
        ttk.Label(top, text=self.text["source"]).grid(row=1, column=0, sticky="w")
        self.source_entry = ttk.Entry(top, textvariable=self.source_var)
        self.source_entry.grid(row=1, column=1, sticky="ew", padx=6)
        self.source_entry.bind("<FocusOut>", self._suggest_from_source)
        self.source_button = ttk.Button(top, text=self.text["choose"], command=self._choose_source)
        self.source_button.grid(row=1, column=2)
        ttk.Label(top, text=self.text["direction"]).grid(row=2, column=0, sticky="w", pady=6)
        self.direction_box = ttk.Combobox(top, state="readonly", textvariable=self.direction_display_var,
                                          values=(self.text["forward"], self.text["reverse"]), width=16)
        self.direction_box.grid(row=2, column=1, sticky="w", padx=6)
        self.direction_box.bind("<<ComboboxSelected>>", self._direction_selected)
        ttk.Label(top, text=self.text["identifier"]).grid(row=3, column=0, sticky="w")
        self.identifier_entry = ttk.Entry(top, textvariable=self.identifier_var)
        self.identifier_entry.grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Label(top, text=self.text["output"]).grid(row=4, column=0, sticky="w", pady=6)
        self.output_entry = ttk.Entry(top, textvariable=self.output_parent_var)
        self.output_entry.grid(row=4, column=1, sticky="ew", padx=6)
        self.output_button = ttk.Button(top, text=self.text["folder"], command=self._choose_output)
        self.output_button.grid(row=4, column=2)
        ttk.Label(top, text=self.text["output_hint"], wraplength=760).grid(
            row=5, column=0, columnspan=3, sticky="w")
        self.purpose_frame = ttk.LabelFrame(top, text=self.text["purpose"])
        self.purpose_frame.grid(row=8, column=0, columnspan=3, sticky="ew", pady=5)
        self.q_candidate_box = ttk.Radiobutton(
            self.purpose_frame, variable=self.q_source_review_var, value=False,
            text=self.text["candidate_purpose"])
        self.q_candidate_box.pack(side="left", padx=8)
        self.q_source_review_box = ttk.Radiobutton(
            self.purpose_frame, variable=self.q_source_review_var, value=True,
            text=self.text["review_purpose"])
        self.q_source_review_box.pack(side="left", padx=8)
        ttk.Label(top, text=self.text["flow"], wraplength=900).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=4)
        self.target_cpu_frame = ttk.Frame(top)
        self.target_cpu_frame.grid(row=2, column=1, columnspan=2, sticky="e", pady=4)
        ttk.Label(self.target_cpu_frame, text=("대상 FX5 CPU (상한 검토)" if self.language == "ko"
                                               else "Target FX5 CPU (ceiling review)")).pack(side="left")
        self.target_cpu_box = ttk.Combobox(self.target_cpu_frame, state="readonly",
                                          textvariable=self.target_cpu_var,
                                          values=("FX5U", "FX5UC", "FX5UJ", "FX5S"), width=10)
        self.target_cpu_box.pack(side="left", padx=6)

        self.advanced_button = ttk.Checkbutton(top, text=self.text["advanced_show"],
            variable=self.advanced_open_var, command=self._toggle_advanced)
        self.advanced_button.grid(row=7, column=0, columnspan=3, sticky="w", pady=4)
        self.notebook = ttk.Notebook(self, height=260)
        self.notebook.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=8)
        self.summary_tab = ttk.Frame(self.notebook, padding=8)
        self.usage_tab = ttk.Frame(self.notebook, padding=8)
        self.address_tab = ttk.Frame(self.notebook, padding=8)
        self.diagnostic_tab = ttk.Frame(self.notebook, padding=8)
        self.detail_tab = ttk.Frame(self.notebook, padding=8)
        for frame, name in ((self.summary_tab, "summary"), (self.usage_tab, "usage"),
                            (self.address_tab, "addresses"),
                            (self.diagnostic_tab, "diagnostics"), (self.detail_tab, "details")):
            self.notebook.add(frame, text=self.text[name])
            frame.columnconfigure(0, weight=1)

        detail_canvas = tk.Canvas(self.detail_tab, highlightthickness=0)
        detail_canvas.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(self.detail_tab, orient="vertical", command=detail_canvas.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        detail_canvas.configure(yscrollcommand=detail_scroll.set)
        detail_content = ttk.Frame(detail_canvas)
        detail_window = detail_canvas.create_window((0, 0), window=detail_content, anchor="nw")
        detail_content.bind("<Configure>", lambda _event: detail_canvas.configure(
            scrollregion=detail_canvas.bbox("all")))
        detail_canvas.bind("<Configure>", lambda event: detail_canvas.itemconfigure(
            detail_window, width=event.width))
        self.detail_tab.rowconfigure(0, weight=1)
        detail_content.columnconfigure(0, weight=1)

        self.step_check_label = ttk.Label(self.summary_tab, text=self.text["simple_check"])
        self.step_check_label.grid(row=0, column=0, sticky="w")
        ttk.Label(self.summary_tab, textvariable=self.simple_summary_var, wraplength=900,
                  justify="left").grid(row=1, column=0, sticky="ew", pady=4)
        self.result_notice_label = ttk.Label(self.summary_tab, textvariable=self.result_notice_var,
                                             wraplength=900, justify="left")
        self.result_notice_label.grid(row=2, column=0, sticky="ew", pady=4)
        self.analysis_summary_label = ttk.Label(detail_content, textvariable=self.summary_var,
                                                 wraplength=900, justify="left")
        self.analysis_summary_label.grid(row=6, column=0, sticky="ew")
        states = ttk.Frame(detail_content)
        self.states_frame = states
        states.grid(row=7, column=0, sticky="ew", pady=8)
        for row, (key, variable) in enumerate((("source_status", self.source_status_var),
                                                ("plan_status", self.plan_status_var),
                                                ("csv_status", self.product_var),
                                                ("gx_status", self.gx_var),
                                                ("field_status", self.field_var))):
            ttk.Label(states, text=self.text[key] + ":").grid(row=row, column=0, sticky="w")
            ttk.Label(states, textvariable=variable).grid(row=row, column=1, sticky="w", padx=8)
        self.bounds_status_title = ttk.Label(states, text=self.text["bounds_status"] + ":")
        self.bounds_status_title.grid(row=5, column=0, sticky="w")
        self.bounds_status_label = ttk.Label(states, textvariable=self.bounds_status_var,
                                             wraplength=700, justify="left")
        self.bounds_status_label.grid(row=5, column=1, sticky="w", padx=8)
        self.file_table = ttk.Treeview(self.summary_tab, columns=("kind", "path"), show="headings", height=3)
        self.file_table.heading("kind", text=self.text["file_kind"])
        self.file_table.heading("path", text=self.text["file_path"])
        self.file_table.column("kind", width=170, stretch=False)
        self.file_table.column("path", width=700)
        self.file_table.grid(row=3, column=0, sticky="nsew")
        self.summary_tab.rowconfigure(3, weight=1)
        selected_file = ttk.Frame(self.summary_tab)
        selected_file.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        selected_file.columnconfigure(1, weight=1)
        ttk.Label(selected_file, text=self.text["selected_file_path"]).grid(row=0, column=0, sticky="w")
        self.selected_file_path_entry = ttk.Entry(
            selected_file, textvariable=self.selected_file_path_var, state="readonly")
        self.selected_file_path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        file_actions = ttk.Frame(self.summary_tab)
        file_actions.grid(row=5, column=0, sticky="w", pady=6)
        self.open_folder_button = ttk.Button(file_actions, text=self.text["open_folder"],
                                              command=self._open_folder, state="disabled")
        self.open_folder_button.pack(side="left")
        self.open_file_button = ttk.Button(file_actions, text=self.text["open_file"],
                                            command=self._open_selected_file, state="disabled")
        self.open_file_button.pack(side="left", padx=8)
        self.file_table.bind("<<TreeviewSelect>>", self._file_selected)
        ttk.Label(self.summary_tab, text=self.text["read_first"]).grid(row=6, column=0, sticky="w")
        self.guide_text = tk.Text(self.summary_tab, height=3, wrap="word", state="disabled")
        self.guide_text.grid(row=7, column=0, sticky="nsew")
        self._set_text(self.guide_text, self._guide())

        self.usage_status_var = tk.StringVar(value=self.text["no_data"])
        ttk.Label(self.usage_tab, textvariable=self.usage_status_var, wraplength=900).grid(
            row=0, column=0, sticky="ew")
        usage_columns = ("range", "points", "width", "program", "comments", "pous", "locators")
        self.usage_table = ttk.Treeview(self.usage_tab, columns=usage_columns,
                                         show="headings", height=12, selectmode="browse")
        for key, width in zip(usage_columns, (150, 140, 110, 105, 105, 130, 300)):
            self.usage_table.heading(key, text=self.text["usage_" + key])
            self.usage_table.column(key, width=width, minwidth=width, stretch=True)
        self.usage_table.grid(row=1, column=0, sticky="nsew")
        self.usage_tab.rowconfigure(1, weight=1)
        usage_vertical = ttk.Scrollbar(self.usage_tab, orient="vertical", command=self.usage_table.yview)
        usage_vertical.grid(row=1, column=1, sticky="ns")
        self.usage_table.configure(yscrollcommand=usage_vertical.set)
        usage_horizontal = ttk.Scrollbar(self.usage_tab, orient="horizontal", command=self.usage_table.xview)
        usage_horizontal.grid(row=2, column=0, sticky="ew")
        self.usage_table.configure(xscrollcommand=usage_horizontal.set)

        self.relocation_frame = ttk.LabelFrame(self.address_tab, text=self.text["relocation"], padding=6)
        self.relocation_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.relocation_load_button = ttk.Button(self.relocation_frame, text=self.text["relocation_load"],
                                                 command=self._load_relocation_settings)
        self.relocation_load_button.grid(row=0, column=0, sticky="w")
        self.relocation_clear_button = ttk.Button(self.relocation_frame, text=self.text["relocation_clear"],
                                                  command=self._clear_relocation_settings)
        self.relocation_clear_button.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(self.relocation_frame, textvariable=self.relocation_info_var, wraplength=870,
                  justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        self.editor = self.editor_factory(self.address_tab, analysis=None,
                                          direction=self.direction_var.get().lower(),
                                          language=self.language,
                                          on_apply=self._mapping_applied,
                                          on_select=self._address_selected)
        self.editor.grid(row=1, column=0, sticky="nsew")
        self.address_tab.rowconfigure(1, weight=1)

        columns = ("code", "scope", "location", "reason", "action")
        self.diagnostic_table = ttk.Treeview(self.diagnostic_tab, columns=columns,
                                              show="headings", height=11)
        for key, width in zip(columns, (180, 120, 180, 310, 350)):
            self.diagnostic_table.heading(key, text=self.text.get(key, key))
            self.diagnostic_table.column(key, width=width, stretch=key in {"reason", "action"})
        self.diagnostic_table.grid(row=0, column=0, sticky="nsew")
        self.diagnostic_tab.rowconfigure(0, weight=1)
        scroll = ttk.Scrollbar(self.diagnostic_tab, command=self.diagnostic_table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.diagnostic_table.configure(yscrollcommand=scroll.set)
        diagnostic_horizontal = ttk.Scrollbar(self.diagnostic_tab, orient="horizontal",
                                               command=self.diagnostic_table.xview)
        diagnostic_horizontal.grid(row=1, column=0, sticky="ew")
        self.diagnostic_table.configure(xscrollcommand=diagnostic_horizontal.set)
        self.diagnostic_table.bind("<<TreeviewSelect>>", self._diagnostic_selected)
        self.diagnostic_summary_var = tk.StringVar()
        ttk.Label(self.diagnostic_tab, textvariable=self.diagnostic_summary_var,
                  wraplength=900, justify="left").grid(row=2, column=0, sticky="ew", pady=(5, 0))
        self.diagnostic_technical_open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.diagnostic_tab, text=self.text["diagnostic_technical"],
                        variable=self.diagnostic_technical_open_var,
                        command=self._toggle_diagnostic_technical).grid(row=3, column=0, sticky="w")
        self.diagnostic_detail = tk.Text(self.diagnostic_tab, height=5, wrap="word", state="disabled")
        self.diagnostic_detail.grid(row=4, column=0, sticky="nsew")
        self.diagnostic_detail.grid_remove()
        diagnostic_actions = ttk.Frame(self.diagnostic_tab)
        diagnostic_actions.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(diagnostic_actions, textvariable=self.diagnostic_filter_var).pack(side="left")
        self.show_all_diagnostics_button = ttk.Button(
            diagnostic_actions, text=self.text["show_all"], command=self._show_all_diagnostics)
        self.show_all_diagnostics_button.pack(side="right")
        self.back_to_address_button = ttk.Button(
            diagnostic_actions, text=self.text["back_to_address"], command=self._back_to_address)
        self.back_to_address_button.pack(side="right", padx=8)

        ttk.Label(detail_content, textvariable=self.fixed_range_var, wraplength=900,
                  justify="left").grid(row=0, column=0, sticky="w")
        self.bounds_note_label = ttk.Label(detail_content, text=self.text["bounds_note"],
                                           wraplength=900, justify="left")
        self.bounds_note_label.grid(row=1, column=0, sticky="w")
        form = ttk.Frame(detail_content)
        form.grid(row=2, column=0, sticky="ew", pady=8)
        self.detail_inputs: list[ttk.Widget] = []
        self.md_inputs: list[ttk.Entry] = []
        self.md_frame = ttk.Frame(form)
        self.md_frame.grid(row=0, column=0, columnspan=2, sticky="w")
        for row, key in enumerate(("max_m", "max_d")):
            ttk.Label(self.md_frame, text=self.text[key]).grid(row=row, column=0, sticky="w", pady=2)
            field = ttk.Entry(self.md_frame, textvariable=self.bound_vars[key], width=18)
            field.grid(row=row, column=1, sticky="w", padx=8)
            self.detail_inputs.append(field)
            self.md_inputs.append(field)
        self.policy_frame = ttk.LabelFrame(form, text=self.text["mapping_policy"])
        self.policy_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        for row, value, label in ((0, "numeric", "numeric"), (1, "identity", "identity"), (2, "range", "range")):
            button = ttk.Radiobutton(self.policy_frame, text=self.text[label], variable=self.policy_var, value=value)
            button.grid(row=row, column=0, sticky="w")
            self.detail_inputs.append(button)
        ttk.Label(self.policy_frame, text=self.text["policy_help"], wraplength=850).grid(
            row=3, column=0, sticky="w")
        self.word_frame = ttk.LabelFrame(form, text="ZR → R")
        self.word_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        word_toggle = ttk.Checkbutton(
            self.word_frame, variable=self.zr_to_r_var,
            text=("Q ZR을 FX5 R로 변경 (번호 보존)" if self.language == "ko" else
                  "Map Q ZR to FX5 R (preserve decimal number)"))
        word_toggle.grid(row=0, column=0, columnspan=2, sticky="w")
        for column, label, command in (
                (0, "불러오기" if self.language == "ko" else "Load", self._load_word_settings),
                (1, "저장" if self.language == "ko" else "Save", self._save_word_settings)):
            button = ttk.Button(self.word_frame, text=label, command=command)
            button.grid(row=1, column=column, sticky="w", padx=4, pady=4)
            self.detail_inputs.append(button)
        self.detail_inputs.append(word_toggle)
        ttk.Label(self.word_frame, wraplength=850, text=(
            "프로그램과 코멘트에 함께 적용합니다. 인덱스 형식과 대상 R 영역은 별도 검증합니다."
            if self.language == "ko" else
            "Applies to program and comments. Indexed forms and target R capacity require separate validation."
        )).grid(row=2, column=0, columnspan=2, sticky="w")
        self.complete_text_box = ttk.Checkbutton(detail_content, variable=self.complete_text_var,
                                                 text=self.text["complete_text"])
        self.complete_text_box.grid(row=5, column=0, sticky="w", pady=8)
        statement_frame = ttk.Frame(detail_content)
        statement_frame.grid(row=5, column=1, sticky="w")
        ttk.Label(statement_frame, text="Statement 기본: In PLC / 원본 유형 유지: preserve_source").pack(anchor="w")
        self.statement_policy_box = ttk.Combobox(statement_frame, textvariable=self.statement_policy_var,
                     values=("in_plc", "preserve_source"), state="readonly")
        self.statement_policy_box.pack(anchor="w")
        ttk.Checkbutton(detail_content, text=self.text["technical_toggle"],
                        variable=self.technical_open_var, command=self._toggle_technical).grid(
                            row=3, column=0, sticky="w")
        self.technical_frame = ttk.Frame(detail_content)
        self.technical_frame.grid(row=4, column=0, sticky="nsew")
        ttk.Label(self.technical_frame, textvariable=self.mapping_info_var, wraplength=900,
                  justify="left").pack(anchor="w")
        self.technical_text = tk.Text(self.technical_frame, height=5, wrap="word", state="disabled")
        self.technical_text.pack(fill="both", expand=True)
        ttk.Button(self.technical_frame, text=self.text["copy"], command=self._copy_detail).pack(anchor="w")
        self.technical_frame.grid_remove()

        controls = ttk.Frame(self)
        controls.grid(row=6, column=0, columnspan=3, sticky="ew")
        self.step_create_label = ttk.Label(controls, text=self.text["simple_create"])
        self.step_create_label.pack(anchor="w")
        self.confirm_box = ttk.Checkbutton(controls, text=self.text["simple_confirm"], variable=self.confirm_var,
                                            command=self._refresh_action)
        self.confirm_box.pack(side="left")
        self.related_button = ttk.Button(controls, text=self.text["show_related"],
                                         command=self._show_related_diagnostics, state="disabled")
        self.related_button.pack(side="left", padx=8)
        self.action_button = ttk.Button(controls, text=self.text["analyze"], command=self._primary_action)
        self.action_button.pack(side="right", ipadx=12, ipady=7)
        self.cancel_button = ttk.Button(controls, text=self.text["cancel"], command=self._cancel,
                                        state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        self._refresh_limit_inputs()
        ttk.Label(self, textvariable=self.status_var, wraplength=950,
                  justify="left").grid(row=7, column=0, columnspan=3, sticky="ew")
        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=4)
        self._refresh_action()
        self._update_mapping_info()

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _choose_source(self) -> None:
        selected = filedialog.askopenfilename(parent=self.master,
                  filetypes=[("GX Works source", "*.gx3 *.gxw"), ("All files", "*.*")])
        if selected:
            self._accept_source(selected)

    def _on_files_dropped(self, event: object) -> str:
        from app.pilot_app import parse_drop_payload
        paths, error = parse_drop_payload(str(getattr(event, "data", "")), self.master.tk.splitlist)
        if error:
            self.status_var.set(error)
        elif len(paths) != 1:
            self.status_var.set("원본 파일 하나만 선택하세요." if self.language == "ko"
                                else "Choose exactly one source file.")
        elif suggest_direction(paths[0]) is None:
            self.status_var.set(".gx3 또는 .gxw 파일을 선택하세요." if self.language == "ko"
                                else "Choose a .gx3 or .gxw file.")
        else:
            self._accept_source(paths[0])
        return "break"

    def _accept_source(self, selected: str) -> None:
        if self._publish_lock:
            self.status_var.set(self.text["working"])
            return
        if not self._check_pending():
            return
        self._suspend_change = True
        try:
            self.source_var.set(selected)
            self.direction_var.set(suggest_direction(selected) or self.direction_var.get())
            for name, field in self.bound_vars.items():
                field.set("")
                self._last_values[name] = ""
            if self.direction_var.get() != Q_TO_FX5:
                self.q_source_review_var.set(False)
            self.identifier_var.set(suggest_identifier(selected))
            if not self.output_parent_var.get():
                self.output_parent_var.set(str(Path(selected).parent))
        finally:
            self._suspend_change = False
        self._refresh_limit_inputs()
        self._invalidate()
        self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
        self._last_values.update({"source": self.source_var.get(),
                                  "direction": self.direction_var.get(),
                                  "q_source_review": self.q_source_review_var.get(),
                                  "identifier": self.identifier_var.get(),
                                  "output": self.output_parent_var.get()})
        self._auto_prepare = True
        self._simple_session = True
        self._start_analyze()

    def _suggest_from_source(self, _event: object = None) -> None:
        raw = self.source_var.get().strip().strip('"')
        if not raw or self.closing or self._publish_lock:
            return
        direction = suggest_direction(raw)
        if not direction and self.identifier_var.get():
            return
        old_direction = self.direction_var.get()
        old_identifier = self.identifier_var.get()
        proposed_identifier = old_identifier or suggest_identifier(raw)
        if ((direction and direction != old_direction) or proposed_identifier != old_identifier) and not self._check_pending():
            return
        self._suspend_change = True
        try:
            if direction:
                self.direction_var.set(direction)
                if direction != old_direction:
                    for name, field in self.bound_vars.items():
                        field.set("")
                        self._last_values[name] = ""
                if direction != Q_TO_FX5:
                    self.q_source_review_var.set(False)
            if not self.identifier_var.get():
                self.identifier_var.set(suggest_identifier(raw))
            if not self.output_parent_var.get():
                self.output_parent_var.set(str(Path(raw).parent))
            self._last_values.update({"direction": self.direction_var.get(),
                                      "q_source_review": self.q_source_review_var.get(),
                                      "identifier": self.identifier_var.get(),
                                      "output": self.output_parent_var.get()})
        finally:
            self._suspend_change = False
        self._refresh_limit_inputs()
        if self.direction_var.get() != old_direction or self.identifier_var.get() != old_identifier:
            self._invalidate()
            self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
        if self.analysis is None:
            self._auto_prepare = True
            self._simple_session = True
            self._start_analyze()

    def _choose_output(self) -> None:
        if self._busy() and self._job_kind in {"verify", "publish"}:
            return
        selected = filedialog.askdirectory(parent=self.master)
        if selected:
            self.output_parent_var.set(selected)
            self._refresh_action()

    def _check_pending(self) -> bool:
        if getattr(self, "editor", None) is not None and hasattr(self.editor, "is_busy") and self.editor.is_busy():
            self.status_var.set(self.text["working"])
            self._select_tab(self.address_tab)
            return False
        if getattr(self, "editor", None) is not None and self.editor.has_pending_edit():
            self.status_var.set(self.text["pending"])
            self._select_tab(self.address_tab)
            return False
        return True

    def _direction_selected(self, _event: object = None) -> None:
        selected = self.direction_display_var.get()
        self.direction_var.set(FX5_TO_Q if selected == self.text["forward"] else Q_TO_FX5)

    def _inputs_changed(self, key: str, variable: tk.StringVar) -> None:
        if self._suspend_change or self.closing:
            return
        if self._busy() and self._job_kind in {"verify", "publish"}:
            self._suspend_change = True
            try:
                variable.set(self._last_values[key])
            finally:
                self._suspend_change = False
            self.status_var.set(self.text["working"])
            return
        if key in {"source", "direction", "q_source_review", "complete_text", "target_cpu"} and not self._check_pending():
            self._suspend_change = True
            try:
                variable.set(self._last_values[key])
            finally:
                self._suspend_change = False
            self._refresh_limit_inputs()
            return
        previous_value = self._last_values.get(key)
        auto_prepare = self._auto_prepare
        if key == "zr_to_r" and variable.get() and self.relocation_settings is not None:
            self._suspend_change = True
            try:
                variable.set(previous_value)
            finally:
                self._suspend_change = False
            self.status_var.set(self.text["relocation_conflict"])
            return
        self._last_values[key] = variable.get()
        if key == "target_cpu" and self.approved_profile is not None:
            profile = self.approved_profile
            if self.settings is profile.settings and self.relocation_settings is profile.relocation_settings:
                if self.analysis is not None:
                    try:
                        self.editor.set_analysis(getattr(self.analysis, "raw_analysis", self.analysis),
                                                 self.direction_var.get().lower(), settings=None)
                    except (ValueError, TypeError) as error:
                        self._suspend_change = True
                        try:
                            variable.set(previous_value)
                            self._last_values[key] = previous_value
                        finally:
                            self._suspend_change = False
                        self.status_var.set(str(error))
                        return
                self.settings, self.relocation_settings = None, None
                self.applied_settings_provenance = (None, None)
                self.relocation_settings_provenance = (None, None)
                self.relocation_info_var.set(self.text["relocation_none"])
            self.approved_profile = None
        if key == "zr_to_r":
            self.word_settings_provenance = (None, None)
        if key in {"source", "direction"}:
            self._suspend_change = True
            try:
                self.zr_to_r_var.set(False)
                self._last_values["zr_to_r"] = False
                self.word_settings_provenance = (None, None)
                for name, field in self.bound_vars.items():
                    field.set("")
                    self._last_values[name] = ""
            finally:
                self._suspend_change = False
        if (self._review_mode() and key in self.bound_vars) or (
                key == "policy" and self.direction_var.get() != FX5_TO_Q):
            return
        if key in {"direction", "q_source_review"}:
            if key == "q_source_review":
                self._suspend_change = True
                try:
                    for name, field in self.bound_vars.items():
                        field.set("")
                        self._last_values[name] = ""
                finally:
                    self._suspend_change = False
            if key == "direction" and self.direction_var.get() != Q_TO_FX5:
                self._suspend_change = True
                try:
                    self.q_source_review_var.set(False)
                    self._last_values["q_source_review"] = False
                finally:
                    self._suspend_change = False
            self._refresh_limit_inputs()
        if key == "complete_text":
            self._refresh_limit_inputs()
        if key == "output":
            if (self._review_mode() or self._simple_session) and variable.get() != previous_value:
                self.confirm_var.set(False)
            self._refresh_action()
            return
        if key in {"source", "direction", "q_source_review", "complete_text"}:
            self._invalidate()
            self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
            if auto_prepare and key != "source":
                self._auto_prepare = True
                self._start_analyze()
        else:
            self._invalidate_plan()

    def _invalidate(self) -> None:
        self._auto_prepare = False
        for name in ("_retry_handle", "_prepare_handle"):
            handle = getattr(self, name)
            if handle is not None:
                self.after_cancel(handle)
                setattr(self, name, None)
        self.generation += 1
        self.cancel_event.set()
        self.workflow.close()
        self.workflow = WorkflowState()
        self.state = self.workflow
        self.cancel_event = threading.Event()
        self.source_identity = None
        self.analysis = None
        self.plan = None
        self.publication = None
        self.settings = None
        self.applied_settings_provenance = (None, None)
        self.relocation_settings = None
        self.relocation_settings_provenance = (None, None)
        self.approved_profile = None
        self._profile_lookup_key = None
        self.relocation_info_var.set(self.text["relocation_none"])
        self._suspend_change = True
        try:
            self.zr_to_r_var.set(False)
            self._last_values["zr_to_r"] = False
            self.word_settings_provenance = (None, None)
        finally:
            self._suspend_change = False
        self._selected_group = None
        self.diagnostic_filter_var.set(self.text["filter_all"])
        self.confirm_var.set(False)
        self.summary_var.set(self.text["no_data"])
        self.usage_status_var.set(self.text["no_data"])
        self.usage_table.delete(*self.usage_table.get_children())
        self.source_status_var.set(self.text["not_run"])
        self.plan_status_var.set(self.text["not_run"])
        self.bounds_status_var.set(self.text["bounds_not_run"])
        self.product_var.set(self.text["not_run"])
        self.gx_var.set(self.text["not_verified"])
        self.field_var.set(self.text["not_verified"])
        self.status_var.set(self.text["stale"] if self.source_var.get() else self.text["idle"])
        self._clear_results()
        self._set_text(self.technical_text, "")
        self._set_text(self.diagnostic_detail, "")
        self._set_text(self.guide_text, self._guide())
        self._update_mapping_info()
        self._refresh_action()
        self._update_simple_summary()
        self.result_notice_var.set(self.text["no_data"])

    def _mapping_applied(self, settings: object) -> None:
        if self._publish_lock:
            raise ValueError(self.text["working"])
        self.settings = settings
        self.approved_profile = None
        path = getattr(self.editor, "loaded_settings_path", None)
        digest = getattr(self.editor, "loaded_settings_sha256", None)
        self.applied_settings_provenance = ((str(path), str(digest)) if path and digest
                                            else (None, None))
        if self.direction_var.get() == FX5_TO_Q:
            self._suspend_change = True
            try:
                self.policy_var.set("range")
                self._last_values["policy"] = "range"
            finally:
                self._suspend_change = False
        self._invalidate_plan()
        self._update_mapping_info()

    def _settings_provenance(self) -> tuple[str | None, str | None]:
        if self.direction_var.get() == FX5_TO_Q and self.policy_var.get() == "numeric":
            return None, None
        return self.applied_settings_provenance if self._active_settings() is not None else (None, None)

    def _active_settings(self) -> object | None:
        if self.direction_var.get() == Q_TO_FX5 and self.q_source_review_var.get():
            return None
        if self.direction_var.get() == FX5_TO_Q and self.policy_var.get() == "numeric":
            from converter.xy_settings import MappingSettings
            from converter.xy_mapping import UnmappedPolicy
            return MappingSettings(Profile.FX5U, Profile.Q, UnmappedPolicy.NUMERIC_IDENTITY, ())
        if self.direction_var.get() == FX5_TO_Q and self.policy_var.get() != "range":
            return None
        return self.settings

    def _active_word_settings(self) -> object | None:
        from converter.word_settings import WordDeviceSettings
        if self.direction_var.get() != Q_TO_FX5 or self._review_mode():
            return None
        return WordDeviceSettings(Profile.Q, Profile.FX5U, True) if self.zr_to_r_var.get() else None

    def _word_provenance(self) -> tuple[str | None, str | None]:
        return self.word_settings_provenance if self._active_word_settings() else (None, None)

    def _active_relocation_settings(self) -> object | None:
        if self.direction_var.get() != Q_TO_FX5 or self._review_mode() or not self.complete_text_var.get():
            return None
        return self.relocation_settings

    def _load_relocation_settings(self) -> None:
        if (self._busy() or self.closing or self._publish_lock or self.direction_var.get() != Q_TO_FX5
                or self._review_mode() or not self.complete_text_var.get() or not self._check_pending()):
            return
        if self.source_identity is None or self.analysis is None:
            self.status_var.set(self.text["stale"])
            return
        if self._active_word_settings() is not None:
            self.status_var.set(self.text["relocation_conflict"])
            return
        selected = filedialog.askopenfilename(parent=self.master, filetypes=[("JSON", "*.json")])
        if not selected:
            return
        expected = self.source_identity
        self.relocation_settings = None
        self.relocation_settings_provenance = (None, None)
        self.relocation_info_var.set(self.text["relocation_none"])
        self._invalidate_plan()
        def load(cancel: Callable[[], bool]) -> object:
            from converter import core
            from converter.device_relocation import DeviceRelocationSettings
            if cancel():
                raise core.ConversionCancelled("settings load cancelled")
            source = core._reject_link_like_ancestors(Path(selected), "relocation settings")
            identity = SourceIdentity.from_file(source, max_bytes=1024 * 1024)
            snapshot = core._open_input_snapshot(source)
            try:
                if snapshot.sha256 != identity.sha256:
                    raise ValueError("relocation settings changed during load")
                settings = DeviceRelocationSettings.from_json(snapshot.body)
                snapshot.verify()
            finally:
                snapshot.close()
            if SourceIdentity.from_file(expected.path) != expected or settings.source_sha256 != expected.sha256:
                raise ValueError("relocation source SHA mismatch; analyze the current source and select its mapping")
            if cancel():
                raise core.ConversionCancelled("settings load cancelled")
            return settings, str(source), identity.sha256
        self._start_worker("relocation_load", load)

    def _clear_relocation_settings(self) -> None:
        if self._busy() or self.closing or self._publish_lock:
            return
        self.relocation_settings = None
        self.relocation_settings_provenance = (None, None)
        self.relocation_info_var.set(self.text["relocation_none"])
        self._invalidate_plan()

    def _load_word_settings(self) -> None:
        if self._busy() or self._review_mode() or self.direction_var.get() != Q_TO_FX5:
            return
        if self.relocation_settings is not None:
            self.status_var.set(self.text["relocation_conflict"])
            return
        selected = filedialog.askopenfilename(parent=self.master, filetypes=[("JSON", "*.json")])
        if not selected:
            return
        self._invalidate_plan()
        def load(cancel: Callable[[], bool]) -> object:
            from converter import core
            from converter.word_settings import MAX_SETTINGS_BYTES, WordDeviceSettings
            source = core._reject_link_like_ancestors(Path(selected), "word settings")
            identity = SourceIdentity.from_file(source, max_bytes=MAX_SETTINGS_BYTES)
            snapshot = core._open_input_snapshot(source)
            try:
                if snapshot.sha256 != identity.sha256:
                    raise ValueError("word settings changed during load")
                settings = WordDeviceSettings.from_json(snapshot.body, expected_source=Profile.Q,
                                                        expected_target=Profile.FX5U)
                snapshot.verify()
            finally:
                snapshot.close()
            if cancel():
                raise core.ConversionCancelled("settings load cancelled")
            return settings, str(source), identity.sha256
        self._start_worker("word_load", load)

    def _save_word_settings(self) -> None:
        if self._busy() or self._review_mode() or self.direction_var.get() != Q_TO_FX5:
            return
        from converter.word_settings import WordDeviceSettings
        selected = filedialog.asksaveasfilename(parent=self.master, defaultextension=".json",
                                                filetypes=[("JSON", "*.json")])
        if not selected:
            return
        settings = WordDeviceSettings(Profile.Q, Profile.FX5U, bool(self.zr_to_r_var.get()))
        def save(cancel: Callable[[], bool]) -> object:
            from converter import core
            if cancel():
                raise core.ConversionCancelled("settings save cancelled")
            target = core._reject_link_like_ancestors(Path(selected), "word settings")
            payload = settings.to_json()
            created = False
            try:
                with target.open("xb") as handle:
                    created = True
                    if handle.write(payload) != len(payload):
                        raise OSError("short settings write")
            except BaseException as error:
                if created:
                    from app.workflow_service import WorkflowPartialPublishError
                    raise WorkflowPartialPublishError(target.parent, (target,), str(error)) from error
                raise
            return target
        self._start_worker("word_save", save)

    def _update_mapping_info(self) -> None:
        if self.direction_var.get() == Q_TO_FX5 and self.q_source_review_var.get():
            self.mapping_info_var.set("원본 주소 보존: 주소 배정과 대상 상한을 적용하지 않습니다."
                                      if self.language == "ko" else
                                      "Source review: address mapping and target limits are not applied.")
            return
        path, file_hash = self._settings_provenance()
        applied = str(getattr(self._active_settings(), "digest", "")) or "-"
        self.mapping_info_var.set(f"{self.text['applied_hash']}: {applied}\n"
                                  f"{self.text['file_hash']}: {file_hash or '-'}"
                                  + (f" ({path})" if path else "")
                                  + (f"\nZR → R: {self._active_word_settings().digest}"
                                     if self._active_word_settings() else "")
                                  + (f"\nD/T/ZR: {self.relocation_settings.digest}\n"
                                     f"{self.text['file_hash']}: {self.relocation_settings_provenance[1]}"
                                     if self._active_relocation_settings() else ""))

    def _address_selected(self, group: object) -> None:
        self._selected_group = group
        self.diagnostic_filter_var.set(self.text["filter_address"].format(
            address=getattr(group, "source_start", "")))
        self._render_diagnostics()
        self.related_button.configure(state="normal")

    def _show_related_diagnostics(self) -> None:
        if self._selected_group is not None:
            self._render_diagnostics()
            self._select_tab(self.diagnostic_tab)

    def _back_to_address(self) -> None:
        if not self._review_mode():
            selected = self.diagnostic_table.selection()
            if selected and hasattr(self.editor, "focus_diagnostic"):
                item = self._diagnostic_items.get(selected[0])
                if item is not None:
                    self.editor.focus_diagnostic(getattr(item, "locator", None),
                                                 getattr(item, "operand", None), diagnostic=item)
            self._select_tab(self.address_tab)

    def _show_all_diagnostics(self) -> None:
        self._selected_group = None
        self.related_button.configure(state="disabled")
        self.diagnostic_filter_var.set(self.text["filter_all"])
        self._render_diagnostics()

    def _invalidate_plan(self) -> None:
        self.generation += 1
        self.cancel_event.set()
        self.cancel_event = threading.Event()
        self.workflow.close()
        self.workflow = WorkflowState()
        self.state = self.workflow
        self.plan = None
        self.publication = None
        self.confirm_var.set(False)
        self.plan_status_var.set(self.text["not_run"])
        self.bounds_status_var.set(self.text["bounds_not_run"])
        self.product_var.set(self.text["not_run"])
        self.gx_var.set(self.text["not_verified"])
        self.field_var.set(self.text["not_verified"])
        self._clear_results()
        if self.analysis is not None and self.source_identity is not None:
            self.workflow.set_inputs(self.direction_var.get(), self.source_identity,
                                     self._settings_snapshot(), self._options_snapshot())
            ticket = self.workflow.begin_analyze()
            self.workflow.finish(ticket, Outcome.SUCCEEDED)
            self._show_diagnostics(getattr(self.analysis, "diagnostics", ()))
        self.status_var.set(self.text["settings_changed"])
        self._update_mapping_info()
        self._set_text(self.guide_text, self._guide())
        self._refresh_action()
        self._update_simple_summary()
        self.result_notice_var.set(self.text["settings_changed"])
        if self._auto_prepare:
            self._schedule_prepare()

    def _schedule_prepare(self) -> None:
        """Coalesce edits and wait for the old worker without accepting its result."""
        if self._prepare_handle is not None:
            self.after_cancel(self._prepare_handle)
        generation = self.generation
        def prepare() -> None:
            self._prepare_handle = None
            if generation != self.generation or not self._auto_prepare or self.closing:
                return
            if self._busy():
                self._prepare_handle = self.after(100, prepare)
                return
            if self.plan is None and self._check_pending():
                if self.analysis is None:
                    self._start_analyze()
                else:
                    self._start_plan()
        self._prepare_handle = self.after(180, prepare)

    def _update_simple_summary(self) -> None:
        mapping = (self.text[self.policy_var.get()] if self.direction_var.get() == FX5_TO_Q
                   else self.text["numeric"])
        if self._active_settings() is not None and self.settings is not None:
            mapping = self.text["range"]
        if self._active_relocation_settings() is not None:
            mapping += " + D/T/ZR"
        if self._active_word_settings() is not None:
            mapping += " + ZR -> R"
        cpu = self.target_cpu_var.get() if self.direction_var.get() == Q_TO_FX5 else (
            "Q (CPU 미확인)" if self.language == "ko" else "Q (CPU unverified)")
        if self._review_mode():
            cpu = self.text["review_purpose"]
        self.simple_summary_var.set(self.text["simple_summary"].format(
            source=Path(self.source_var.get()).name or "-", direction=self.direction_display_var.get(),
            cpu=cpu, mapping=mapping))
        if self.approved_profile is not None:
            label = "자동 적용한 승인 배정" if self.language == "ko" else "Approved mapping applied automatically"
            self.simple_summary_var.set(self.simple_summary_var.get() + f"\n{label}: {self.approved_profile.display_name}")

    def _fallback_notice(self) -> str:
        devices = "SM1255/SD1255" if self.direction_var.get() == FX5_TO_Q else "SM4095/SD4095"
        return self.text["fallback_notice"].format(devices=devices)

    def _show_result_notice(self, status: str, report: Mapping[str, Any], *, publishable: bool = True,
                            created: bool = False) -> None:
        blocked = report.get("import_readiness", {}).get("state") == "BLOCKED"
        key = ("simple_hold" if not publishable or blocked or status in {"HOLD", "BLOCKED"}
               else "simple_fallback" if has_import_review(report)
               else "simple_review" if status in {"REVIEW", "REVIEW_ONLY"} or self._review_mode()
               else "simple_created" if created else "simple_result")
        notice = self.text[key]
        if has_import_review(report):
            notice += "\n" + self._fallback_notice()
        self.result_notice_var.set(notice)
        self._update_simple_summary()

    def _toggle_advanced(self) -> None:
        self._refresh_limit_inputs()

    def _select_tab(self, tab: ttk.Frame) -> None:
        if tab in (self.usage_tab, self.address_tab, self.detail_tab):
            self.advanced_open_var.set(True)
            self._refresh_limit_inputs()
        self.notebook.select(tab)

    def _guide(self) -> str:
        if self.complete_text_var.get() and not self._review_mode():
            if self.direction_var.get() == FX5_TO_Q:
                guide = ("전체 POU를 변환하고 승인된 Q 명령과 코멘트 호환 처리를 적용합니다. 원문 코멘트와 대조표도 보존합니다.\n"
                        "파일별 분류와 COMMENT-source 원문을 확인하세요. 가져오기 후보는 GX Works2에서 별도로 검증하세요."
                        if self.language == "ko" else
                        "Converts all POUs with approved Q instruction and comment compatibility rules, preserving source comments and a comparison ledger.\n"
                        "Inspect each file's role and COMMENT-source originals. Validate import candidates in GX Works2 separately.")
                report = getattr(getattr(self.plan, "engine_plan", None), "report", {})
                return guide + ("\n" + self._fallback_notice() if has_import_review(report) else "")
            guide = ("전체 POU와 COMMENT의 명령, 피연산자와 주석을 보존합니다.\n"
                    "선택한 FX5 CPU의 상한을 검토합니다. 실제 메모리 할당과 모듈 구성은 GX에서 확인하세요.\n"
                    "전체 COMMENT는 대조용입니다. COMMENT-import 파일은 상한 안의 코멘트를 기종별 검증하기 위한 후보입니다."
                    if self.language == "ko" else
                    "Preserves all decoded POU instructions, operands and COMMENT text.\n"
                    "The selected FX5 CPU ceilings are reviewed. Check actual allocation and modules in GX.\n"
                    "Full COMMENT is for comparison. COMMENT-import files are candidates requiring target validation.")
            report = getattr(getattr(self.plan, "engine_plan", None), "report", {})
            return guide + ("\n" + self._fallback_notice() if has_import_review(report) else "")
        if self.direction_var.get() == Q_TO_FX5 and self.q_source_review_var.get():
            return ("검토용 CSV는 Q 원본 주소와 판독한 명령 및 주석을 보존합니다.\n"
                    "FX5 가져오기용 파일이 아닙니다. 대상 주소 배정과 명령 호환 검증은 별도입니다.\n"
                    "공식 Step과 판독 범위 밖 필드는 보고서의 미검증 범위를 확인하세요."
                    if self.language == "ko" else
                    "Review CSV preserves Q source addresses and decoded instructions and text.\n"
                    "It is not an FX5 import file. Target mapping and compatibility remain unverified.\n"
                    "See the report for official steps and fields outside the reader model.")
        return import_guide(self.direction_var.get(), self.language)

    def _settings_snapshot(self) -> dict[str, object]:
        path, file_hash = self._settings_provenance()
        return {"mapping_digest": str(getattr(self._active_settings(), "digest", "")),
                "word_mapping_digest": str(getattr(self._active_word_settings(), "digest", "")),
                "word_settings_provenance": self._word_provenance(),
                "relocation_digest": str(getattr(self._active_relocation_settings(), "digest", "")),
                "relocation_settings_provenance": self.relocation_settings_provenance,
                "settings_source_path": path, "settings_source_sha256": file_hash,
                "source_bounds": vars(self._bounds("source")),
                "target_bounds": None if self._review_mode() else vars(self._bounds("target")),
                "max_m": "" if self._review_mode() else self.bound_vars["max_m"].get(),
                "max_d": "" if self._review_mode() else self.bound_vars["max_d"].get()}

    def _options_snapshot(self) -> dict[str, object]:
        path, file_hash = self._settings_provenance()
        return {"identifier": self.identifier_var.get(),
                "policy": self.policy_var.get() if self.direction_var.get() == FX5_TO_Q else None,
                "q_source_review": self._review_mode(),
                "complete_text": self.complete_text_var.get(),
                "statement_policy": self.statement_policy_var.get(),
                "target_cpu": self.target_cpu_var.get() if self.direction_var.get() == Q_TO_FX5 and self.complete_text_var.get() else None,
                "settings_source_path": path, "settings_source_sha256": file_hash}

    def _review_mode(self) -> bool:
        return self.direction_var.get() == Q_TO_FX5 and self.q_source_review_var.get()

    def _bounds(self, side: str) -> Bounds:
        source = Profile.FX5U if self.direction_var.get() == FX5_TO_Q else Profile.Q
        target = Profile.Q if source is Profile.FX5U else Profile.FX5U
        return fixed_bounds(source if side == "source" else target)

    def _request(self) -> object:
        from app.workflow_service import WorkflowRequest
        from app.pilot_app import normalize_user_path
        source = normalize_user_path(self.source_var.get())
        if self._review_mode():
            return WorkflowRequest(
                direction=Q_TO_FX5, source_path=source,
                project_identifier=self.identifier_var.get().strip() or None,
                output_parent=Path(self.output_parent_var.get().strip())
                if self.output_parent_var.get().strip() else None,
                options={"q_source_review": True})
        selected_settings = self._active_settings()
        path, file_hash = self._settings_provenance()
        options = {"forward_policy": self.policy_var.get()} if self.direction_var.get() == FX5_TO_Q else {}
        options["complete_text"] = self.complete_text_var.get()
        if options["complete_text"]:
            options["statement_policy"] = self.statement_policy_var.get()
        if self.direction_var.get() == Q_TO_FX5 and self.complete_text_var.get():
            options["target_cpu"] = self.target_cpu_var.get()
        if path and file_hash:
            options.update({"settings_source_path": path, "settings_source_sha256": file_hash})
        word_path, word_hash = self._word_provenance()
        if word_path and word_hash:
            options.update({"word_settings_source_path": word_path, "word_settings_source_sha256": word_hash})
        relocation_path, relocation_hash = self.relocation_settings_provenance
        if self._active_relocation_settings() and relocation_path and relocation_hash:
            options.update({"relocation_settings_source_path": relocation_path,
                            "relocation_settings_source_sha256": relocation_hash})
        return WorkflowRequest(direction=self.direction_var.get(), source_path=source,
                               project_identifier=self.identifier_var.get().strip() or None,
                               settings=selected_settings,
                               word_settings=self._active_word_settings(),
                               relocation_settings=self._active_relocation_settings(),
                               max_m=None, max_d=None,
                               output_parent=Path(self.output_parent_var.get().strip())
                               if self.output_parent_var.get().strip() else None,
                               options=options)

    def _primary_action(self) -> None:
        if self.closing or self.workflow.stage == Stage.CLOSED or self._busy() or not self._check_pending():
            return
        if self._simple_session:
            self._auto_prepare = True
        if self.analysis is None:
            self._start_analyze()
        elif self.plan is None or self.workflow.stage != Stage.PLAN_READY or not self.plan.publishable:
            self._start_plan()
        else:
            self._start_publish()

    def _on_return(self, event: tk.Event) -> None:
        if isinstance(event.widget, ttk.Button):
            return
        if self.plan is not None and self.workflow.stage == Stage.PLAN_READY:
            return "break"
        if str(self.action_button.cget("state")) == "normal":
            self._primary_action()
        return "break"

    def _on_escape(self, _event: tk.Event) -> None:
        if self._busy():
            self._cancel()

    def _busy(self) -> bool:
        # A finished thread may still have a terminal event waiting in Tk's
        # queue. Keep its slot until that event is consumed, otherwise a new
        # settings job can be overwritten by the preceding analysis result.
        return self.worker is not None

    def _start_worker(self, kind: str, action: Callable[[Callable[[], bool]], object]) -> None:
        if self._busy() or self.closing:
            return
        generation = self.generation
        cancel_event = self.cancel_event
        def run() -> None:
            try:
                result = action(cancel_event.is_set)
                self.events.put((generation, kind, result))
            except BaseException as error:
                self.events.put((generation, "error", (kind, error)))
        self.worker = threading.Thread(target=run, name=f"converter-{kind}", daemon=False)
        self._worker_generation = generation
        self._job_kind = kind
        self.worker.start()
        self.progress.start(10)
        self.cancel_button.configure(state="normal")
        self.action_button.configure(state="disabled")
        self.status_var.set(self.text["working"])

    def _start_analyze(self) -> None:
        if self.closing:
            return
        if not self.source_var.get().strip():
            self.status_var.set(self.text["idle"])
            return
        if self._busy():
            if (self._worker_generation == self.generation
                    and self._job_kind in {"identify", "analyze", "profile_load", "plan"}):
                return
            if self._retry_handle is None:
                generation = self.generation
                def retry() -> None:
                    self._retry_handle = None
                    if (generation == self.generation and not self.closing
                            and self.analysis is None and self.plan is None):
                        self._start_analyze()
                self._retry_handle = self.after(100, retry)
            return
        if self.cancel_event.is_set():
            self.cancel_event = threading.Event()
        try:
            from app.workflow_service import WorkflowRequest
            from app.pilot_app import normalize_user_path
            request = WorkflowRequest(
                self.direction_var.get(), normalize_user_path(self.source_var.get()),
                options={"complete_text": self.complete_text_var.get(), "q_source_review": self._review_mode()})
        except (ValueError, OSError) as error:
            self.status_var.set(str(error))
            return
        self._start_worker("identify", lambda _cancel: (request, SourceIdentity.from_file(request.source_path)))

    def _approved_profile_key(self) -> tuple:
        return (self.source_identity.sha256 if self.source_identity else None,
                self.direction_var.get(), self.target_cpu_var.get(), self.complete_text_var.get())

    def _prepare_approved_profile(self) -> bool:
        """Load source-bound settings off the UI thread, without replacing user edits."""
        if (not self._simple_session or self.source_identity is None or self.analysis is None
                or self._review_mode() or not self.complete_text_var.get()
                or self.direction_var.get() != Q_TO_FX5 or self.settings is not None
                or self.relocation_settings is not None or self._active_word_settings() is not None):
            return False
        key = self._approved_profile_key()
        if key == self._profile_lookup_key:
            return False
        def load(cancel):
            from app.approved_profiles import load_approved_profile
            from converter.core import ConversionCancelled
            if cancel():
                raise ConversionCancelled("approved mapping load cancelled")
            profile = load_approved_profile(*key)
            if cancel():
                raise ConversionCancelled("approved mapping load cancelled")
            return key, profile
        self._start_worker("profile_load", load)
        return True

    def _start_plan(self) -> None:
        if self._busy() or self.closing:
            return
        if self.cancel_event.is_set():
            self.cancel_event = threading.Event()
        if self._prepare_approved_profile():
            return
        try:
            request = self._request()
            if self.source_identity is None:
                raise ValueError(self.text["stale"])
            if self.workflow.stage in {Stage.COMPLETED, Stage.FAILED, Stage.CANCELLED,
                                       Stage.PARTIAL_PUBLISH}:
                # A new run keeps earlier files but needs a fresh plan revision.
                self._invalidate_plan()
            self.workflow.set_inputs(self.direction_var.get(), self.source_identity,
                                     self._settings_snapshot(), self._options_snapshot())
            self._ticket = self.workflow.begin_plan()
        except (ValueError, OSError) as error:
            self.status_var.set(str(error))
            self._select_tab(self.detail_tab)
            return
        self._start_worker("plan", lambda cancel: self.service.plan(request, cancel_check=cancel))

    def _start_publish(self) -> None:
        if not self.confirm_var.get():
            self.status_var.set(self.text["confirm"])
            return
        try:
            request = self._request()
            if request.output_parent is None:
                raise ValueError(self.text["output"])
        except (ValueError, OSError) as error:
            self.status_var.set(str(error))
            return
        self._set_publish_lock(True)
        self._start_worker("verify", lambda _cancel: (request, SourceIdentity.from_file(request.source_path)))

    def _set_publish_lock(self, busy: bool) -> None:
        self._publish_lock = busy
        state = "disabled" if busy else "normal"
        for widget in (self.source_entry, self.source_button, self.identifier_entry,
                       self.output_entry, self.output_button, self.q_source_review_box,
                       self.q_candidate_box, self.relocation_load_button, self.relocation_clear_button,
                       *self.detail_inputs):
            widget.configure(state=state)
        self.complete_text_box.configure(state=state)
        self.direction_box.configure(state="disabled" if busy else "readonly")
        self.target_cpu_box.configure(state="disabled" if busy else "readonly")
        if not busy:
            self._refresh_limit_inputs()
        if hasattr(self.editor, "set_external_busy"):
            self.editor.set_external_busy(busy)

    def _refresh_limit_inputs(self) -> None:
        self.statement_policy_box.configure(state="readonly" if self.complete_text_var.get()
            and not self._review_mode() and not self._publish_lock else "disabled")
        direction = self.direction_var.get()
        selected_tab = self.notebook.select()
        advanced = self.advanced_open_var.get()
        self.advanced_button.configure(text=self.text["advanced_hide" if advanced else "advanced_show"])
        for widget in (self.analysis_summary_label, self.states_frame):
            widget.grid() if advanced else widget.grid_remove()
        if advanced:
            self.related_button.pack(side="left", padx=8)
        else:
            self.related_button.pack_forget()
        self.target_cpu_frame.grid() if direction == Q_TO_FX5 and self.complete_text_var.get() and not self._review_mode() else self.target_cpu_frame.grid_remove()
        self.direction_display_var.set(self.text["forward" if direction == FX5_TO_Q else "reverse"])
        review = self._review_mode()
        self.relocation_frame.grid() if direction == Q_TO_FX5 and self.complete_text_var.get() and not review else self.relocation_frame.grid_remove()
        if direction == Q_TO_FX5 and advanced:
            self.purpose_frame.grid()
        else:
            self.purpose_frame.grid_remove()
        state = "disabled" if self._publish_lock else "normal"
        for widget in (self.q_candidate_box, self.q_source_review_box):
            widget.configure(state=state)
        for widget in (self.bounds_status_title, self.bounds_status_label):
            widget.grid() if not review and (direction == Q_TO_FX5 or self.complete_text_var.get()) else widget.grid_remove()
        for tab in (self.usage_tab, self.address_tab, self.detail_tab):
            self.notebook.tab(tab, state="normal" if advanced and (not review or tab != self.address_tab) else "hidden")
        if selected_tab in {str(self.usage_tab), str(self.address_tab), str(self.detail_tab)} and (
                not advanced or review and selected_tab == str(self.address_tab)):
            self._select_tab(self.summary_tab)
        self.md_frame.grid_remove()
        self.word_frame.grid() if direction == Q_TO_FX5 and not review else self.word_frame.grid_remove()
        self.bounds_note_label.grid() if direction == Q_TO_FX5 and not review else self.bounds_note_label.grid_remove()
        self.policy_frame.grid() if direction == FX5_TO_Q else self.policy_frame.grid_remove()
        source = Profile.FX5U if direction == FX5_TO_Q else Profile.Q
        target = Profile.Q if source is Profile.FX5U else Profile.FX5U
        def range_text(profile: Profile) -> str:
            name = "Q" if profile is Profile.Q else "FX5"
            return f"{name} X/Y ({profile.radix})"
        self.fixed_range_var.set(self.text["review_ranges"].format(source=range_text(source)) if review else
                                 self.text["fixed_ranges"].format(
                                     source=range_text(source), target=range_text(target)))
        self.back_to_address_button.configure(state="disabled" if review else "normal")
        self.related_button.configure(state="disabled" if review or self._selected_group is None else "normal")
        self.confirm_box.configure(text=(
            "Q 원본과 새 저장 위치를 확인했습니다" if self.language == "ko" else
            "I confirmed the Q source and new save location") if review else self.text["simple_confirm"])
        self._set_text(self.guide_text, self._guide())
        for widget in self.md_inputs:
            widget.configure(state="disabled")

    def _toggle_technical(self) -> None:
        if self.technical_open_var.get():
            self.technical_frame.grid()
        else:
            self.technical_frame.grid_remove()

    def _toggle_diagnostic_technical(self) -> None:
        if self.diagnostic_technical_open_var.get():
            self.diagnostic_detail.grid()
        else:
            self.diagnostic_detail.grid_remove()

    def _cancel(self) -> None:
        self._auto_prepare = False
        if self._retry_handle is not None:
            self.after_cancel(self._retry_handle)
            self._retry_handle = None
        if self._prepare_handle is not None:
            self.after_cancel(self._prepare_handle)
            self._prepare_handle = None
        self.cancel_event.set()
        self.workflow.request_cancel()
        self.status_var.set(self.text["cancel_requested"])
        self.cancel_button.configure(state="disabled")

    def _poll(self) -> None:
        self._poll_handle = None
        try:
            while True:
                generation, kind, value = self.events.get_nowait()
                if generation != self.generation:
                    if kind != "open_error" and generation == self._worker_generation:
                        self.worker = None
                        self._worker_generation = None
                        self._job_kind = ""
                    if not self._busy():
                        self._refresh_action()
                    continue
                if kind != "open_error":
                    # The worker has queued its terminal result; its remaining
                    # code only returns from the thread function.
                    self.worker = None
                    self._worker_generation = None
                    self._job_kind = ""
                self._handle_event(kind, value)
        except queue.Empty:
            pass
        if self.closing and not self._busy():
            self.workflow.close()
            self._destroy_window()
            return
        if not self._busy() and not self.closing:
            self.progress.stop()
            self._refresh_action()
        if self.winfo_exists():
            self._poll_handle = self.after(int(self.config_data.get("queue_poll_ms", 100)), self._poll)

    def _handle_event(self, kind: str, value: object) -> None:
        if kind == "open_error":
            self.status_var.set(f"{self.text['failed']} {value}")
            self._refresh_action()
            return
        self.progress.stop()
        self.cancel_button.configure(state="disabled")
        if kind == "error":
            self._auto_prepare = False
            from converter.core import ConversionCancelled, PartialPublishError
            phase, error = value
            if phase == "profile_load":
                self._profile_lookup_key = None
            logging.getLogger("converter.pilot").warning(
                "workflow error phase=%s error_type=%s", phase, type(error).__name__)
            if isinstance(error, PartialPublishError):
                partial = tuple(str(path) for path in
                                getattr(error, "partial_paths", getattr(error, "partial_outputs", ())))
                if self._ticket is not None:
                    self.workflow.finish(self._ticket, Outcome.PARTIAL_PUBLISH,
                                         detail=str(error), partial_outputs=partial)
                    self._ticket = None
                self.status_var.set(self.text["partial"])
                self.product_var.set(self.text["csv_partial"])
                self._set_text(self.technical_text, str(error))
                for path in partial:
                    self.file_table.insert("", "end", values=("PARTIAL", path))
                partial_dir = getattr(error, "output_dir", None)
                if partial or partial_dir:
                    self.result_dir = Path(partial_dir or Path(partial[0]).parent)
                    self.open_folder_button.configure(state="normal")
                self._select_tab(self.summary_tab)
                self._set_publish_lock(False)
                self.closing = False
                self._refresh_action()
                return
            if isinstance(error, ConversionCancelled) or self.cancel_event.is_set():
                if self._ticket is not None:
                    self.workflow.finish(self._ticket, Outcome.CANCELLED)
                    self._ticket = None
                self.status_var.set(self.text["cancelled"])
                self._set_publish_lock(False)
                self._refresh_action()
                return
            if self._ticket is not None:
                self.workflow.finish(self._ticket, Outcome.FAILED, detail=str(error))
                self._ticket = None
            if phase in {"plan", "publish"}:
                self._invalidate_plan()
            elif phase == "verify":
                self._invalidate()
                self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
            self._set_publish_lock(False)
            from app.workflow_service import WorkflowDiagnostic
            code = {"identify": "SOURCE_READ_FAILED", "analyze": "SOURCE_ANALYSIS_FAILED",
                    "plan": "PLAN_FAILED", "verify": "INPUT_CHANGED",
                    "publish": "PUBLICATION_FAILED", "relocation_load": "RELOCATION_SETTINGS_INVALID",
                    "profile_load": "APPROVED_PROFILE_INVALID",
                    "word_load": "WORD_SETTINGS_INVALID", "word_save": "WORD_SETTINGS_INVALID"}.get(phase, "WORKFLOW_FAILED")
            action = ("원본 파일과 접근 권한을 확인하세요." if phase in {"identify", "analyze"}
                      else "입력과 설정을 다시 확인한 뒤 계획을 점검하세요." if phase in {"plan", "verify"}
                      else "이미 생성된 파일을 보존하고 결과 폴더와 진단을 확인하세요.")
            if self.language == "en":
                action = ("Check the source file and access permissions." if phase in {"identify", "analyze"}
                          else "Check inputs and settings, then inspect the plan again." if phase in {"plan", "verify"}
                          else "Preserve any generated files and inspect the result folder and diagnostics.")
            self._show_diagnostics((WorkflowDiagnostic(code=code, severity="error", scope="project",
                                                      reason=type(error).__name__, action=action,
                                                      detail=str(error)),))
            self._select_tab(self.diagnostic_tab)
            self.status_var.set(self.text["failed"])
            self.result_notice_var.set(self.text["failed"])
            self._set_text(self.technical_text, f"{type(error).__name__}: {error}")
            self._refresh_action()
            return
        if self.cancel_event.is_set() and kind not in {"publish", "word_save"}:
            if self._ticket is not None:
                self.workflow.finish(self._ticket, Outcome.CANCELLED)
                self._ticket = None
            self.status_var.set(self.text["cancelled"])
            self._set_publish_lock(False)
            self._refresh_action()
            return
        if kind == "relocation_load":
            settings, path, digest = value
            if self._active_word_settings() is not None:
                self.status_var.set(self.text["relocation_conflict"])
                return
            self.relocation_settings = settings
            self.approved_profile = None
            self.relocation_settings_provenance = (path, digest)
            ranges = "; ".join(
                f"{rule.source_family}{rule.source_start}..{rule.source_start + rule.count - 1} -> "
                f"{rule.target_family}{rule.target_start}..{rule.target_start + rule.count - 1}"
                for rule in settings.rules[:8])
            if len(settings.rules) > 8:
                ranges += "; " + self.text["relocation_more"].format(count=len(settings.rules) - 8,
                                                                      total=len(settings.rules))
            self.relocation_info_var.set(self.text["relocation_applied"].format(
                name=Path(path).name, ranges=ranges, held=len(settings.held_comments)))
            self._invalidate_plan()
            self._select_tab(self.address_tab)
            return
        if kind == "word_load":
            settings, path, digest = value
            self._suspend_change = True
            try:
                self.zr_to_r_var.set(settings.zr_to_r)
                self._last_values["zr_to_r"] = settings.zr_to_r
            finally:
                self._suspend_change = False
            self.word_settings_provenance = (path, digest) if settings.zr_to_r else (None, None)
            self._invalidate_plan()
            return
        if kind == "word_save":
            prefix = (("취소 요청 중 설정 파일 저장 완료: " if self.language == "ko" else
                       "Settings file saved after cancellation was requested: ") if self.cancel_event.is_set()
                      else ("설정 파일 저장 완료: " if self.language == "ko" else "Settings file saved: "))
            self.status_var.set(prefix + str(value))
            return
        if kind == "profile_load":
            key, profile = value
            if key != self._approved_profile_key() or self.settings is not None or self.relocation_settings is not None:
                self.status_var.set(self.text["stale"])
                self._refresh_action()
                return
            if profile is not None:
                if not self._check_pending():
                    self._auto_prepare = False
                    self.confirm_var.set(False)
                    self._refresh_action()
                    return
                try:
                    self._sync_analysis_editor(self.analysis, approved_profile=profile, approved_key=key)
                except (ValueError, TypeError) as error:
                    self._handle_event("error", ("profile_load", error))
                    return
                self.settings, self.relocation_settings = profile.settings, profile.relocation_settings
                self.applied_settings_provenance = (profile.settings_source_path, profile.settings_source_sha256)
                self.relocation_settings_provenance = (profile.relocation_settings_source_path,
                                                       profile.relocation_settings_source_sha256)
                self.approved_profile = profile
                self.confirm_var.set(False)
                self.relocation_info_var.set(self.text["relocation_applied"].format(
                    name=profile.display_name, ranges=f"D -> D, T -> T, ZR -> R ({len(profile.relocation_settings.rules)})",
                    held=len(profile.relocation_settings.held_comments)))
                self._show_analysis(self.analysis, sync_editor=False)
                self._invalidate_plan()
                self._update_mapping_info()
                self._update_simple_summary()
            self._profile_lookup_key = key
            self._start_plan()
            return
        if kind == "identify":
            if self.closing:
                self.status_var.set(self.text["cancelled"])
                return
            request, identity = value
            if self.source_identity is not None and identity != self.source_identity:
                auto_prepare = self._auto_prepare
                self._invalidate()
                self._auto_prepare = auto_prepare
                self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
            elif self.analysis is not None or self.plan is not None:
                self.analysis = None
                self._invalidate_plan()
            self.confirm_var.set(False)
            self.source_identity = identity
            self.workflow.set_inputs(self.direction_var.get(), identity,
                                     self._settings_snapshot(), self._options_snapshot())
            self._ticket = self.workflow.begin_analyze()
            self._start_worker("analyze", lambda cancel: self.service.analyze(request, cancel_check=cancel))
            return
        if kind == "analyze":
            identity = getattr(value, "source_identity", None)
            if identity is not None and identity != self.source_identity:
                self._invalidate()
                self.status_var.set(self.text["stale"])
                return
            self.workflow.finish(self._ticket, Outcome.SUCCEEDED if identity else Outcome.FAILED)
            self._ticket = None
            if not self._show_analysis(value):
                self._auto_prepare = False
                self.confirm_var.set(False)
                self.result_notice_var.set(self.text["hold"])
                self._refresh_action()
                return
            if identity is not None:
                self.analysis = value
                self.source_status_var.set("완료" if self.language == "ko" else "Complete")
                self.status_var.set(self.text["source_review_checked" if self._review_mode() else "source_checked"])
                self._update_simple_summary()
                if self._auto_prepare and not self.editor.has_pending_edit():
                    self._start_plan()
                    return
            else:
                self.source_status_var.set("HOLD")
                self.status_var.set(self.text["hold"])
                self._select_tab(self.diagnostic_tab)
        elif kind == "plan":
            planned_identity = getattr(getattr(value, "analysis", None), "source_identity", None)
            if planned_identity != self.source_identity:
                self._invalidate()
                self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
                self.status_var.set(self.text["stale"])
                return
            self.plan = value
            report = getattr(getattr(value, "engine_plan", None), "report", {})
            if not isinstance(report, Mapping):
                report = {}
            import_blocked = report.get("import_readiness", {}).get("state") == "BLOCKED"
            logging.getLogger("converter.pilot").info(
                "workflow plan source_sha256=%s direction=%s cpu=%s import_state=%s blockers=%s",
                getattr(value, "source_sha256", None), self.direction_var.get(),
                report.get("target_review", {}).get("cpu"),
                report.get("import_readiness", {}).get("state", "NOT_REPORTED"),
                report.get("import_readiness", {}).get("known_blocker_occurrences", 0))
            publishable = bool(getattr(value, "publishable", False))
            self.workflow.finish(self._ticket, Outcome.SUCCEEDED, publishable=publishable,
                                 product_status=str(getattr(value, "status", "HOLD")), gx_status="NOT_RUN")
            self._ticket = None
            self._show_diagnostics(getattr(value, "diagnostics", ()))
            plan_status = str(getattr(value, "status", "HOLD"))
            self._show_result_notice(plan_status, report, publishable=publishable)
            if publishable and plan_status == "REVIEW":
                displayed_plan = self.text["plan_source_review" if self._review_mode() else "plan_review"]
            elif publishable and plan_status == "CANDIDATE":
                displayed_plan = self.text["plan_candidate"]
            elif not publishable and plan_status == "HOLD":
                displayed_plan = self.text["plan_hold"]
            else:
                displayed_plan = plan_status
            self.plan_status_var.set(displayed_plan)
            if self.direction_var.get() == Q_TO_FX5 and not self._review_mode():
                self.bounds_status_var.set(project_bounds_status(value, self.language))
            self.product_var.set(self.text["not_run"])
            missing = tuple(getattr(value, "required_settings", ()))
            missing_text = ", ".join(self.text.get(key, key) for key in missing)
            ready_key = "review_plan_ready" if self._review_mode() else "plan_ready"
            self.status_var.set(self.text[ready_key] if publishable else
                                self.text["hold"] + (" " + missing_text if missing_text else ""))
            if import_blocked and publishable:
                self.plan_status_var.set(self.text["plan_blocked"])
                self.status_var.set(self.text["plan_blocked"])
            if has_import_review(report):
                if publishable and not import_blocked:
                    self.plan_status_var.set(self.text["plan_fallback"])
                    self.status_var.set(self.text["plan_fallback"])
                self.status_var.set(self.status_var.get() + " " + self._fallback_notice())
            self._set_text(self.guide_text, self._guide())
            if self._auto_prepare and publishable and plan_status == "CANDIDATE" and not import_blocked and not has_import_review(report):
                self.status_var.set(self.text["prepared"])
            if self._auto_prepare:
                self._select_tab(self.summary_tab)
            elif not publishable:
                target_tab = (self.address_tab if "settings" in missing else
                              self.detail_tab if any("bounds" in key or key in {"max_m", "max_d"}
                                                     for key in missing) else self.diagnostic_tab)
                self._select_tab(target_tab)
        elif kind == "verify":
            if self.closing:
                self._set_publish_lock(False)
                self.status_var.set(self.text["cancelled"])
                return
            request, identity = value
            try:
                self.workflow.confirm_plan(identity, self._settings_snapshot(), self._options_snapshot())
                self._ticket = self.workflow.begin_publish(identity, self._settings_snapshot(),
                                                           self._options_snapshot())
            except (ValueError, RuntimeError) as error:
                if isinstance(error, StaleSourceError):
                    self._invalidate()
                    self.editor.set_analysis(None, self.direction_var.get().lower(), settings=None)
                else:
                    self._invalidate_plan()
                self._set_publish_lock(False)
                self.status_var.set(f"{self.text['stale']} {error}")
                return
            self._start_worker("publish", lambda cancel: self.service.publish(
                self.plan, output_parent=request.output_parent, cancel_check=cancel))
            return
        elif kind == "publish":
            completed_after_cancel = self.cancel_event.is_set()
            self.publication = value
            status = str(getattr(value, "status", "CANDIDATE"))
            report = getattr(value, "report", {})
            if not isinstance(report, Mapping):
                report = {}
            import_blocked = report.get("import_readiness", {}).get("state") == "BLOCKED"
            logging.getLogger("converter.pilot").info(
                "workflow publication source_sha256=%s status=%s cpu=%s import_state=%s blockers=%s files=%s",
                report.get("source_sha256"), status, report.get("target_review", {}).get("cpu"),
                report.get("import_readiness", {}).get("state", "NOT_REPORTED"),
                report.get("import_readiness", {}).get("known_blocker_occurrences", 0),
                len(getattr(value, "artifacts", ())))
            self.workflow.finish(self._ticket, Outcome.SUCCEEDED, product_status=status,
                                 gx_status="NOT_RUN")
            self._ticket = None
            self.product_var.set(self.text["csv_created"].format(status=status))
            self._show_result_notice(status, report, created=True)
            self._show_publication(value)
            self._select_tab(self.summary_tab)
            self.status_var.set(self.text["completed_after_cancel"] if completed_after_cancel
                                else self.text["completed_blocked"] if import_blocked
                                else self.text["completed_review"] if self._review_mode()
                                else self.text["completed"])
            if has_import_review(report):
                self.status_var.set(self.status_var.get() + " " + self._fallback_notice())
            self._set_publish_lock(False)
            if completed_after_cancel:
                self.closing = False
        self._refresh_action()

    def _sync_analysis_editor(self, result: object, *, approved_profile: object = None,
                              approved_key: tuple | None = None) -> None:
        source = getattr(result, "raw_analysis", result)
        options = {}
        settings = self.settings
        if approved_profile is not None:
            identity = getattr(result, "source_identity", None)
            if (identity is None or identity != self.source_identity or
                    approved_key != self._approved_profile_key() or self._review_mode() or
                    self.direction_var.get() != Q_TO_FX5 or not self.complete_text_var.get() or
                    approved_profile.relocation_settings.source_sha256 != identity.sha256):
                raise ValueError(self.text["stale"])
            settings = approved_profile.settings
            options = {"settings_applied": True, "applied_source_sha256": identity.sha256,
                       "applied_provenance": (approved_profile.settings_source_path,
                                               approved_profile.settings_source_sha256, settings.digest)}
        self.editor.set_analysis(source, self.direction_var.get().lower(),
                                 source_bounds=self._bounds("source"), target_bounds=self._bounds("target"),
                                 settings=settings, **options)

    def _show_analysis(self, result: object, *, sync_editor: bool = True) -> bool:
        from app.usage_editor import inventory_for_analysis
        if sync_editor and not self._review_mode():
            try:
                self._sync_analysis_editor(result, approved_profile=self.approved_profile,
                                           approved_key=self._profile_lookup_key)
            except (ValueError, TypeError) as error:
                self.status_var.set(str(error))
                return False
        source = getattr(result, "raw_analysis", result)
        pous = tuple(getattr(result, "programs", getattr(source, "pous", ())))
        program = tuple(getattr(source, "program_references", ()))
        comments = tuple(getattr(source, "comment_references", ()))
        unchecked = tuple(getattr(result, "uninspected_scope", ()))
        lines = (
            self.text["summary_counts"].format(pous=len(pous), program=len(program),
                                                comments=len(comments)),
            self.text["summary_source_uninspected"].format(count=len(unchecked)),
            self.fixed_range_var.get(), self.text["cpu_limits"],
            self.text["converter_limits"], self.text["unverified"],
        )
        if not self._review_mode():
            lines += (self.text["project_limits"],)
        if self.direction_var.get() == Q_TO_FX5:
            lines += (self.text["reverse_safety"],)
        self.summary_var.set("\n".join(lines))
        profile = Profile.FX5U if self.direction_var.get() == FX5_TO_Q else Profile.Q
        inventory = inventory_for_analysis(source, profile)
        self.usage_table.delete(*self.usage_table.get_children())
        for group in inventory.groups:
            self.usage_table.insert("", "end", values=(
                f"{group.source_start}~{group.source_end}",
                f"{group.used_points}/{group.interval_points}", group.max_access_width,
                group.program_references, group.comment_references,
                ", ".join(group.pous), ", ".join(group.locators)))
        self.usage_status_var.set(self.text["usage_uninspected"].format(
            count=len(inventory.unsupported)))
        self._show_diagnostics(getattr(result, "diagnostics", getattr(source, "diagnostics", ())))
        return True

    def _show_diagnostics(self, diagnostics: object) -> None:
        self._all_diagnostics = tuple(diagnostics or ())
        self._render_diagnostics()

    def _render_diagnostics(self) -> None:
        from app.usage_editor import group_matches_diagnostic
        self.diagnostic_table.delete(*self.diagnostic_table.get_children())
        self.diagnostic_summary_var.set("")
        self._diagnostic_items = {}
        for item in self._all_diagnostics:
            if self._selected_group is not None:
                profile = Profile.FX5U if self.direction_var.get() == FX5_TO_Q else Profile.Q
                if not group_matches_diagnostic(self._selected_group,
                                                getattr(item, "locator", None),
                                                getattr(item, "operand", None), profile, diagnostic=item):
                    continue
            row = self.diagnostic_table.insert("", "end", values=display_diagnostic_values(item, self.language))
            self._diagnostic_items[row] = item

    def _diagnostic_selected(self, _event: object = None) -> None:
        selected = self.diagnostic_table.selection()
        if not selected:
            return
        item = self._diagnostic_items[selected[0]]
        _code, _scope, location, reason, action = display_diagnostic_values(item, self.language)
        operand = str(getattr(item, "operand", "") or "")
        address = f"{operand} | " if operand else ""
        span = diagnostic_span_summary(item, self.language)
        self.diagnostic_summary_var.set("\n".join(
            part for part in (f"{address}{location}", reason, span, action) if part))
        detail = str(getattr(item, "detail", ""))
        self._set_text(self.diagnostic_detail, f"{getattr(item, 'code', '')}\n{operand}\n{detail}".strip())

    def _show_publication(self, result: object) -> None:
        artifacts = getattr(result, "artifacts", ())
        artifact_roles = getattr(result, "artifact_roles", {})
        report = getattr(result, "report", None)
        status = str(getattr(result, "status", "CANDIDATE"))
        if isinstance(artifacts, Mapping):
            artifacts = artifacts.values()
        paths = [Path(item) for item in artifacts or ()]
        self.file_table.delete(*self.file_table.get_children())
        self.selected_file_path_var.set("")
        for path in paths:
            relative = path.relative_to(result.output_dir).as_posix() if path.is_relative_to(result.output_dir) else path.name
            role = artifact_roles.get(relative, artifact_kind(path, status, report))
            self.file_table.insert("", "end", values=(role, str(path)))
        self.result_dir = Path(getattr(result, "output_dir", paths[0].parent if paths else "")) if paths or getattr(result, "output_dir", None) else None
        self.open_folder_button.configure(state="normal" if self.result_dir else "disabled")
        self._show_all_diagnostics()
        self._show_diagnostics(getattr(result, "diagnostics", ()))

    def _file_selected(self, _event: object = None) -> None:
        selected = self.file_table.selection()
        self.open_file_button.configure(state="normal" if selected else "disabled")
        self.selected_file_path_var.set(
            str(self.file_table.item(selected[0], "values")[1]) if selected else "")

    def _clear_results(self) -> None:
        self.file_table.delete(*self.file_table.get_children())
        self.selected_file_path_var.set("")
        self.diagnostic_table.delete(*self.diagnostic_table.get_children())
        self.diagnostic_summary_var.set("")
        self._all_diagnostics = ()
        self._selected_group = None
        self.related_button.configure(state="disabled")
        self.diagnostic_filter_var.set(self.text["filter_all"])
        self.result_dir = None
        self.open_folder_button.configure(state="disabled")
        self.open_file_button.configure(state="disabled")

    def _refresh_action(self) -> None:
        if self._busy() or self.closing or self.workflow.stage == Stage.CLOSED:
            self.action_button.configure(state="disabled")
            return
        if self.analysis is None:
            label = self.text["analyze"] if self.source_var.get().strip() else self.text["publish"]
            enabled = bool(self.source_var.get().strip())
        elif self.plan is None or self.workflow.stage != Stage.PLAN_READY or not self.plan.publishable:
            label = self.text["plan"]
            enabled = not self._busy()
        else:
            label = self.text["publish_review"] if self._review_mode() else self.text["publish"]
            enabled = bool(getattr(self.plan, "publishable", False) and self.confirm_var.get()
                           and self.output_parent_var.get().strip())
        if self._auto_prepare and label in {self.text["analyze"], self.text["plan"]}:
            label = self.text["prepare"]
        self.action_button.configure(text=label, state="normal" if enabled else "disabled")

    def _copy_detail(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.technical_text.get("1.0", "end-1c"))

    def _open_path(self, path: Path) -> None:
        generation = self.generation
        def run() -> None:
            try:
                if os.name == "nt":
                    os.startfile(path)
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except OSError as error:
                self.events.put((generation, "open_error", error))
        threading.Thread(target=run, name="converter-open-result", daemon=True).start()

    def _open_folder(self) -> None:
        if self.result_dir is not None:
            self._open_path(self.result_dir)

    def _open_selected_file(self) -> None:
        selected = self.file_table.selection()
        if selected:
            self._open_path(Path(self.file_table.item(selected[0], "values")[1]))

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.editor.has_pending_edit() and not messagebox.askyesno(
                self.text["title"], self.text["close_pending"], parent=self.master):
            return
        self.closing = True
        self.cancel_event.set()
        self.workflow.close()
        if self._busy():
            self.status_var.set(self.text["closed"])
            self.action_button.configure(state="disabled")
            self.cancel_button.configure(state="disabled")
        else:
            self._destroy_window()

    def _destroy_window(self) -> None:
        if self._prepare_handle is not None:
            self.after_cancel(self._prepare_handle)
            self._prepare_handle = None
        if self._retry_handle is not None:
            self.after_cancel(self._retry_handle)
            self._retry_handle = None
        if self._poll_handle is not None:
            self.after_cancel(self._poll_handle)
            self._poll_handle = None
        self.progress.stop()
        self.master.destroy()
