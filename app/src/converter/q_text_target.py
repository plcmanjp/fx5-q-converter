"""Q text adapter: source records stay immutable; target failures are review data.

Input: complete-text record dictionaries, including neutral-IR provenance.
Output: projected instruction/operands and existing core validation findings.
"""
from dataclasses import replace
from . import statement_layout
import hashlib
import re

from . import core, annunciator

_XY = re.compile(r"(?:K[1-8])?[XY][0-9A-F]+(?:(?:ZZ|LZ|Z)[0-9]+)?\Z", re.I)
_Q_COMMENT_FIXED_LIMITS = {"Q06UDV": {"SM": 2047, "SD": 2047}}
_Q_COMMENT_MODULE_LIMITS = {"Q06UDV": {"unit_ranges": ((0, 0x1FF), (0x3E0, 0x3FF)), "buffer_maximum": 65535}}
_Q_COMMENT_MODULE_AUTHORITIES = {
    "comment_manual": "https://dl.mitsubishielectric.com/dl/fa/document/manual/plc/sh080779eng/sh080779engas.pdf",
    "cpu_manual": "https://dl.mitsubishielectric.com/dl/fa/document/manual/plc/sh080807eng/sh080807engae.pdf",
}
_Q_COMMENT_RANGE_AUTHORITY = (
    "https://www.mitsubishielectric.com/fa/vn_en/products/faspec/detail.page?"
    "category=ex&formNm=QnUDVCPU_Q06UDVCPU_3885&id=spec&kisyu=%2Fplcq&lang=2"
)


def instruction(record):
    operands = record["operands"]
    return core.Instruction(
        record.get("sequence", 0), record["step"] or 0,
        record.get("target_opcode", record["opcode"]), operands[0] if operands else "",
        line_statement=record["text"] if record["kind"] == "statement" else "",
        note=record["text"] if record["kind"] == "note" else record.get("inline_note", ""),
        note_subtype=record["subtype"], continuation_operands=tuple(operands[1:]),
        continuation_rows=tuple(tuple(r) for r in record.get("continuation_rows", ())),
        source_record_id=record.get("source_record_id", ""),
        source_digest=record.get("source_digest", ""),
        source_locator=record.get("source_locator", record["locator"]),
        source_semantic_tags=tuple(record.get("source_semantic_tags", ())),
    )


def project(record, mapped_operands, source_sha256, program_name, *, initial_findings=()):
    source = instruction(record)
    findings = [*initial_findings, *(core.validate_records((source,)) if record["kind"] == "instruction" else [])]
    findings += core._validate_fx_io_source_record(source)
    if record.get("semantic_binding_issue"):
        findings.append([str(source.sequence), str(source.source_step), source.instruction, source.operand,
                         record["semantic_binding_issue"], "SOURCE_SEMANTIC_BINDING_REQUIRED", "P002|Converter",
                         f"program={program_name};source_record_id={source.source_record_id};source_locator={source.source_locator}"])
    findings.extend(record.get("source_findings", ()))
    try:
        target = core.project_q_record(source_sha256, program_name, source)
        # core validates FX spelling before projection. Only now substitute the
        # user RangeMapper output; a Q hex token must never re-enter FX parsing.
        xy = {token: mapped for token, mapped in zip(record["operands"], mapped_operands, strict=True)
              if _XY.fullmatch(token)}
        ops = [xy.get(str(token), str(token)) for token in (target.operand, *target.continuation_operands) if token]
        target = replace(target, operand=ops[0] if ops else "", continuation_operands=tuple(ops[1:]))
        if record["kind"] == "instruction":
            findings += core._validate_q_target_record(program_name, target, source_record=source)
        findings += core.validate_ladder_notes((target,), program_name=program_name)
        if not findings:
            target = replace(target, note=core.project_ladder_note(target.note))
    except core.ConversionError as error:
        findings.append([str(source.sequence), str(source.source_step), source.instruction, source.operand,
                         str(error), error.finding_code, error.mining_tracks, error.candidate])
        target = source
        ops = list(record["operands"])
    if findings:
        # A blocked row is exact source review, never an invented executable
        # placeholder or partially projected instruction.
        target = source
        ops = list(record["operands"])
    projection = {"target_kind": record["kind"], "target_opcode": record["opcode"] if findings else target.instruction,
                  "projection": "Q_APPROVED_PROJECTION" if not findings and (target.instruction != record["opcode"] or ops != record["operands"])
                                else "SOURCE_OPCODE",
                  "target_note": target.note}
    if not findings and source.instruction == "FLT2DINT":
        projection["semantic_delta"] = "FX5_TRUNCATION_TO_Q_ROUNDING"
    if target.note != source.note:
        projection["note_projection"] = "Q_CP949_32_BYTES"
        projection["source_note"] = source.note
    return projection, ops, findings


