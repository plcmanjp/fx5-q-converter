"""Map X/Y in a complete FX5U-to-Q result without changing its source archive."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile

from p006_converter.xy_mapping import Bounds, MappingError, Profile, Reference, fixed_bounds
from p006_converter.xy_settings import MappingSettings, preview
from p006_converter.core import (ConversionError, Q06UDV_REJECTED_OPERANDS,
                                 TARGET_EXCLUSION_EXACT_REASON, _project_q_comment_device,
                                 _q06udv_rejected_operand)


SCHEMA = "plcman.p006.forward-xy-candidate.v1"
_XY_START = re.compile(r"^(?:@?K[1-8])?[XY]", re.ASCII)
_LEGACY_SPECIAL_EXCLUSION_REASON = "Q06UDV special device number exceeds 2047"
_LEGACY_EXACT_EXCLUSION_REASON = "Q06UDV exact-known unsupported operand"


class ForwardMappingError(ValueError):
    pass


def _hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_csv(path: Path, *, columns: int | None = None) -> tuple[list[list[str]], bytes]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
        raise ForwardMappingError(f"INPUT_CSV_INVALID:{path.name}")
    body = path.read_bytes()
    if not body.startswith(b"\xff\xfe"):
        raise ForwardMappingError(f"INPUT_CSV_ENCODING:{path.name}")
    try:
        rows = list(csv.reader(io.StringIO(body.decode("utf-16"), newline=""), delimiter="\t"))
    except (UnicodeError, csv.Error) as error:
        raise ForwardMappingError(f"INPUT_CSV_PARSE:{path.name}") from error
    if len(rows) < 2 or (columns is not None and any(len(row) != columns for row in rows[2:])):
        raise ForwardMappingError(f"INPUT_CSV_COLUMNS:{path.name}")
    return rows, body


def _encode(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\r\n").writerows(rows)
    return b"\xff\xfe" + stream.getvalue().encode("utf-16le")


def _filtered_comment_review(result_dir: Path, report: dict, comments: list[list[str]]) -> bytes | None:
    summary = report.get("comments", {})
    if not isinstance(summary, dict):
        raise ForwardMappingError("FORWARD_COMMENT_SUMMARY_INVALID")
    count = summary.get("excluded_target_incompatible_devices", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ForwardMappingError("FORWARD_EXCLUDED_COMMENT_COUNT_INVALID")
    if count == 0:
        return None
    review_meta = report.get("comment_review", {})
    if not isinstance(review_meta, dict):
        raise ForwardMappingError("FORWARD_FILTERED_COMMENT_PROVENANCE_INVALID")
    header = ["Device Name", "Q Device Name", "Source Comment", "GX Works2 Comment",
              "Source CP949 Bytes", "GX Works2 CP949 Bytes", "Status"]
    exclusion_reason = summary.get("target_exclusion_reason")
    if (summary.get("target_exclusion_finding_code") != "TARGET_UNSUPPORTED"
            or exclusion_reason not in {TARGET_EXCLUSION_EXACT_REASON, _LEGACY_SPECIAL_EXCLUSION_REASON,
                                        _LEGACY_EXACT_EXCLUSION_REASON}
            or summary.get("full_originals_preserved_in_comment_review_csv") is not True
            or review_meta.get("published") is not True or review_meta.get("header") != header):
        raise ForwardMappingError("FORWARD_FILTERED_COMMENT_PROVENANCE_INVALID")
    path = result_dir / "comment-review.csv"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
        raise ForwardMappingError("FORWARD_FILTERED_COMMENT_REVIEW_INVALID")
    body = path.read_bytes()
    try:
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    except (UnicodeError, csv.Error) as error:
        raise ForwardMappingError("FORWARD_FILTERED_COMMENT_REVIEW_PARSE") from error
    if not rows or rows[0] != header or any(len(row) != len(header) for row in rows[1:]):
        raise ForwardMappingError("FORWARD_FILTERED_COMMENT_REVIEW_COLUMNS")
    excluded = [row for row in rows[1:] if row[6].startswith("TARGET_DEVICE_EXCLUDED")]
    included = [row for row in rows[1:] if not row[6].startswith("TARGET_DEVICE_EXCLUDED")]
    if (len(excluded) != count or len(rows) - 1 != summary.get("total")
            or len(included) != summary.get("import_rows")
            or Counter((row[1], row[3]) for row in included)
               != Counter((row[0], row[1]) for row in comments[2:])):
        raise ForwardMappingError("FORWARD_FILTERED_COMMENT_REVIEW_MISMATCH")
    for row in excluded:
        if exclusion_reason == _LEGACY_SPECIAL_EXCLUSION_REASON:
            match = re.fullmatch(r"(?:SM|SD)(\d+)(?:\.[0-9A-F])?", row[0], re.ASCII)
            valid_exclusion = match is not None and int(match.group(1)) > 2047
            valid_statuses = {"TARGET_DEVICE_EXCLUDED|EXACT", "TARGET_DEVICE_EXCLUDED|TRUNCATED"}
        else:
            # Verify the report's source-to-target projection as well as the
            # exact unsupported target; a forged review row could otherwise
            # silently omit a supported source device from the mapped result.
            try:
                held = (_q06udv_rejected_operand(row[1]) if exclusion_reason == TARGET_EXCLUSION_EXACT_REASON
                        else row[1] in Q06UDV_REJECTED_OPERANDS)
                valid_exclusion = _project_q_comment_device(row[0]) == row[1] and held
            except ConversionError:
                valid_exclusion = False
            valid_statuses = {"TARGET_DEVICE_EXCLUDED|EXACT", "TARGET_DEVICE_EXCLUDED|TRUNCATED",
                              "TARGET_DEVICE_EXCLUDED|ENCODING_UNREPRESENTABLE"}
        if not valid_exclusion or row[6] not in valid_statuses:
            raise ForwardMappingError("FORWARD_FILTERED_COMMENT_NOT_SPECIAL")
    return body


def _normal_inputs(result_dir: Path) -> tuple[dict, list[tuple[str, list[list[str]], bytes]], list[list[str]], bytes, bytes, bytes | None]:
    if not result_dir.is_dir() or result_dir.is_symlink():
        raise ForwardMappingError("FORWARD_RESULT_DIRECTORY_INVALID")
    report_path = result_dir / "conversion-report.json"
    if not report_path.is_file() or report_path.is_symlink():
        raise ForwardMappingError("FORWARD_REPORT_MISSING")
    report_body = report_path.read_bytes()
    try:
        report = json.loads(report_body.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise ForwardMappingError("FORWARD_REPORT_INVALID") from error
    programs = report.get("programs")
    summary = report.get("publication_summary", {})
    if (report.get("target", {}).get("cpu") != "QCPU (Q mode) Q06UDV"
            or report.get("status", {}).get("publication") != "COMPLETE_NORMAL"
            or not isinstance(programs, list) or not programs
            or summary.get("normal_q_pou_count") != len(programs)
            or any(item.get("publication_class") != "NORMAL_Q_CSV" for item in programs)):
        raise ForwardMappingError("FORWARD_RESULT_NOT_COMPLETE_NORMAL")
    names = [item.get("q_output") for item in programs]
    if any(not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."} for name in names):
        raise ForwardMappingError("FORWARD_Q_OUTPUT_NAME_INVALID")
    if len({name.casefold() for name in names}) != len(names):
        raise ForwardMappingError("FORWARD_Q_OUTPUT_NAME_COLLISION")
    tables = []
    for name in names:
        rows, body = _read_csv(result_dir / name, columns=7)
        if (len(rows) < 4 or rows[1] != ["PLC Information:", "QCPU (Q mode) Q06UDV"]
                or rows[2] != ["Step No.", "Line Statement", "Instruction", "I/O(Device)", "Blank", "PI Statement", "Note"]):
            raise ForwardMappingError(f"FORWARD_Q_PREAMBLE_INVALID:{name}")
        if any(_q06udv_rejected_operand(row[3]) for row in rows[3:]):
            raise ForwardMappingError(f"FORWARD_CURRENT_TARGET_HOLD:{name}")
        tables.append((name, rows, body))
    comments, comment_body = _read_csv(result_dir / "comment.csv", columns=2)
    if comments[1] != ["Device Name", "Comment"]:
        raise ForwardMappingError("FORWARD_COMMENT_HEADER_INVALID")
    if any(_q06udv_rejected_operand(row[0]) for row in comments[2:]):
        raise ForwardMappingError("FORWARD_CURRENT_TARGET_HOLD:comment.csv")
    if any(rows[0] != tables[0][1][0] for _name, rows, _body in tables) or comments[0] != tables[0][1][0]:
        raise ForwardMappingError("FORWARD_PROJECT_IDENTIFIER_MISMATCH")
    review_body = _filtered_comment_review(result_dir, report, comments)
    return report, tables, comments, comment_body, report_body, review_body


def plan_forward_xy(result_dir: Path, *, settings: MappingSettings,
                    fx5_bounds: Bounds | None = None, q_bounds: Bounds | None = None) -> tuple[dict[str, bytes], dict]:
    if settings.source is not Profile.FX5U or settings.target is not Profile.Q:
        raise ForwardMappingError("XY_DIRECTION_MISMATCH")
    report, tables, comments, comment_body, report_body, review_body = _normal_inputs(result_dir)
    source = Path(report["source"]["path"])
    if not source.is_file() or source.is_symlink() or _hash(source.read_bytes()) != report["source"]["sha256"]:
        raise ForwardMappingError("FORWARD_SOURCE_SNAPSHOT_MISMATCH")
    snapshots = {name: _hash(body) for name, _rows, body in tables}
    snapshots["comment.csv"] = _hash(comment_body)
    if review_body is not None:
        snapshots["comment-review.csv"] = _hash(review_body)
    source_hash = _hash(report_body + b"\0" + json.dumps(snapshots, sort_keys=True).encode("ascii"))
    program_refs = [Reference(row[3], f"{name}:row:{index}:operand")
                    for name, rows, _body in tables for index, row in enumerate(rows[3:], 3)
                    if _XY_START.match(row[3])]
    comment_refs = [Reference(row[0], f"comment:row:{index}:device")
                    for index, row in enumerate(comments[2:], 2) if _XY_START.match(row[0])]
    try:
        result = preview(settings, source_bounds=fx5_bounds, target_bounds=q_bounds,
                         input_sha256=source_hash, program=program_refs, comments=comment_refs)
        result.require_publishable(settings=settings, input_sha256=source_hash,
                                   source_bounds=fx5_bounds, target_bounds=q_bounds)
    except MappingError as error:
        raise ForwardMappingError(f"XY_PREFLIGHT_HOLD:{error}") from error
    projected = {item.locator: item.target for item in result.projections}
    outputs: dict[str, bytes] = {}
    for name, rows, _body in tables:
        updated = [row.copy() for row in rows]
        for index, row in enumerate(updated[3:], 3):
            row[3] = projected.get(f"{name}:row:{index}:operand", row[3])
        outputs[name] = _encode(updated)
    updated_comments = [row.copy() for row in comments]
    for index, row in enumerate(updated_comments[2:], 2):
        row[0] = projected.get(f"comment:row:{index}:device", row[0])
    outputs["comment.csv"] = _encode(updated_comments)
    mapping_report = {
        "schema": SCHEMA, "direction": "FX5U_TO_Q06UDV", "source_gx3_sha256": report["source"]["sha256"],
        "forward_report_sha256": _hash(report_body), "forward_artifacts_sha256": snapshots,
        "settings_sha256": settings.digest, "input_snapshot_sha256": source_hash,
        "program_occurrences": result.program_occurrences, "comment_rows": result.comment_rows,
        "mapped_unique_points": result.mapped_unique_points,
        "unmapped_unique_points": result.unmapped_unique_points,
        "source_excluded_nonxy_special_comments": report.get("comments", {}).get("excluded_target_incompatible_devices", 0),
        "official_validation": "NOT_RUN", "hardware_assignment": "UNVERIFIED",
        "production_adoption": "NOT_GRANTED", "candidate": "GX_WORKS2_IMPORT_REBUILD_REEXPORT_REQUIRED",
        "fixed_xy_bounds": {"source": {"profile": Profile.FX5U.value,
                                         "x_max": fixed_bounds(Profile.FX5U).x,
                                         "y_max": fixed_bounds(Profile.FX5U).y},
                            "target": {"profile": Profile.Q.value,
                                         "x_max": fixed_bounds(Profile.Q).x,
                                         "y_max": fixed_bounds(Profile.Q).y}},
        "outputs_sha256": {name: _hash(body) for name, body in outputs.items()},
    }
    fx, q = fx5_bounds or Bounds(), q_bounds or Bounds()
    mapping_report["project_bounds_check"] = {
        "status": "NOT_CHECKED", "provided": dict.fromkeys(("source_x", "source_y", "target_x", "target_y")),
    }
    mapping_report["device_capacity_policy"] = {"state": "NOT_APPLIED", "blocking": False}
    outputs["xy-mapping-report.json"] = (json.dumps(mapping_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return outputs, mapping_report


def map_forward_xy(result_dir: Path, output_dir: Path, *, settings: MappingSettings,
                   fx5_bounds: Bounds | None = None, q_bounds: Bounds | None = None) -> dict:
    if output_dir.exists() or output_dir.is_symlink() or not output_dir.parent.is_dir() or output_dir.parent.is_symlink():
        raise ForwardMappingError("OUTPUT_DIRECTORY_MUST_BE_NEW")
    outputs, report = plan_forward_xy(result_dir, settings=settings, fx5_bounds=fx5_bounds, q_bounds=q_bounds)
    with tempfile.TemporaryDirectory(prefix=".p006-fxq-xy-", dir=output_dir.parent) as scratch:
        staging = Path(scratch) / "candidate"
        staging.mkdir()
        for name, body in outputs.items():
            (staging / name).write_bytes(body)
        current_report, current_tables, _comments, current_comment_body, current_report_body, current_review_body = _normal_inputs(result_dir)
        current_snapshots = {name: _hash(body) for name, _rows, body in current_tables}
        current_snapshots["comment.csv"] = _hash(current_comment_body)
        if current_review_body is not None:
            current_snapshots["comment-review.csv"] = _hash(current_review_body)
        if (_hash(current_report_body) != report["forward_report_sha256"]
                or current_report["source"]["sha256"] != report["source_gx3_sha256"]
                or current_snapshots != report["forward_artifacts_sha256"]):
            raise ForwardMappingError("FORWARD_INPUT_CHANGED_BEFORE_PUBLICATION")
        current_source = Path(current_report["source"]["path"])
        if not current_source.is_file() or current_source.is_symlink() or _hash(current_source.read_bytes()) != report["source_gx3_sha256"]:
            raise ForwardMappingError("FORWARD_SOURCE_CHANGED_BEFORE_PUBLICATION")
        if output_dir.exists() or output_dir.is_symlink():
            raise ForwardMappingError("OUTPUT_DIRECTORY_EXISTS")
        os.rename(staging, output_dir)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply verified X/Y rules to a complete FX5U-to-Q result")
    parser.add_argument("--forward-result-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--xy-settings", required=True, type=Path)
    for name in ("fx5-max-x", "fx5-max-y", "q-max-x", "q-max-y"):
        parser.add_argument("--" + name, type=int)
    args = parser.parse_args(argv)
    try:
        settings = MappingSettings.from_json(args.xy_settings.read_bytes(), expected_source=Profile.FX5U,
                                             expected_target=Profile.Q)
        report = map_forward_xy(args.forward_result_dir, args.output_dir, settings=settings,
                                fx5_bounds=Bounds(args.fx5_max_x, args.fx5_max_y),
                                q_bounds=Bounds(args.q_max_x, args.q_max_y))
    except (ForwardMappingError, MappingError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "HOLD", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "CANDIDATE", "report": str(args.output_dir / "xy-mapping-report.json"),
                      "mapped_unique_points": report["mapped_unique_points"]}, ensure_ascii=False))
    return 0
