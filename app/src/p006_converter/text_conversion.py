"""Complete source ledger with existing approved target text projections.

Only decoded POU/COMMENT data is published. Target limitations remain explicit
review findings; source text stays in the ledger and source review artifacts.
This is a text contract, not approval to download or execute it on a PLC.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping

from . import core, fx5_target, q_text_target, q_to_fx5 as q, q_source_review, upstream_contracts
from .word_settings import WordDeviceSettings
from .device_relocation import DeviceRelocationSettings, operand_width
from .xy_settings import MappingSettings
from .module_mapping import ModuleMapper
from . import statement_policy as statements
from . import statement_layout
from .xy_mapping import Profile, RangeMapper, Reference, UnmappedPolicy

SCHEMA = "plcman.p006.complete-text.v1"
HEADER = core.Q_HEADER
_XY = re.compile(r"((?:K[1-8])?[XY][0-9A-F]+)((?:ZZ|LZ|Z)[0-9]+)?\Z", re.I)
_WORD = re.compile(r"ZR([0-9]+)(.*)\Z", re.I)


class SourceCommentDecodeError(ValueError):
    """Keep source decoding failures separate from target comment exclusions."""

    code = "TEXT_SOURCE_COMMENT_UNDECODED"

    def __init__(self, coverage, emitted_count, findings):
        self.detail = json.dumps({"coverage": dict(coverage), "emitted_count": emitted_count,
                                  "findings": list(findings)}, ensure_ascii=False, sort_keys=True)
        self.reason = (f'원본 장치 코멘트 {coverage["total"]:,}개 중 '
                       f'{coverage["unknown"]:,}개의 주소 또는 저장 형식이 미해독입니다.'
                       if coverage["unknown"] else "원본 장치 코멘트의 개수 또는 저장 위치 결속을 확인하지 못했습니다.")
        self.action = "코멘트 해독 지원과 원본 위치를 확인한 뒤 다시 분석하세요. 원본과 미해독 근거는 보존합니다."
        super().__init__("GX3 COMMENT data or binding is undecoded")


class SourcePIStatementUnsupportedError(ValueError):
    """Refuse source promotion while the separate P/I carrier is unsupported."""

    code = "TEXT_Q_PI_STATEMENT_UNSUPPORTED"

    def __init__(self, input_sha256: str, pou: str, store: str, source_digest: str):
        self.reason = "Q 원본의 P/I 문장 저장 형식을 지원하지 않아 변환을 보류합니다."
        self.action = "원본 파일의 P/I 본문은 보존됩니다. P/I 판독 지원을 확인한 후 다시 분석하세요."
        self.detail = json.dumps({"input_sha256": input_sha256, "pou": pou,
                                  "source_store": str(store), "source_digest": source_digest,
                                  "reader_diagnostic": "<i:text:pi>",
                                  "target_support": "NOT_IMPLEMENTED"}, ensure_ascii=False, sort_keys=True)
        super().__init__("Q P/I Statement carrier is unsupported; original source retained")


class TextPublicationCleanupError(core.PartialPublishError):
    """Retain structured recovery locations when staging cleanup fails."""

    def __init__(self, output_dir: Path, cleanup_dir: Path, published: bool,
                 planned_files: tuple[str, ...]):
        self.output_dir = output_dir
        self.cleanup_dir = cleanup_dir
        self.publication_completed = published
        self.partial_paths = ()
        self.recovery_uncertain = False
        try:
            core._reject_link_like_ancestors(output_dir, "recovery directory")
            self.partial_paths = tuple(output_dir / leaf for leaf in planned_files
                                       if (output_dir / leaf).is_file()
                                       and not (output_dir / leaf).is_symlink())
        except (OSError, ValueError):
            self.recovery_uncertain = True
        super().__init__(f"text staging cleanup failed; publication_completed={published}; "
                         f"preserved_folder={output_dir}; cleanup_folder={cleanup_dir}")


def _check(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise core.ConversionCancelled("text conversion cancelled")


def _leaf(name: str) -> str:
    # Preserve the exact POU name; never silently rename or truncate it.
    if (not name or name.endswith((" ", "."))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or PureWindowsPath(f"{name}.csv").is_reserved()):
        raise ValueError(f"POU name cannot be used as a Windows CSV filename: {name!r}")
    leaf = f"{name}.csv"
    if len(leaf.encode("utf-16-le")) // 2 > 255:
        raise ValueError(f"POU CSV filename is too long: {name!r}")
    return leaf


def _store_csv(outputs: dict[str, bytes], leaf: str, rows: list[list[str]]) -> None:
    if any(existing.casefold() == leaf.casefold() for existing in outputs):
        raise ValueError(f"POU/comment CSV filenames collide: {leaf!r}")
    outputs[leaf] = q._csv(rows)


def _record(kind: str, locator: str, *, opcode: str = "", operands: list[str] | None = None,
            text: str = "", subtype: str = "", step: int | None = None) -> dict[str, Any]:
    return {"kind": kind, "locator": locator, "opcode": opcode,
            "operands": operands or [], "text": text, "subtype": subtype, "step": step}


def _q_document(source: Path, body: bytes, cancel: Callable[[], bool] | None) -> dict[str, Any]:
    ir_module, reader = q._reader_modules()
    with tempfile.TemporaryDirectory(prefix="p006-text-q-") as scratch:
        copy = Path(scratch) / "source.gxw"
        copy.write_bytes(body)
        q._preflight_comment_directory(copy, reader)
        ir = q._build_source_ir(ir_module, reader, copy)
        if (ir.get("schema_name") != "plcman.gxw.reference-ir" or ir.get("schema_version") != 1
                or ir.get("input", {}).get("sha256") != hashlib.sha256(body).hexdigest()
                or ir.get("analysis", {}).get("state") not in {"COMPLETE", "PARTIAL"}):
            raise ValueError("Q source IR is incomplete or changed")
        streams = reader.load_all_substreams(str(copy))
        pous, comments, registry = reader.collect_pous(streams)
        if not pous or registry is None or set(registry[2]) != set(pous) or len(registry[2]) != len(pous):
            raise ValueError("Q POU registry is not verified")
        pairs, warning = reader.device_comment_pairs(streams)
        if warning not in {None, "NoCommentStream"} or len(pairs) != len({d for d, _ in pairs}):
            raise ValueError("Q COMMENT binding is not verified")
        if {d: text for d, text in pairs if text} != comments:
            raise ValueError("Q COMMENT binding differs from reader")
        occurrences = iter(ir["occurrences"])
        programs = []
        for name in registry[2]:
            _check(cancel)
            store, data, _ = pous[name]
            rows, unknown_i, unknown_d = q._reader_pou_rows(reader, data, comments)
            if "<i:text:pi>" in unknown_i:
                raise SourcePIStatementUnsupportedError(hashlib.sha256(body).hexdigest(), name, store,
                                                        hashlib.sha256(reader.program_section(data)).hexdigest())
            if unknown_i or unknown_d:
                raise ValueError("Q source has undecoded instruction or operand")
            notes = iter(reader.typed_note_records(data, comments, framed_types=True, text_encoding="auto"))
            records = []
            previous = None
            source_digest = hashlib.sha256(reader.program_section(data)).hexdigest()
            typed_statements = {}
            statement_binding_error = ""
            try:
                typed_statements = {v["row_index"]: v for v in reader.typed_statement_records(
                    data, comments, text_encoding="auto")}
            except (ValueError, AttributeError) as error:
                statement_binding_error = type(error).__name__
            for index, (op, operands, _) in enumerate(rows):
                _check(cancel)
                locator = f"_hdb/{store}:row:{index}"
                if op == "__STMT__":
                    statement = _record("statement", locator, text=operands)
                    statement["source_digest"] = source_digest
                    typed = typed_statements.pop(index, None)
                    if typed is not None:
                        raw = bytes.fromhex(typed["raw_text_hex"])
                        if (len(raw) != typed["encoded_length"]
                                or reader._decode_pou_text(raw, "auto") != typed["text"]
                                or operands != ("*" if typed["subtype"] == "s" else "") + typed["text"]):
                            raise ValueError("Q Statement raw text/type binding differs")
                        statement.update(statements.source_fields(typed["text"], typed["subtype"], typed["raw_type"],
                            {"input_sha256": hashlib.sha256(body).hexdigest(), "source_digest": source_digest,
                             "locator": locator, "raw_text_hex": typed["raw_text_hex"], "offset": typed["offset"],
                             "source_step_width": typed["source_step_width"]}))
                    else:
                        statement["statement_binding_issue"] = statement_binding_error or "STATEMENT_RAW_RECORD_MISSING"
                    records.append(statement)
                elif op == "__NOTE__":
                    note = next(notes, None)
                    if (not note or note.get("text") != operands or note.get("subtype") not in {"s", "i"}
                            or note.get("attachment") != previous):
                        raise ValueError("Q Note text, type or attachment differs")
                    raw = bytes.fromhex(note["raw_text_hex"])
                    if len(raw) != note["encoded_length"] or reader._decode_pou_text(raw, "auto") != operands:
                        raise ValueError("Q Note encoded text differs")
                    records.append(_record("note", locator, text=operands, subtype=note["subtype"]))
                else:
                    tokens = ir_module.split_operands(operands)
                    item = next(occurrences, None)
                    if (not item or item["pou"]["name"] != name or item["record"]["opcode"] != op
                            or item["record"]["sequence"] != index
                            or item["provenance"]["source_locator"] != locator
                            or str(item["provenance"]["source_store"]) != str(store)
                            or item["provenance"]["source_digest"] != source_digest
                            or [v["raw_token"] for v in item["operands"]] != tokens):
                        raise ValueError("Q source row differs from reference IR")
                    previous = {"instruction": op, "operands": tokens}
                    records.append(_record("instruction", locator, opcode=op, operands=tokens, step=index))
            if next(notes, None) is not None:
                raise ValueError("Q Note inventory differs")
            if typed_statements:
                raise ValueError("Q Statement typed inventory differs")
            programs.append({"name": name, "records": records})
        if next(occurrences, None) is not None:
            raise ValueError("Q source instruction inventory differs")
        return {"programs": programs,
                "comments": [{"device": d, "text": text, "scope": "common_device", "language": "source",
                              "program_id": None, "locator": f"comment:{d}"} for d, text in pairs],
                "metadata": {"reader_commit": q.READER_COMMIT, "reader_wheel_sha256": q.READER_SHA256,
                             "execution_access_analysis": ir["analysis"],
                             "encoding": "STRICT_REVERSIBLE_CP949_THEN_CP1252"}}


def _fx5_document(source: Path, body: bytes, cancel: Callable[[], bool] | None) -> dict[str, Any]:
    value = upstream_contracts.build_source_neutral_ir(
        source, source_bytes=body, contracts=core._require_source_contract(),
        public_overlay=core._validated_public_upstream())
    if value["input"]["sha256"] != hashlib.sha256(body).hexdigest():
        raise ValueError("GX3 source digest differs")
    for kind in ("pou", "record"):
        counts = value["coverage"][kind]
        if counts["decoded"] != counts["total"] or counts["partial"] or counts["unknown"]:
            raise ValueError(f"GX3 source {kind} is not completely decoded")
    comment_counts = value["coverage"]["comment"]
    comment_findings = [f for f in value["findings"] if f.get("object_kind") == "comment"]
    if (comment_counts["unknown"] or len(value["comments"]) != comment_counts["total"]
            or comment_findings):
        raise SourceCommentDecodeError(comment_counts, len(value["comments"]), comment_findings)
    # Non-English slots and local scope are marked partial by the execution
    # profile. Preserve their declared slot/scope and source table separately.
    for comment in value["comments"]:
        if comment["value_state"] in {"VALUE_PRESENT", "EMPTY_STRING"} and not isinstance(comment["value"], str):
            raise ValueError("GX3 COMMENT text is invalid")
    programs = []
    q_source_context = {}
    semantic_programs, semantic_issue = q_text_target.bind_source_semantics(source, body, value["pous"], context=q_source_context)
    for pou in value["pous"]:
        _check(cancel)
        rows, _ = core._neutral_ir_instructions(pou)
        records = []
        source_records = iter(r for r in pou["records"] if r["kind"] != "continuation")
        for row in rows:
            source_record = next(source_records)
            kind = source_record["kind"]
            # Several decoded commands can share one serialized MIL block
            # locator. Their record IDs, not the block alone, own projections.
            locator = f'pou:{pou.get("pou_id", pou["name"])}:{row.source_locator}:record:{row.source_record_id}'
            if kind == "statement":
                record = _record("statement", locator, text=row.line_statement, step=row.source_step)
                evidence = source_record.get("provenance", {}).get("relation_evidence", ())
                raw_types = [v.split("=", 1)[1] for v in evidence if v.startswith("statement_raw_st=")]
                body_hashes = [v.split("=", 1)[1] for v in evidence if v.startswith("statement_body_sha256=")]
                if len(raw_types) == len(body_hashes) == 1 and raw_types[0] in {"i", "s"}:
                    raw_type = raw_types[0]
                    statement_body = row.line_statement[1:] if raw_type == "s" else row.line_statement
                    if ((raw_type != "s" or row.line_statement.startswith("*"))
                            and hashlib.sha256(statement_body.encode("utf-8")).hexdigest() == body_hashes[0]):
                        record.update(statements.source_fields(statement_body, raw_type, raw_type,
                            {"input_sha256": hashlib.sha256(body).hexdigest(), "source_digest": row.source_digest,
                             "locator": locator, "source_record_id": row.source_record_id,
                             "relation_evidence": list(evidence)}))
                    else:
                        record["statement_binding_issue"] = "STATEMENT_RAW_BODY_MISMATCH"
                else:
                    record["statement_binding_issue"] = "STATEMENT_RAW_TYPE_UNVERIFIED"
            elif kind == "note":
                record = _record("note", locator, text=row.note,
                                 subtype=row.note_subtype, step=row.source_step)
            else:
                record = _record("instruction", locator, opcode=source_record["opcode"],
                                 operands=[v for v in (row.operand, *row.continuation_operands) if v], step=row.source_step)
                record["target_opcode"] = row.instruction
            record["source_locator"] = row.source_locator
            record["source_record_id"] = row.source_record_id
            record["source_digest"] = row.source_digest
            record["source_semantic_tags"] = list(dict.fromkeys(
                (*row.source_semantic_tags, *source_record.get("source_semantic_tags", ()))))
            record["sequence"] = row.sequence
            record["source_instruction"] = row.instruction
            record["continuation_rows"] = [list(r) for r in row.continuation_rows]
            record["inline_note"] = row.note if kind == "instruction" else ""
            tags, issue, findings = q_text_target.semantic_evidence(
                row, semantic_programs.get(pou["name"], {}).get(row.source_record_id))
            record["source_semantic_tags"] = list(dict.fromkeys((*record["source_semantic_tags"], *tags)))
            if issue:
                record["semantic_binding_issue"] = semantic_issue or issue
            record["source_findings"] = [list(f) for f in findings]
            records.append(record)
        programs.append({"name": pou["name"], "records": records})
    return {"programs": programs, "q_source_context": q_source_context,
            "comments": [{"device": c["device_id"], "text": c["value"], "scope": c["scope"],
                          "program_id": c["program_id"], "language": c["language_slot"],
                          "source_entry": c["provenance"]["source_entry"],
                          "locator": c["provenance"]["source_locator"], "value_state": c["value_state"]}
                         for c in value["comments"]],
            "metadata": {"producer": value["producer"], "source_cpu_observation": value["profile"]["cpu_ui_selection"],
                         "labels_preserved_as_metadata": value["labels"], "source_findings": value["findings"]}}


@dataclass(frozen=True)
class TextReference:
    spelling: str
    locator: str
    pou: str = ""
    status: str = "decoded"


@dataclass(frozen=True)
class TextSourceAnalysis:
    source_sha256: str
    pous: tuple[str, ...]
    program_references: tuple[TextReference, ...]
    comment_references: tuple[TextReference, ...]
    inspection_scope: tuple[str, ...] = ("source bytes", "POU registry", "program rows", "comment binding")
    uninspected_scope: tuple[str, ...] = ("target mapping", "GX import validation")
    complete_text: bool = True


def analyze_text_source(source: Path, *, direction: str,
                        cancel_check: Callable[[], bool] | None = None) -> TextSourceAnalysis:
    """Use the same verified source document as complete-text publication."""
    source = Path(source)
    suffix = {"Q_TO_FX5": ".gxw", "FX5_TO_Q": ".gx3"}.get(direction)
    if suffix is None or source.suffix.lower() != suffix:
        raise ValueError("invalid text source or direction")
    _check(cancel_check)
    core._reject_link_like_ancestors(source, "text source")
    with closing(core._open_input_snapshot(source)) as snapshot:
        document = (_q_document if direction == "Q_TO_FX5" else _fx5_document)(
            source, snapshot.body, cancel_check)
        program_refs = []
        for program in document["programs"]:
            _check(cancel_check)
            for record in program["records"]:
                program_refs.extend(TextReference(token, f'{record["locator"]}:operand:{i}', program["name"])
                                    for i, token in enumerate(record["operands"]))
        comment_refs = tuple(TextReference(c["device"], f'comment:{i}:{c["locator"]}')
                             for i, c in enumerate(document["comments"]))
        q_source_review._verify_snapshot_content(snapshot)
        _check(cancel_check)
        return TextSourceAnalysis(snapshot.sha256, tuple(p["name"] for p in document["programs"]),
                                  tuple(program_refs), comment_refs)


@dataclass(frozen=True)
class TextPlan:
    source_path: Path
    source_sha256: str
    direction: str
    outputs: Mapping[str, bytes]
    report: Mapping[str, Any]

    def __post_init__(self) -> None:
        frozen = dict(self.outputs)
        for leaf, payload in frozen.items():
            if (not isinstance(leaf, str) or Path(leaf).name != leaf or leaf in {"", ".", ".."}
                    or any(c in leaf for c in '/\\:\x00') or not isinstance(payload, bytes)):
                raise ValueError("invalid text artifact name or payload")
        object.__setattr__(self, "outputs", MappingProxyType(frozen))


def plan_text_conversion(source: Path, *, project_identifier: str, direction: str,
                         settings: MappingSettings | None = None,
                         word_settings: WordDeviceSettings | None = None,
                         relocation_settings: DeviceRelocationSettings | None = None,
                         target_cpu: str = "FX5U",
                         statement_policy: str = statements.IN_PLC,
                         unmapped_policy: UnmappedPolicy = UnmappedPolicy.NUMERIC_IDENTITY,
                         cancel_check: Callable[[], bool] | None = None) -> TextPlan:
    source = Path(source)
    statements.validate(statement_policy)
    profiles = {"Q_TO_FX5": (Profile.Q, Profile.FX5U, ".gxw"),
                "FX5_TO_Q": (Profile.FX5U, Profile.Q, ".gx3")}
    if direction not in profiles:
        raise ValueError("unsupported text conversion direction")
    src, target, suffix = profiles[direction]
    if direction == "Q_TO_FX5":
        fx5_target.validate_cpu(target_cpu)
    if source.suffix.lower() != suffix or not project_identifier or any(c in project_identifier for c in "\r\n\t\x00"):
        raise ValueError("invalid source or project identifier")
    if settings and (settings.source is not src or settings.target is not target):
        raise ValueError("X/Y settings direction mismatch")
    if word_settings and direction != "Q_TO_FX5":
        raise ValueError("word settings direction mismatch")
    if relocation_settings and (direction != "Q_TO_FX5" or word_settings is not None):
        raise ValueError("relocation requires Q_TO_FX5 and cannot combine with legacy word settings")
    mapper = RangeMapper(source=src, target=target, rules=settings.rules if settings else (),
                         unmapped=settings.unmapped if settings else unmapped_policy,
                         allow_target_aliases=True)
    _check(cancel_check)
    core._reject_link_like_ancestors(source, "text source")
    with closing(core._open_input_snapshot(source)) as snapshot:
        document = (_q_document if direction == "Q_TO_FX5" else _fx5_document)(source, snapshot.body, cancel_check)
        modules = ModuleMapper(target_cpu, settings.module_rules if settings else ()) if direction == "Q_TO_FX5" else None
        module_rows = modules.preflight(
            [token for program in document["programs"] for record in program["records"] for token in record["operands"]]
            + [c["device"] for c in document["comments"]]) if modules else []
        relocation = relocation_settings.preflight(document, snapshot.sha256) if relocation_settings else None
        references = []
        for program in document["programs"]:
            for record in program["records"]:
                references.extend(Reference(m[1], f'{record["locator"]}:operand:{i}')
                                  for i, token in enumerate(record["operands"]) if (m := _XY.fullmatch(token)))
        references.extend(Reference(m[1], f'comment:{i}:{c["locator"]}')
                          for i, c in enumerate(document["comments"]) if (m := _XY.fullmatch(c["device"])))
        if len({r.locator for r in references}) != len(references):
            raise ValueError("source reference locator collision")
        audit = mapper.preflight_audit(references)
        projections = {p.locator: p.target for p in audit.projections}
        alias_warnings = [{"code": "XY_TARGET_ALIAS", "severity": "warning",
                           "target": alias.target, "sources": list(alias.sources),
                           "locators": list(alias.locators), "blocking": False}
                          for alias in audit.target_aliases]
        native_r = any(re.match(r"R[0-9]", token, re.I)
                       for program in document["programs"] for record in program["records"] for token in record["operands"])
        native_r = native_r or any(re.match(r"R[0-9]", c["device"], re.I) for c in document["comments"])
        has_zr = any(_WORD.fullmatch(token) for program in document["programs"]
                     for record in program["records"] for token in record["operands"])
        has_zr = has_zr or any(_WORD.fullmatch(c["device"]) for c in document["comments"])
        if word_settings and word_settings.zr_to_r and native_r and has_zr:
            raise ValueError("ZR to R mapping would alias a native Q R bank")

        def mapped(token: str, locator: str) -> str:
            if modules:
                token = modules.project(token)
            if match := _XY.fullmatch(token):
                return projections[locator] + (match[2] or "")
            if word_settings and word_settings.zr_to_r and (match := _WORD.fullmatch(token)):
                return "R" + match[1] + match[2]
            if relocation_settings:
                return relocation_settings.project(token)
            return token

        outputs = {}
        if relocation_settings:
            outputs['device-relocation-settings.json'] = relocation_settings.to_json()
        inventory = []
        counts = Counter()
        target_counts = Counter()
        target_findings = []
        transformations = []
        for program in document["programs"]:
            _check(cancel_check)
            leaf = _leaf(program["name"])
            table = [[project_identifier], (["Module Type Information:", "FX5CPU FX5U"] if target is Profile.FX5U
                                           else ["PLC Information:", "QCPU (Q mode) Q06UDV"]),
                     core.GXW3_EXPORT_HEADER if target is Profile.FX5U else HEADER]
            ledger = []
            program_findings = []
            note_findings = (q_text_target.note_findings(program["records"], program["name"])
                             if direction == "FX5_TO_Q" else [])
            for record_index, record in enumerate(program["records"]):
                counts[record["kind"]] += 1
                projected = [mapped(token, f'{record["locator"]}:operand:{i}') for i, token in enumerate(record["operands"])]
                mapped_source_operands = list(projected)
                projection = (fx5_target.project_instruction(record["opcode"], projected)
                              if direction == "Q_TO_FX5" and record["kind"] == "instruction" else
                              {"target_kind": record["kind"], "target_opcode": record.get("target_opcode", record["opcode"])})
                if modules and any(modules.project(token) != token for token in record["operands"]):
                    projection["module_projection"] = "MODULE_NUMBER_MAPPING"
                if direction == "FX5_TO_Q":
                    projection, projected, findings = q_text_target.project(
                        record, projected, snapshot.sha256, program["name"],
                        initial_findings=tuple(f for f in note_findings if f[0] == str(record_index)))
                    program_findings.extend([*f[:7], (f[7] if core._candidate_program(f[7]) == program["name"] else
                                                     f'program={program["name"]};{f[7]}')] for f in findings)
                    target_findings.extend({"scope": "program", "pou": program["name"],
                                            "locator": record["locator"], "csv": leaf,
                                            "csv_record": len(table) + 1, "csv_instruction_record": len(table) + 1,
                                            "source_opcode": record["opcode"], "state": "BLOCKED",
                                            "code": f[5], "reason": f[4], "candidate": f[7]} for f in findings)
                if direction == "Q_TO_FX5" and record["kind"] == "instruction":
                    fallback = fx5_target.project_module_fallback(record["opcode"], projected, target_cpu)
                    if fallback:
                        projection.update(target_opcode=fallback["target_opcode"], projection=fallback["code"],
                                          diagnostic_fallback=fallback)
                        projected = fallback["target_operands"]
                if projection["target_kind"] == "statement":
                    projection.update(statements.project(record, statement_policy,
                        generated=record["kind"] == "instruction" and record["opcode"] == "NOPLF",
                        target_profile="Q06UDV" if direction == "FX5_TO_Q" else "FX5U",
                        input_sha256=snapshot.sha256))
                    if projection["statement_state"] == "HELD":
                        target_findings.append({"scope": "program", "pou": program["name"],
                            "locator": record["locator"], "csv": leaf, "csv_record": len(table) + 1,
                            "csv_instruction_record": len(table) + 1, "state": "BLOCKED",
                            "source_opcode": record["opcode"], "code": projection["statement_reason"],
                            "reason": "Statement body/type or target serialization requires independent evidence"})
                target_counts[projection["target_kind"]] += 1
                if (projection.get("projection") not in {None, "SOURCE_OPCODE"}
                        or projection.get("note_projection") or projection.get("module_projection")
                        or projection.get("statement_generated")
                        or projection.get("statement_reason") == "PERIPHERAL_TO_IN_PLC"):
                    transformations.append({"pou": program["name"], "locator": record["locator"],
                                            "source_opcode": record["opcode"], "source_operands": record["operands"],
                                            "target_operands": projected, **projection})
                if direction == "Q_TO_FX5":
                    for operand_index, token in enumerate(mapped_source_operands):
                        width = 7 if record["opcode"] in fx5_target.CLOCK_ALIASES else 1
                        if relocation_settings:
                            width = operand_width(record['opcode'], record['operands'], operand_index,
                                                  bool(re.fullmatch(r'(?:D|R|ZR)[0-9]+\.[0-9A-F](?:(?:ZZ|LZ|Z)[0-9]+)?', token, re.I))) or 1
                        finding = fx5_target.device_check(token, target_cpu, width=width)
                        if finding and finding["state"] != "CANDIDATE":
                            fallback = projection.get("diagnostic_fallback")
                            if fallback and finding["code"] == "TARGET_MODULE_NUMBER_UNSUPPORTED":
                                finding = {**finding, "state": "REVIEW", "diagnostic_fallback": fallback["code"],
                                           "target_opcode": fallback["target_opcode"], "target_operands": fallback["target_operands"],
                                           "publication": fallback["publication"], "runtime_action": fallback["runtime_action"],
                                           "reason": finding["reason"] + " " + fallback["reason"]}
                            target_findings.append({"scope": "program", "pou": program["name"],
                                                    "locator": f'{record["locator"]}:operand:{operand_index}',
                                                    "csv": leaf, "csv_record": len(table) + 1 + (
                                                        operand_index if operand_index < len(projected) else 0),
                                                    "csv_instruction_record": len(table) + 1,
                                                    "source_opcode": record["opcode"], **finding})
                row = [str(record["step"]) if record["step"] is not None else "", "", "", "", "", "", ""]
                if projection["target_kind"] == "instruction":
                    row[2:4] = [projection["target_opcode"], projected[0] if projected else ""]
                    row[6] = projection.get("target_note", "")
                    table.append(row)
                    table.extend(["", "", "", operand, "", "", ""] for operand in projected[1:])
                    counts["operand"] += len(record["operands"])
                elif projection["target_kind"] == "statement":
                    row[1] = projection.get("target_text", record["text"])
                    table.append(row)
                else:
                    row[6] = (projection.get("target_note") if direction == "FX5_TO_Q" else
                              ("*" if record["subtype"] == "s" else "") + record["text"])
                    table.append(row)
                ledger.append({**record, **projection, "target_operands": projected,
                               "mapped_source_operands": mapped_source_operands})
            statement_steps = None
            if direction == "Q_TO_FX5" and any(r["target_kind"] == "statement" for r in ledger):
                target_steps, statement_steps = statement_layout.widths(ledger, direction)
                primary = 3
                for record, target_step in zip(ledger, target_steps, strict=True):
                    table[primary][0] = str(target_step) if target_step is not None else ""
                    record["target_step"] = target_step
                    primary += 1 + (max(0, len(record["target_operands"]) - 1)
                                    if record["target_kind"] == "instruction" else 0)
            _store_csv(outputs, leaf, table)
            item = {"name": program["name"], "csv": leaf, "records": ledger}
            if statement_steps is not None:
                item["statement_steps"] = statement_steps
            if direction == "Q_TO_FX5":
                fallback_records = [r for r in ledger if r.get("diagnostic_fallback")]
                if fallback_records:
                    review_leaf = f'{program["name"]}.txt'
                    review_lines = [f'POU {program["name"]}: FX5 모듈 검토용 폴백',
                                    'REVIEW_ONLY / DO_NOT_DOWNLOAD_OR_RUN / SOURCE_FUNCTION_NOT_IMPLEMENTED']
                    for record in fallback_records:
                        review_lines.extend([f'원문 위치: {record["locator"]}; step={record["step"]}',
                                             '원문: ' + record["opcode"] + ' ' + ' '.join(record["operands"]),
                                             '검토용 대상: ' + record["target_opcode"] + ' ' + ' '.join(record["target_operands"]),
                                             record["diagnostic_fallback"]["reason"]])
                    outputs[review_leaf] = ('\n'.join(review_lines) + '\n').encode("utf-8")
                    item.update(publication_class="IMPORT_REVIEW_CSV", publication="REVIEW_ONLY",
                                runtime_action="DO_NOT_DOWNLOAD_OR_RUN", review_output=review_leaf,
                                diagnostic_fallback_count=len(fallback_records),
                                fx5_semantic_class="TARGET_MODULE_UNSUPPORTED_HELD")
                    positions = [i for i, row in enumerate(table, 1) if len(row) >= 4 and row[2:4] == ["OUT", "SM4095"]]
                    if positions:
                        item["dummy_output_coil_review"] = {"pou": program["name"], "csv": leaf,
                            "opcode": "OUT", "device": "SM4095", "emitted_out_count": len(positions),
                            "approved_fallback_count": sum(r["target_opcode"] == "OUT" and r["target_operands"] == ["SM4095"]
                                                           for r in fallback_records),
                            "csv_records": positions, "check_code": "DUPLICATE_COIL_REVIEW" if len(positions) >= 2 else None}
            if direction == "FX5_TO_Q":
                for contextual in document.get("q_source_context", {}).get("findings", ()):
                    if core._candidate_program(contextual[7]) not in {"", program["name"]}:
                        continue
                    if list(contextual) in program_findings:
                        continue
                    program_findings.append(list(contextual))
                    source_record = next((r for r in ledger if str(r.get("sequence", 0)) == contextual[0]
                                          and str(r["step"] or 0) == contextual[1]), None)
                    target_findings.append({"scope": "program", "pou": program["name"], "csv": leaf,
                                            "locator": source_record["locator"] if source_record else f'pou:{program["name"]}:context',
                                            "csv_record": None, "csv_instruction_record": None,
                                            "source_opcode": contextual[2], "state": "BLOCKED", "code": contextual[5],
                                            "reason": contextual[4], "candidate": contextual[7]})
                emitted = q_text_target.program_output(program, ledger, document.get("q_source_context", {}),
                                                       program_findings, snapshot.sha256, project_identifier)
                if emitted is None and not any(f.get("pou") == program["name"] and f["state"] == "BLOCKED"
                                               for f in target_findings):
                    target_findings.append({"scope": "program", "pou": program["name"], "csv": leaf,
                                            "locator": f'pou:{program["name"]}:emission', "csv_record": None,
                                            "csv_instruction_record": None, "state": "BLOCKED",
                                            "code": "Q_PROGRAM_EMISSION_HELD",
                                            "reason": "Independent complete-POU source binding or approved emission eligibility failed",
                                            "candidate": ""})
                if emitted is not None:
                    outputs[leaf] = emitted["payload"]
                    item.update(publication_class=emitted["publication_class"], target_layout=emitted["target_layout"],
                                serialized_records=emitted["serialized_records"])
                    if "statement_steps" in emitted:
                        item["statement_steps"] = emitted["statement_steps"]
                    target_steps_by_id = {}
                    for serialized in emitted["serialized_records"]:
                        target_steps_by_id.setdefault(serialized["source_record_id"], serialized["target_step"])
                    for record in ledger:
                        record["target_step"] = target_steps_by_id.get(record.get("source_record_id"))
                    positions = {}
                    csv_position = 4
                    for serialized in emitted["serialized_records"]:
                        positions.setdefault(serialized["source_record_id"], csv_position)
                        csv_position += serialized["csv_row_count"]
                    local_target_findings = defaultdict(list)
                    for finding in target_findings:
                        if finding.get("pou") == program["name"]:
                            local_target_findings[finding["locator"]].append(finding)
                    for record in ledger:
                        expansion = emitted.get("expansions", {}).get(record.get("source_record_id"))
                        if expansion:
                            first = expansion["target_expansion"][0]
                            record.update(**expansion, target_opcode=first["opcode"], target_operands=first["operands"])
                            transformations.append({"pou": program["name"], "locator": record["locator"],
                                                    "source_opcode": record["opcode"], "source_operands": record["operands"],
                                                    **expansion})
                        fallback = emitted["fallbacks"].get(record.get("source_record_id"))
                        if fallback:
                            record.update(target_opcode=fallback["target_opcode"], target_operands=fallback["target_operands"],
                                          target_note=fallback["target_note"], diagnostic_fallback=fallback,
                                          projection=fallback["code"])
                            transformations.append({"pou": program["name"], "locator": record["locator"],
                                                    "source_opcode": record["opcode"], "source_operands": record["operands"],
                                                    "projection": fallback["code"], **fallback})
                        for finding in local_target_findings[record["locator"]]:
                            finding["csv_record"] = positions[record["source_record_id"]]
                            finding["csv_instruction_record"] = finding["csv_record"]
                            if fallback and fallback["code"] == core.OFFICIAL_DUMMY_FALLBACK:
                                finding.update(state="REVIEW", diagnostic_fallback=fallback["code"],
                                               target_opcode=fallback["target_opcode"], target_operands=fallback["target_operands"],
                                               device=" ".join(fallback["target_operands"]), publication="REVIEW_ONLY",
                                               runtime_action="DO_NOT_DOWNLOAD_OR_RUN")
                            elif fallback:
                                # A typed source-mining or structural review proves a
                                # diagnostic shape only. Keep the original blocker.
                                finding.update(diagnostic_fallback=fallback["code"],
                                               target_opcode=fallback["target_opcode"], target_operands=fallback["target_operands"],
                                               publication=fallback["publication"], runtime_action=fallback["runtime_action"])
                    if emitted["review_text"] is not None:
                        review_leaf = f'{program["name"]}.txt'
                        outputs[review_leaf] = emitted["review_text"]
                        item.update(review_output=review_leaf, publication=emitted["publication"], runtime_action="DO_NOT_DOWNLOAD_OR_RUN",
                                    q_semantic_class=emitted["q_semantic_class"], review_status=emitted["review_status"],
                                    diagnostic_fallback_count=len(emitted["fallbacks"]),
                                    official_dummy_fallback_count=sum(f["code"] == core.OFFICIAL_DUMMY_FALLBACK
                                                                     for f in emitted["fallbacks"].values()))
            inventory.append(item)
        groups = defaultdict(list)
        overrides = (q_text_target.approved_comment_overrides(document["comments"], snapshot.sha256)
                     if direction == "FX5_TO_Q" else {})
        for i, comment in enumerate(document["comments"]):
            token = mapped(comment["device"], f'comment:{i}:{comment["locator"]}')
            if direction == "FX5_TO_Q":
                comment = q_text_target.comment(comment, token, override=overrides.get(comment["device"]))
                token = comment["target_device"]
            held = bool(relocation_settings and comment['device'] in relocation_settings.held_comments)
            groups[(comment["scope"], comment["program_id"], comment["language"],
                    comment.get("source_entry") if comment["scope"] == "program_device" else None)].append(
                        {**comment, "target_device": token,
                         **({'mapping_state': 'RETAIN_SOURCE_UNMAPPED'} if held else {})})
        comment_inventory = []
        comment_imports = []
        comment_locations = {}
        comment_header = ["Device Name", "English"] if target is Profile.FX5U else core.COMMENT_HEADER
        for i, (key, comments) in enumerate(sorted(groups.items(), key=lambda item: str(item[0]))):
            leaf = (f"COMMENT-source-{i:03d}.csv" if direction == "FX5_TO_Q" else
                    "COMMENT.csv" if len(groups) == 1 else f"COMMENT-{i:03d}.csv")
            _store_csv(outputs, leaf, [[project_identifier], comment_header,
                                   *[[c["device"] if direction == "FX5_TO_Q" else c["target_device"], c["text"]]
                                     for c in comments if c["text"] is not None]])
            comment_inventory.append({"scope": key[0], "program_id": key[1], "language": key[2],
                                      "source_entry": key[3], "csv": leaf, "records": comments})
            if direction == "FX5_TO_Q":
                csv_record = 3
                for comment in comments:
                    comment_locations[id(comment)] = (leaf, csv_record if comment["text"] is not None else None)
                    if comment["state"] != "CANDIDATE":
                        target_findings.append({"scope": "comment", "group_csv": leaf, "csv": leaf,
                                                "csv_record": csv_record if comment["text"] is not None else None,
                                                "locator": comment["locator"], "device": comment["device"],
                                                "state": comment["state"], "code": comment["code"],
                                                "reason": comment.get("reason", "")})
                    if (comment.get("projection_status") == "TRUNCATED" or comment.get("target_text") != comment["text"]
                            or comment["target_device"] != comment["device"]):
                        transformations.append({"scope": "comment", "locator": comment["locator"],
                                                "source_device": comment["device"], "target_device": comment["target_device"],
                                                "projection": comment.get("projection_status", comment["code"]),
                                                "source_text": comment["text"], "target_text": comment.get("target_text"),
                                                "source_bytes": comment.get("source_bytes"), "target_bytes": comment.get("target_bytes")})
                    if comment["text"] is not None:
                        csv_record += 1
                retained = [c for c in comments if c["state"] != "CANDIDATE"]
                retained_leaf = f"COMMENT-retained-{i:03d}.json"
                outputs[retained_leaf] = (json.dumps(retained, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                comment_imports.append({"full_csv": leaf, "full_csv_role": "SOURCE_REVIEW_ONLY",
                                        "candidates": [], "retained": retained_leaf, "retained_count": len(retained)})
            if direction == "Q_TO_FX5":
                partition = fx5_target.comment_partition(comments, target_cpu)
                candidates = []
                all_records = [c for c, a in zip(comments, partition["assessments"])
                               if a["state"] == "CANDIDATE" and c["text"] is not None]
                combined_leaf = f"COMMENT-import-{i:03d}-all.csv"
                _store_csv(outputs, combined_leaf, [[project_identifier], comment_header,
                                                *[[c["target_device"], c["text"]] for c in all_records]])
                for family, records in sorted(partition["candidates"].items()):
                    candidate_leaf = f"COMMENT-import-{i:03d}-{family}.csv"
                    _store_csv(outputs, candidate_leaf, [[project_identifier], comment_header,
                                                     *[[c["target_device"], c["text"]] for c in records]])
                    candidates.append({"csv": candidate_leaf, "family": family, "count": len(records)})
                retained_leaf = f"COMMENT-retained-{i:03d}.json"
                outputs[retained_leaf] = (json.dumps(partition["retained"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                csv_record = 3
                for comment, assessment in zip(comments, partition["assessments"], strict=True):
                    location = csv_record if comment["text"] is not None else None
                    if assessment["state"] != "CANDIDATE":
                        target_findings.append({"scope": "comment", "group_csv": leaf, "csv": leaf,
                                                "csv_record": location, **assessment})
                    if comment["text"] is not None:
                        csv_record += 1
                comment_imports.append({"full_csv": leaf, "full_csv_role": "SOURCE_REVIEW_ONLY",
                                        "candidates": candidates, "retained": retained_leaf,
                                        "combined_candidate": {"csv": combined_leaf, "count": len(all_records)},
                                        "retained_count": len(partition["retained"]),
                                        "family_counts": partition["family_counts"],
                                        "first_non_candidate": partition["first_non_candidate"]})
        if direction == "FX5_TO_Q":
            candidates = [c for comments in groups.values() for c in comments if c["state"] == "CANDIDATE"]
            by_target = defaultdict(list)
            for comment in candidates:
                by_target[comment["target_device"].casefold()].append(comment)
            collisions = {device for device, records in by_target.items() if len(records) > 1}
            for comment in candidates:
                if comment["target_device"].casefold() in collisions:
                    comment.update(state="BLOCKED", code="COMMENT_TARGET_ALIAS_COLLISION")
                    target_findings.append({"scope": "comment", "locator": comment["locator"],
                                            "state": "BLOCKED", "code": comment["code"],
                                            "device": comment["target_device"],
                                            "csv": comment_locations[id(comment)][0],
                                            "csv_record": comment_locations[id(comment)][1]})
            candidates = [c for c in candidates if c["state"] == "CANDIDATE"]
            _store_csv(outputs, "COMMENT.csv", [[project_identifier], comment_header,
                                               *[[c["target_device"], c["target_text"]] for c in candidates]])
            for group, inventory_group in zip(comment_imports, comment_inventory, strict=True):
                retained = [c for c in inventory_group["records"] if c["state"] != "CANDIDATE"]
                outputs[group["retained"]] = (json.dumps(retained, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                group["retained_count"] = len(retained)
                group["combined_candidate"] = {"csv": "COMMENT.csv", "count": len(candidates)}
        elif not groups:
            _store_csv(outputs, "COMMENT.csv", [[project_identifier], comment_header])
        dummy_output_coils = []
        output_double_coils = []
        retentive_timer_allocations = []
        if direction == "Q_TO_FX5":
            dummy_output_coils = [p["dummy_output_coil_review"] for p in inventory if p.get("dummy_output_coil_review")]
        if direction == "FX5_TO_Q":
            for program in inventory:
                emitted_rows = list(csv.reader(io.StringIO(outputs[program["csv"]].decode("utf-16")), delimiter="\t"))
                approved_count = sum(r.get("diagnostic_fallback", {}).get("code") in {
                                         core.OFFICIAL_DUMMY_FALLBACK, core.TYPED_MINING_FALLBACK,
                                         core.STRUCTURAL_RESULT_DUMMY_FALLBACK}
                                     and r.get("target_opcode") == "OUT" and r.get("target_operands") == ["SM1255"]
                                     for r in program["records"])
                coils = {}
                for position, row in enumerate(emitted_rows, 1):
                    if (len(row) >= 4 and row[2] == "OUT"
                            and re.fullmatch(r"(?:SM|SB|ST|[YMLBFVTC])[0-9A-F]+", row[3])):
                        coils.setdefault(row[3], []).append(position)
                for device, positions in coils.items():
                    if len(positions) < 2:
                        continue
                    if device == "SM1255" and approved_count >= 2:
                        continue  # The existing approved-dummy diagnostic owns this group.
                    detail = {"pou": program["name"], "csv": program["csv"], "opcode": "OUT",
                              "device": device, "emitted_out_count": len(positions),
                              "csv_records": positions, "check_code": "C9300",
                              "scope": "SAME_POU_EXACT_LITERAL_ONLY"}
                    output_double_coils.append(detail)
                    target_findings.append({"scope": "program", "pou": program["name"], "csv": program["csv"],
                                            "locator": f'pou:{program["name"]}:output-coils:{device}',
                                            "csv_record": positions[0], "csv_instruction_record": positions[0],
                                            "state": "INFO", "code": "Q_OUTPUT_DOUBLE_COIL_REVIEW", "device": device,
                                            "target_opcode": "OUT", "target_operands": [device],
                                            "reason": f'동일 POU에 OUT {device}가 {len(positions)}개 있습니다. '
                                                      'GX Works2 Check Program에서 C9300 중복 코일 오류가 발생할 수 있습니다. '
                                                      '중복 출력은 그대로 변환하며 변환·발행을 보류하지 않습니다. 정상 여부는 사용자가 판단합니다.'})
                st_operands = {row[3] for row in emitted_rows[3:] if len(row) >= 4
                               and re.match(r"@?ST[0-9]", row[3], re.I)}
                if st_operands:
                    literals = sorted({int(token[2:]) for token in st_operands if re.fullmatch(r"ST[0-9]+", token, re.I)})
                    indexed = sorted(token for token in st_operands
                                     if re.fullmatch(r"@?ST[0-9]+(?:ZZ|LZ|Z)[0-9]+", token, re.I))
                    unresolved = sorted(token for token in st_operands
                                        if not re.fullmatch(r"ST[0-9]+", token, re.I) and token not in indexed)
                    allocation = {"pou": program["name"], "csv": program["csv"], "family": "ST",
                                  "literal_addresses": literals,
                                  "literal_address_range": f"ST0..ST{literals[-1]}" if literals else None,
                                  "minimum_literal_points": literals[-1] + 1 if literals else None,
                                  "point_count_scope": "STATIC_LITERAL_LOWER_BOUND_ONLY",
                                  "indexed_operands": indexed, "unverified_operands": unresolved,
                                  "allocation": "NOT_CHECKED"}
                    program["retentive_timer_allocation"] = allocation
                    retentive_timer_allocations.append(allocation)
                if not approved_count:
                    continue
                positions = [i for i, row in enumerate(emitted_rows, 1)
                             if len(row) >= 4 and row[2:4] == ["OUT", "SM1255"]]
                detail = {"pou": program["name"], "csv": program["csv"], "opcode": "OUT", "device": "SM1255",
                          "approved_fallback_count": approved_count, "emitted_out_count": len(positions),
                          "csv_records": positions,
                          "check_code": "C9300" if approved_count >= 2 and len(positions) >= 2 else None}
                program["dummy_output_coil_review"] = detail
                dummy_output_coils.append(detail)
                if detail["check_code"]:
                    target_findings.append({"scope": "program", "pou": program["name"], "csv": program["csv"],
                                            "locator": f'pou:{program["name"]}:dummy-output-coils',
                                            "csv_record": positions[0], "csv_instruction_record": positions[0],
                                            "state": "INFO", "code": "Q_DUMMY_DOUBLE_COIL_REVIEW", "device": "SM1255",
                                            "target_opcode": "OUT", "target_operands": ["SM1255"],
                                            "reason": f'승인된 OUT SM1255 폴백 {approved_count}개(발행 CSV OUT SM1255 {len(positions)}개). '
                                                      'GX Works2 Check Program에서 C9300 중복 코일 오류가 발생할 수 있습니다. '
                                                      '중복 자체는 변환·발행을 보류하지 않으며 정상 여부는 사용자가 판단합니다. '
                                                      '미지원 원문 기능에 대한 폴백 검토 상태는 별도로 유지합니다.'})
        program_roles = {}
        for program in inventory:
            blocked = any(f["scope"] == "program" and f.get("pou") == program["name"]
                          and f["state"] == "BLOCKED" for f in target_findings)
            program_roles[program["csv"]] = (core.TYPED_MINING_REVIEW_STATUS
                                            if program.get("review_status") == core.TYPED_MINING_REVIEW_STATUS else
                                            "SOURCE_REVIEW_ONLY" if blocked or program.get("publication_class") == "SOURCE_REVIEW_ONLY" else "IMPORT_REVIEW_CSV"
                                            if program.get("publication_class") == "IMPORT_REVIEW_CSV" else "TEXT_CANDIDATE")
        blocked_findings = [f for f in target_findings if f["state"] == "BLOCKED"]
        review_findings = [f for f in target_findings if f.get("diagnostic_fallback") and f["state"] == "REVIEW"]
        mining_review_findings = [f for f in target_findings if f.get("diagnostic_fallback") == core.TYPED_MINING_FALLBACK]
        import_check = None
        if direction in {"Q_TO_FX5", "FX5_TO_Q"}:
            import_check = {"state": "BLOCKED" if blocked_findings else "REVIEW" if any(
                                f["state"] == "REVIEW" for f in target_findings) else "NOT_VERIFIED",
                            "known_blocker_occurrences": len(blocked_findings),
                            "program_blocker_occurrences": sum(f["scope"] == "program" for f in blocked_findings),
                            "blocked_programs": [p["name"] for p in inventory
                                                 if any(f["scope"] == "program" and f.get("pou") == p["name"]
                                                        for f in blocked_findings)],
                            "review_only_occurrences": len({(f["pou"], f["locator"]) for f in review_findings}),
                            "import_review_programs": [p["name"] for p in inventory
                                                       if program_roles[p["csv"]] == "IMPORT_REVIEW_CSV"],
                            "typed_mining_review_programs": [p["name"] for p in inventory
                                                              if program_roles[p["csv"]] == core.TYPED_MINING_REVIEW_STATUS],
                            "allocation": "NOT_CHECKED", "official_import": "NOT_RUN",
                            "diagnostic_file": "FX5-import-review.tsv" if direction == "Q_TO_FX5" else "Q-import-review.tsv",
                            "guide_file": "FX5-import-readme.txt" if direction == "Q_TO_FX5" else "Q-import-readme.txt"}
            if direction == "FX5_TO_Q":
                import_check["dummy_output_coils"] = dummy_output_coils
                import_check["output_double_coils"] = output_double_coils
                import_check["retentive_timer_allocations"] = retentive_timer_allocations
            elif dummy_output_coils:
                import_check["dummy_output_coils"] = dummy_output_coils
            # This is an ordinary UTF-8 diagnostic table, never a GX program CSV.
            diagnostic = io.StringIO(newline="")
            writer = csv.writer(diagnostic, delimiter="\t", lineterminator="\n")
            writer.writerow(["State", "Code", "Scope", "POU", "CSV", "CSV Record", "Instruction CSV Record", "Locator",
                             "Opcode", "Device", "Family", "Base", "End", "Manufacturer Ceiling", "Radix", "Reason", "Candidate",
                             "Diagnostic Fallback", "Target Opcode", "Target Operands", "Publication", "Runtime Action"])
            for finding in target_findings:
                writer.writerow([finding.get(k, "") for k in
                                 ("state", "code", "scope", "pou", "csv", "csv_record", "csv_instruction_record", "locator",
                                  "source_opcode", "device", "family", "base", "end", "ceiling", "radix", "reason", "candidate",
                                  "diagnostic_fallback", "target_opcode", "target_operands", "publication", "runtime_action")])
            outputs[import_check["diagnostic_file"]] = diagnostic.getvalue().encode("utf-8-sig")
            outputs[import_check["guide_file"]] = (
                f'FX5 가져오기 상태 / Import readiness: {import_check["state"]}\n'
                f'대상 CPU / Target CPU: {target_cpu}\n'
                f'알려진 차단 참조 / Known blocking references: {len(blocked_findings)}\n'
                '상한 초과 및 미지원 장치가 남은 프로그램 CSV는 원문 대조용입니다.\n'
                'Program CSVs with known blocking references are SOURCE_REVIEW_ONLY.\n'
                'FX5-import-review.tsv에서 파일, CSV 레코드 번호, 장치와 원인을 확인하세요.\n'
                'CSV Record is the one-based parsed record number, including headers; not a source step.\n'
                'Instruction CSV Record is the primary instruction row; GX may report it for a continuation operand error.\n'
                '제조사 상한 안의 장치도 실제 프로젝트 할당에 따라 Import가 실패할 수 있습니다.\n'
                'Manufacturer ceilings do not prove the actual project allocation or instruction support.\n'
                '전체 COMMENT.csv는 원문 대조용입니다. COMMENT-import-*-all.csv는 별도 검증할 후보입니다.\n'
                'Full COMMENT is for comparison; COMMENT-import-*-all.csv is an unverified candidate.\n'
                + ('명시 배정 설정을 적용했습니다. 실제 프로젝트 할당과 동작은 별도 검증이 필요합니다.\n'
                   if relocation_settings else
                   '주소를 임의 재배정하지 않았습니다. T/D/ZR 배정과 프로젝트 설정을 확정한 뒤 재변환하세요.\n')
                + 'No implicit device remapping. Actual allocation requires separate validation.\n'
                'COMPLETE_TEXT means source text preservation; GX import, Rebuild and field acceptance remain unverified.\n'
            ).encode("utf-8-sig")
            if direction == "Q_TO_FX5" and review_findings:
                outputs[import_check["guide_file"]] += (
                    'FX5 검토용 폴백은 SM4095/SD4095를 사용합니다. Q 검토용 SM1255/SD1255와 구분하세요.\n'
                    'IMPORT_REVIEW_CSV는 원래 모듈 기능을 수행하지 않습니다. REVIEW_ONLY / DO_NOT_DOWNLOAD_OR_RUN.\n'
                    '미지원 원문과 치환 사유는 POU TXT·원장·FX5-import-review.tsv에 보존합니다. '
                    'BLOCKED 원문이 남은 혼합 CSV는 SOURCE_REVIEW_ONLY이며 가져오기 후보가 아닙니다.\n'
                    'indexed 전체 명령 대체는 실제 인덱스/장치 계산을 수행하지 않습니다. '
                    '모듈 구성과 버퍼 기능을 구현한 뒤 더미를 대체하세요.\n'
                    '여러 OUT SM4095는 대상 GX의 중복 코일 검토가 필요합니다. '
                    'Q 검토용 OUT SM1255에서는 C9300 중복 코일 오류가 관측되었습니다.\n'
                ).encode("utf-8")
                for detail in dummy_output_coils:
                    if detail["check_code"]:
                        outputs[import_check["guide_file"]] += (
                            f'POU {detail["pou"]} ({detail["csv"]}): OUT SM4095 '
                            f'{detail["emitted_out_count"]}개. 대상 GX에서 중복 코일 검토가 필요합니다.\n'
                        ).encode("utf-8")
            if direction == "FX5_TO_Q":
                outputs[import_check["guide_file"]] = (
                    f'Q 가져오기 상태: {import_check["state"]}\n'
                    '대상: Q06UDV. Q-import-review.tsv에서 미지원 원인을 확인하세요.\n'
                    '제조사 장치 상한과 실제 프로젝트의 장치 할당은 다릅니다. '
                    'ST를 포함한 모든 사용 장치가 대상 CPU와 PLC Parameter의 Device 설정에 '
                    '충분히 할당되어 있는지 가져오기 전에 확인하세요. 미할당 주소는 범위 초과로 누락될 수 있습니다.\n'
                    '한글 Statement/Note 또는 코멘트를 사용하는 경우, 가져오기 전에 GX Works2의 '
                    'Tool > Language Selection > Project Language를 Korean으로 설정하세요. '
                    'Project Language는 화면 UI 언어와 별개입니다. '
                    'English 프로젝트에서는 한글이 ?로 손실될 수 있으므로 설정 후 가져오고, '
                    '저장·재열기·공식 Export에서 원문이 유지되는지 확인하세요.\n'
                    'SOURCE_REVIEW_ONLY 프로그램은 미지원 원문을 포함하므로 가져오기 후보가 아닙니다.\n'
                    'TYPED_MINING_REVIEW_ONLY는 타입 근거에 따른 마이닝 검토 자료입니다. '
                    '원본 의미 보류가 유지되며 가져오기 후보가 아닙니다. MINING_REVIEW_ONLY / DO_NOT_DOWNLOAD_OR_RUN.\n'
                    'IMPORT_REVIEW_CSV는 기존 승인 게이트를 통과한 SM1255/SD1255 검토용 더미를 포함합니다.\n'
                    '원래 기능은 구현되지 않았습니다. REVIEW_ONLY / DO_NOT_DOWNLOAD_OR_RUN.\n'
                    '미변환 원문과 치환 사유는 각 POU의 TXT, 원장 및 Q-import-review.tsv에 유지합니다.\n'
                    'COMMENT.csv는 Q 투영 후보입니다. COMMENT-source-*.csv와 원장은 원문 검토용입니다.\n'
                    '코멘트는 CP949 32바이트로 투영하며 변환과 잘림은 report.json에 기록합니다.\n'
                    '공식 가져오기, Rebuild, 저장, 재열기와 현장 동작은 확인하지 않았습니다.\n'
                ).encode("utf-8-sig")
                for detail in output_double_coils:
                    outputs[import_check["guide_file"]] += (
                        f'POU {detail["pou"]} ({detail["csv"]}): OUT {detail["device"]} '
                        f'{detail["emitted_out_count"]}개, CSV 행 {detail["csv_records"]}. '
                        'GX Works2 C9300 참고 정보입니다. 중복 출력은 그대로 변환하며 정상 여부는 사용자가 판단합니다.\n'
                    ).encode("utf-8")
                for allocation in retentive_timer_allocations:
                    requirement = (f'{allocation["literal_address_range"]}를 포함하는 최소 '
                                   f'{allocation["minimum_literal_points"]}점(정적 주소 기준 하한)' if allocation["literal_addresses"] else
                                   '정적 주소만으로 필요한 점수를 계산할 수 없음')
                    outputs[import_check["guide_file"]] += (
                        f'POU {allocation["pou"]} ({allocation["csv"]}): 유지형 타이머 ST 할당 확인 필요: '
                        f'{requirement}. 대상 CPU와 PLC Parameter > Device에서 할당을 확인하고, '
                        'PLC System에서 타이머 시간 단위를 확인하세요.\n'
                    ).encode("utf-8")
                    if allocation["indexed_operands"] or allocation["unverified_operands"]:
                        outputs[import_check["guide_file"]] += (
                            'ST 인덱스/미확인 참조: '
                            + ', '.join([*allocation["indexed_operands"], *allocation["unverified_operands"]])
                            + '. 실제 도달 주소와 필요한 전체 점수는 미검증이며 위 하한으로 충분하다고 판단할 수 없습니다.\n'
                        ).encode("utf-8")
                for detail in dummy_output_coils:
                    if detail["check_code"]:
                        outputs[import_check["guide_file"]] += (
                            f'POU {detail["pou"]} ({detail["csv"]}): 승인된 OUT SM1255 폴백 '
                            f'{detail["approved_fallback_count"]}개, 발행 CSV OUT SM1255 {detail["emitted_out_count"]}개. '
                            'GX Works2 Check Program에서 C9300 중복 코일 오류가 발생할 수 있습니다. '
                            '중복 자체는 변환·발행을 보류하지 않으며 정상 여부는 사용자가 판단합니다. '
                            '원문 폴백 명령의 기능을 대상 PLC에 맞게 구현한 뒤 더미를 대체해야 합니다. '
                            'REVIEW_ONLY / DO_NOT_DOWNLOAD_OR_RUN.\n'
                        ).encode("utf-8")
        warnings = list(alias_warnings)
        if output_double_coils:
            warnings.append({"code": "Q_OUTPUT_DOUBLE_COIL_REVIEW", "severity": "info", "blocking": False,
                             "count": len(output_double_coils),
                             "occurrences": sum(d["emitted_out_count"] for d in output_double_coils),
                             "diagnostic_file": import_check["diagnostic_file"]})
        if mining_review_findings:
            warnings.append({"code": "Q_TYPED_MINING_REVIEW", "severity": "warning", "blocking": False,
                             "count": len({(f["pou"], f["locator"]) for f in mining_review_findings}),
                             "diagnostic_file": import_check["diagnostic_file"],
                             "publication": core.TYPED_MINING_REVIEW_PUBLICATION,
                             "runtime_action": core.TYPED_MINING_REVIEW_RUNTIME_ACTION})
        if review_findings:
            warnings.append({"code": "FX5_IMPORT_REVIEW" if direction == "Q_TO_FX5" else "Q_IMPORT_REVIEW", "severity": "warning", "blocking": False,
                             "count": import_check["review_only_occurrences"], "diagnostic_file": import_check["diagnostic_file"],
                             "publication": "REVIEW_ONLY", "runtime_action": "DO_NOT_DOWNLOAD_OR_RUN"})
        if blocked_findings:
            warnings.append({"code": "FX5_IMPORT_BLOCKED" if direction == "Q_TO_FX5" else "Q_IMPORT_BLOCKED", "severity": "warning", "blocking": False,
                             "count": len(blocked_findings), "diagnostic_file": import_check["diagnostic_file"],
                             "scope": "TARGET_IMPORT_ONLY_TEXT_PRESERVED"})
        report = {"schema": SCHEMA, "status": "COMPLETE_TEXT", "direction": direction,
                  "source_sha256": snapshot.sha256, "project_identifier": project_identifier,
                  "pous": len(inventory), "counts": {**dict(counts), "comment": len(document["comments"])},
                  "programs": [{"name": p["name"], "text_output": p["csv"],
                                "text_output_role": program_roles[p["csv"]],
                                **{k: p[k] for k in ("publication_class", "publication", "runtime_action", "q_semantic_class", "review_status",
                                                     "review_output", "diagnostic_fallback_count", "official_dummy_fallback_count", "target_layout",
                                                     "dummy_output_coil_review", "retentive_timer_allocation", "fx5_semantic_class", "statement_steps") if k in p}}
                               for p in inventory],
                  "comment_outputs": [{k: v for k, v in c.items() if k != "records"} for c in comment_inventory],
                  "xy_policy": settings.unmapped.value if settings else unmapped_policy.value,
                  "xy_settings_sha256": settings.digest if settings else None,
                  "module_mapping": {"policy": "EXPLICIT_THEN_FX5_MINIMUM", "rows": module_rows,
                                     "buffer_semantics": "NOT_CHECKED"} if modules else None,
                  "xy_target_alias_policy": "WARN_AND_PRESERVE_ALL_SOURCE_RECORDS",
                  "xy_target_alias_count": len(alias_warnings), "warnings": warnings,
                  "target_counts": dict(target_counts), "transformations": transformations,
                  "statement_policy": statement_policy,
                  "statement_policy_sha256": statements.StatementSettings(statement_policy).digest,
                  "statement_summary": statements.summary(
                      [r for p in inventory for r in p["records"]], statement_policy),
                  "statement_default_notice": statements.MIGRATION_NOTICE,
                  "target_review": {"cpu": target_cpu if direction == "Q_TO_FX5" else "Q06UDV",
                                    "state": ("BLOCKED" if any(f["state"] == "BLOCKED" for f in target_findings)
                                              else "REVIEW"),
                                    "authority": ({"manual": fx5_target.MANUAL, "printed_page": 85} if direction == "Q_TO_FX5" else
                                                  {"contract": "EXISTING_P006_Q_TARGET_VALIDATION"}),
                                    "findings": target_findings, "allocation": "NOT_CHECKED",
                                    "instruction_access_spans": "CLOCK_7_WORDS_AND_K_GROUPS_ONLY",
                                    "dynamic_index_access": "NOT_CHECKED",
                                    "instruction_and_module_compatibility": "NOT_CHECKED"},
                  "comment_import": {"policy": "FULL_SOURCE_PRESERVED_IMPORT_CANDIDATES_ISOLATED_BY_FAMILY",
                                     "candidate_scope": "MANUFACTURER_CEILING_ONLY_ALLOCATION_AND_GX_IMPORT_NOT_PROVEN",
                                     "groups": comment_imports},
                  "word_settings_sha256": word_settings.digest if word_settings else None,
                  "device_capacity_limits": "NOT_APPLIED", "hardware_compatibility": "NOT_CHECKED",
                  "csv_metadata_profile": "FX5U" if target is Profile.FX5U else "Q06UDV",
                  "csv_metadata_scope": "FORMAT_TEMPLATE_ONLY_NOT_ACTUAL_CPU_OR_CAPACITY",
                  "comment_csv_value_column": comment_header[1],
                  "opcode_policy": "SOURCE_SPELLING_IN_LEDGER_OFFICIAL_EQUIVALENT_ALIASES_IN_CSV", "text_policy": (
                      "SOURCE_PRESERVED_Q_COMMENT_CP949_32_BYTES" if direction == "FX5_TO_Q" else "NO_TRUNCATION_OR_REPLACEMENT"),
                  "step_policy": "SOURCE_PROVENANCE_IMMUTABLE_TARGET_CLOSED_WIDTHS_OR_UNVERIFIED_GX_RECOMPUTE",
                  "official_gx_validation": "NOT_RUN_FOR_THIS_OUTPUT", "field_validation": "NOT_RUN",
                  "outputs_sha256": {leaf: hashlib.sha256(payload).hexdigest() for leaf, payload in outputs.items()}}
        if import_check is not None:
            report["import_readiness"] = import_check
        if module_rows:
            outputs['FX5-import-readme.txt'] += (
                '\n모듈 주소 매핑: 원본 → 기본안 → 적용안\n'
                + '\n'.join(f'{r["source"]} → {r["initial"]} → {r["target"]} ({r["reason"]})' for r in module_rows)
                + '\nG 번호, 비트와 인덱스는 보존합니다. 모듈 모델과 버퍼 기능 호환성은 미검증입니다.\n'
            ).encode('utf-8')
            report['outputs_sha256']['FX5-import-readme.txt'] = hashlib.sha256(outputs['FX5-import-readme.txt']).hexdigest()
        if relocation is not None:
            report['device_relocation'] = relocation
            report['target_review']['instruction_access_spans'] = 'KNOWN_STATIC_SPANS_WITH_RELOCATION_PREFLIGHT'
            report['target_review']['dynamic_index_access'] = 'REVIEW_REQUIRED'
            outputs['FX5-import-readme.txt'] += (
                '\n명시 배정 설정을 오프라인 시험용으로 적용했습니다. D는 D로 유지합니다.\n'
                'Explicit relocation applied for offline testing; D remains D.\n'
                '미배정 코멘트는 원장과 retained JSON에 보존하며 Import 후보에서 제외합니다.\n'
                'Held comments are preserved and excluded from import candidates.\n'
                'Dynamic accesses, retention, actual allocation and native lifecycle remain unverified.\n'
            ).encode('utf-8')
            report['outputs_sha256']['FX5-import-readme.txt'] = hashlib.sha256(outputs['FX5-import-readme.txt']).hexdigest()
        outputs["source-target-ledger.json"] = (json.dumps({"programs": inventory, "comments": comment_inventory,
                                                           "source_metadata": document["metadata"]}, ensure_ascii=False) + "\n").encode("utf-8")
        report["outputs_sha256"]["source-target-ledger.json"] = hashlib.sha256(outputs["source-target-ledger.json"]).hexdigest()
        outputs["report.json"] = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        q_source_review._verify_snapshot_content(snapshot)
        return TextPlan(source, snapshot.sha256, direction, MappingProxyType(outputs),
                        q_source_review._freeze(report))


def publish_text_conversion(plan: TextPlan, output_dir: Path,
                            cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    output_dir = Path(output_dir)
    try:
        report = json.loads(plan.outputs["report.json"].decode("utf-8"))
        hashes = report["outputs_sha256"]
        if (report["schema"] != SCHEMA or report["status"] != "COMPLETE_TEXT"
                or report["source_sha256"] != plan.source_sha256 or report["direction"] != plan.direction
                or set(hashes) != set(plan.outputs) - {"report.json"}
                or any(hashlib.sha256(plan.outputs[leaf]).hexdigest() != digest for leaf, digest in hashes.items())):
            raise ValueError("text plan manifest differs from artifact bytes")
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("text plan manifest is invalid") from error
    core._reject_link_like_ancestors(plan.source_path, "text source")
    core._reject_link_like_ancestors(output_dir.parent, "text output parent")
    if output_dir.exists() or output_dir.is_symlink() or not output_dir.parent.is_dir():
        raise ValueError("text output directory must be new")
    _check(cancel_check)
    with closing(core._open_input_snapshot(plan.source_path)) as snapshot:
        if snapshot.sha256 != plan.source_sha256:
            raise ValueError("source changed since text plan")
        cancelled = False
        scratch_path = None
        published = False
        def observed_cancel():
            nonlocal cancelled
            cancelled = bool(cancel_check and cancel_check()) or cancelled
            return cancelled
        try:
            with tempfile.TemporaryDirectory(prefix=".p006-text-", dir=output_dir.parent) as scratch:
                scratch_path = Path(scratch)
                staging = scratch_path / "result"
                staging.mkdir()
                core._write_atomically(tuple((staging / leaf, payload) for leaf, payload in plan.outputs.items()),
                                       snapshot, observed_cancel)
                q_source_review._verify_snapshot_content(snapshot)
                _check(observed_cancel)
                if output_dir.exists() or output_dir.is_symlink():
                    raise ValueError("text output directory already exists")
                # Windows rename atomically refuses even an empty existing directory.
                if os.name != "nt":
                    raise ValueError("atomic text publication requires the Windows product runtime")
                os.rename(staging, output_dir)
                published = True
        except Exception as error:
            if scratch_path is not None and scratch_path.exists():
                retained = output_dir if published else scratch_path / "result"
                raise TextPublicationCleanupError(retained, scratch_path, published,
                                                  tuple(plan.outputs)) from error
            if cancelled or isinstance(error, core.ConversionCancelled):
                raise core.ConversionCancelled("text conversion cancelled before publication") from error
            if isinstance(error, core.PartialPublishError):
                raise RuntimeError("text staging failed; no output set was published") from error
            raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Complete POU and COMMENT text conversion without CPU/device limits")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project-identifier", required=True)
    parser.add_argument("--direction", choices=("Q_TO_FX5", "FX5_TO_Q"), required=True)
    parser.add_argument("--xy-settings", type=Path)
    parser.add_argument("--word-settings", type=Path)
    parser.add_argument("--device-relocation", type=Path)
    parser.add_argument("--target-cpu", choices=fx5_target.CPUS, default="FX5U")
    parser.add_argument("--statement-policy", choices=statements.POLICIES,
                        help="Line Statement target type; default in_plc, preserve_source retains decoded types")
    parser.add_argument("--statement-settings", type=Path, help="Explicit Statement policy settings JSON")
    parser.add_argument("--unmapped-policy", choices=[p.value for p in UnmappedPolicy],
                        default=UnmappedPolicy.NUMERIC_IDENTITY.value)
    args = parser.parse_args(argv)
    src, target = (Profile.Q, Profile.FX5U) if args.direction == "Q_TO_FX5" else (Profile.FX5U, Profile.Q)
    policy_snapshot = None
    try:
        policy = statements.IN_PLC
        if args.statement_settings:
            core._reject_link_like_ancestors(args.statement_settings, "Statement settings")
            policy_snapshot = core._open_input_snapshot(args.statement_settings)
            policy = statements.StatementSettings.from_json(policy_snapshot.body).policy
            policy_snapshot.verify()
        if args.statement_policy is not None:
            if args.statement_settings and args.statement_policy != policy:
                raise ValueError("Statement policy differs from explicit settings file")
            policy = args.statement_policy
        settings = MappingSettings.from_json(args.xy_settings.read_bytes(), expected_source=src, expected_target=target) if args.xy_settings else None
        words = WordDeviceSettings.from_json(args.word_settings.read_bytes(), expected_source=src, expected_target=target) if args.word_settings else None
        relocation = DeviceRelocationSettings.from_json(args.device_relocation.read_bytes()) if args.device_relocation else None
        plan = plan_text_conversion(args.input, project_identifier=args.project_identifier, direction=args.direction,
                                    settings=settings, word_settings=words, relocation_settings=relocation,
                                    target_cpu=args.target_cpu,
                                    statement_policy=policy,
                                    unmapped_policy=UnmappedPolicy(args.unmapped_policy))
        if policy_snapshot is not None:
            policy_snapshot.verify()
        report = publish_text_conversion(plan, args.output_dir)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "HOLD", "reason": str(error)}, ensure_ascii=True))
        return 1
    finally:
        if policy_snapshot is not None:
            policy_snapshot.close()
    print(json.dumps({"status": report["status"], "pous": report["pous"], "counts": report["counts"]}))
    return 0
