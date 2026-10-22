"""Publish a source-address-preserving Q GXW review set, never an FX5 candidate.

Input: bounded regular GXW snapshot. Output: UTF-16LE tab-separated review
tables and a UTF-8 JSON manifest in a new directory. The pinned Q reader and
reference IR are dependencies; no address or instruction mapping is applied.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Callable

from p006_converter.core import (
    ConversionCancelled, PartialPublishError, _open_input_snapshot,
    _reject_link_like_ancestors, _write_atomically,
)
from p006_converter import q_to_fx5 as q
from p006_converter.xy_mapping import Profile, fixed_bounds


SCHEMA = "plcman.p006.q-source-review.v1"
_SOURCE_BLOCKERS = {
    "Q_SOURCE_INVALID", "Q_READER_UNKNOWN", "Q_IR_SCHEMA_MISMATCH",
    "Q_IR_INCOMPLETE_OR_CHANGED", "Q_IR_ROW_MISMATCH", "Q_IR_CONTENT_MISMATCH",
    "Q_COMMENT_BINDING_UNVERIFIED", "Q_COMMENT_BINDING_MISMATCH",
    "Q_MODULE_COMMENTS_UNRESOLVED", "OPERAND_UNCLASSIFIED",
}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise q.ReverseConversionError("Q_CONVERSION_CANCELLED")


def _verify_snapshot_content(snapshot: Any) -> None:
    """Recheck the retained source handle immediately before publication."""
    snapshot.verify()
    os.lseek(snapshot.descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        block = os.read(snapshot.descriptor, 1024 * 1024)
        if not block:
            break
        total += len(block)
        if total > q.MAX_INPUT_BYTES:
            raise q.ReverseConversionError("Q_SOURCE_CHANGED_BEFORE_PUBLICATION")
        digest.update(block)
    if digest.hexdigest() != snapshot.sha256:
        raise q.ReverseConversionError("Q_SOURCE_CHANGED_BEFORE_PUBLICATION")
    snapshot.verify()


def _source_tables(copy: Path, ir: dict[str, Any], reader: Any,
                   split_operands: Callable[[str], list[str]],
                   project_identifier: str, cancel_check: Callable[[], bool] | None,
                   cpu_observation: str | None) -> tuple[dict[str, bytes], dict[str, Any]]:
    streams = reader.load_all_substreams(str(copy))
    pous, comments, registry = reader.collect_pous(streams)
    if registry is None or not pous or set(registry[2]) != set(pous):
        raise q.ReverseConversionError("Q_POU_REGISTRY_UNVERIFIED")
    if comments:
        pairs, warning = reader.device_comment_pairs(streams)
        if warning is not None or len(pairs) != len({device for device, _ in pairs}):
            raise q.ReverseConversionError("Q_COMMENT_BINDING_UNVERIFIED")
        if {device: text for device, text in pairs if text} != comments:
            raise q.ReverseConversionError("Q_COMMENT_BINDING_MISMATCH")
        comments = dict(pairs)
    elif streams:
        pairs, warning = reader.device_comment_pairs(streams)
        if warning is None:
            comments = dict(pairs)
        elif warning != "NoCommentStream":
            raise q.ReverseConversionError("Q_COMMENT_BINDING_UNVERIFIED")
    names = list(registry[2])
    if len(set(names)) != len(names):
        raise q.ReverseConversionError("Q_POU_REGISTRY_UNVERIFIED")
    leaves = [q._safe_leaf(name) + ".q-review.csv" for name in names]
    if len({leaf.casefold() for leaf in [*leaves, "COMMENT.q-review.csv", "report.json"]}) != len(leaves) + 2:
        raise q.ReverseConversionError("OUTPUT_NAME_COLLISION")
    if not hasattr(reader, "project_info"):
        raise q.ReverseConversionError("Q_SOURCE_HEADER_UNVERIFIED")
    observed_name, observed_cpu = reader.project_info(str(copy))
    if (not isinstance(observed_name, str) or not observed_name
            or not isinstance(observed_cpu, str) or not observed_cpu):
        raise q.ReverseConversionError("Q_SOURCE_HEADER_UNVERIFIED")
    if cpu_observation is not None and observed_cpu != cpu_observation:
        raise q.ReverseConversionError("Q_SOURCE_HEADER_UNVERIFIED")
    occurrences = ir["occurrences"]
    occurrence_index = 0
    outputs: dict[str, bytes] = {}
    text_counts: Counter[str] = Counter()
    for name, leaf in zip(names, leaves):
        _check_cancel(cancel_check)
        store, body, _score = pous[name]
        source_digest = hashlib.sha256(reader.program_section(body)).hexdigest()
        framed_text = getattr(reader, "POU_TEXT_ENCODING_API", 0) == 1
        rows, unknown_instructions, unknown_devices = q._reader_pou_rows(reader, body, comments)
        if unknown_instructions or unknown_devices:
            raise q.ReverseConversionError("Q_READER_UNKNOWN", name)
        typed_notes = (reader.typed_note_records(body, comments, text_encoding="auto", framed_types=True)
                       if framed_text else reader.typed_note_records(body, comments)
                       ) if hasattr(reader, "typed_note_records") else None
        note_count = sum(row[0] == "__NOTE__" for row in rows)
        if note_count and (typed_notes is None or len(typed_notes) != note_count):
            raise q.ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", name)
        notes = iter(typed_notes or ())
        table = [[observed_name], ["PLC Information:", observed_cpu or "UNVERIFIED"],
                 ["REVIEW_ONLY", "Q_SOURCE_ADDRESSES_UNCHANGED", "NOT_GX_IMPORT_CSV"],
                 ["Source Locator", "Row Kind", "Instruction", "I/O(Device)",
                  "Line Statement", "Note", "Note Raw Marker", "Note Subtype"]]
        for row_index, (opcode, raw_operands, _comment) in enumerate(rows):
            _check_cancel(cancel_check)
            text_locator = f"_hdb/{store}:row:{row_index}"
            if opcode == "__STMT__":
                if "\ufffd" in raw_operands or (not framed_text and raw_operands.encode("cp1252").decode("cp1252") != raw_operands):
                    raise q.ReverseConversionError("Q_TEXT_LOSSY_DECODE", name)
                table.append([text_locator, "STATEMENT", "", "", raw_operands, "", "", ""])
                text_counts["statement"] += 1
                continue
            if opcode == "__NOTE__":
                note = next(notes)
                if ("\ufffd" in raw_operands or
                        (not framed_text and raw_operands.encode("cp1252").decode("cp1252") != raw_operands)):
                    raise q.ReverseConversionError("Q_TEXT_LOSSY_DECODE", name)
                preceding = next(((item[0], split_operands(item[1])) for item in reversed(rows[:row_index])
                                  if item[0] not in {"__STMT__", "__NOTE__"}), None)
                attachment = note.get("attachment")
                if (note["text"] != raw_operands or preceding is None
                        or not isinstance(attachment, dict)
                        or attachment.get("instruction") != preceding[0]
                        or attachment.get("operands") != preceding[1]
                        or re.fullmatch(r"0x[0-9a-f]{2}", str(note.get("raw_type"))) is None
                        or note.get("subtype") not in {None, "s", "i"}):
                    raise q.ReverseConversionError("Q_NOTE_TYPE_UNVERIFIED", name)
                table.append([text_locator, "NOTE", "", "", "", raw_operands,
                              note["raw_type"], note["subtype"] or "UNVERIFIED"])
                text_counts["note"] += 1
                continue
            if occurrence_index >= len(occurrences):
                raise q.ReverseConversionError("Q_IR_ROW_MISMATCH")
            occurrence = occurrences[occurrence_index]
            occurrence_index += 1
            provenance = occurrence["provenance"]
            operands = occurrence["operands"]
            if (occurrence["pou"]["name"] != name or str(provenance["source_store"]) != str(store)
                    or provenance["source_digest"] != source_digest
                    or provenance["source_locator"] != text_locator
                    or occurrence["record"]["sequence"] != row_index
                    or occurrence["record"]["opcode"] != opcode
                    or [item["raw_token"] for item in operands] != split_operands(raw_operands)):
                raise q.ReverseConversionError("Q_IR_CONTENT_MISMATCH")
            if any(item.get("status") != "decoded" for item in operands):
                raise q.ReverseConversionError("OPERAND_UNCLASSIFIED")
            locator = provenance["source_locator"]
            table.append([locator, "INSTRUCTION", opcode,
                          operands[0]["raw_token"] if operands else "", "", "", "", ""])
            for position, operand in enumerate(operands[1:], 1):
                table.append([f"{locator}:operand:{position}", "CONTINUATION", "",
                              operand["raw_token"], "", "", "", ""])
        outputs[leaf] = q._csv(table)
    if occurrence_index != len(occurrences):
        raise q.ReverseConversionError("Q_IR_ROW_MISMATCH")
    comment_rows = [[observed_name], ["REVIEW_ONLY", "Q_SOURCE_ADDRESSES_UNCHANGED"],
                    ["Device Name", "Comment"]]
    for device, value in sorted(comments.items()):
        if (not isinstance(value, str) or "\ufffd" in value
                or any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in value)):
            raise q.ReverseConversionError("Q_COMMENT_TEXT_UNVERIFIED", device)
        comment_rows.append([device, value])
    outputs["COMMENT.q-review.csv"] = q._csv(comment_rows)
    report = {
        "schema": SCHEMA, "status": "REVIEW", "target_candidate": False,
        "direction": "Q_SOURCE_REVIEW", "source_address_profile": "Q_XY_HEX",
        "source_cpu_identity": "UNVERIFIED", "source_cpu_observation": cpu_observation,
        "fixed_xy_source_bounds": {"profile": Profile.Q.value,
                                    "x_max": fixed_bounds(Profile.Q).x,
                                    "y_max": fixed_bounds(Profile.Q).y},
        "source_project_observation": observed_name, "project_identifier": project_identifier,
        "source_project_observation_basis": "READER_METADATA_OR_FILENAME_FALLBACK",
        "reader_commit": q.READER_COMMIT, "reader_wheel_sha256": q.READER_SHA256,
        "pous": names, "occurrences": occurrence_index, "comments": len(comments),
        "statements": text_counts["statement"], "notes": text_counts["note"],
        "note_subtype_policy": "RAW_MARKER_AND_OBSERVED_SUBTYPE_UNVERIFIED_WHEN_UNKNOWN",
        "official_validation": "NOT_RUN", "official_pou_membership": "NOT_CHECKED",
        "uninspected_fields": ["official_step", "p_i_statement", "note_semantic_subtype"],
        "preserved_scope": ["decoded_opcode", "decoded_raw_operand", "decoded_statement",
                            "decoded_note_text", "note_raw_marker", "bound_comment"],
        "fx5_mapping": "NOT_APPLIED",
        "outputs_sha256": {leaf: hashlib.sha256(payload).hexdigest()
                           for leaf, payload in outputs.items()},
    }
    return outputs, report


def plan_q_source_review(source: Path, *, project_identifier: str,
                         cancel_check: Callable[[], bool] | None = None) -> q.QPlan:
    """Freeze verified Q source review artifacts without publishing them."""
    source = Path(source)
    analysis = q.analyze_q_source(source, cancel_check=cancel_check)
    if not project_identifier or any(char in project_identifier for char in "\r\n\t\x00"):
        diagnostic = q._q_diagnostic("PROJECT_IDENTIFIER_INVALID")
        return q.QPlan(analysis, (), analysis.diagnostics + (diagnostic,), False, None, None)
    if any(diagnostic.code in _SOURCE_BLOCKERS for diagnostic in analysis.diagnostics):
        return q.QPlan(analysis, (), analysis.diagnostics, False, None, None)
    snapshot = None
    try:
        _check_cancel(cancel_check)
        _reject_link_like_ancestors(source, "Q source")
        snapshot = _open_input_snapshot(source)
        if (source.suffix.lower() != ".gxw" or len(snapshot.body) > q.MAX_INPUT_BYTES
                or snapshot.sha256 != analysis.source_sha256):
            raise q.ReverseConversionError("Q_SOURCE_CHANGED_SINCE_ANALYSIS")
        ir_module, reader = q._reader_modules()
        with tempfile.TemporaryDirectory(prefix=".p006-q-review-plan-") as scratch:
            copy = Path(scratch) / source.name
            copy.write_bytes(snapshot.body)
            q._preflight_comment_directory(copy, reader)
            ir = q._build_source_ir(ir_module, reader, copy)
            if (ir.get("schema_name") != "plcman.gxw.reference-ir" or ir.get("schema_version") != 1
                    or ir.get("input", {}).get("sha256") != snapshot.sha256
                    or ir.get("analysis", {}).get("state") != "COMPLETE"):
                raise q.ReverseConversionError("Q_IR_INCOMPLETE_OR_CHANGED")
            outputs, report = _source_tables(copy, ir, reader, ir_module.split_operands,
                                             project_identifier,
                                             cancel_check, analysis.source_cpu_observation)
        snapshot.verify()
        report["source_sha256"] = snapshot.sha256
        report["diagnostic_counts"] = dict(Counter(item.code for item in analysis.diagnostics))
        report["diagnostics"] = [asdict(item) for item in analysis.diagnostics]
        outputs["report.json"] = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        return q.QPlan(analysis, (), analysis.diagnostics, True, _freeze(outputs), _freeze(report))
    except (OSError, RuntimeError, q.ReverseConversionError, ValueError, KeyError, TypeError,
            UnicodeError) as error:
        if isinstance(error, q.ReverseConversionError) and error.code == "Q_CONVERSION_CANCELLED":
            raise
        code = error.code if isinstance(error, q.ReverseConversionError) else "Q_SOURCE_REVIEW_UNVERIFIED"
        diagnostic = q._q_diagnostic(code, detail=type(error).__name__)
        return q.QPlan(analysis, (), analysis.diagnostics + (diagnostic,), False, None, None)
    finally:
        if snapshot is not None:
            snapshot.close()


def export_q_source_review(source: Path, output_dir: Path, *, project_identifier: str,
                           expected_source_sha256: str | None = None,
                           cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Publish a REVIEW-only set to a new directory after source revalidation."""
    source, output_dir = Path(source), Path(output_dir)
    if (output_dir.exists() or output_dir.is_symlink() or not output_dir.parent.is_dir()
            or output_dir.parent.is_symlink()):
        raise q.ReverseConversionError("OUTPUT_DIRECTORY_MUST_BE_NEW")
    plan = plan_q_source_review(source, project_identifier=project_identifier, cancel_check=cancel_check)
    if not plan.publishable or plan.outputs is None or plan.report is None:
        reason = next((item.code for item in reversed(plan.diagnostics)), "Q_SOURCE_REVIEW_UNVERIFIED")
        raise q.ReverseConversionError(reason)
    report = json.loads(plan.outputs["report.json"].decode("utf-8"))
    if expected_source_sha256 is not None and expected_source_sha256 != report["source_sha256"]:
        raise q.ReverseConversionError("Q_SOURCE_CHANGED_SINCE_PLAN")
    _check_cancel(cancel_check)
    _reject_link_like_ancestors(output_dir.parent, "Q review output")
    snapshot = _open_input_snapshot(source)
    try:
        if snapshot.sha256 != report["source_sha256"]:
            raise q.ReverseConversionError("Q_SOURCE_CHANGED_SINCE_PLAN")
        cancelled = False

        def observed_cancel() -> bool:
            nonlocal cancelled
            cancelled = bool(cancel_check is not None and cancel_check()) or cancelled
            return cancelled

        scratch_path: Path | None = None
        renamed = False
        try:
            with tempfile.TemporaryDirectory(prefix=".p006-q-review-", dir=output_dir.parent) as scratch:
                scratch_path = Path(scratch)
                staging = scratch_path / "review"
                staging.mkdir()
                payloads = tuple((staging / leaf, body) for leaf, body in plan.outputs.items())
                _write_atomically(payloads, snapshot, observed_cancel)
                _verify_snapshot_content(snapshot)
                if output_dir.exists() or output_dir.is_symlink():
                    raise q.ReverseConversionError("OUTPUT_DIRECTORY_EXISTS")
                _check_cancel(observed_cancel)
                os.rename(staging, output_dir)
                renamed = True
        except Exception as error:
            # TemporaryDirectory has now attempted cleanup. Never report its
            # deleted staging names as if they were surviving output files.
            if renamed and (output_dir.exists() or output_dir.is_symlink()):
                actual = ([output_dir, *sorted(output_dir.iterdir())]
                          if not output_dir.is_symlink() and output_dir.is_dir() else [output_dir])
                raise PartialPublishError("incomplete review output set; partial_outputs="
                                          + ",".join(map(str, actual))) from error
            if scratch_path is not None and scratch_path.exists():
                actual = [scratch_path, *sorted(scratch_path.rglob("*"))]
                raise PartialPublishError("review staging cleanup failed; partial_outputs="
                                          + ",".join(map(str, actual))) from error
            if cancelled or isinstance(error, ConversionCancelled):
                raise q.ReverseConversionError("Q_CONVERSION_CANCELLED") from error
            if isinstance(error, q.ReverseConversionError):
                raise
            raise q.ReverseConversionError("Q_REVIEW_PUBLICATION_FAILED", type(error).__name__) from error
        return report
    finally:
        snapshot.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Q GXW source-preserving review CSV export")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--project-identifier", required=True)
    args = parser.parse_args(argv)
    try:
        report = export_q_source_review(args.input, args.output_dir,
                                        project_identifier=args.project_identifier)
    except (q.ReverseConversionError, OSError, ValueError) as error:
        print(json.dumps({"status": "HOLD", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "REVIEW", "target_candidate": False,
                      "report": str(args.output_dir / "report.json"),
                      "occurrences": report["occurrences"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
