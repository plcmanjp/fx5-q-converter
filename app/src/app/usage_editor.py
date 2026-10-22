"""Embedded, source-usage-driven X/Y editor for the current work item.

Address interpretation and collision checks belong to xy_mapping. This view
never supplies CPU ceilings or assigns a target address on the user's behalf.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable

from app.xy_settings_dialog import MAX_EDITOR_POINTS, range_rows, save_new_settings, set_form_busy
from converter.core import _handle_final_path, _reject_link_like_ancestors, _stat_identity
from converter.xy_mapping import (
    Bounds, MappingError, Profile, Reference, Rule, UnmappedAddressError, UnmappedPolicy, _OPERAND,
    _format, _parse_plain, fixed_bounds,
)
from converter.xy_settings import MappingSettings, MAX_RULES, MAX_SETTINGS_BYTES, preview


# Legacy parser argument, with no address ceiling.
_SYNTAX_CEILING = Bounds()
_IO_PREFIX = re.compile(r"(?:K[0-9]+)?[XY]", re.ASCII)
_TEXT_INDEX = re.compile(r"(?:ZZ|LZ|Z)[0-9]+", re.ASCII)


def _effective_bounds(bounds: Bounds | None) -> Bounds:
    """An absent operator limit is unchecked, never a verified CPU limit."""
    return bounds if bounds is not None else Bounds()
_TEXT = {
    "ko": {
        "policy": "미배정 주소 처리", "source": "원본 구간", "used": "사용/구간",
        "target": "대상 시작", "end": "계산된 끝", "pou": "POU", "refs": "참조",
        "comments": "코멘트", "state": "상태", "access": "최대 접근 폭", "selected_target": "선택 구간 대상 시작",
        "usage_type": "참조 유형", "comment_only": "코멘트 전용", "program_only": "프로그램", "both": "프로그램+코멘트",
        "allowed": "주소 표기: 원본 {source}, 대상 {target}. 디바이스 주소 상한은 적용하지 않습니다.",
        "policy_hold": "미배정 주소는 보류", "policy_spelling": "원본 주소 철자 유지",
        "policy_numeric": "같은 번호를 대상 진수로 표시",
        "policy_help_hold": "대상 배정이 없는 주소는 보류합니다.",
        "policy_help_spelling": "예: FX5 X10은 Q X10으로 표기됩니다.",
        "policy_help_numeric": "예: FX5 X10(8진)은 Q X8(16진)으로 표기됩니다.",
        "policy_help_spelling_reverse": "예: Q X10은 FX5 X10으로 표기됩니다.",
        "policy_help_numeric_reverse": "예: Q X10(16진)은 FX5 X20(8진)으로 표기됩니다.",
        "pending_count": "미적용 변경 {count}건. 구간 입력을 반영하고 현재 작업에 적용한 뒤 계획을 점검하세요.",
        "not_applied": "배정이 현재 작업에 적용되지 않았습니다. 원본 사용 현황을 확인하고 필요한 배정을 적용하세요.",
        "ready_count": "현재 작업에 적용된 배정입니다. 계획을 점검하세요.",
        "technical_toggle": "설정 파일과 해시 보기",
        "commit_row": "구간 입력 반영", "discard_row": "입력 취소",
        "discard_changes": "배정 변경 취소",
        "apply": "현재 작업에 적용", "load": "설정 불러오기", "save": "새 파일로 저장",
        "reverse": "반대 방향 후보 확인", "initial": "원본 사용 주소를 확인하고 대상 시작 주소를 입력하세요.",
        "assigned": "배정", "unassigned": "미배정", "verify": "검증 필요",
        "bounds_needed": "주소 상한 없음, 하드웨어 설정은 별도 확인", "bounds_set": "주소 상한 없음, 하드웨어 설정은 별도 확인",
        "summary": "{source} ({source_radix}진수) -> {target} ({target_radix}진수) | 실제 사용 {points}점, 연속 구간 {groups}개 | {bounds}",
        "unused_existing": " | 기존 규칙의 미사용 주소 {count}점 유지",
        "unsupported": "확인하지 못한 X/Y 참조 {count}건. 적용을 보류합니다.",
        "row_pending": "미반영 구간 입력이 있습니다. 입력 반영 또는 취소 후 행을 바꾸세요.",
        "detail": "영향 POU: {pous} | 위치: {locators}", "none": "없음",
        "discarded": "선택 구간 입력을 취소했습니다.",
        "changes_discarded": "미적용 배정 변경을 취소했습니다. 원본이나 방향을 바꿀 수 있습니다.",
        "row_committed": "구간 입력을 반영했습니다. 현재 작업에 적용하면 전체 참조를 다시 점검합니다.",
        "applied": "현재 작업에 적용했습니다. 계획을 점검하세요.",
        "bounds_changed": "I/O 상한이 바뀌었습니다. 현재 작업에 다시 적용해 전체 참조를 점검하세요.",
        "loading": "설정 파일을 읽는 중입니다.",
        "loaded": "설정을 불러왔습니다. 현재 작업에 적용해 전체 참조를 점검하세요.",
        "saving": "새 설정 파일을 저장하는 중입니다.",
        "saved": "새 설정 파일을 저장했습니다. 현재 작업에 적용은 별도로 선택하세요.",
        "reversed": "반대 방향 후보 검증을 통과했습니다. 새 원본과 범위를 확인한 뒤 별도 작업에 적용하세요.",
        "loaded_identity": "불러온 설정: {path} | 파일 SHA-256: {file_sha} | 설정 SHA-256: {canonical_sha}",
        "reverse_list": "반대 방향 후보 규칙", "reverse_save": "후보를 새 파일로 저장",
        "reverse_collapse": "후보 표 접기",
        "reverse_note": "후보 {source} → {target}: 새 원본을 확인하세요. 고정 X/Y 검사와 별도로 대상 프로젝트 적합성을 확인하세요.",
        "reverse_stale": "후보 생성 후 현재 주소 설정이 바뀌었습니다. 후보를 다시 확인하세요.",
        "error": "보류: {reason} 대상 시작 주소와 접근 구간을 확인한 뒤 다시 적용하세요.",
    },
    "en": {
        "policy": "Unmapped address handling", "source": "Source range", "used": "Used/range",
        "target": "Target start", "end": "Calculated end", "pou": "POU", "refs": "References",
        "comments": "Comments", "state": "State", "access": "Max access width", "selected_target": "Selected range target start",
        "usage_type": "Reference type", "comment_only": "Comments only", "program_only": "Program", "both": "Program + comments",
        "allowed": "Address notation: source {source}, target {target}. No device address ceiling is applied.",
        "policy_hold": "Hold unmapped addresses", "policy_spelling": "Keep source address spelling",
        "policy_numeric": "Show same number in target radix",
        "policy_help_hold": "An address without a target mapping remains on hold.",
        "policy_help_spelling": "Example: FX5 X10 is written as Q X10.",
        "policy_help_numeric": "Example: FX5 X10 (octal) is written as Q X8 (hex).",
        "policy_help_spelling_reverse": "Example: Q X10 is written as FX5 X10.",
        "policy_help_numeric_reverse": "Example: Q X10 (hex) is written as FX5 X20 (octal).",
        "pending_count": "{count} unapplied changes. Commit the range input, apply to this work, then check the plan.",
        "not_applied": "No mapping has been applied to this work. Review source usage and apply any required mapping.",
        "ready_count": "Mapping applied to this work. Check the plan.",
        "technical_toggle": "Show settings file and hashes",
        "commit_row": "Commit range input", "discard_row": "Discard input",
        "discard_changes": "Discard mapping changes",
        "apply": "Apply to current work", "load": "Load settings", "save": "Save as new file",
        "reverse": "Check reverse candidate", "initial": "Review used source addresses and enter target starts.",
        "assigned": "Assigned", "unassigned": "Unassigned", "verify": "Check needed",
        "bounds_needed": "No address ceiling; hardware settings checked separately", "bounds_set": "No address ceiling; hardware settings checked separately",
        "summary": "{source} (base {source_radix}) -> {target} (base {target_radix}) | {points} used points, {groups} contiguous ranges | {bounds}",
        "unused_existing": " | {count} unused points retained from existing rules",
        "unsupported": "{count} X/Y references could not be inspected. Apply is blocked.",
        "row_pending": "Commit or discard the current range input before changing rows.",
        "detail": "Affected POU: {pous} | Locations: {locators}", "none": "None",
        "discarded": "Selected range input discarded.",
        "changes_discarded": "Unapplied mapping changes discarded. You can change the source or direction.",
        "row_committed": "Range input committed. Apply to recheck all references.",
        "applied": "Applied to current work. Check the plan.",
        "bounds_changed": "I/O bounds changed. Apply again to recheck all references.",
        "loading": "Reading settings file.",
        "loaded": "Settings loaded. Apply to recheck all references.",
        "saving": "Saving a new settings file.",
        "saved": "New settings file saved. Apply to current work separately.",
        "reversed": "Reverse candidate validated. Check its new source and bounds before use.",
        "loaded_identity": "Loaded settings: {path} | file SHA-256: {file_sha} | canonical settings SHA-256: {canonical_sha}",
        "reverse_list": "Reverse candidate rules", "reverse_save": "Save candidate as new file",
        "reverse_collapse": "Collapse candidate table",
        "reverse_note": "Candidate {source} → {target}: confirm the new source. Check target-project suitability separately from fixed X/Y validation.",
        "reverse_stale": "Current mapping changed after candidate creation. Check the candidate again.",
        "error": "Blocked: {reason} Check the target start and access span, then apply again.",
    },
}


def direction_profiles(direction: str) -> tuple[Profile, Profile]:
    normalized = direction.strip().lower()
    if normalized in {"fx5_to_q", "fx5->q"}:
        return Profile.FX5U, Profile.Q
    if normalized in {"q_to_fx5", "q->fx5"}:
        return Profile.Q, Profile.FX5U
    raise ValueError(f"지원하지 않는 변환 방향: {direction}")


@dataclass(frozen=True)
class UsageGroup:
    rule_id: str
    family: str
    first: int
    last: int
    source_start: str
    source_end: str
    used_points: int
    interval_points: int
    pous: tuple[str, ...]
    program_references: int
    comment_references: int
    locators: tuple[str, ...]
    max_access_width: int = 1


@dataclass(frozen=True)
class UsageInventory:
    groups: tuple[UsageGroup, ...]
    program: tuple[Reference, ...]
    comments: tuple[Reference, ...]
    unsupported: tuple[str, ...]
    source_sha256: str | None
    complete_text: bool = False


def _raw_references(analysis, kind: str):
    for ordinal, item in enumerate(getattr(analysis, kind, ())):
        if hasattr(item, "spelling"):
            spelling = item.spelling
            locator = item.locator
            pou = item.pou
        elif kind == "program_references":
            spelling = item.raw_token
            locator = item.source_locator
            pou = item.pou
        else:
            spelling = item.device
            locator = item.source_locator
            pou = ""
        yield spelling, locator, pou, getattr(item, "status", "decoded"), ordinal


def inventory_for_analysis(analysis, source: Profile) -> UsageInventory:
    """Collect distinct actual points, including grouped operands and comments.

    Numeric intervals are formed only from adjacent used points. An unsupported
    X/Y-like token is reported and never silently omitted from apply preflight.
    """
    if analysis is None:
        return UsageInventory((), (), (), (), None)
    analysis = getattr(analysis, "raw_analysis", analysis)
    if analysis is None:
        return UsageInventory((), (), (), (), None)
    complete_text = getattr(analysis, "complete_text", False) is True
    uses: dict[tuple[str, int], list[tuple[str, str, str, int, int]]] = {}
    program: list[Reference] = []
    comments: list[Reference] = []
    unsupported: list[str] = []
    unsupported.extend(f"판독되지 않은 POU: {pou}"
                       for pou in getattr(analysis, "unchecked_pous", ()))
    target_checks = {"TARGET_PROJECT_BOUNDS_NOT_CHECKED", "target ceilings",
                     "target mapping", "GX import validation"}
    unsupported.extend(f"판독되지 않은 범위: {scope}"
                       for scope in getattr(analysis, "uninspected_scope", ())
                       if scope not in target_checks)
    for kind, sink in (("program_references", program), ("comment_references", comments)):
        for spelling, locator, pou, status, ordinal in _raw_references(analysis, kind):
            if not isinstance(spelling, str) or not _IO_PREFIX.match(spelling.upper()):
                continue
            match = _OPERAND.fullmatch(spelling.upper())
            if (match is None or status != "decoded" or
                    (match.group(4) and not (complete_text and _TEXT_INDEX.fullmatch(match.group(4))))):
                unsupported.append(f"{locator}: {spelling}")
                continue
            prefix, family, digits, _ = match.groups()
            try:
                first = _parse_plain(family + digits, source, _SYNTAX_CEILING)
            except MappingError:
                unsupported.append(f"{locator}: {spelling}")
                continue
            width = 4 * int(prefix[1]) if prefix else 1
            base = spelling[:-len(match.group(4))] if match.group(4) else spelling
            sink.append(Reference(base, locator))
            for number in range(first.number, first.number + width):
                uses.setdefault((family, number), []).append((kind, pou, locator, ordinal, width))
    groups: list[UsageGroup] = []
    for family in ("X", "Y"):
        numbers = sorted(number for key_family, number in uses if key_family == family)
        if not numbers:
            continue
        runs: list[list[int]] = []
        for number in numbers:
            if not runs or number != runs[-1][-1] + 1:
                runs.append([number])
            else:
                runs[-1].append(number)
        for index, run in enumerate(runs, 1):
            first, last = run[0], run[-1]
            # Each source reference counts once per group, even if its operand
            # footprint covers several points in the group.
            program_keys: set[int] = set()
            comment_keys: set[int] = set()
            pous: set[str] = set()
            locators: set[str] = set()
            widths: set[int] = set()
            for number in run:
                for kind, pou, locator, ordinal, width in uses[(family, number)]:
                    (program_keys if kind == "program_references" else comment_keys).add(ordinal)
                    widths.add(width)
                    if pou:
                        pous.add(pou)
                    locators.add(locator)
            groups.append(UsageGroup(
                f"{family}{index:03d}", family, first, last,
                _format(family, first, source), _format(family, last, source),
                len(run), last - first + 1, tuple(sorted(pous)),
                len(program_keys), len(comment_keys), tuple(sorted(locators)), max(widths),
            ))
    return UsageInventory(tuple(groups), tuple(program), tuple(comments),
                          tuple(dict.fromkeys(unsupported)),
                          getattr(analysis, "source_sha256", None), complete_text)


def group_matches_diagnostic(group: UsageGroup, locator: str | None,
                             operand: str | None, source: Profile, *, diagnostic=None) -> bool:
    """Require every supplied source locator and operand to match the group."""
    if getattr(diagnostic, "code", None) == "XY_TARGET_ALIAS":
        try:
            detail = json.loads(getattr(diagnostic, "detail", ""))
        except (TypeError, ValueError):
            return False
        if not isinstance(detail, dict):
            return False
        sources, locators = detail.get("sources"), detail.get("locators")
        if (not isinstance(sources, list) or not isinstance(locators, list) or
                not all(isinstance(item, str) and item for item in (*sources, *locators))):
            return False
        return (any(group_matches_diagnostic(group, None, spelling, source) for spelling in sources)
                and any(group_matches_diagnostic(group, location, None, source) for location in locators))
    locator_matches = not locator or any(locator == item or locator.startswith(item + ":") or
                                         item.startswith(locator + ":") for item in group.locators)
    if not locator_matches:
        return False
    if not operand:
        return bool(locator)
    match = _OPERAND.fullmatch(operand.upper())
    if match is None or match.group(4):
        return False
    try:
        address = _parse_plain(match.group(2) + match.group(3), source, _SYNTAX_CEILING)
    except MappingError:
        return False
    return group.family == address.family and group.first <= address.number <= group.last


def settings_for_targets(inventory: UsageInventory, *, source: Profile, target: Profile,
                         source_bounds: Bounds | None, target_bounds: Bounds | None,
                         targets: dict[str, str], unmapped: UnmappedPolicy) -> MappingSettings:
    if len(inventory.groups) > MAX_RULES:
        raise MappingError(f"사용 구간이 설정 한도 {MAX_RULES}개를 넘습니다")
    if sum(group.interval_points for group in inventory.groups) > MAX_EDITOR_POINTS:
        raise MappingError(f"사용 구간 합계가 편집기 한도 {MAX_EDITOR_POINTS}점을 넘습니다")
    if inventory.unsupported:
        raise MappingError(f"확인하지 못한 X/Y 참조 {len(inventory.unsupported)}건이 있습니다")
    if not inventory.source_sha256:
        raise MappingError("원본 해시를 확인하지 못했습니다")
    rules = tuple(Rule(group.rule_id, group.source_start, group.interval_points,
                       targets[group.rule_id].strip().upper())
                  for group in inventory.groups if targets.get(group.rule_id, "").strip())
    settings = MappingSettings(source, target, unmapped, rules)
    _require_current_usage(settings, inventory, source_bounds, target_bounds)
    return settings


def _require_current_usage(settings: MappingSettings, inventory: UsageInventory,
                           source_bounds: Bounds | None, target_bounds: Bounds | None, *,
                           allow_target_aliases: bool | None = None) -> None:
    if inventory.unsupported:
        raise MappingError(f"확인하지 못한 X/Y 참조 {len(inventory.unsupported)}건이 있습니다")
    if not inventory.source_sha256:
        raise MappingError("원본 해시를 확인하지 못했습니다")
    source_bounds, target_bounds = _effective_bounds(source_bounds), _effective_bounds(target_bounds)
    if allow_target_aliases is None:
        allow_target_aliases = inventory.complete_text
    result = preview(settings, source_bounds=source_bounds, target_bounds=target_bounds,
                     input_sha256=inventory.source_sha256,
                     program=inventory.program, comments=inventory.comments,
                     allow_target_aliases=allow_target_aliases)
    result.require_publishable(settings=settings, input_sha256=inventory.source_sha256,
                               source_bounds=source_bounds, target_bounds=target_bounds,
                               allow_target_aliases=allow_target_aliases)


def _overlay_used_edits(settings: MappingSettings, inventory: UsageInventory,
                        edited_targets: dict[str, str], *, source_bounds: Bounds | None,
                        target_bounds: Bounds | None, unmapped: UnmappedPolicy) -> MappingSettings:
    """Replace only edited used intervals while preserving imported v1 rules.

    Unused portions of an imported rule remain mapped. All calculations use the
    existing mapper's source and target codecs, then full preflight validates
    the resulting rule set and references.
    """
    source_bounds, target_bounds = _effective_bounds(source_bounds), _effective_bounds(target_bounds)
    mapper = settings.mapper(source_bounds=source_bounds, target_bounds=target_bounds,
                             allow_target_aliases=inventory.complete_text)
    rules = list(settings.rules)
    used_ids = {rule.rule_id for rule in rules}

    def identifier(base: str) -> str:
        if base not in used_ids:
            used_ids.add(base)
            return base
        index = 1
        while True:
            suffix = f"_{index}"
            value = base[:64 - len(suffix)] + suffix
            if value not in used_ids:
                used_ids.add(value)
                return value
            index += 1

    for group in inventory.groups:
        if group.rule_id not in edited_targets:
            continue
        preserved: list[Rule] = []
        for rule in rules:
            if not rule.enabled:
                preserved.append(rule)
                continue
            first = _parse_plain(rule.source_start, settings.source, source_bounds)
            last = first.number + rule.count - 1
            if first.family != group.family or last < group.first or first.number > group.last:
                preserved.append(rule)
                continue
            # Keep the original rule ID on the first surviving fragment.
            fragments = ((first.number, min(last, group.first - 1)),
                         (max(first.number, group.last + 1), last))
            first_fragment = True
            for start, end in fragments:
                if start > end:
                    continue
                source_start = _format(group.family, start, settings.source)
                target_start = mapper.project(Reference(source_start,
                                                        f"preserved:{rule.rule_id}")).target
                preserved.append(Rule(rule.rule_id if first_fragment else identifier(rule.rule_id),
                                      source_start, end - start + 1, target_start))
                first_fragment = False
        target_start = edited_targets[group.rule_id]
        if target_start:
            preserved.append(Rule(identifier(group.rule_id), group.source_start,
                                  group.interval_points, target_start))
        rules = preserved
    return replace(settings, unmapped=unmapped, rules=tuple(rules))


def read_settings_snapshot(path: Path) -> tuple[bytes, str]:
    """Read at most the v1 limit from one regular no-link settings file."""
    path = _reject_link_like_ancestors(Path(path), "settings")
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > MAX_SETTINGS_BYTES:
        raise MappingError("설정 파일 형식 또는 크기가 올바르지 않습니다")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before):
            raise MappingError("설정 파일이 읽는 동안 변경되었습니다")
        final_path = _handle_final_path(descriptor)
        intended = os.path.normcase(os.path.abspath(path.resolve()))
        if final_path != intended:
            raise MappingError("설정 파일의 실제 경로가 선택 경로와 다릅니다")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(8192, MAX_SETTINGS_BYTES + 1 - total))
            if not block:
                break
            total += len(block)
            if total > MAX_SETTINGS_BYTES:
                raise MappingError("설정 파일 크기 제한을 넘었습니다")
            chunks.append(block)
        body = b"".join(chunks)
        if not body or _stat_identity(os.fstat(descriptor)) != _stat_identity(opened):
            raise MappingError("설정 파일이 읽는 동안 변경되었습니다")
        _reject_link_like_ancestors(path, "settings")
        if _stat_identity(path.stat(follow_symlinks=False)) != _stat_identity(opened):
            raise MappingError("설정 파일 경로가 읽는 동안 변경되었습니다")
        return body, hashlib.sha256(body).hexdigest()
    finally:
        os.close(descriptor)


class UsageEditor(ttk.Frame):
    """Frame API for the unified workflow; callbacks receive in-memory settings."""

    def __init__(self, parent, *, analysis=None, direction: str,
                 source_bounds: Bounds | None = None, target_bounds: Bounds | None = None,
                 settings: MappingSettings | None = None,
                 on_apply: Callable[[MappingSettings], None] | None = None,
                 on_select: Callable[[UsageGroup], None] | None = None,
                 language: str = "ko"):
        source, target = direction_profiles(direction)
        super().__init__(parent)
        self.on_apply = on_apply
        self.on_select = on_select
        self.language = language if language in _TEXT else "ko"
        self._status_key = "initial"
        self._status_args: dict[str, object] = {}
        self.source, self.target = source, target
        self.source_bounds, self.target_bounds = source_bounds, target_bounds
        self.inventory = inventory_for_analysis(analysis, source)
        self.applied_settings: MappingSettings | None = None
        self.loaded_settings_path: Path | None = None
        self.loaded_settings_sha256: str | None = None
        self.loaded_settings_canonical_sha256: str | None = None
        self._draft_settings = settings or self._default_settings()
        self._applied_provenance = (None, None, None)
        self._validate_direction(self._draft_settings)
        self._validate_loaded_rules(self._draft_settings)
        self._targets = self._targets_from_settings(self._draft_settings)
        self._base_targets = dict(self._targets)
        self._edited_targets: set[str] = set()
        self._selected_id: str | None = None
        self._entry_snapshot = ""
        self._policy_snapshot = self._draft_settings.unmapped.value
        self._show_technical = tk.BooleanVar(value=False)
        self._io_serial = 0
        self._io_busy = False
        self._external_busy = False
        self._bounds_invalidated = settings is not None
        self.reverse_candidate_settings: MappingSettings | None = None
        self._reverse_basis: tuple[object, ...] | None = None
        self._build()
        self._refresh()

    def _validate_direction(self, settings: MappingSettings) -> None:
        if (settings.source, settings.target) != (self.source, self.target):
            raise MappingError("설정 방향이 현재 작업과 다릅니다")

    def _default_settings(self) -> MappingSettings:
        policy = UnmappedPolicy.NUMERIC_IDENTITY if self.inventory.complete_text else UnmappedPolicy.HOLD
        return MappingSettings(self.source, self.target, policy, ())

    def _validate_loaded_rules(self, settings: MappingSettings) -> None:
        self._validate_direction(settings)
        settings.mapper(source_bounds=_effective_bounds(self.source_bounds),
                        target_bounds=_effective_bounds(self.target_bounds),
                        allow_target_aliases=self.inventory.complete_text)

    def _targets_from_settings(self, settings: MappingSettings) -> dict[str, str]:
        mapper = settings.mapper(source_bounds=_effective_bounds(self.source_bounds),
                                 target_bounds=_effective_bounds(self.target_bounds),
                                 allow_target_aliases=self.inventory.complete_text)
        targets: dict[str, str] = {}
        unrepresentable: set[str] = set()
        for group in self.inventory.groups:
            checkpoints = {group.first, group.last}
            has_rule = False
            for rule in settings.rules:
                if not rule.enabled:
                    continue
                start = _parse_plain(rule.source_start, self.source,
                                     _effective_bounds(self.source_bounds))
                end = start.number + rule.count - 1
                if start.family != group.family or end < group.first or start.number > group.last:
                    continue
                has_rule = True
                checkpoints.update(number for number in (start.number - 1, start.number, end, end + 1)
                                   if group.first <= number <= group.last)
            try:
                first = mapper.project(Reference(group.source_start, f"usage:{group.rule_id}"))
                start_number = _parse_plain(first.target, self.target,
                                            _effective_bounds(self.target_bounds)).number
                continuous = True
                # Each rule and each identity gap is linear only when its ends
                # agree with the same displacement. Inspect all internal edges.
                for number in checkpoints:
                    spelling = _format(group.family, number, self.source)
                    projected = mapper.project(Reference(spelling, f"usage:{group.rule_id}"))
                    target_number = _parse_plain(projected.target, self.target,
                                                  _effective_bounds(self.target_bounds)).number
                    if target_number != start_number + number - group.first:
                        continuous = False
                        break
                targets[group.rule_id] = first.target if continuous else ""
                if not continuous:
                    unrepresentable.add(group.rule_id)
            except UnmappedAddressError:
                targets[group.rule_id] = ""
                if has_rule:
                    unrepresentable.add(group.rule_id)
            except MappingError:
                targets[group.rule_id] = ""
                unrepresentable.add(group.rule_id)
        self._unrepresentable_groups = unrepresentable
        return targets

    def _unused_rule_points(self) -> int:
        total = 0
        for rule in self._draft_settings.rules:
            if not rule.enabled:
                continue
            try:
                first = _parse_plain(rule.source_start, self.source,
                                     _effective_bounds(self.source_bounds))
            except MappingError:
                continue
            last = first.number + rule.count - 1
            used = sum(max(0, min(last, group.last) - max(first.number, group.first) + 1)
                       for group in self.inventory.groups if group.family == first.family)
            total += rule.count - used
        return total

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.summary = tk.StringVar()
        ttk.Label(self, textvariable=self.summary, wraplength=850).grid(
            row=0, column=0, sticky="ew", padx=8, pady=5)
        policy_row = ttk.Frame(self)
        policy_row.grid(row=1, column=0, sticky="w", padx=8)
        self._labels: dict[str, ttk.Label] = {}
        self._buttons: dict[str, ttk.Button] = {}
        self._labels["policy"] = ttk.Label(policy_row)
        self._labels["policy"].pack(side="left")
        self.policy = tk.StringVar(value=self._draft_settings.unmapped.value)
        self.policy.trace_add("write", lambda *_args: self._policy_changed())
        self.policy_display = tk.StringVar()
        self.policy_box = ttk.Combobox(policy_row, textvariable=self.policy_display,
                                       state="readonly", width=30)
        self.policy_box.pack(side="left", padx=6)
        self.policy_box.bind("<<ComboboxSelected>>", self._policy_selected)
        self.policy_help = tk.StringVar()
        ttk.Label(policy_row, textvariable=self.policy_help, wraplength=380).pack(side="left", padx=6)
        columns = ("source", "used", "usage_type", "target", "end", "pou", "refs", "comments", "access", "state")
        self.table = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse", height=8)
        headings = columns
        widths = (145, 90, 120, 95, 95, 55, 55, 70, 110, 85)
        for key, heading, width in zip(columns, headings, widths):
            self.table.heading(key, text=self._t(heading))
            self.table.column(key, width=width, minwidth=width, stretch=True)
        self.table.grid(row=2, column=0, sticky="nsew", padx=8, pady=5)
        self.table.bind("<<TreeviewSelect>>", self._selected)
        form = ttk.Frame(self)
        form.grid(row=3, column=0, sticky="ew", padx=8)
        self._labels["selected_target"] = ttk.Label(form)
        self._labels["selected_target"].pack(side="left")
        self.target_entry = tk.StringVar()
        self.target_entry.trace_add("write", lambda *_args: self._refresh_edit_state())
        ttk.Entry(form, textvariable=self.target_entry, width=16).pack(side="left", padx=5)
        self._buttons["commit_row"] = ttk.Button(form, command=lambda: self._run(self.commit_row))
        self._buttons["commit_row"].pack(side="left")
        self._buttons["discard_row"] = ttk.Button(form, command=self.discard_row)
        self._buttons["discard_row"].pack(side="left", padx=5)
        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky="w", padx=8, pady=5)
        for key, action in (("apply", self.apply_current), ("load", self.load_settings),
                            ("discard_changes", self.discard_changes),
                            ("save", self.save_settings), ("reverse", self.reverse_candidate)):
            button = ttk.Button(actions, command=lambda fn=action: self._run(fn))
            button.pack(side="left", padx=5)
            self._buttons[key] = button
        self.status = tk.StringVar(value=self._t("initial"))
        ttk.Label(self, textvariable=self.status, wraplength=850).grid(
            row=5, column=0, sticky="ew", padx=8, pady=5)
        self.edit_state = tk.StringVar()
        ttk.Label(self, textvariable=self.edit_state, wraplength=850).grid(
            row=6, column=0, sticky="ew", padx=8, pady=5)
        self.details = tk.StringVar()
        ttk.Label(self, textvariable=self.details, wraplength=850).grid(
            row=7, column=0, sticky="ew", padx=8, pady=5)
        self.settings_identity = tk.StringVar()
        self.technical_toggle = ttk.Checkbutton(self, variable=self._show_technical,
                                                command=self._toggle_technical)
        self.technical_toggle.grid(row=9, column=0, sticky="w", padx=8)
        self.technical_identity = ttk.Label(self, textvariable=self.settings_identity, wraplength=850)
        self.technical_identity.grid(row=10, column=0, sticky="ew", padx=8, pady=5)
        self.technical_identity.grid_remove()
        self.reverse_panel = ttk.LabelFrame(self)
        self.reverse_panel.grid(row=11, column=0, sticky="ew", padx=8, pady=5)
        self.reverse_panel.columnconfigure(0, weight=1)
        reverse_columns = ("id", "source", "source_end", "count", "target", "target_end")
        self.reverse_table = ttk.Treeview(self.reverse_panel, columns=reverse_columns,
                                          show="headings", height=4)
        reverse_headings = ("ID", "source", "end", "used", "target", "end")
        for key, heading in zip(reverse_columns, reverse_headings):
            self.reverse_table.heading(key, text=heading if heading == "ID" else self._t(heading))
            self.reverse_table.column(key, width=105, stretch=True)
        self.reverse_table.grid(row=0, column=0, sticky="ew")
        self.reverse_y_scroll = ttk.Scrollbar(self.reverse_panel, orient="vertical",
                                              command=self.reverse_table.yview)
        self.reverse_y_scroll.grid(row=0, column=1, sticky="ns")
        self.reverse_table.configure(yscrollcommand=self.reverse_y_scroll.set)
        self.reverse_x_scroll = ttk.Scrollbar(self.reverse_panel, orient="horizontal",
                                              command=self.reverse_table.xview)
        self.reverse_x_scroll.grid(row=1, column=0, sticky="ew")
        self.reverse_table.configure(xscrollcommand=self.reverse_x_scroll.set)
        self.reverse_note = tk.StringVar()
        ttk.Label(self.reverse_panel, textvariable=self.reverse_note, wraplength=830).grid(
            row=2, column=0, sticky="w")
        self.reverse_save_button = ttk.Button(
            self.reverse_panel, command=lambda: self._run(self.save_reverse_candidate))
        self.reverse_save_button.grid(row=3, column=0, sticky="w", pady=4)
        self.reverse_collapse_button = ttk.Button(
            self.reverse_panel, command=self.collapse_reverse_candidate)
        self.reverse_collapse_button.grid(row=3, column=0, sticky="e", pady=4)
        self.reverse_panel.grid_remove()
        self.table.configure(xscrollcommand=lambda first, last: self.x_scroll.set(first, last))
        self.x_scroll = ttk.Scrollbar(self, orient="horizontal", command=self.table.xview)
        self.x_scroll.grid(row=8, column=0, sticky="ew", padx=8)
        self.table.configure(yscrollcommand=lambda first, last: self.y_scroll.set(first, last))
        self.y_scroll = ttk.Scrollbar(self, orient="vertical", command=self.table.yview)
        self.y_scroll.grid(row=2, column=1, sticky="ns", pady=5)
        self.set_language(self.language)

    def _t(self, key: str, **values) -> str:
        return _TEXT[self.language][key].format(**values)

    def _set_status(self, key: str, **values) -> None:
        self._status_key, self._status_args = key, values
        self.status.set(self._t(key, **values))

    def set_external_busy(self, busy: bool) -> None:
        """Freeze editor controls during a parent verification/publication job."""
        self._external_busy = bool(busy)
        self._refresh_busy_controls()

    def is_busy(self) -> bool:
        return self._io_busy or self._external_busy

    def _refresh_busy_controls(self) -> None:
        busy = self._external_busy or self._io_busy
        set_form_busy(self, busy)
        self.table.state(["disabled"] if busy else ["!disabled"])
        self.reverse_table.state(["disabled"] if busy else ["!disabled"])
        self.reverse_save_button.state(
            ["disabled"] if busy or self.reverse_candidate_settings is None else ["!disabled"])

    def set_language(self, language: str) -> None:
        if language not in _TEXT:
            raise ValueError(f"unsupported UI language: {language}")
        self.language = language
        for key, label in self._labels.items():
            label.configure(text=self._t(key))
        for key, button in self._buttons.items():
            button.configure(text=self._t(key))
        for key in ("source", "used", "usage_type", "target", "end", "pou", "refs", "comments", "access", "state"):
            self.table.heading(key, text=self._t(key))
        self.policy_box.configure(values=tuple(self._policy_labels().values()))
        self._refresh_policy_display()
        self._refresh_policy_help()
        self.technical_toggle.configure(text=self._t("technical_toggle"))
        self.reverse_panel.configure(text=self._t("reverse_list"))
        self.reverse_save_button.configure(text=self._t("reverse_save"))
        self.reverse_collapse_button.configure(text=self._t("reverse_collapse"))
        if self.reverse_candidate_settings is not None:
            self.reverse_note.set(self._t("reverse_note",
                                          source=self.reverse_candidate_settings.source.display_name,
                                          target=self.reverse_candidate_settings.target.display_name))
        self.status.set(self._t(self._status_key, **self._status_args))
        self._refresh_identity()
        self._refresh()
        self._refresh_busy_controls()

    def _policy_labels(self) -> dict[str, str]:
        return {UnmappedPolicy.HOLD.value: self._t("policy_hold"),
                UnmappedPolicy.SOURCE_SPELLING_IDENTITY.value: self._t("policy_spelling"),
                UnmappedPolicy.NUMERIC_IDENTITY.value: self._t("policy_numeric")}

    def _refresh_policy_display(self) -> None:
        self.policy_display.set(self._policy_labels().get(self.policy.get(), self._t("policy_hold")))

    def _policy_selected(self, _event=None) -> None:
        for value, label in self._policy_labels().items():
            if label == self.policy_display.get():
                self.policy.set(value)
                self._refresh_edit_state()
                return

    def _policy_changed(self) -> None:
        if not hasattr(self, "policy_display"):
            return
        self._refresh_policy_display()
        self._refresh_policy_help()
        self._refresh_edit_state()

    def _refresh_policy_help(self) -> None:
        key = {UnmappedPolicy.HOLD.value: "policy_help_hold",
               UnmappedPolicy.SOURCE_SPELLING_IDENTITY.value: "policy_help_spelling",
               UnmappedPolicy.NUMERIC_IDENTITY.value: "policy_help_numeric"}.get(
                   self.policy.get(), "policy_help_hold")
        if self.source is Profile.Q and key != "policy_help_hold":
            key += "_reverse"
        self.policy_help.set(self._t(key))

    def _toggle_technical(self) -> None:
        if self._show_technical.get():
            self.technical_identity.grid()
        else:
            self.technical_identity.grid_remove()

    def _refresh_edit_state(self) -> None:
        if not hasattr(self, "edit_state"):
            return
        count = len(self._edited_targets)
        if self.target_entry.get().strip() != self._entry_snapshot:
            count += 1
        if self.policy.get() != self._policy_snapshot:
            count += 1
        if self._bounds_invalidated:
            count += 1
        if (self._draft_settings != self.applied_settings and self._draft_settings.rules
                and count == 0):
            count = 1
        self.edit_state.set(self._t("pending_count", count=count) if count else
                            self._t("not_applied") if self.applied_settings is None else
                            self._t("ready_count"))

    def _refresh_identity(self) -> None:
        if self.loaded_settings_path is None:
            self.settings_identity.set("")
        else:
            self.settings_identity.set(self._t(
                "loaded_identity", path=str(self.loaded_settings_path),
                file_sha=self.loaded_settings_sha256,
                canonical_sha=self.loaded_settings_canonical_sha256))

    def _invalidate_reverse(self) -> None:
        self.reverse_candidate_settings = None
        self._reverse_basis = None
        self.reverse_table.delete(*self.reverse_table.get_children())
        self.reverse_note.set("")
        self.reverse_panel.grid_remove()
        self._refresh_busy_controls()

    def collapse_reverse_candidate(self) -> None:
        """Restore space to the source table without discarding the candidate."""
        self.reverse_panel.grid_remove()

    def _run(self, action) -> None:
        try:
            action()
        except (ValueError, OSError) as error:
            self._set_status("error", reason=str(error))

    def _target_end(self, group: UsageGroup, target_start: str) -> str:
        if not target_start:
            return ""
        trial = MappingSettings(self.source, self.target, UnmappedPolicy.HOLD,
                                (Rule(group.rule_id, group.source_start,
                                      group.interval_points, target_start),))
        mapper = trial.mapper(source_bounds=_effective_bounds(self.source_bounds),
                              target_bounds=_effective_bounds(self.target_bounds),
                              allow_target_aliases=self.inventory.complete_text)
        return mapper.project(Reference(group.source_end, f"usage:{group.rule_id}")).target

    def _refresh(self) -> None:
        self.table.delete(*self.table.get_children())
        self._refresh_policy_help()
        for group in self.inventory.groups:
            target = self._targets.get(group.rule_id, "")
            try:
                end = self._target_end(group, target)
                state = (self._t("assigned") if target else self._t("verify")
                         if group.rule_id in self._unrepresentable_groups else self._t("unassigned"))
            except MappingError:
                end, state = "", self._t("verify")
            usage_type = ("both" if group.program_references and group.comment_references else
                          "program_only" if group.program_references else "comment_only")
            self.table.insert("", "end", iid=group.rule_id, values=(
                f"{group.source_start}~{group.source_end}",
                f"{group.used_points}/{group.interval_points}", self._t(usage_type), target, end,
                len(group.pous), group.program_references, group.comment_references,
                group.max_access_width, state))
        points = sum(group.used_points for group in self.inventory.groups)
        bounds = self._t("bounds_needed")
        summary = self._t("summary", source=self.source.display_name, source_radix=self.source.radix,
                          target=self.target.display_name, target_radix=self.target.radix,
                          points=points, groups=len(self.inventory.groups), bounds=bounds)
        unused = self._unused_rule_points()
        if unused:
            summary += self._t("unused_existing", count=unused)
        def address_space(profile: Profile) -> str:
            return f"{profile.display_name} X/Y ({profile.radix})"
        summary += "\n" + self._t("allowed", source=address_space(self.source),
                                      target=address_space(self.target))
        self.summary.set(summary)
        self._refresh_edit_state()
        if self.inventory.unsupported:
            self._set_status("unsupported", count=len(self.inventory.unsupported))

    def _selected(self, _event=None) -> None:
        selected = self.table.selection()
        if self._io_busy or self._external_busy:
            if self._selected_id and selected != (self._selected_id,):
                self.table.selection_set(self._selected_id)
            elif not self._selected_id and selected:
                self.table.selection_remove(*selected)
            return
        if not selected or selected[0] == self._selected_id:
            return
        if self.target_entry.get().strip() != self._entry_snapshot:
            if self._selected_id:
                self.table.selection_set(self._selected_id)
            else:
                self.table.selection_remove(*selected)
            self._set_status("row_pending")
            return
        self._selected_id = selected[0]
        self._entry_snapshot = self._targets.get(self._selected_id, "")
        self.target_entry.set(self._entry_snapshot)
        group = next(item for item in self.inventory.groups if item.rule_id == self._selected_id)
        self.details.set(self._t("detail", pous=", ".join(group.pous) or self._t("none"),
                                 locators=", ".join(group.locators[:5]) or self._t("none")))
        if self.on_select is not None:
            self.on_select(group)

    def focus_diagnostic(self, locator: str | None, operand: str | None, *, diagnostic=None) -> bool:
        """Select a source group linked to a diagnostic without discarding edits."""
        if self.is_busy() or self.target_entry.get().strip() != self._entry_snapshot:
            self._set_status("row_pending")
            return False
        matches = [group for group in self.inventory.groups
                   if group_matches_diagnostic(group, locator, operand, self.source, diagnostic=diagnostic)]
        candidate = next((group for group in matches if group.rule_id == self._selected_id),
                         matches[0] if matches else None)
        if candidate is None or not self.table.exists(candidate.rule_id):
            return False
        self.table.selection_set(candidate.rule_id)
        self.table.see(candidate.rule_id)
        self._selected()
        return self._selected_id == candidate.rule_id

    def discard_row(self) -> None:
        if self._external_busy or self._io_busy:
            raise ValueError("작업이 끝난 뒤 편집하세요")
        self.target_entry.set(self._entry_snapshot)
        self._set_status("discarded")
        self._refresh_edit_state()

    def discard_changes(self) -> None:
        """Restore the applied map, or clear a draft that has never been applied."""
        if self.is_busy():
            raise ValueError("작업이 끝난 뒤 배정 변경을 취소하세요")
        settings = self.applied_settings or self._default_settings()
        targets = self._targets_from_settings(settings)
        self._draft_settings = settings
        self._targets = targets
        self._base_targets = dict(targets)
        self._edited_targets.clear()
        self._bounds_invalidated = False
        self.loaded_settings_path, self.loaded_settings_sha256, self.loaded_settings_canonical_sha256 = (
            self._applied_provenance if self.applied_settings is not None else (None, None, None))
        self._policy_snapshot = settings.unmapped.value
        self.policy.set(settings.unmapped.value)
        self._entry_snapshot = targets.get(self._selected_id, "")
        self.target_entry.set(self._entry_snapshot)
        self._invalidate_reverse()
        self._refresh_identity()
        self._refresh()
        if self._selected_id and self.table.exists(self._selected_id):
            self.table.selection_set(self._selected_id)
        self._set_status("changes_discarded")

    def commit_row(self) -> None:
        if self._io_busy or self._external_busy:
            raise ValueError("파일 작업이 끝난 뒤 편집하세요")
        if not self._selected_id:
            raise ValueError("먼저 원본 구간을 선택하세요")
        value = self.target_entry.get().strip().upper()
        group = next(item for item in self.inventory.groups if item.rule_id == self._selected_id)
        if value:
            self._target_end(group, value)
        self._targets[self._selected_id] = value
        self._invalidate_reverse()
        if value == self._base_targets.get(self._selected_id, ""):
            self._edited_targets.discard(self._selected_id)
        else:
            self._edited_targets.add(self._selected_id)
        self._entry_snapshot = value
        self.target_entry.set(value)
        self._refresh()
        self.table.selection_set(group.rule_id)
        self._set_status("row_committed")

    def has_pending_edit(self) -> bool:
        if self._has_user_edit():
            return True
        if self._bounds_invalidated:
            return True
        if self.applied_settings is None:
            return bool(self._draft_settings.rules)
        return self._draft_settings != self.applied_settings

    def _has_user_edit(self) -> bool:
        if self.target_entry.get().strip() != self._entry_snapshot:
            return True
        if self.policy.get() != self._policy_snapshot:
            return True
        return bool(self._edited_targets)

    def _require_row_committed(self) -> None:
        if self.target_entry.get().strip() != self._entry_snapshot:
            raise ValueError("선택 구간 입력을 먼저 반영하거나 취소하세요")

    def _candidate(self) -> MappingSettings:
        self._require_row_committed()
        policy = UnmappedPolicy(self.policy.get())
        if self._edited_targets:
            edits = {group_id: self._targets[group_id] for group_id in self._edited_targets}
            candidate = _overlay_used_edits(
                self._draft_settings, self.inventory, edits,
                source_bounds=self.source_bounds, target_bounds=self.target_bounds,
                unmapped=policy)
        else:
            candidate = replace(self._draft_settings, unmapped=policy)
        _require_current_usage(candidate, self.inventory,
                               self.source_bounds, self.target_bounds)
        return candidate

    def apply_current(self) -> MappingSettings:
        if self._io_busy or self._external_busy:
            raise ValueError("파일 작업이 끝난 뒤 적용하세요")
        candidate = self._candidate()
        if self.on_apply is not None:
            self.on_apply(candidate)
        self.applied_settings = candidate
        self._applied_provenance = (self.loaded_settings_path, self.loaded_settings_sha256,
                                    self.loaded_settings_canonical_sha256)
        self._draft_settings = candidate
        self._invalidate_reverse()
        self._base_targets = self._targets_from_settings(candidate)
        self._targets = dict(self._base_targets)
        self._edited_targets.clear()
        self._bounds_invalidated = False
        self._policy_snapshot = self.policy.get()
        self._refresh()
        if self._selected_id and self.table.exists(self._selected_id):
            self.table.selection_set(self._selected_id)
        self._set_status("applied")
        return candidate

    def set_analysis(self, analysis, direction: str, *, source_bounds: Bounds | None = None,
                     target_bounds: Bounds | None = None,
                     settings: MappingSettings | None = None, settings_applied: bool = False,
                     applied_source_sha256: str | None = None,
                     applied_provenance: tuple[str, str, str] | None = None) -> None:
        """Replace the inventory; supplied settings remain a draft by default.

        An already-applied, source-bound profile may explicitly synchronize its
        baseline. This does not bypass pending edits or changed-bound guards.
        """
        if self._io_busy or self._external_busy:
            raise ValueError("설정 파일 작업이 끝난 뒤 원본을 바꾸세요")
        if self.has_pending_edit():
            raise ValueError("미적용 주소 편집이 있습니다. 적용 또는 배정 변경 취소 후 원본을 바꾸세요")
        source, target = direction_profiles(direction)
        next_inventory = inventory_for_analysis(analysis, source)
        if type(settings_applied) is not bool:
            raise MappingError("적용 여부는 명시적인 참/거짓 값이어야 합니다")
        if settings_applied and (settings is None or not applied_source_sha256 or
                                 applied_source_sha256 != next_inventory.source_sha256):
            raise MappingError("이미 적용된 배정은 현재 원본 해시와 일치해야 합니다")
        if not settings_applied and (applied_source_sha256 is not None or applied_provenance is not None):
            raise MappingError("미적용 배정에는 적용 근거를 지정할 수 없습니다")
        policy = UnmappedPolicy.NUMERIC_IDENTITY if next_inventory.complete_text else UnmappedPolicy.HOLD
        candidate = settings or MappingSettings(source, target, policy, ())
        if (candidate.source, candidate.target) != (source, target):
            raise MappingError("설정 방향이 현재 작업과 다릅니다")
        candidate.mapper(source_bounds=_effective_bounds(source_bounds),
                         target_bounds=_effective_bounds(target_bounds),
                         allow_target_aliases=next_inventory.complete_text)
        provenance = (None, None, None)
        if applied_provenance is not None:
            if (not isinstance(applied_provenance, tuple) or len(applied_provenance) != 3 or
                    any(not isinstance(item, str) or not item for item in applied_provenance) or
                    not re.fullmatch(r"[0-9a-f]{64}", applied_provenance[1]) or
                    applied_provenance[2] != candidate.digest):
                raise MappingError("적용된 배정의 파일과 설정 해시를 확인하세요")
            provenance = (Path(applied_provenance[0]), *applied_provenance[1:])
        self._io_serial += 1
        self.source, self.target = source, target
        self.source_bounds, self.target_bounds = source_bounds, target_bounds
        self.inventory = next_inventory
        self._invalidate_reverse()
        self._draft_settings = candidate
        self.applied_settings = candidate if settings_applied else None
        self._applied_provenance = provenance
        self.loaded_settings_path, self.loaded_settings_sha256, self.loaded_settings_canonical_sha256 = provenance
        self._refresh_identity()
        self._bounds_invalidated = settings is not None and not settings_applied
        self._targets = self._targets_from_settings(candidate)
        self._base_targets = dict(self._targets)
        self._edited_targets.clear()
        self._selected_id = None
        self._entry_snapshot = ""
        self.target_entry.set("")
        self.policy.set(candidate.unmapped.value)
        self._policy_snapshot = candidate.unmapped.value
        self.details.set("")
        self._refresh()

    def set_bounds(self, source_bounds: Bounds | None,
                   target_bounds: Bounds | None) -> None:
        """Invalidate an applied map when the current work's verified limits change."""
        if self._io_busy or self._external_busy:
            raise ValueError("설정 파일 작업이 끝난 뒤 상한을 바꾸세요")
        if self.has_pending_edit() and not (self._bounds_invalidated and not self._has_user_edit()):
            raise ValueError("미적용 주소 편집이 있습니다. 적용 또는 입력 취소 후 상한을 바꾸세요")
        if (source_bounds, target_bounds) == (self.source_bounds, self.target_bounds):
            return
        self.source_bounds, self.target_bounds = source_bounds, target_bounds
        self._invalidate_reverse()
        if not self._edited_targets:
            try:
                targets = self._targets_from_settings(self._draft_settings)
            except MappingError:
                # Keep the visible draft while the new limits are invalid.
                # Applying it still requires full validation against these limits.
                pass
            else:
                self._targets = targets
                self._base_targets = dict(targets)
        self.applied_settings = None
        self._bounds_invalidated = True
        self._refresh()
        try:
            if any(self._targets.values()):
                self._candidate()
        except (ValueError, MappingError) as error:
            self._set_status("error", reason=str(error))
        else:
            self._set_status("bounds_changed")

    def _background_io(self, operation, complete) -> None:
        if self._io_busy or self._external_busy:
            raise ValueError("설정 파일 작업이 진행 중입니다")
        self._io_serial += 1
        serial = self._io_serial
        self._io_busy = True
        self._refresh_busy_controls()
        results: queue.Queue[object] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result = operation()
            except Exception as error:
                result = error
            results.put(result)

        def poll() -> None:
            if not self.winfo_exists() or serial != self._io_serial:
                return
            try:
                result = results.get_nowait()
            except queue.Empty:
                self.after(20, poll)
                return
            self._io_busy = False
            self._refresh_busy_controls()
            try:
                if isinstance(result, BaseException):
                    raise result
                complete(result)
            except Exception as error:
                self._set_status("error", reason=str(error))
        threading.Thread(target=worker, daemon=True).start()
        self.after(20, poll)

    def load_settings(self) -> None:
        if self._external_busy or self._io_busy:
            raise ValueError("작업이 끝난 뒤 설정을 불러오세요")
        self._require_row_committed()
        if self.has_pending_edit():
            raise ValueError("미적용 주소 편집이 있습니다. 현재 작업에 적용한 뒤 불러오세요")
        chosen = filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json")])
        if not chosen:
            return
        source, target = self.source, self.target
        source_bounds, target_bounds = self.source_bounds, self.target_bounds
        allow_target_aliases = self.inventory.complete_text
        self._set_status("loading")
        def operation():
            path = Path(chosen).absolute()
            payload, file_sha = read_settings_snapshot(path)
            settings = MappingSettings.from_json(payload,
                                                 expected_source=source, expected_target=target)
            settings.mapper(source_bounds=_effective_bounds(source_bounds),
                            target_bounds=_effective_bounds(target_bounds),
                            allow_target_aliases=allow_target_aliases)
            return settings, path, file_sha
        def complete(result):
            settings, path, file_sha = result
            self._validate_loaded_rules(settings)
            targets = self._targets_from_settings(settings)
            self._draft_settings = settings
            self._invalidate_reverse()
            self._targets = targets
            self._base_targets = dict(targets)
            self._edited_targets.clear()
            self.loaded_settings_path = path
            self.loaded_settings_sha256 = file_sha
            self.loaded_settings_canonical_sha256 = settings.digest
            self._refresh_identity()
            self.policy.set(settings.unmapped.value)
            self._policy_snapshot = settings.unmapped.value
            self._entry_snapshot = targets.get(self._selected_id, "")
            self.target_entry.set(self._entry_snapshot)
            self._refresh()
            self._set_status("loaded")
        self._background_io(operation, complete)

    def save_settings(self) -> None:
        if self._external_busy or self._io_busy:
            raise ValueError("작업이 끝난 뒤 설정을 저장하세요")
        candidate = self._candidate()
        chosen = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                               filetypes=[("JSON", "*.json")])
        if not chosen:
            return
        source_bounds, target_bounds = self.source_bounds, self.target_bounds
        allow_target_aliases = self.inventory.complete_text
        self._set_status("saving")
        self._background_io(
            lambda: save_new_settings(Path(chosen), candidate,
                                      _effective_bounds(source_bounds), _effective_bounds(target_bounds),
                                      allow_target_aliases=allow_target_aliases),
            lambda _result: self._set_status("saved"),
        )

    def reverse_candidate(self) -> MappingSettings:
        if self._external_busy or self._io_busy:
            raise ValueError("작업이 끝난 뒤 후보를 확인하세요")
        candidate = self._candidate()
        _require_current_usage(candidate, self.inventory, self.source_bounds, self.target_bounds,
                               allow_target_aliases=False)
        reversed_settings = candidate.reversed_candidate(
            source_bounds=_effective_bounds(self.target_bounds),
            target_bounds=_effective_bounds(self.source_bounds))
        rows = range_rows(reversed_settings, _effective_bounds(self.target_bounds),
                          _effective_bounds(self.source_bounds))
        self.reverse_candidate_settings = reversed_settings
        self._reverse_basis = (self.inventory.source_sha256, self.source_bounds,
                               self.target_bounds, candidate.digest)
        self.reverse_table.delete(*self.reverse_table.get_children())
        for rule_id, _enabled, source, source_end, count, target, target_end in rows:
            self.reverse_table.insert("", "end", values=(
                rule_id, source, source_end, count, target, target_end))
        self.reverse_note.set(self._t("reverse_note", source=reversed_settings.source.display_name,
                                      target=reversed_settings.target.display_name))
        self.reverse_panel.grid()
        self._refresh_busy_controls()
        self._set_status("reversed")
        return reversed_settings

    def save_reverse_candidate(self) -> None:
        if self.is_busy():
            raise ValueError("작업이 끝난 뒤 후보를 저장하세요")
        if self.reverse_candidate_settings is None or self._reverse_basis is None:
            raise ValueError("반대 방향 후보를 먼저 확인하세요")
        current = self._candidate()
        _require_current_usage(current, self.inventory, self.source_bounds, self.target_bounds,
                               allow_target_aliases=False)
        basis = (self.inventory.source_sha256, self.source_bounds,
                 self.target_bounds, current.digest)
        if basis != self._reverse_basis:
            raise ValueError(self._t("reverse_stale"))
        chosen = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                               filetypes=[("JSON", "*.json")])
        if not chosen:
            return
        candidate = self.reverse_candidate_settings
        source_bounds, target_bounds = self.target_bounds, self.source_bounds
        self._set_status("saving")
        self._background_io(
            lambda: save_new_settings(Path(chosen), candidate,
                                      _effective_bounds(source_bounds), _effective_bounds(target_bounds)),
            lambda _result: self._set_status("saved"),
        )