def bind_source_semantics(source_path, body, neutral_pous, *, context=None):
    """Read existing MIL semantics from the same snapshot, without using comments.

    Neutral promotions have no independent MIL semantics. They retain source
    text but cannot prove the absence of source-only holds.
    """
    try:
        parsed = core.read_gx3(source_path, _source_bytes=body)
    except core.ConversionError as error:
        return {}, f"Existing source semantic binding unavailable: {error}"
    programs = (parsed.programs if isinstance(parsed, core.ParsedMultiProject) else
                (core.ParsedProgram(parsed.program_name, "", parsed.instructions, parsed.structure,
                                    parsed.findings, False, parsed.opaque_blocks, parsed.recovery_methods,
                                    parsed.coverage, parsed.source_operations),))
    validated_findings = core.validate_project(parsed)
    source_sha256 = hashlib.sha256(body).hexdigest()
    result = {}
    for program in programs:
        name = program.name if isinstance(program, core.ParsedProgram) else program.program_name
        promoted = any(method[0] == "p002_neutral_ir" for method in program.recovery_methods)
        if promoted and core._actual_source_recovered_review_eligible(program, source_sha256, validated_findings):
            promoted = False
        rows = {}
        for row in program.instructions:
            if row.source_record_id in rows:
                return {}, "Existing source semantic binding has duplicate record IDs"
            rows[row.source_record_id] = (row, promoted, tuple(
                finding for finding in program.findings
                if finding[0] == str(row.sequence) and finding[1] == str(row.source_step)))
        result[name] = rows
    if set(result) != {pou["name"] for pou in neutral_pous}:
        return {}, "Existing source semantic POU inventory differs from Neutral IR"
    if context is not None:
        context.update(programs={p.name: p for p in programs},
                       findings=validated_findings,
                       preserve_source_steps=isinstance(parsed, core.ParsedMultiProject))
    return result, ""


