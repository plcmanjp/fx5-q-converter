"""Source-first, immutable P006 workflow adapter.

Input: one GX3/GXW path, optional v1 MappingSettings, optional project bounds and options.
Output: typed analysis/plan/publication records. The direction engines remain
the authority for conversion and safe output publication.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import tempfile
from collections import defaultdict
from types import MappingProxyType
from typing import Any, Callable, Mapping

from p006_app.workflow_state import SourceIdentity
from p006_converter import core, q_to_fx5, q_source_review, text_conversion
from p006_converter.forward_analysis import ForwardAnalysis, analyze_fx5_source
from p006_converter.map_forward_xy import map_forward_xy, plan_forward_xy
from p006_converter.xy_mapping import Bounds, MappingError, Profile, Reference, RangeMapper, UnmappedPolicy
from p006_converter.xy_settings import MAX_SETTINGS_BYTES, MappingSettings, preview
from p006_converter.word_settings import WordDeviceSettings
from p006_converter.device_relocation import DeviceRelocationSettings
from p006_converter import statement_policy as statements

CancelCheck = Callable[[], bool]
FX5_TO_Q = "FX5_TO_Q"
Q_TO_FX5 = "Q_TO_FX5"
_IDENTIFIER = re.compile(r"[^\w-]+", re.UNICODE)
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})
_XY = re.compile(r"(?:K[1-8])?([XY])([0-9A-F]+)\Z")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True)
class WorkflowRequest:
    direction: str
    source_path: Path
    project_identifier: str | None = None
    settings: MappingSettings | None = None
    source_bounds: Bounds | None = None
    target_bounds: Bounds | None = None
    max_m: int | None = None
    max_d: int | None = None
    output_parent: Path | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    word_settings: WordDeviceSettings | None = None
    relocation_settings: DeviceRelocationSettings | None = None

    def __post_init__(self) -> None:
        # A frozen dataclass alone does not freeze a caller-owned dict.
        canonical = json.loads(json.dumps(dict(self.options), sort_keys=True,
                                          ensure_ascii=True, allow_nan=False))
        if canonical.get("complete_text") is True and not canonical.get("q_source_review"):
            canonical["statement_policy"] = statements.validate(canonical.get("statement_policy", statements.IN_PLC))
        object.__setattr__(self, "options", _freeze_json(canonical))


@dataclass(frozen=True)
class WorkflowDiagnostic:
    code: str
    severity: str
    scope: str
    pou: str | None = None
    locator: str | None = None
    operand: str | None = None
    reason: str = ""
    action: str = ""
    detail: str = ""


@dataclass(frozen=True)
class WorkflowAnalysis:
    request: WorkflowRequest
    source_identity: SourceIdentity | None
    programs: tuple[str, ...]
    used_addresses: tuple[str, ...]
    diagnostics: tuple[WorkflowDiagnostic, ...]
    required_settings: tuple[str, ...]
    profile_evidence: str
    inspection_scope: tuple[str, ...]
    uninspected_scope: tuple[str, ...]
    raw_analysis: ForwardAnalysis | q_to_fx5.QSourceAnalysis | text_conversion.TextSourceAnalysis | None = None

    @property
    def source_sha256(self) -> str | None:
        return self.source_identity.sha256 if self.source_identity else None


@dataclass(frozen=True)
class WorkflowPlan:
    request: WorkflowRequest
    analysis: WorkflowAnalysis
    source_sha256: str | None
    settings_hash: str | None
    required_settings: tuple[str, ...]
    diagnostics: tuple[WorkflowDiagnostic, ...]
    publishable: bool
    status: str
    engine_plan: q_to_fx5.QPlan | text_conversion.TextPlan | None = None


@dataclass(frozen=True)
class WorkflowPublication:
    output_dir: Path
    artifacts: tuple[Path, ...]
    report: Mapping[str, Any]
    status: str
    diagnostics: tuple[WorkflowDiagnostic, ...] = ()
    artifact_roles: Mapping[str, str] = field(default_factory=dict)


class WorkflowPartialPublishError(core.PartialPublishError):
    def __init__(self, output_dir: Path, partial_paths: tuple[Path, ...], reason: str):
        self.output_dir = output_dir
        self.partial_paths = partial_paths
        super().__init__(f"{reason}; partial_outputs={','.join(str(path) for path in partial_paths)}")


def _artifact_roles(destination: Path, artifacts: tuple[Path, ...], *, mapped: bool,
                    report: Mapping[str, Any] | None = None) -> Mapping[str, str]:
    roles: dict[str, str] = {}
    program_roles: dict[str, str] = {}
    for program in (report or {}).get("programs", ()):
        if not isinstance(program, Mapping):
            continue
        role = "IMPORT_CANDIDATE" if program.get("publication_class") == "NORMAL_Q_CSV" else "REVIEW_ONLY"
        for key in ("q_output", "import_review_output", "review_output", "mining_review_output",
                    "typed_mining_review_output", "source_recovered_review_output", "structural_review_output"):
            name = program.get(key)
            if isinstance(name, str) and name:
                program_roles[Path(name).name.casefold()] = role if key == "q_output" else "REVIEW_ONLY"
    for path in artifacts:
        relative = path.relative_to(destination).as_posix()
        leaf = path.name.lower()
        if mapped and relative.startswith("raw/"):
            role = "BASELINE_REFERENCE"
        elif leaf in program_roles:
            role = program_roles[leaf]
        elif leaf.endswith(".json"):
            role = "REPORT"
        elif "invalid" in leaf or leaf.endswith(".txt"):
            role = "DIAGNOSTIC"
        elif "review" in leaf:
            role = "REVIEW_ONLY"
        elif leaf == "normalized.csv":
            role = "SOURCE_NORMALIZED"
        else:
            role = "IMPORT_CANDIDATE"
        roles[relative] = role
    return MappingProxyType(roles)


def _forward_diagnostics(result: ForwardAnalysis) -> tuple[WorkflowDiagnostic, ...]:
    """Adapt the existing ledger without changing its codes or denominator."""
    diagnostics = []
    for values in result.findings:
        row = dict(zip(core.INVALID_HEADER, values))
        fields = dict(part.split("=", 1) for part in row.get("candidate", "").split(";") if "=" in part)
        pou = fields.get("program") or None
        operand = fields.get("operands", row.get("operand", ""))
        if row.get("instruction") == "COMMENT":
            locator = f"comment:{row.get('operand', '')}"
        else:
            matching = [ref for ref in result.program_references
                        if (not pou or ref.pou == pou) and
                        str(ref.source_step) == row.get("source_step")]
            locator = matching[0].locator if matching else f"{pou or 'project'}:step:{row.get('source_step', '')}"
            if not pou and len({ref.pou for ref in matching}) == 1:
                pou = matching[0].pou
        code = row.get("finding_code", "SOURCE_FINDING")
        diagnostics.append(WorkflowDiagnostic(code, "error", "row", pou, locator, operand,
                           row.get("error", ""), "해당 원본 위치와 지원 경계를 확인하세요.", " | ".join(values)))
    return tuple(diagnostics)


def suggest_project_identifier(source: Path) -> str:
    """Suggest a Windows-safe identifier; never claim project metadata evidence."""
    stem = _IDENTIFIER.sub("_", Path(source).stem).strip(" ._-")[:80]
    if not stem or stem.upper() in _RESERVED:
        stem = f"Project_{hashlib.sha256(Path(source).name.encode('utf-8')).hexdigest()[:8]}"
    return stem


def _valid_identifier(value: str) -> bool:
    return bool(value and value == value.strip(" .") and len(value) <= 80 and
                not any(char in value for char in '<>:"/\\|?*\x00\r\n\t') and
                value.split(".", 1)[0].upper() not in _RESERVED)


def _check_cancel(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise core.ConversionCancelled("workflow cancelled")


def _diagnostic(code: str, *, detail: str = "", locator: str | None = None,
                pou: str | None = None, operand: str | None = None) -> WorkflowDiagnostic:
    return WorkflowDiagnostic(code, "error", "project" if locator is None else "row", pou,
                              locator, operand, code, "원본과 설정을 확인하세요.", detail)


def _text_failure(error: Exception, fallback: str) -> WorkflowDiagnostic:
    if isinstance(error, (text_conversion.SourceCommentDecodeError,
                          text_conversion.SourcePIStatementUnsupportedError)):
        return WorkflowDiagnostic(error.code, "error", "project", reason=error.reason,
                                  action=error.action, detail=error.detail)
    return _diagnostic(fallback, detail=str(error))


def _from_q(item: q_to_fx5.QDiagnostic) -> WorkflowDiagnostic:
    return WorkflowDiagnostic(item.code, item.severity, item.blocking_scope, item.pou,
                              item.source_locator, item.operand, item.reason, item.action, item.detail)


def _unique_xy(tokens: list[str]) -> tuple[str, ...]:
    return tuple(sorted({token for token in tokens if _XY.fullmatch(token.upper())}))


def analyze(request: WorkflowRequest, cancel_check: CancelCheck | None = None) -> WorkflowAnalysis:
    _check_cancel(cancel_check)
    source = Path(request.source_path)
    if request.direction not in {FX5_TO_Q, Q_TO_FX5}:
        raise ValueError("unsupported conversion direction")
    if request.options.get("complete_text") is True and not request.options.get("q_source_review"):
        try:
            result = text_conversion.analyze_text_source(source, direction=request.direction,
                                                         cancel_check=cancel_check)
            identity = SourceIdentity.from_file(source)
            if identity.sha256 != result.source_sha256:
                raise core.ConversionError("source changed during text analysis")
            used = _unique_xy([ref.spelling for ref in (*result.program_references, *result.comment_references)])
            return WorkflowAnalysis(request, identity, result.pous, used, (), (), "UNKNOWN",
                                    result.inspection_scope, result.uninspected_scope, result)
        except core.ConversionCancelled:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            identity = None
            if isinstance(error, text_conversion.SourcePIStatementUnsupportedError):
                # A known unsupported carrier is a valid input identity with
                # incomplete analysis. Preserve its HOLD diagnosis for the UI
                # only while the bytes still match the reader's own evidence.
                try:
                    current = SourceIdentity.from_file(source)
                    if current.sha256 == json.loads(error.detail)["input_sha256"]:
                        identity = current
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            return WorkflowAnalysis(request, identity, (), (),
                                    (_text_failure(error, "TEXT_SOURCE_INVALID"),), (),
                                    "UNKNOWN", (), ("source",))
    if request.direction == FX5_TO_Q:
        try:
            result = analyze_fx5_source(source, cancel_check=cancel_check)
            identity = SourceIdentity.from_file(source)
            if identity.sha256 != result.source_sha256:
                raise core.ConversionError("source changed during analysis")
            diagnostics = _forward_diagnostics(result)
            diagnostics += tuple(_diagnostic(item.finding_code, detail=item.message)
                                 for item in result.source_diagnostics)
            used = _unique_xy([r.spelling for r in (*result.program_references, *result.comment_references)])
            required = ()
            return WorkflowAnalysis(request, identity, result.pous, used, diagnostics, required,
                                    "UNKNOWN", result.inspection_scope, result.uninspected_scope, result)
        except core.ConversionCancelled:
            raise
        except (OSError, ValueError, core.ConversionError) as error:
            return WorkflowAnalysis(request, None, (), (), (_diagnostic("FX5_SOURCE_INVALID", detail=str(error)),),
                                    (), "UNKNOWN", (), ("source",))
    result = q_to_fx5.analyze_q_source(source, cancel_check=cancel_check)
    _check_cancel(cancel_check)
    try:
        identity = SourceIdentity.from_file(source) if result.source_sha256 else None
    except (OSError, ValueError) as error:
        return WorkflowAnalysis(request, None, (), (), (_diagnostic("Q_SOURCE_CHANGED", detail=str(error)),), (),
                                "UNKNOWN", (), ("source",))
    if identity and identity.sha256 != result.source_sha256:
        return WorkflowAnalysis(request, None, (), (), (_diagnostic("Q_SOURCE_CHANGED"),), (),
                                "UNKNOWN", (), ("source",))
    used = _unique_xy([r.raw_token for r in result.program_references] +
                      [r.device for r in result.comment_references])
    return WorkflowAnalysis(request, identity, result.pous, used,
                            tuple(_from_q(d) for d in result.diagnostics), (), "UNKNOWN",
                            result.inspection_scope, result.uninspected_scope, result)


def _request_digest(request: WorkflowRequest) -> str:
    values = {"direction": request.direction, "project_identifier": request.project_identifier,
              "settings": request.settings.digest if request.settings else None,
              "word_settings": request.word_settings.digest if request.word_settings else None,
              "relocation_settings": request.relocation_settings.digest if request.relocation_settings else None,
              "source_bounds": vars(request.source_bounds) if request.source_bounds else None,
              "target_bounds": vars(request.target_bounds) if request.target_bounds else None,
              "max_m": request.max_m, "max_d": request.max_d, "options": _plain_json(request.options)}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _check_settings_origin(request: WorkflowRequest) -> None:
    for prefix in ("settings", "word_settings", "relocation_settings", "statement_settings"):
        path = request.options.get(f"{prefix}_source_path")
        expected = request.options.get(f"{prefix}_source_sha256")
        if not path and not expected:
            continue
        if not isinstance(path, str) or not isinstance(expected, str) or not path or not expected:
            raise ValueError("incomplete imported settings identity")
        source = core._reject_link_like_ancestors(Path(path), "settings source")
        current = SourceIdentity.from_file(source, max_bytes=(1024 * 1024 if prefix == "relocation_settings"
                                                            else MAX_SETTINGS_BYTES))
        if current.sha256 != expected:
            raise ValueError("imported settings changed since load; reload and confirm the current mapping")


def plan(request: WorkflowRequest, cancel_check: CancelCheck | None = None) -> WorkflowPlan:
    analysis = analyze(request, cancel_check)
    _check_cancel(cancel_check)
    diagnostics = list(analysis.diagnostics)
    required = list(analysis.required_settings)
    if request.settings and request.settings.module_rules and (
            request.options.get("complete_text") is not True or request.options.get("q_source_review")):
        diagnostics.append(_diagnostic("MODULE_MAPPING_TEXT_REQUIRED"))
        return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request),
                            tuple(required), tuple(diagnostics), False, "HOLD")
    if request.relocation_settings is not None:
        code = ("RELOCATION_DIRECTION_MISMATCH" if request.direction != Q_TO_FX5 else
                "RELOCATION_WORD_CONFLICT" if request.word_settings is not None else
                "RELOCATION_TEXT_REQUIRED" if request.options.get("complete_text") is not True
                or request.options.get("q_source_review") else
                "RELOCATION_SOURCE_MISMATCH" if request.relocation_settings.source_sha256 != analysis.source_sha256
                else None)
        if code:
            diagnostics.append(_diagnostic(code))
            return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request),
                                tuple(required), tuple(diagnostics), False, "HOLD")
    try:
        _check_settings_origin(request)
    except (OSError, ValueError, core.ConversionError) as error:
        diagnostics.append(_diagnostic("SETTINGS_SOURCE_CHANGED", detail=str(error)))
    identifier = request.project_identifier or suggest_project_identifier(request.source_path)
    if not _valid_identifier(identifier):
        diagnostics.append(_diagnostic("PROJECT_IDENTIFIER_INVALID"))
    if request.options.get("complete_text") is True and not request.options.get("q_source_review"):
        # The text engine independently verifies source completeness. Findings
        # from the execution projection remain visible as information.
        diagnostics = [WorkflowDiagnostic(d.code, "info", d.scope, d.pou, d.locator, d.operand,
                                           d.reason, d.action, d.detail)
                       if d.code not in {"PROJECT_IDENTIFIER_INVALID", "SETTINGS_SOURCE_CHANGED"}
                       else d for d in diagnostics]
        engine = None
        try:
            engine = text_conversion.plan_text_conversion(
                Path(request.source_path), project_identifier=identifier, direction=request.direction,
                settings=request.settings, word_settings=request.word_settings,
                relocation_settings=request.relocation_settings, cancel_check=cancel_check,
                target_cpu=request.options.get("target_cpu", "FX5U"),
                statement_policy=request.options.get("statement_policy", statements.IN_PLC),
                unmapped_policy=(UnmappedPolicy.SOURCE_SPELLING_IDENTITY
                                 if request.direction == FX5_TO_Q and request.options.get("forward_policy") == "identity"
                                 else UnmappedPolicy.NUMERIC_IDENTITY))
            if engine.source_sha256 != analysis.source_sha256:
                diagnostics.append(_diagnostic("SOURCE_CHANGED"))
            fallback_devices = "SM1255/SD1255" if request.direction == FX5_TO_Q else "SM4095/SD4095"
            fallback_action = (f"{fallback_devices} 치환과 POU별 원문 대조 자료를 확인하세요. "
                               "다운로드와 실행은 미수락입니다.")
            for warning in engine.report.get("warnings", ()):
                if warning["code"] == "Q_OUTPUT_DOUBLE_COIL_REVIEW":
                    continue  # Per-device INFO findings below carry the locations and user decision policy.
                if warning["code"] in {"FX5_IMPORT_BLOCKED", "Q_IMPORT_BLOCKED"}:
                    diagnostics.append(WorkflowDiagnostic(
                        warning["code"], "warning", "project",
                        reason=f'대상 가져오기 차단 참조가 {warning["count"]}건 남아 있습니다.',
                        action="파일별 분류와 가져오기 검토 보고서의 주소, 명령 및 프로젝트 할당을 확인하세요.",
                        detail=json.dumps(_plain_json(warning), ensure_ascii=False)))
                    continue
                if warning["code"] in {"Q_IMPORT_REVIEW", "FX5_IMPORT_REVIEW"}:
                    diagnostics.append(WorkflowDiagnostic(
                        warning["code"], "warning", "project",
                        reason=f'폴백 적용 검토 참조가 {warning["count"]}건 있습니다.',
                        action=fallback_action,
                        detail=json.dumps(_plain_json(warning), ensure_ascii=False)))
                    continue
                if warning["code"] == "Q_TYPED_MINING_REVIEW":
                    diagnostics.append(WorkflowDiagnostic(
                        warning["code"], "warning", "project",
                        reason=f'원문 의미 확인이 필요한 타입 마이닝 검토가 {warning["count"]}건 있습니다.',
                        action="MINING_REVIEW_ONLY 자료입니다. 원문과 타입 근거를 확인하세요. 가져오기 후보가 아니며 다운로드와 실행은 금지합니다.",
                        detail=json.dumps(_plain_json(warning), ensure_ascii=False)))
                    continue
                if warning["code"] != "XY_TARGET_ALIAS":
                    diagnostics.append(WorkflowDiagnostic(
                        warning["code"], "warning", "project",
                        reason="대상 변환에 검토 항목이 남아 있습니다.",
                        action="파일별 분류와 보고서를 확인하세요. 다운로드와 실행은 미수락입니다.",
                        detail=json.dumps(_plain_json(warning), ensure_ascii=False)))
                    continue
                diagnostics.append(WorkflowDiagnostic(
                    warning["code"], "warning", "row", locator=warning["locators"][0],
                    operand=warning["target"],
                    reason="서로 다른 원본 주소가 같은 대상 주소로 변환됩니다.",
                    action="원본 참조는 모두 보존했습니다. 중복 주소와 코멘트를 확인하세요.",
                    detail=json.dumps(_plain_json(warning), ensure_ascii=False)))
            target_groups = defaultdict(list)
            for finding in engine.report.get("target_review", {}).get("findings", ()):
                subject = finding.get("family") or finding.get("source_opcode") or finding.get("device") or "Q"
                target_groups[(finding["code"], finding["scope"], subject, finding.get("state", "BLOCKED"))].append(finding)
            review_programs = {p["name"] for p in engine.report.get("programs", ())
                               if p.get("text_output_role") == "IMPORT_REVIEW_CSV"}
            for (code, scope, family, state), findings in sorted(target_groups.items()):
                if state == "INFO":
                    diagnostics.append(WorkflowDiagnostic(
                        code, "info", scope, locator=findings[0].get("locator"), operand=family,
                        reason=findings[0]["reason"],
                        action="중복 출력은 그대로 변환합니다. 정상 여부는 사용자가 판단합니다.",
                        detail=json.dumps({"count": len(findings), "examples": [_plain_json(f) for f in findings[:5]]},
                                          ensure_ascii=False)))
                    continue
                fallback = state == "REVIEW" and any(f.get("pou") in review_programs for f in findings)
                mining = any(f.get("diagnostic_fallback") == core.TYPED_MINING_FALLBACK for f in findings)
                diagnostics.append(WorkflowDiagnostic(
                    code, "warning", scope, locator=findings[0].get("locator"), operand=family,
                    reason=(f"{family} 원본 의미 마이닝 보류 {len(findings)}건: {code}" if mining else
                            f"{family} 폴백 적용 검토 {len(findings)}건: {code}" if fallback else
                            f"{family} 대상 호환 검토 {len(findings)}건: {code}"),
                    action=("타입 근거는 진단 형태만 증명합니다. 원본 의미 보류를 해소해야 하며 가져오기, 다운로드와 실행은 금지합니다."
                            if mining else fallback_action
                            if fallback else "원문은 보존했습니다. 대상 CPU 상한, 메모리 할당과 명령 호환성을 확인하세요. "
                            + ("COMMENT-source는 대조용이며 COMMENT.csv는 Q 코멘트 후보입니다."
                               if request.direction == FX5_TO_Q else
                               "전체 COMMENT는 대조용이며, COMMENT-import 파일을 기종과 설정에 맞게 검증하세요.")),
                    detail=json.dumps({"count": len(findings), "examples": [_plain_json(f) for f in findings[:5]],
                                       "target_state": engine.report["target_review"]["state"]}, ensure_ascii=False)))
        except core.ConversionCancelled:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            failure = _text_failure(error, "TEXT_SOURCE_OR_MAPPING_INVALID")
            if isinstance(error, (text_conversion.SourceCommentDecodeError,
                                  text_conversion.SourcePIStatementUnsupportedError)):
                diagnostics = [d for d in diagnostics if d.code != failure.code]
            diagnostics.append(failure)
        ready = engine is not None and not any(d.severity == "error" for d in diagnostics)
        status = ("REVIEW" if ready and engine.report.get("import_readiness", {}).get("state") in {"BLOCKED", "REVIEW"}
                  else "CANDIDATE" if ready else "HOLD")
        return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request), (),
                            tuple(diagnostics), ready, status, engine)
    if request.direction == Q_TO_FX5 and analysis.source_sha256:
        if request.options.get("q_source_review") is True:
            # Source review never turns a target incompatibility into approval.
            # A supplied mapping would contradict exact source preservation.
            if any(value is not None for value in (request.settings, request.word_settings, request.max_m, request.max_d,
                                                   request.source_bounds, request.target_bounds)):
                diagnostics.append(_diagnostic("SOURCE_REVIEW_TARGET_SETTINGS_NOT_ALLOWED"))
                return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request),
                                    (), tuple(diagnostics), False, "HOLD")
            engine = q_source_review.plan_q_source_review(
                Path(request.source_path), project_identifier=identifier, cancel_check=cancel_check)
            seen_diagnostics = set(diagnostics)
            for finding in engine.diagnostics:
                diagnostic = _from_q(finding)
                if diagnostic not in seen_diagnostics:
                    diagnostics.append(diagnostic)
                    seen_diagnostics.add(diagnostic)
            ready = (engine.publishable and engine.analysis.source_sha256 == analysis.source_sha256
                     and _valid_identifier(identifier)
                     and not any(item.code == "SETTINGS_SOURCE_CHANGED" for item in diagnostics))
            return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request),
                                (), tuple(diagnostics), ready, "REVIEW" if ready else "HOLD", engine)
        engine = q_to_fx5.plan_q_to_fx5(Path(request.source_path), project_identifier=identifier,
                                         max_m=request.max_m, max_d=request.max_d, settings=request.settings,
                                         word_settings=request.word_settings,
                                         q_io_bounds=request.source_bounds, fx5_io_bounds=request.target_bounds,
                                         cancel_check=cancel_check)
        if engine.analysis.source_sha256 != analysis.source_sha256:
            diagnostics.append(_diagnostic("Q_SOURCE_CHANGED"))
        seen_diagnostics = set(diagnostics)
        for finding in engine.diagnostics:
            diagnostic = _from_q(finding)
            if diagnostic not in seen_diagnostics:
                diagnostics.append(diagnostic)
                seen_diagnostics.add(diagnostic)
        required.extend(engine.required_settings)
        _check_cancel(cancel_check)
        ready = engine.publishable and not diagnostics
        return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request),
                            tuple(dict.fromkeys(required)), tuple(diagnostics), ready,
                            "CANDIDATE" if ready else "HOLD", engine)
    if request.direction == FX5_TO_Q and analysis.source_sha256:
        if request.word_settings is not None:
            diagnostics.append(_diagnostic("WORD_DIRECTION_MISMATCH"))
        if request.settings:
            if request.settings.source is not Profile.FX5U or request.settings.target is not Profile.Q:
                diagnostics.append(_diagnostic("XY_DIRECTION_MISMATCH"))
        if isinstance(analysis.raw_analysis, ForwardAnalysis):
            raw = analysis.raw_analysis
            program_refs = tuple(Reference(ref.spelling, ref.locator) for ref in raw.program_references
                                 if _XY.fullmatch(ref.spelling.upper()))
            comment_refs = tuple(Reference(ref.spelling, ref.locator) for ref in raw.comment_references
                                 if _XY.fullmatch(ref.spelling.upper()))
            try:
                if request.settings:
                    mapping = preview(request.settings, source_bounds=request.source_bounds,
                                      target_bounds=request.target_bounds, input_sha256=analysis.source_sha256,
                                      program=program_refs, comments=comment_refs)
                    diagnostics.extend(_diagnostic("XY_UNMAPPED", detail=ref.source,
                                                   locator=ref.locator, operand=ref.source)
                                       for ref in mapping.unmapped)
                else:
                    # Legacy bounds impose no capacity limits. Radix and
                    # whole-span mapping integrity remain checked here.
                    RangeMapper(source=Profile.FX5U, target=Profile.Q,
                                source_bounds=request.source_bounds, target_bounds=request.target_bounds,
                                rules=(), unmapped=UnmappedPolicy.SOURCE_SPELLING_IDENTITY).preflight(
                                    (*program_refs, *comment_refs))
            except MappingError as error:
                diagnostics.append(_diagnostic("XY_PREFLIGHT_HOLD", detail=str(error),
                                               locator=error.locator, operand=error.operand))
            # Use the authoritative forward projection to test the complete-normal
            # prerequisite. A source ledger alone cannot predict allowed review
            # fallback output, comment exclusions or continuation footprints.
            if request.settings and not required and not any(item.code.startswith("XY_") for item in diagnostics):
                try:
                    with tempfile.TemporaryDirectory(prefix="p006-plan-") as directory:
                        raw_dir = Path(directory)
                        core.convert(Path(request.source_path), identifier,
                                     raw_dir / "normalized.csv", raw_dir / "q-import.csv",
                                     raw_dir / "invalid.csv", cancel_check=cancel_check,
                                     comment_output=raw_dir / "comment.csv",
                                     comment_review_output=raw_dir / "comment-review.csv",
                                     report_output=raw_dir / "conversion-report.json",
                                     expected_source_sha256=analysis.source_sha256)
                        _check_cancel(cancel_check)
                        plan_forward_xy(raw_dir, settings=request.settings,
                                        fx5_bounds=request.source_bounds, q_bounds=request.target_bounds)
                except core.ConversionCancelled:
                    raise
                except (OSError, ValueError, core.ConversionError) as error:
                    diagnostics.append(_diagnostic("FORWARD_MAPPING_PREREQUISITE", detail=str(error)))
    # Forward findings are review output, not necessarily a reason to suppress
    # the existing engine's per-POU review artifacts.
    ready = bool(request.direction == FX5_TO_Q and analysis.source_sha256 and not required and
                 not any(item.code in {"PROJECT_IDENTIFIER_INVALID", "XY_DIRECTION_MISMATCH", "WORD_DIRECTION_MISMATCH",
                                       "XY_SETTINGS_REQUIRED", "XY_UNMAPPED", "XY_PREFLIGHT_HOLD",
                                       "FX5_SOURCE_INVALID", "FORWARD_MAPPING_PREREQUISITE", "SETTINGS_SOURCE_CHANGED"} for item in diagnostics))
    return WorkflowPlan(request, analysis, analysis.source_sha256, _request_digest(request),
                        tuple(dict.fromkeys(required)), tuple(diagnostics), ready,
                        "REVIEW" if ready and diagnostics else "CANDIDATE" if ready else "HOLD")


def _new_output_path(parent: Path, identifier: str) -> Path:
    core._reject_link_like_ancestors(parent, "output parent")
    if not parent.is_dir():
        raise ValueError("output parent must exist")
    for index in range(1, 10001):
        candidate = parent / (identifier if index == 1 else f"{identifier}-{index}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ValueError("no free result directory name")


def publish(plan: WorkflowPlan, output_parent: Path | None = None,
            cancel_check: CancelCheck | None = None) -> WorkflowPublication:
    if not plan.publishable or not plan.source_sha256:
        raise ValueError("workflow plan is on hold")
    request = plan.request
    if _request_digest(request) != plan.settings_hash:
        raise ValueError("workflow settings changed since plan")
    _check_settings_origin(request)
    _check_cancel(cancel_check)
    current = SourceIdentity.from_file(request.source_path)
    if current != plan.analysis.source_identity:
        raise ValueError("source changed since plan")
    parent = Path(output_parent or request.output_parent or request.source_path.parent)
    identifier = request.project_identifier or suggest_project_identifier(request.source_path)
    destination = _new_output_path(parent, identifier)
    if request.options.get("complete_text") is True and not request.options.get("q_source_review"):
        if not isinstance(plan.engine_plan, text_conversion.TextPlan):
            raise ValueError("complete text plan is absent")
        report = text_conversion.publish_text_conversion(plan.engine_plan, destination, cancel_check)
        artifacts = tuple(sorted(destination.iterdir()))
        full_comments = {group["full_csv"] for group in report.get("comment_import", {}).get("groups", ())}
        program_roles = {p["text_output"]: p["text_output_role"] for p in report["programs"]}
        roles = MappingProxyType({path.name: (program_roles[path.name] if path.name in program_roles else
                                             "SOURCE_REVIEW_ONLY" if path.name in full_comments else
                                             "TEXT_CANDIDATE" if path.suffix == ".csv" else "REPORT")
                                  for path in artifacts})
        status = "REVIEW" if report.get("import_readiness", {}).get("state") in {"BLOCKED", "REVIEW"} else "CANDIDATE"
        return WorkflowPublication(destination, artifacts, report, status, plan.diagnostics, roles)
    if request.direction == Q_TO_FX5:
        if request.options.get("q_source_review") is True:
            report = q_source_review.export_q_source_review(
                Path(request.source_path), destination, project_identifier=identifier,
                expected_source_sha256=plan.source_sha256, cancel_check=cancel_check)
            artifacts = tuple(sorted(destination.iterdir()))
            return WorkflowPublication(destination, artifacts, report, "REVIEW", plan.diagnostics,
                                       _artifact_roles(destination, artifacts, mapped=False, report=report))
        report = q_to_fx5.convert_q_to_fx5(Path(request.source_path), destination,
                                            project_identifier=identifier, max_m=request.max_m,
                                            max_d=request.max_d, settings=request.settings,
                                            word_settings=request.word_settings,
                                            q_io_bounds=request.source_bounds, fx5_io_bounds=request.target_bounds,
                                            expected_source_sha256=plan.source_sha256,
                                            cancel_check=cancel_check)
        artifacts = tuple(sorted(destination.iterdir()))
        return WorkflowPublication(destination, artifacts, report, "CANDIDATE", plan.diagnostics,
                                   _artifact_roles(destination, artifacts, mapped=False, report=report))
    destination.mkdir(exist_ok=False)
    try:
        raw = destination / "raw" if request.settings else destination
        if request.settings:
            raw.mkdir()
        kwargs = {"comment_output": raw / "comment.csv", "comment_review_output": raw / "comment-review.csv",
                  "report_output": raw / "conversion-report.json", "expected_source_sha256": plan.source_sha256}
        core.convert(Path(request.source_path), identifier, raw / "normalized.csv", raw / "q-import.csv",
                     raw / "invalid.csv", cancel_check=cancel_check, **kwargs)
        _check_cancel(cancel_check)
        report_path = raw / "conversion-report.json"
        report: Mapping[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        status = ("CANDIDATE" if report.get("status", {}).get("publication") == "COMPLETE_NORMAL"
                  else "REVIEW")
        if request.settings:
            mapped = destination / "mapped"
            map_forward_xy(raw, mapped, settings=request.settings,
                           fx5_bounds=request.source_bounds, q_bounds=request.target_bounds)
            report = json.loads((mapped / "xy-mapping-report.json").read_text(encoding="utf-8"))
        _check_cancel(cancel_check)
        if SourceIdentity.from_file(request.source_path) != current:
            raise ValueError("source changed before publication")
    except Exception as error:
        # Keep exact partial paths. Never delete an output after publication starts.
        partial = tuple(sorted(path for path in destination.rglob("*") if path.is_file()))
        if partial:
            raise WorkflowPartialPublishError(destination, partial, str(error)) from error
        raise
    artifacts = tuple(sorted(path for path in destination.rglob("*") if path.is_file()))
    return WorkflowPublication(destination, artifacts, report, status, plan.diagnostics,
                               _artifact_roles(destination, artifacts, mapped=bool(request.settings), report=report))
