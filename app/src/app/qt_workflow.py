"""Native Qt commands. The retained HTTP POC never imports this publisher.

All conversion, mapping and atomic publication authority stays in WorkflowService.
Commands replan and compare the exact displayed binding before any publication.
"""
from contextlib import closing
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import time
from types import MappingProxyType

from app import workflow_service as service
from app.generation_guidance import EXPLICIT_EXECUTION_REQUEST, Q_CPUS
from app.preparation_session import Session, present, request_for
from app.usage_editor import direction_profiles, inventory_for_analysis, _require_current_usage
from app.workflow_state import SourceIdentity
from app.xy_settings_dialog import range_rows
from converter import core, text_conversion
from converter.xy_mapping import Bounds
from converter.xy_settings import MappingSettings
from converter.word_settings import WordDeviceSettings
from converter.device_relocation import DeviceRelocationSettings
from converter.module_mapping import source_unit, unit_number


def settings_bytes(path, limit=1024 * 1024):
    path = core._reject_link_like_ancestors(Path(path), "settings")
    SourceIdentity.from_file(path, max_bytes=limit)
    with closing(core._open_input_snapshot(path)) as snapshot:
        if not 0 < len(snapshot.body) <= limit:
            raise ValueError("설정 파일 크기 제한을 넘었습니다")
        payload = snapshot.body
        snapshot.verify()
        return payload, snapshot.sha256


def request_from(body, identity):
    request, profile = request_for(Path(body["path"]), body["cpu"], body["mode"], identity,
                                   body.get("statement_policy", "in_plc"))
    options = dict(request.options)
    if request.direction == "FX5_TO_Q":
        options["actual_q_cpu"] = body.get("actual_q_cpu", "").strip()
    source, target = direction_profiles(request.direction)
    settings, word, relocation = request.settings, request.word_settings, request.relocation_settings
    manual = False
    if body.get("xy_json") is not None:
        settings = MappingSettings.from_json(body["xy_json"].encode("utf-8"),
                                             expected_source=source, expected_target=target)
        for key in ("settings_source_path", "settings_source_sha256"):
            options.pop(key, None)
        manual = True
    for kind, parser in (("xy", lambda b: MappingSettings.from_json(b, expected_source=source, expected_target=target)),
                         ("word", lambda b: WordDeviceSettings.from_json(b, expected_source=source, expected_target=target)),
                         ("relocation", DeviceRelocationSettings.from_json)):
        path = body.get(kind + "_path")
        if path:
            payload, digest = settings_bytes(path)
            value = parser(payload)
            prefix = {"xy": "settings", "word": "word_settings", "relocation": "relocation_settings"}[kind]
            options[prefix + "_source_path"] = str(Path(path).absolute())
            options[prefix + "_source_sha256"] = digest
            if kind == "xy":
                settings = value
            elif kind == "word":
                word = value
            else:
                relocation = value
            manual = True
    if request.options.get("q_source_review") and any((settings, word, relocation)):
        raise ValueError("원문 대조에는 주소 변경 설정을 적용할 수 없습니다")
    identifier = body.get("identifier", "").strip() or service.suggest_project_identifier(request.source_path)
    return replace(request, settings=settings, word_settings=word, relocation_settings=relocation,
                   project_identifier=identifier, options=options), None if manual else profile