def program_output(program, ledger, context, findings, source_sha256, project_identifier):
    """Reuse complete-POU eligibility and verified emit paths, never row heuristics."""
    legacy = context.get("programs", {}).get(program["name"])
    if legacy is None or len(legacy.instructions) != len(ledger) or legacy.opaque_blocks:
        return None
    if any(r.get("semantic_binding_issue") for r in ledger):
        return None
    if any(r.get("kind") == "statement" and r.get("statement_state") == "HELD" for r in ledger):
        return None
    if any(core._validate_fx_io_source_record(r) for r in legacy.instructions):
        return None
    for source, record in zip(legacy.instructions, ledger, strict=True):
        neutral = instruction({**record, "target_opcode": record.get("source_instruction", core._official_source_opcode(record["opcode"]))})
        if semantic_evidence(neutral, (source, False, ()))[1]:
            return None
    invalid = [list(row) for row in dict.fromkeys(
        tuple(row) for row in [*context["findings"], *findings])]
    names = tuple(context["programs"])
    projected = []
    for source, record in zip(legacy.instructions, ledger, strict=True):
        try:
            base = core.project_q_record(source_sha256, legacy.name, source)
        except core.ConversionError:
            return None
        mapped = record.get("mapped_source_operands", record["operands"])
        if len(mapped) != len(record["operands"]):
            return None
        xy = {token: replacement for token, replacement in zip(record["operands"], mapped, strict=True)
              if _XY.fullmatch(token)}
        operands = [xy.get(str(token), str(token))
                    for token in (base.operand, *base.continuation_operands) if token]
        rows = base.continuation_rows
        if rows:
            if len(rows) != max(0, len(operands) - 1):
                return None
            rows = tuple(tuple((*row[:3], operand, *row[4:]))
                         for row, operand in zip(rows, operands[1:], strict=True))
        projected.append(replace(base,
                                 operand=operands[0] if operands else "", continuation_operands=tuple(operands[1:]),
                                 continuation_rows=rows,
                                 line_statement=record["target_text"] if record["kind"] == "statement" else base.line_statement))
    projected = tuple(projected)
    keys = core._target_unsupported_fallback_keys(legacy.name, legacy.instructions, invalid, names, program=legacy)
    target, keys = core._verified_official_dummy_projection(legacy.name, legacy.instructions, projected, keys)
    code = core.OFFICIAL_DUMMY_FALLBACK if keys else ""
    typed = {}
    if not keys:
        typed = core._typed_mining_fallback_keys(legacy, invalid, names)
        target, typed = core._verified_typed_mining_projection(legacy.name, legacy.instructions, projected, typed)
        keys = frozenset(typed)
        code = core.TYPED_MINING_FALLBACK if keys else ""
    if not keys:
        structural = core._actual_structural_review_projection(legacy, projected, invalid, source_sha256)
        if structural is not None:
            target = structural
            keys = frozenset(core._fallback_record_key(source)
                             for source, before, after in zip(legacy.instructions, projected, target, strict=True)
                             if before != after)
            code = core.STRUCTURAL_RESULT_DUMMY_FALLBACK
    global_blocker, blocked = core._pou_blockers(invalid, names, {legacy.name: keys} if keys else {})
    normal = (not keys and not global_blocker and legacy.name not in blocked
              and core._actual_source_recovery_allows_normal_q(legacy, source_sha256, invalid))
    local_findings = [row for row in invalid if core._candidate_program(row[7]) == legacy.name]
    ans_review = (not keys and not normal and not global_blocker
                  and any(annunciator.supported_ans(row) for row in projected)
                  and all(row[5] == core.TARGET_UNSUPPORTED for row in local_findings)
                  and all(row.instruction != "ANS" or annunciator.supported_ans(row) for row in projected)
                  and core._actual_source_recovery_allows_normal_q(legacy, source_sha256, invalid))
    if not keys and not normal and not ans_review:
        return None
    if ans_review:
        # Same mixed-review policy as the text writer: keep unsupported rows
        # source-exact while expanding the independently bound ANS rows.
        held = {core._finding_fallback_key(row) for row in local_findings}
        target = tuple(source if core._fallback_record_key(source) in held else projected_row
                       for source, projected_row in zip(legacy.instructions, projected, strict=True))
    try:
        target = tuple(replace(row, note=core.project_ladder_note(row.note)) for row in target)
    except core.ConversionError:
        return None
    details = {}
    review_text = None
    mining = code == core.TYPED_MINING_FALLBACK
    structural_review = code == core.STRUCTURAL_RESULT_DUMMY_FALLBACK
    publication = core.TYPED_MINING_REVIEW_PUBLICATION if mining else core.DIAGNOSTIC_FALLBACK_PUBLICATION
    source_target_pairs = tuple(zip(legacy.instructions, target, strict=True))
    has_ans = any(row.instruction == "ANS" for row in target)
    if has_ans:
        try:
            target = annunciator.expand(target)
        except ValueError:
            return None
    if keys:
        # Validation findings stay held even when an import-shaped diagnostic
        # exists. Mark the source operation and the exact canonical substitute.
        fallback_records = {}
        # Expansion changes target cardinality; fallback identity stays source based.
        for source, replacement in source_target_pairs:
            key = core._fallback_record_key(source)
            if key not in keys:
                continue
            fallback_records[key] = replacement
            details[source.source_record_id] = {
                "code": code, "kind": typed.get(key, core._official_dummy_fallback_kind(source)),
                "target_opcode": replacement.instruction,
                "target_operands": [v for v in (replacement.operand, *replacement.continuation_operands) if v],
                "target_note": core.project_ladder_note(replacement.note),
                "publication": publication,
                "runtime_action": core.DIAGNOSTIC_FALLBACK_RUNTIME_ACTION,
                "compatibility": core.DIAGNOSTIC_FALLBACK_COMPATIBILITY,
                "reason": "; ".join(row[4] for row in invalid if core._finding_fallback_key(row) == key
                                    and core._candidate_program(row[7]) == legacy.name),
            }
        local = tuple(tuple(row) for row in invalid if core._candidate_program(row[7]) == legacy.name)
        review_text = core.partial_review_text(legacy.name, legacy.instructions, legacy.opaque_blocks,
                                              local, fallback_records=fallback_records)
        layout = {"status": "NOT_APPLIED_NON_NORMAL_Q", "reason": "NORMAL_Q_CSV_REQUIRED", "source_ir_changed": False}
    elif ans_review:
        layout = {"status": "NOT_APPLIED_NON_NORMAL_Q", "reason": "NORMAL_Q_CSV_REQUIRED", "source_ir_changed": False}
    else:
        layout_result = core.apply_constant_root_layout(target)
        target, layout = layout_result.records, layout_result.report
    step_report = None
    target_only_steps = has_ans or any(r.get("target_kind") == "statement" for r in ledger)
    if target_only_steps:
        # Source binding above is complete before any target-only step work.
        by_id = {r.get("source_record_id"): r for r in ledger}
        aligned = [{**by_id.get(r.source_record_id, {}),
                    "target_kind": "instruction" if r.instruction else "statement" if r.line_statement else "note",
                    "target_opcode": r.instruction,
                    "target_operands": [v for v in (r.operand, *r.continuation_operands) if v]}
                   for r in target]
        steps, step_report = statement_layout.widths(aligned, "FX5_TO_Q")
        target = tuple(replace(r, target_step=step) for r, step in zip(target, steps, strict=True))
    payload = core.q_import_csv(project_identifier, target, preserve_source_steps=context["preserve_source_steps"],
                                target_only_steps=target_only_steps)
    # Target-only extra layout rows belong here, outside the one-to-one source ledger.
    serialized = [{"sequence": r.sequence, "source_step": r.source_step, "target_step": r.target_step,
                   "source_record_id": r.source_record_id, "source_digest": r.source_digest,
                   "source_locator": r.source_locator, "opcode": r.instruction,
                   "operands": [v for v in (r.operand, *r.continuation_operands) if v],
                   "statement": r.line_statement, "pi_statement": r.pi_statement, "note": core.project_ladder_note(r.note),
                   "csv_row_count": 1 + len(r.continuation_rows or r.continuation_operands)}
                  for r in target]
    expansions = {}
    if has_ans:
        for record in ledger:
            if record["opcode"] != "ANS":
                continue
            expansions[record["source_record_id"]] = {
                "projection": annunciator.PROJECTION,
                "timer_unit_policy": "NOT_CHECKED_OR_CHANGED",
                "operand_policy": "SOURCE_T_D_K_F_VERBATIM",
                "target_expansion": [{"opcode": row["opcode"], "operands": row["operands"]}
                                     for row in serialized if row["source_record_id"] == record["source_record_id"]],
            }
    return {"payload": payload, "fallbacks": details, "review_text": review_text, "expansions": expansions,
            **({"statement_steps": step_report} if step_report is not None else {}),
            "target_layout": layout, "serialized_records": serialized,
            "publication_class": (core.TYPED_MINING_REVIEW_STATUS if mining else
                                  "SOURCE_REVIEW_ONLY" if structural_review or ans_review else
                                  "IMPORT_REVIEW_CSV" if keys else "NORMAL_Q_CSV"),
            "review_status": (core.TYPED_MINING_REVIEW_STATUS if mining else
                              core.STRUCTURAL_RESULT_DUMMY_FALLBACK if structural_review else
                              "TARGET_UNSUPPORTED_HELD" if ans_review else ""),
            "publication": publication if keys or ans_review else "",
            "q_semantic_class": ("MINING_REVIEW_HELD" if mining else
                                 "STRUCTURAL_DUMMY_NOT_EQUIVALENT" if structural_review else
                                 "TARGET_UNSUPPORTED_HELD" if keys or ans_review else "Q_TARGET_VALIDATION_PASSED")}


