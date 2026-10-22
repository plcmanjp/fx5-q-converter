"""Converter's explicit ANS rewrite, not an upstream decoder or timing model.

Keep every operand verbatim. CPU timer units, allocation and runtime behaviour
are the user's responsibility and are neither read nor corrected here.
"""
from dataclasses import replace
import re

PROJECTION = "ANS_TO_Q_TIMER_SET_F"
F_BIT_OPS = frozenset({"LD", "LDI", "AND", "ANI", "OR", "ORI", "OUT", "SET", "RST"})


def plain_f_bit(record):
    return (record.instruction in F_BIT_OPS
            and re.fullmatch(r"F[0-9]+", record.operand) is not None
            and not record.continuation_operands and not record.continuation_rows)


def extended_f_form(record):
    """F operand families covered by the issue100 Q06UDV offline oracle.

    This approves syntax only: preserve the index and digit count verbatim,
    without choosing device allocations or evaluating index register values.
    Other F transfers, comparisons and indexed groups retain their own gates.
    """
    ops = (record.operand, *record.continuation_operands)
    if record.continuation_rows and (
            len(record.continuation_rows) != len(ops) - 1
            or any(len(row) != 7 or row[3] != operand
                   or any(row[c] for c in (0, 1, 2, 4))
                   for row, operand in zip(record.continuation_rows, ops[1:]))):
        return False
    if record.instruction in {"OR", "OUT"} and len(ops) == 1:
        index = re.fullmatch(r"F[0-9]+Z([0-9]+)", ops[0])
        # Q06UDV has a fixed maximum of twenty index registers, Z0..Z19.
        return index is not None and int(index[1]) <= 19
    if len(ops) != 2:
        return False
    if record.instruction == "MOV":
        return (re.fullmatch(r"K4F[0-9]+", ops[0]) is not None
                and re.fullmatch(r"K4M[0-9]+", ops[1]) is not None)
    if record.instruction in {"AND<", "AND="}:
        if ops[0] == "K0" and re.fullmatch(r"K2F[0-9]+", ops[1]):
            return True
    digit = {"AND=": "4", "ANDD=": "8"}.get(record.instruction)
    return (digit is not None and ops[1] == "K0"
            and re.fullmatch(r"K" + digit + r"F[0-9]+", ops[0]) is not None)


def supported_ans(record):
    ops = (record.operand, *record.continuation_operands)
    if (record.instruction != "ANS" or len(ops) != 3
            or not re.fullmatch(r"T[0-9]+", ops[0])
            or not re.fullmatch(r"(?:D[0-9]+|K-?[0-9]+)", ops[1])
            or not re.fullmatch(r"F[0-9]+", ops[2])):
        return False
    # Continuation annotations are carried to their corresponding target row.
    return not record.continuation_rows or (
        len(record.continuation_rows) == 2
        and all(len(row) == 7 and row[3] == operand
                and not any(row[c] for c in (0, 1, 2, 4))
                for row, operand in zip(record.continuation_rows, ops[1:], strict=True)))


def expand(records):
    """Expand at the target emission boundary, keeping source identities intact.

Terminal branches need OUT T n / AND T / SET F. If the next instruction
consumes the current condition, surround that work with MPS/MRD/MPP so the
following original instruction still receives the original condition.
"""
    output = []
    depth = 0
    for index, record in enumerate(records):
        if record.instruction == "MPS":
            depth += 1
        elif record.instruction == "MPP":
            depth -= 1
        if record.instruction != "ANS":
            output.append(record)
            continue
        if not supported_ans(record):
            raise ValueError("ANS requires plain T, D/K, F operands and lossless continuation rows")
        following = next((r.instruction for r in records[index + 1:] if r.instruction), "END")
        terminal = following in {"MRD", "MPP", "END", "FEND"} or (
            following.startswith("LD") and depth == 0)
        if not terminal and not 0 <= depth < 16:
            raise ValueError("ANS expansion exceeds the Q MPS stack capacity")
        timer, preset, alarm = (record.operand, *record.continuation_operands)
        blank = replace(record, instruction="", operand="", continuation_operands=(),
                        continuation_rows=(), line_statement="", pi_statement="", note="",
                        note_subtype="", target_step=None)
        timer_row = replace(record, instruction="OUT", continuation_operands=(preset,),
                            continuation_rows=record.continuation_rows[:1], target_step=None)
        contact = replace(blank, instruction="AND", operand=timer)
        alarm_row = replace(blank, instruction="SET", operand=alarm)
        if record.continuation_rows:
            alarm_row = replace(alarm_row, pi_statement=record.continuation_rows[1][5],
                                note=record.continuation_rows[1][6])
        if terminal:
            output.extend((timer_row, contact, alarm_row))
        else:
            # Source Statement belongs before the first generated operation.
            output.extend((replace(blank, instruction="MPS", line_statement=record.line_statement),
                           replace(timer_row, line_statement=""), replace(blank, instruction="MRD"),
                           contact, alarm_row, replace(blank, instruction="MPP")))
    return tuple(output)