def binding_for(plan, body):
    data = {"source": asdict(plan.analysis.source_identity), "settings": plan.settings_hash,
            "cpu": body["cpu"], "actual_q_cpu": body.get("actual_q_cpu", ""), "mode": body["mode"],
            "parent": str(Path(body.get("output_parent") or plan.request.source_path.parent).absolute())}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def display_plan(plan, profile, body):
    result = present(plan, profile)
    source, _ = direction_profiles(plan.request.direction)
    inventory = inventory_for_analysis(plan.analysis, source)
    # A fail-closed engine plan deliberately has no artifacts or report.
    # Display its HOLD diagnostics without changing publication authority.
    outputs = getattr(plan.engine_plan, "outputs", None) or {}
    result.update(file_generation="NOT_GENERATED", binding=binding_for(plan, body),
                  identifier=plan.request.project_identifier, inventory=[asdict(g) for g in inventory.groups],
                  actual_target_cpu=body.get("actual_q_cpu") if plan.request.direction == "FX5_TO_Q" else body["cpu"],
                  actual_cpu_validation="NOT_VERIFIED", allocation_validation="NOT_CHECKED",
                  inventory_unsupported=list(inventory.unsupported), used_addresses=list(plan.analysis.used_addresses),
                  xy_json=plan.request.settings.to_json().decode("utf-8") if plan.request.settings else None,
                  output_parent=str(Path(body.get("output_parent") or plan.request.source_path.parent).absolute()),
                  planned_files=list(outputs), artifacts=[], output_dir=None,
                  settings_origins=[{"path": plan.request.options[prefix + "_source_path"],
                                     "sha256": plan.request.options[prefix + "_source_sha256"]}
                                    for prefix in ("settings", "word_settings", "relocation_settings")
                                    if plan.request.options.get(prefix + "_source_path")])
    # Read-only content comes from the authoritative immutable plan. No file execution.
    previews = {}
    preview_bytes = 0
    for leaf, payload in outputs.items():
        if leaf.endswith((".csv", ".txt", ".tsv", ".json")) and len(payload) <= 2 * 1024 * 1024 and preview_bytes + len(payload) <= 8 * 1024 * 1024:
            try:
                previews[leaf] = payload.decode("utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
            except UnicodeError:
                continue
            preview_bytes += len(payload)
    result["preview_omitted"] = [p for p in outputs if p not in previews]
    result["previews"] = previews
    # Comment provenance size must not hide small program comparisons. Preserve
    # actual engine serialization, including one-to-many output relationships.
    result["ledger_programs"] = []
    ledger = outputs.get("source-target-ledger.json", b"")
    if ledger and len(ledger) <= 32 * 1024 * 1024:
        programs = json.loads(ledger).get("programs", [])
        if len(json.dumps(programs, ensure_ascii=False).encode("utf-8")) <= 4 * 1024 * 1024:
            result["ledger_programs"] = programs
    result["program_comparison_available"] = bool(result["ledger_programs"])
    report = getattr(plan.engine_plan, "report", None) or {}
    result["comment_original_count"] = report.get("counts", {}).get("comment")
    raw = plan.analysis.raw_analysis
    refs = (*getattr(raw, "program_references", ()), *getattr(raw, "comment_references", ()))
    result["module_inventory"] = sorted({unit for ref in refs
        if (unit := source_unit(getattr(ref, "spelling", getattr(ref, "raw_token", getattr(ref, "device", ""))))) is not None}, key=unit_number)
    return result


def with_actual_cpu_report(plan, body):
    """Bind selected model metadata while retaining the engine's target dialect."""
    engine = plan.engine_plan
    if (plan.request.direction != "FX5_TO_Q" or engine is None
            or "report.json" not in (getattr(engine, "outputs", None) or {})):
        return plan
    report = json.loads(engine.outputs["report.json"].decode("utf-8"))
    report["actual_target"] = {"cpu": body.get("actual_q_cpu", "").strip(),
                               "cpu_validation": "NOT_VERIFIED",
                               "allocation_validation": "NOT_CHECKED"}
    outputs = dict(engine.outputs)
    outputs["report.json"] = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return replace(plan, engine_plan=replace(engine, report=MappingProxyType(report),
                                           outputs=MappingProxyType(outputs)))


class SourceAnalysisFailure(ValueError):
    """Retain reader findings without treating missing identity as changed bytes."""

    def __init__(self, code, message, diagnostics):
        super().__init__(message)
        self.code = code
        self.diagnostics = [asdict(item) for item in diagnostics]


def require_analysis_identity(analysis, identity, path):
    if analysis.source_identity == identity:
        return
    try:
        current = SourceIdentity.from_file(path)
    except (OSError, ValueError) as error:
        raise SourceAnalysisFailure("SOURCE_RECHECK_FAILED",
                                    f"원본 재확인을 완료하지 못했습니다: {error}", analysis.diagnostics) from error
    if current != identity:
        raise SourceAnalysisFailure("SOURCE_CHANGED", "분석 중 원본 내용이 변경되었습니다. 다시 분석하세요.",
                                    analysis.diagnostics)
    code = "SOURCE_ANALYSIS_FAILED" if analysis.source_identity is None else "SOURCE_IDENTITY_MISMATCH"
    raise SourceAnalysisFailure(code, "원본 분석을 완료하지 못했습니다. 원본 진단 상세를 확인하세요.",
                                analysis.diagnostics)


def qt_worker(conn, cancelled, body):
    result = None
    try:
        conn.send({"phase": "SOURCE_READING", "label": "원본 파일과 SHA 확인 중"})
        path = core._reject_link_like_ancestors(Path(body["path"]), "source")
        identity = SourceIdentity.from_file(path)
        request, profile = request_from(body, identity)
        conn.send({"phase": "ANALYZING", "label": "기존 엔진에서 POU와 사용 주소 분석 중"})
        analysis = service.analyze(request, cancelled.is_set)
        require_analysis_identity(analysis, identity, path)
        unsupported_pi = analysis.raw_analysis is None and any(
            item.code == text_conversion.SourcePIStatementUnsupportedError.code and item.severity == "error"
            for item in analysis.diagnostics)
        conn.send({"phase": "PLANNING", "label": f"{len(analysis.programs)}개 POU의 CPU, 주소, 출력 용도 점검 중"})
        if request.settings:
            range_rows(request.settings, Bounds(), Bounds(), allow_target_aliases=True)
            if not unsupported_pi:
                inventory = inventory_for_analysis(analysis, request.settings.source)
                _require_current_usage(request.settings, inventory, None, None)
        plan = with_actual_cpu_report(service.plan(request, cancelled.is_set), body)
        if unsupported_pi and (plan.status != "HOLD" or plan.publishable or plan.engine_plan is not None or not any(
                item.code == text_conversion.SourcePIStatementUnsupportedError.code and item.severity == "error"
                for item in plan.diagnostics)):
            raise ValueError("P/I 판독 보류 근거가 바뀌었습니다. 원본을 다시 분석하세요")
        if SourceIdentity.from_file(path) != identity:
            raise ValueError("원본 내용이 변경되었습니다. 다시 분석하세요")
        result = display_plan(plan, profile, body)
        try:
            parent = core._reject_link_like_ancestors(Path(result["output_parent"]), "output parent")
            if not parent.is_dir():
                raise ValueError("output parent must exist")
            result["output_destination_validation"] = "VALID"
        except (OSError, ValueError) as error:
            result.update(output_destination_validation="INVALID", output_destination_error=str(error))
        if (body.get("command") == "validate" and body["mode"] == "convert"
                and request.settings is not None and not unsupported_pi):
            result["validated_xy_json"] = body.get("xy_json")
        if body.get("command") == "publish":
            if result["output_destination_validation"] != "VALID":
                raise ValueError("결과 저장 위치를 사용할 수 없습니다. 존재하는 일반 폴더를 선택하세요")
            # Old confirmation flags do not authorize publication.
            if body.get("execution_request") != EXPLICIT_EXECUTION_REQUEST:
                raise ValueError("명시적 변환 요청이 필요합니다. 변환 버튼으로 실행하세요")
            if request.direction == "FX5_TO_Q" and body.get("actual_q_cpu", "").strip() not in Q_CPUS:
                raise ValueError("실제 대상 Q CPU를 선택하세요. 현재 선택 범위는 Q06UDVCPU이며 실제 프로젝트 할당과 GX 검증은 별도입니다")
            if not plan.publishable or body.get("expected_binding") != result["binding"]:
                raise ValueError("표시한 계획이 변경되었거나 보류 상태입니다. 다시 분석하고 확인하세요")
            conn.send({"phase": "PUBLISHING", "label": "새 결과 폴더에 기존 엔진의 파일 세트 생성 중"})
            publication = service.publish(plan, Path(result["output_parent"]), cancelled.is_set)
            result.update(file_generation="GENERATED", output_dir=str(publication.output_dir),
                          artifacts=[{"name": p.name, "path": str(p), "role": publication.artifact_roles[p.name]}
                                     for p in publication.artifacts])
        if cancelled.is_set() and result["file_generation"] != "GENERATED":
            raise core.ConversionCancelled("cancelled")
        conn.send({"phase": "COMPLETE", "label": "파일 생성 완료. GX와 현장 검증은 별도입니다." if result["file_generation"] == "GENERATED"
                   else "변환 준비 완료. 문제와 출력 용도를 검토하세요.", "result": result})
    except Exception as error:
        if result is not None:
            result["file_generation"] = "CANCELLED" if isinstance(error, core.ConversionCancelled) else "FAILED"
            partial = getattr(error, "partial_paths", ())
            result["remaining_files"] = [str(p) for p in partial]
            result["preserved_folder"] = str(getattr(error, "output_dir", ""))
            result["not_generated"] = [p for p in result["planned_files"] if p not in {Path(v).name for v in partial}]
            if isinstance(error, text_conversion.TextPublicationCleanupError):
                result.update(recovery_uncertain=error.recovery_uncertain,
                              cleanup_folder=str(error.cleanup_dir),
                              publication_completed=error.publication_completed,
                              recovery_message=str(error))
            elif isinstance(error, core.PartialPublishError) and not isinstance(error, service.WorkflowPartialPublishError):
                result["recovery_uncertain"] = True
                result["recovery_message"] = str(error)
        conn.send({"phase": "CANCELLED" if isinstance(error, core.ConversionCancelled) else "FAILED",
                   "label": "취소되었습니다. 원본과 기존 결과는 보존됩니다." if isinstance(error, core.ConversionCancelled)
                   else "파일 생성을 완료하지 못했습니다. 오류와 복구 상세를 확인하세요." if body.get("command") == "publish"
                   else "분석과 검사를 완료하지 못했습니다. 오류 상세를 확인하세요.",
                   "error": {"code": getattr(error, "code", type(error).__name__), "message":
                             "임시 폴더 정리 실패. 결과의 복구 상세에서 남은 파일을 확인하세요."
                             if isinstance(error, text_conversion.TextPublicationCleanupError) else str(error),
                             **({"diagnostics": error.diagnostics} if isinstance(error, SourceAnalysisFailure) else {})},
                   **({"result": result} if result is not None else {})})
    finally:
        conn.close()


class QtSession(Session):
    """Cooperative native cancellation keeps publication cleanup observable."""
    def __init__(self):
        super().__init__(qt_worker)
        self.last_settings_check = 0

    def snapshot(self):
        state = super().snapshot()
        result = state.get("result")
        if result and time.monotonic() - self.last_settings_check > 2:
            self.last_settings_check = time.monotonic()
            try:
                unchanged = all(SourceIdentity.from_file(v["path"], max_bytes=1024 * 1024).sha256 == v["sha256"]
                                for v in result.get("settings_origins", []))
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                with self.lock:
                    if self.generation == state["generation"] and self.result is result:
                        self.result = None
                        self.events.append({"phase": "INVALIDATED", "label": "주소 설정 파일이 변경되었습니다. 이전 계획과 확인을 무효화했습니다."})
                        self.version += 1
                state = super().snapshot()
        return state

    def cancel(self):
        with self.lock:
            if self.active:
                self.active[1].set()
                self.events.append({"phase": "CANCELLING", "label": "취소 요청 처리 중. 파일 정리가 끝날 때까지 기다려 주세요."})
                self.version += 1

    def _reap(self, job):
        proc, cancel, conn, thread, scratch = job
        cancel.set()
        deadline = time.monotonic() + 30
        if thread is not None and thread.ident is not None:
            thread.join(max(0, deadline - time.monotonic()))
        # A terminal pipe event can precede Windows process teardown. Share
        # the bounded grace period rather than leaving only one second for it.
        proc.join(max(0, deadline - time.monotonic()))
        if proc.is_alive() or (thread is not None and thread.is_alive()):
            raise RuntimeError("엔진 정지 미확인. 소유 프로세스와 임시 폴더를 보존했습니다")
        conn.close()
        scratch.cleanup()
        proc.close()
