"""Read-only FX5 analysis for the common workflow, without output settings.

The existing reader and validators remain the conversion authority. Analysis
does not approve a target profile, change source spelling or publish artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from p006_converter import core


@dataclass(frozen=True)
class SourceReference:
    pou: str
    locator: str
    source_step: int | None
    operand_index: int | None
    spelling: str
    comment: str | None = None


@dataclass(frozen=True)
class ForwardAnalysis:
    source_sha256: str
    source_cpu_identity: str
    pous: tuple[str, ...]
    program_references: tuple[SourceReference, ...]
    comment_references: tuple[SourceReference, ...]
    findings: tuple[tuple[str, ...], ...]
    source_diagnostics: tuple[core.SourceDiagnostic, ...]
    unchecked_pous: tuple[str, ...]
    inspection_scope: tuple[str, ...]
    uninspected_scope: tuple[str, ...]
    official_validation: str = "NOT_RUN"

    @property
    def complete(self) -> bool:
        return not self.unchecked_pous and not self.uninspected_scope

    @property
    def comments_inspected(self) -> bool:
        return not any(item.category == "device_comment" for item in self.source_diagnostics)


def analyze_fx5_source(
    source: Path, *, cancel_check: Callable[[], bool] | None = None,
) -> ForwardAnalysis:
    """Analyze the verified snapshot; no result directory or target settings.

``complete`` describes source inspection only. Even complete inspection can
contain blocking findings, and neither state is GX or field acceptance.
"""
    def checkpoint() -> None:
        if cancel_check is not None and cancel_check():
            raise core.ConversionCancelled("Source analysis cancelled")

    checkpoint()
    source = core._reject_link_like_ancestors(Path(source), "source")
    if not source.is_file() or source.suffix.lower() != ".gx3":
        raise core.ConversionError("Source must be a regular .gx3 file")
    snapshot = core._open_input_snapshot(source)
    try:
        checkpoint()
        project = core.read_gx3(source, _source_bytes=snapshot.body)
        snapshot.verify()
        checkpoint()
        programs = (project.programs if isinstance(project, core.ParsedMultiProject)
                    else (project,))
        names: list[str] = []
        references: list[SourceReference] = []
        unchecked: list[str] = []
        for program in programs:
            checkpoint()
            name = (program.name if isinstance(program, core.ParsedProgram)
                    else program.program_name)
            names.append(name)
            if program.opaque_blocks or program.coverage.opaque_rows:
                unchecked.append(name)
            for record in program.instructions:
                operands = (record.operand, *record.continuation_operands)
                locator = record.source_locator or f"{name}:sequence:{record.sequence}"
                references.extend(
                    SourceReference(name, locator, record.source_step, index, token)
                    for index, token in enumerate(operands) if token
                )
        # Preserve the existing ledger taxonomy and denominator exactly. Source
        # diagnostics are separate because they are not necessarily ledger rows.
        findings = tuple(tuple(row) for row in (
            *core.validate_project(project), *core.validate_comments(project.comments)))
        comments = tuple(SourceReference("", f"comment:{item.device}", None,
                                         None, item.device, item.text)
                         for item in project.comments)
        checkpoint()
        snapshot.verify()
        return ForwardAnalysis(
            snapshot.sha256, "VALIDATED_FX5_FAMILY_MODEL_NOT_EXPOSED", tuple(names),
            tuple(references), comments, findings, tuple(project.source_diagnostics),
            tuple(unchecked), ("SOURCE_STRUCTURE", "PROGRAM_REFERENCES", "COMMENT_REFERENCES",
                               "EXISTING_Q_VALIDATORS", "TARGET_PROJECT_BOUNDS_NOT_CHECKED"),
            tuple([f"POU:{name}" for name in unchecked]
                  + [f"{item.scope}:{item.category}:{item.finding_code}"
                     for item in project.source_diagnostics]),
        )
    finally:
        snapshot.close()