def semantic_evidence(neutral, bound):
    if bound is None:
        return (), "Existing source semantic record is unavailable", ()
    legacy, promoted, findings = bound
    if promoted:
        return (), "Existing source row uses Neutral IR promotion without independent MIL semantics", findings
    if ((legacy.source_record_id, legacy.source_digest, legacy.source_locator, legacy.source_step,
         core._official_source_opcode(legacy.instruction), legacy.operand, legacy.continuation_operands)
            != (neutral.source_record_id, neutral.source_digest, neutral.source_locator, neutral.source_step,
                core._official_source_opcode(neutral.instruction), neutral.operand, neutral.continuation_operands)):
        return (), "Existing source semantic record differs from Neutral IR provenance or operands", findings
    if (legacy.line_statement, legacy.note, legacy.note_subtype) != (neutral.line_statement, neutral.note, neutral.note_subtype):
        return (), "Existing source statement or note semantics differ from Neutral IR", findings
    return legacy.source_semantic_tags, "", findings


def note_findings(records, program_name):
    """Reuse the existing note subtype and attachment lifecycle authority."""
    sources = tuple(replace(instruction(r), sequence=i) for i, r in enumerate(records))
    operations = tuple(core.SerializedOperation(
        r["kind"], sources[i].instruction,
        ("NOTE", "st=" + r["subtype"]) if r["kind"] == "note" else (r["kind"].upper(),)
    ) for i, r in enumerate(records))
    return core._inline_sts_target_findings(program_name, sources, operations)


