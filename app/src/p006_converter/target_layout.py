"""Fail-closed target-only ladder layout transforms.

This module deliberately works after source decoding, source validation, and
one-to-one target projection.  It never changes source IR and only returns a
new target serialization sequence when the complete structural and symbolic
proof succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable


_READ_OPS = frozenset({"LD", "LDI", "AND", "ANI", "OR", "ORI"})
_COMBINE_OPS = frozenset({"ANB", "ORB"})
_FORBIDDEN_OPS = frozenset({
    "CJ", "CJP", "JMP", "CALL", "RET", "MC", "MCR", "END", "FEND", "GOEND",
    "EI", "DI", "FOR", "NEXT", "BREAK", "IRET", "STOP",
})
_WRITE_ACTIONS = frozenset({
    "OUT", "SET", "RST", "MOV", "DMOV", "BMOV", "FMOV", "SMOV", "XCH", "DXCH",
    "ADD", "DADD", "SUB", "DSUB", "MUL", "DMUL", "DIV", "DDIV", "INC", "DINC",
    "DEC", "DDEC", "ZRST", "WTOB", "BTOW",
})
_PREFIX_ACTION_ARITIES = {
    "MOVP": (2,), "DMOVP": (2,), "FMOVP": (3,), "PLS": (1,),
    "DATERD": (1,), "DATEWR": (1,), "BKRST": (2,),
    "+": (2, 3), "-": (2, 3), "*": (3,),
}
_PREFIX_ACTIONS = _WRITE_ACTIONS | _PREFIX_ACTION_ARITIES.keys()
_COMPARISONS = {
    f"{mode}{relation}": mode
    for mode in ("LD", "AND", "OR") for relation in ("=", "<>", "<", "<=", ">", ">=")
}
_PREFIX_EDGE_READS = {"LDP": "LD", "LDF": "LD", "ANDP": "AND", "ANDF": "AND", "ORP": "OR", "ORF": "OR"}
LAYOUT_RULE = "SM400_CONSTANT_ROOT_PROVEN_SPLITS_V2"
_NESTED_RULE = "SM400_CONSTANT_ROOT_BALANCED_OUTER_SPLIT_V1"
_FLAT_RULE = "SM400_CONSTANT_ROOT_FLAT_BRANCH_PAIRS_V1"


@dataclass(frozen=True)
class TargetLayoutResult:
    """Target-only records and a JSON-safe provenance/diagnostic envelope."""

    records: tuple[Any, ...]
    report: dict[str, object]


@dataclass(frozen=True)
class _Evaluation:
    read_events: tuple[tuple[object, ...], ...]
    action_events: tuple[tuple[str, tuple[str, ...], tuple[object, ...]], ...]
    ordered_events: tuple[tuple[object, ...], ...]
    error: str = ""
    final_condition: tuple[object, ...] | None = None
    root_action_complete: bool = False


def _atom(opcode: str, operand: str, occurrence: int | None) -> tuple[object, ...]:
    """Keep each dynamic read distinct while allowing the fixed SM400 root."""
    payload: tuple[object, ...] = (operand,) if occurrence is None else (operand, occurrence)
    return ("not", *payload) if opcode in {"LDI", "ANI", "ORI"} else ("atom", *payload)


def _combine(kind: str, left: tuple[object, ...], right: tuple[object, ...]) -> tuple[object, ...]:
    return (kind, left, right)


def _record_operands(record: Any) -> tuple[str, ...]:
    return tuple(value for value in (record.operand, *record.continuation_operands) if value)


def _has_annotation(record: Any) -> bool:
    return bool(record.line_statement or record.pi_statement or record.note)


def _is_edge_or_control(opcode: str) -> bool:
    return (
        opcode in _FORBIDDEN_OPS
        or opcode.endswith("P") and opcode[:-1] in {"LD", "LDI", "AND", "ANI", "OR", "ORI"}
        or opcode.endswith("F") and opcode[:-1] in {"LD", "LDI", "AND", "ANI", "OR", "ORI"}
    )


def _evaluate(records: Iterable[Any], *, prefix: bool = False) -> _Evaluation:
    """Model conditions, ordered events and the boundary state separately.

    Prefix-only pulse operations are opaque: only their known stack effect is
    used to establish a completed root before an independently proved window.
    They are never eligible for rewriting inside that window.
    """
    current: tuple[object, ...] | None = None
    boolean_stack: list[tuple[object, ...]] = []
    mps_stack: list[tuple[object, ...]] = []
    reads: list[tuple[object, ...]] = []
    actions: list[tuple[str, tuple[str, ...], tuple[object, ...]]] = []
    ordered_events: list[tuple[object, ...]] = []
    allowed_actions = _PREFIX_ACTIONS if prefix else _WRITE_ACTIONS
    read_occurrence = 0
    root_action_complete = False
    for record in records:
        opcode = record.instruction
        operands = _record_operands(record)
        if not opcode:
            # A line statement is presentation metadata, not an IL Boolean
            # operation.  Other instruction-less payloads remain unknown.
            if not operands and not record.pi_statement and not record.note:
                continue
            return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_EMPTY_INSTRUCTION")
        if _has_annotation(record):
            return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_ANNOTATED_SCOPE")
        if _is_edge_or_control(opcode) and not (prefix and opcode in _PREFIX_EDGE_READS):
            return _Evaluation((), (), (), f"LAYOUT_UNSUPPORTED_CONTROL_{opcode}")
        mode = _COMPARISONS.get(opcode) or (_PREFIX_EDGE_READS.get(opcode) if prefix else None)
        if opcode in _READ_OPS or mode:
            if len(operands) != (2 if opcode in _COMPARISONS else 1):
                return _Evaluation((), (), (), f"LAYOUT_UNSUPPORTED_READ_SHAPE_{opcode}")
            mode = mode or ({"LDI": "LD", "ANI": "AND", "ORI": "OR"}.get(opcode, opcode))
            occurrence: int | None = None
            constant = opcode in _READ_OPS and operands == ("SM400",)
            if not constant:
                read_occurrence += 1
                occurrence = read_occurrence
            atom = (_atom(opcode, operands[0], occurrence) if opcode in _READ_OPS
                    else ("predicate", opcode, operands, occurrence))
            if mode == "LD":
                if current is not None:
                    if not (root_action_complete and not mps_stack and not boolean_stack):
                        boolean_stack.append(current)
                current = atom
            elif mode == "AND":
                if current is None:
                    return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_BOOLEAN_ROOT")
                current = _combine("and", current, atom)
            else:
                if current is None:
                    return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_BOOLEAN_ROOT")
                current = _combine("or", current, atom)
            root_action_complete = False
            if not constant:
                payload = operands[0] if len(operands) == 1 else operands
                reads.append((str(occurrence), opcode, payload))
                ordered_events.append(("READ", occurrence, opcode, payload))
            continue
        if opcode in _COMBINE_OPS:
            if operands or current is None or not boolean_stack:
                return _Evaluation((), (), (), f"LAYOUT_UNSUPPORTED_COMBINE_{opcode}")
            current = _combine("and" if opcode == "ANB" else "or", boolean_stack.pop(), current)
            root_action_complete = False
            continue
        if opcode == "INV" or (prefix and opcode == "MEP"):
            if operands or current is None:
                return _Evaluation((), (), (), f"LAYOUT_UNSUPPORTED_SHAPE_{opcode}")
            current = ("not_result" if opcode == "INV" else "opaque_prefix_MEP", current)
            root_action_complete = False
            continue
        if opcode == "MPS":
            if operands or current is None or boolean_stack:
                return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_MPS")
            mps_stack.append(current)
            root_action_complete = False
            continue
        if opcode == "MRD":
            if operands or not mps_stack or boolean_stack:
                return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_MRD")
            current = mps_stack[-1]
            root_action_complete = False
            continue
        if opcode == "MPP":
            if operands or not mps_stack or boolean_stack:
                return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_MPP")
            current = mps_stack.pop()
            root_action_complete = False
            continue
        if opcode not in allowed_actions or not operands or current is None:
            return _Evaluation((), (), (), f"LAYOUT_UNSUPPORTED_OPCODE_{opcode}")
        if opcode in _PREFIX_ACTION_ARITIES and len(operands) not in _PREFIX_ACTION_ARITIES[opcode]:
            return _Evaluation((), (), (), f"LAYOUT_UNSUPPORTED_ACTION_SHAPE_{opcode}")
        if boolean_stack:
            return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_BOOLEAN_ROOT")
        actions.append((opcode, operands, current))
        ordered_events.append(("WRITE", opcode, operands, current))
        # Retain the result for action->MPS and action->AND->action.  Only a
        # later LD after a completed root action starts a separate network.
        root_action_complete = not mps_stack
    if boolean_stack or mps_stack:
        return _Evaluation((), (), (), "LAYOUT_UNSUPPORTED_UNBALANCED_SCOPE")
    return _Evaluation(tuple(reads), tuple(actions), tuple(ordered_events),
                       final_condition=current, root_action_complete=root_action_complete)


def _mps_pairs(records: tuple[Any, ...]) -> tuple[dict[int, int], list[int], str]:
    """Return MPS/MPP pairings and depth before every record without inference."""
    stack: list[int] = []
    pairs: dict[int, int] = {}
    depths: list[int] = []
    for index, record in enumerate(records):
        depths.append(len(stack))
        if record.instruction == "MPS":
            stack.append(index)
        elif record.instruction == "MPP":
            if not stack:
                return {}, [], "LAYOUT_UNSUPPORTED_UNBALANCED_SCOPE"
            pairs[stack.pop()] = index
    if stack:
        return {}, [], "LAYOUT_UNSUPPORTED_UNBALANCED_SCOPE"
    return pairs, depths, ""


@dataclass(frozen=True)
class _Candidate:
    root: int
    end: int
    replacements: tuple[tuple[int, str], ...]
    insert_after: tuple[int, ...]
    rule: str


def _candidates(records: tuple[Any, ...]) -> list[_Candidate]:
    """Select disjoint original scopes; never rerun on synthesized records."""
    pairs, depths, error = _mps_pairs(records)
    if error:
        return []
    candidates: list[_Candidate] = []
    for root in range(len(records) - 1):
        if not (
            records[root].instruction == "LD"
            and _record_operands(records[root]) == ("SM400",)
            and records[root + 1].instruction == "MPS"
            and not _record_operands(records[root + 1])
            and depths[root] == 0
            and root + 1 in pairs
        ):
            continue
        # A new root is only proven after a completed root-level write or at
        # the beginning.  This excludes LD SM400 nested in an earlier Boolean
        # calculation, even if its raw MPS depth happens to be zero.
        if root and records[root - 1].instruction not in _PREFIX_ACTIONS:
            continue
        outer_mps = root + 1
        outer_end = pairs[outer_mps]
        nested = [
            index for index, end in pairs.items()
            if outer_mps < index < outer_end and depths[index] == 1 and end < outer_end
        ]
        if not nested:
            branches = [i for i in range(outer_mps + 1, outer_end)
                        if records[i].instruction == "MRD"]
            if not branches:
                continue
            # Keep two original branches per MPS/MPP pair.  A final odd
            # branch starts with LD alone.  No size/output-count inference.
            changes = [(i, "MPP" if n % 2 == 0 else "LD") for n, i in enumerate(branches)]
            inserts = tuple(i for n, i in enumerate(branches) if n % 2 == 1)
            if len(branches) % 2:
                changes.append((outer_end, "LD"))
            candidates.append(_Candidate(root, outer_end, tuple(changes), inserts, _FLAT_RULE))
            continue
        if len(nested) != 1:
            continue
        nested_mps = nested[0]
        nested_end = pairs[nested_mps]
        prior = [
            index for index in range(outer_mps + 1, nested_mps)
            if records[index].instruction == "MRD" and depths[index] == 1
        ]
        following = [
            index for index in range(nested_end + 1, outer_end)
            if records[index].instruction == "MRD" and depths[index] == 1
        ]
        if not prior or not following:
            continue
        candidates.append(_Candidate(root, outer_end, ((prior[-1], "MPP"), (following[0], "LD")),
                                     (following[0],), _NESTED_RULE))
    return candidates


def _layout_report(status: str, *, diagnostic: str = "", provenance: dict[str, object] | None = None) -> dict[str, object]:
    report: dict[str, object] = {
        "status": status,
        "rule": LAYOUT_RULE,
        "source_ir_changed": False,
        "source_audit_relation": "PRE_LAYOUT_ONE_TO_ONE_AUDIT_ONLY",
    }
    if diagnostic:
        report["diagnostic"] = diagnostic
    if provenance is not None:
        report["provenance"] = provenance
    return report


def _anchor(record: Any) -> dict[str, int]:
    return {"source_sequence": record.sequence, "source_step": record.source_step}


def _rewrite(records: tuple[Any, ...], candidates: list[_Candidate]) -> tuple[tuple[Any, ...], dict[int, int]]:
    changes = {index: opcode for c in candidates for index, opcode in c.replacements}
    insertions = {index for c in candidates for index in c.insert_after}
    output: list[Any] = []
    offsets: dict[int, int] = {}
    delta = 0
    for index, source in enumerate(records):
        offsets[index] = len(output)
        record = replace(source, target_step=source.source_step + delta) if delta else source
        if index in changes:
            opcode = changes[index]
            record = replace(record, instruction=opcode, operand="SM400" if opcode == "LD" else "")
        output.append(record)
        if index in insertions:
            output.append(replace(
                source, sequence=-1, source_step=-1, target_step=source.source_step + delta + 1,
                instruction="MPS", operand="", continuation_operands=(), continuation_rows=(),
                source_record_id="", source_digest="", source_locator="",
                source_semantic_tags=("TARGET_LAYOUT_SYNTHETIC",),
            ))
            delta += 1
    offsets[len(records)] = len(output)
    return tuple(output), offsets


def _window_end(records: tuple[Any, ...], outer_end: int) -> int | None:
    # Include the final branch action, then compare the full exit state. Any
    # following instructions consequently receive the same condition/stacks.
    index = outer_end + 1
    while index < len(records):
        opcode = records[index].instruction
        if opcode in _WRITE_ACTIONS:
            return index
        if opcode not in (_READ_OPS | _COMBINE_OPS | _COMPARISONS.keys() | {"INV"}):
            return None
        index += 1
    return None


def _prove(records: tuple[Any, ...], candidate: _Candidate) -> tuple[int | None, _Evaluation, str]:
    prefix = _evaluate(records[:candidate.root], prefix=True)
    if prefix.error:
        return None, prefix, f"LAYOUT_UNSUPPORTED_PREFIX_{prefix.error}"
    if prefix.final_condition is not None and not prefix.root_action_complete:
        return None, prefix, "LAYOUT_UNSUPPORTED_PREFIX_INCOMPLETE_ROOT"
    controls = {candidate.root + 1, candidate.end, *(i for i, _ in candidate.replacements)}
    if _has_annotation(records[candidate.root]) or any(
        _has_annotation(records[i]) or _record_operands(records[i]) for i in controls
    ):
        return None, prefix, "LAYOUT_UNSUPPORTED_CONTROL_SHAPE"
    end = _window_end(records, candidate.end)
    if end is None:
        return None, prefix, "LAYOUT_UNSUPPORTED_OUTER_CLOSE"
    before = _evaluate(records[candidate.root:end + 1])
    transformed, offsets = _rewrite(records, [candidate])
    after = _evaluate(transformed[offsets[candidate.root]:offsets[end + 1]])
    if before.error or after.error:
        return None, before, before.error or after.error
    # Includes final condition and completed-root state, not just write traces.
    if before != after:
        return None, before, "LAYOUT_EVENT_TRACE_MISMATCH"
    if not before.root_action_complete:
        return None, before, "LAYOUT_UNSUPPORTED_INCOMPLETE_ROOT"
    return end, before, ""


def apply_constant_root_layout(records: tuple[Any, ...]) -> TargetLayoutResult:
    """Batch independently proven constant-root scopes in original index space.

    Nested outer splits and flat branch pairs have separate provenance. Unknown
    scopes remain byte-for-byte original (apart from cumulative target steps).
    Source IR and device assignments never change; native size acceptance is a
    separate gate, not implied by this symbolic proof.
    """
    candidates = _candidates(records)
    if not candidates:
        looks_like_constant_root = any(r.instruction == "LD" and _record_operands(r) == ("SM400",) for r in records)
        looks_like_root_split = any(r.instruction == "LD" and records[i + 1].instruction == "MPS"
                                    for i, r in enumerate(records[:-1]))
        diagnostic = ("LAYOUT_UNSUPPORTED_STRUCTURE" if looks_like_constant_root else
                      ("LAYOUT_UNSUPPORTED_CONSTANT_ROOT" if looks_like_root_split else ""))
        return TargetLayoutResult(records, _layout_report(
            "UNSUPPORTED" if looks_like_root_split else "NOT_APPLICABLE", diagnostic=diagnostic))
    steps = [r.source_step for r in records]
    if any(s < 0 for s in steps) or any(b <= a for a, b in zip(steps, steps[1:])) or any(
        r.target_step is not None for r in records
    ):
        return TargetLayoutResult(records, _layout_report("UNSUPPORTED", diagnostic="LAYOUT_UNSUPPORTED_TARGET_STEP_ORDER"))
    accepted: list[_Candidate] = []
    proof_ends: list[int] = []
    scopes: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    last_end = -1
    for candidate in candidates:
        end, evaluation, error = _prove(records, candidate)
        if not error and candidate.root <= last_end:
            error = "LAYOUT_UNSUPPORTED_OVERLAPPING_PROOF_WINDOWS"
        if error:
            skipped.append({"rule": candidate.rule, "root_anchor": _anchor(records[candidate.root]), "diagnostic": error})
            continue
        assert end is not None
        accepted.append(candidate)
        proof_ends.append(end)
        last_end = end
        scopes.append({
            "rule": candidate.rule, "root_anchor": _anchor(records[candidate.root]),
            "proof_end_anchor": _anchor(records[end]),
            "changes": [{**_anchor(records[i]), "from_opcode": records[i].instruction, "to_opcode": op}
                        for i, op in candidate.replacements],
            "nonconstant_read_event_count": len(evaluation.read_events),
            "write_action_event_count": len(evaluation.action_events),
            "ordered_nonconstant_event_count": len(evaluation.ordered_events),
            "event_trace_equivalent": True, "exit_state_equivalent": True,
            "prefix_evidence": "KNOWN_STACK_EFFECTS_AND_COMPLETED_ROOT_ONLY",
        })
    if not accepted:
        return TargetLayoutResult(records, _layout_report("UNSUPPORTED", diagnostic=str(skipped[0]["diagnostic"]),
                                                          provenance={"skipped_scopes": skipped}))
    transformed, offsets = _rewrite(records, accepted)
    synthetic_records = []
    rebase = []
    delta = 0
    for candidate, scope, end in zip(accepted, scopes, proof_ends, strict=True):
        synthetic = []
        for index in candidate.insert_after:
            row = transformed[offsets[index] + 1]
            synthetic.append({"target_only": True, "instruction": "MPS", "target_step": row.target_step,
                              "source_record_id": "", "source_locator": "", "after_source_sequence": records[index].sequence})
            delta += 1
            rebase.append({"after_source_sequence": records[index].sequence, "delta": 1, "cumulative_delta": delta})
        scope["synthetic_records"] = synthetic
        scope["target_record_range"] = [offsets[candidate.root], offsets[end + 1]]
        synthetic_records.extend(synthetic)
    provenance = {"scopes": scopes, "skipped_scopes": skipped, "synthetic_records": synthetic_records,
                  "target_step_rebases": rebase, "total_insertions": delta,
                  "event_trace_equivalent": True, "exit_state_equivalent": True}
    # Keep the V1 single-scope fields for existing audit readers.
    if len(accepted) == 1 and accepted[0].rule == _NESTED_RULE:
        candidate = accepted[0]
        provenance.update({"root_anchor": scopes[0]["root_anchor"],
                           "outer_mrd_to_mpp": _anchor(records[candidate.replacements[0][0]]),
                           "split_mrd_to_ld": _anchor(records[candidate.replacements[1][0]]),
                           "synthetic_record": synthetic_records[0], "target_step_rebase": rebase[0],
                           **{k: scopes[0][k] for k in ("nonconstant_read_event_count", "write_action_event_count", "ordered_nonconstant_event_count")}})
    return TargetLayoutResult(transformed, _layout_report("APPLIED", provenance=provenance))