def approved_comment_overrides(comments, source_sha256):
    """Consume only existing source-hash and expected-text approved overrides."""
    owned = core.COMMENT_OVERRIDES_BY_SOURCE_SHA256.get(source_sha256, {})
    if not owned:
        return {}
    selected = [c for c in comments if c["device"] in owned and c["scope"] == "common_device"
                and c["language"] in {"English", "ENGLISH", "source"}]
    try:
        if len({c["device"] for c in selected}) != len(selected):
            raise core.ConversionError("Approved comment override has duplicate source binding")
        projected = core._apply_comment_overrides(source_sha256, tuple(core.DeviceComment(
            c["device"], core._normalize_device_comment_text(c["device"], c["text"], None)) for c in selected))
        return {c.device: {"source_text": c.text, "target_text": c.target_text, "status": c.target_status}
                for c in projected}
    except core.ConversionError as error:
        return {device: {"error": str(error)} for device in owned}


def comment(record, mapped_device, *, override=None):
    result = {**record, "target_device": mapped_device, "target_text": None, "state": "RETAINED",
              "code": "COMMENT_VALUE_ABSENT"}
    if record["text"] is None:
        return result
    try:
        normalized = core._normalize_device_comment_text(record["device"], record["text"], None)
        target_text, status = normalized, "NORMALIZED" if normalized != record["text"] else None
        if override is not None and record["scope"] == "common_device" and record["language"] in {"English", "ENGLISH", "source"}:
            if override.get("error") or override.get("source_text") != normalized:
                raise core.ConversionError("Approved comment override does not match source binding",
                                           finding_code="APPROVED_COMMENT_OVERRIDE_SOURCE_MISMATCH")
            target_text, status = override["target_text"], override["status"]
        projected = core.project_comment(core.DeviceComment(
            record["device"], record["text"], target_text, status))
        device = mapped_device if _XY.fullmatch(record["device"]) else str(projected.device)
        result.update(target_device=device, target_text=projected.projected_text,
                      projection_status=projected.status, source_bytes=projected.source_bytes,
                      target_bytes=projected.projected_bytes)
        fixed = re.fullmatch(r"(SM|SD)([0-9]+)(?:\.[0-9A-F])?", device, re.I)
        range_reason = ""
        if fixed:
            family, address = fixed[1].upper(), int(fixed[2])
            maximum = _Q_COMMENT_FIXED_LIMITS["Q06UDV"][family]
            result["target_device_range"] = {"cpu": "Q06UDV", "family": family, "address": address,
                                             "minimum": 0, "maximum": maximum, "fixed_points": maximum + 1,
                                             "authority": _Q_COMMENT_RANGE_AUTHORITY}
            if address > maximum:
                range_reason = (f'Q06UDV {family} 코멘트 주소 {address}는 고정 범위 '
                                f'{family}0..{maximum}({maximum + 1}점)를 초과합니다. '
                                '원문은 보존하고 Q 가져오기 후보에서 제외합니다.')
        module = re.fullmatch(r"U([0-9A-F]+)\\G([0-9]+)(?:\.([0-9A-F]))?", device, re.I)
        if module:
            unit, address = int(module[1], 16), int(module[2], 10)
            limits = _Q_COMMENT_MODULE_LIMITS["Q06UDV"]
            valid_unit = any(first <= unit <= last for first, last in limits["unit_ranges"])
            result["target_device_range"] = {
                "cpu": "Q06UDV", "family": "U\\G", "unit": unit, "unit_radix": 16,
                "unit_ranges": [[first, last] for first, last in limits["unit_ranges"]],
                "buffer_address": address, "buffer_radix": 10, "buffer_minimum": 0,
                "buffer_maximum": limits["buffer_maximum"], "bit_suffix": module[3],
                "scope": "COMMENT_ADDRESS_RANGE_ONLY_HARDWARE_COMPATIBILITY_NOT_CHECKED",
                "authority": _Q_COMMENT_MODULE_AUTHORITIES,
            }
            if not valid_unit or address > limits["buffer_maximum"]:
                range_reason = (f'Q06UDV 모듈 코멘트 {device}는 지원 범위 '
                                'U0..U1FF 또는 U3E0..U3FF(16진수), G0..G65535(10진수)를 벗어납니다. '
                                'U200..U3DF는 코멘트 미지원 구간입니다. '
                                '원문은 보존하고 Q 가져오기 후보에서 제외합니다.')
        if core._q06udv_rejected_operand(device):
            result.update(state="BLOCKED", code="TARGET_DEVICE_EXCLUDED",
                          reason=core._q06udv_operand_hold_reason((device,), "device")
                                 + ("; " + range_reason if range_reason else ""))
        elif range_reason:
            result.update(state="BLOCKED", code="Q_COMMENT_DEVICE_RANGE_EXCEEDED", reason=range_reason)
        # The source reader preserves raw slot 0 as CmtNo:0. Keep that
        # provenance intact while accepting the same slot as legacy 0/"0".
        # This does not identify a human language or authorize text overrides.
        elif record["scope"] != "common_device" or record["language"] not in {"source", "English", "ENGLISH", 0, "0", "CmtNo:0"}:
            result.update(code="COMMENT_BINDING_REVIEW_REQUIRED")
        else:
            result.update(state="CANDIDATE", code="COMMENT_PROJECTED")
    except core.ConversionError as error:
        result.update(state="BLOCKED", code=error.finding_code or "COMMENT_PROJECTION_BLOCKED", reason=str(error))
    return result
