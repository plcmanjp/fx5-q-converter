"""Closed target width observations, never source step or length guesses."""
import hashlib
import json
from pathlib import Path

WIDTH_FILE = Path(__file__).with_name("resources") / "statement-format-widths.json"
WIDTH_SHA256 = "76534fb6d8fd1dd371d432fdcaf06901cd85977d6bea539193beb8f659414bc9"


def widths(records, direction):
    if direction not in {"FX5_TO_Q", "Q_TO_FX5"}:
        raise ValueError("unknown Statement width direction")
    raw = WIDTH_FILE.read_bytes()
    if WIDTH_FILE.is_symlink() or hashlib.sha256(raw).hexdigest() != WIDTH_SHA256:
        raise ValueError("Statement width authority differs")
    profile = "Q06UDV" if direction == "FX5_TO_Q" else "FX5U"
    data = json.loads(raw)["profiles"][profile]
    observed = {(r["body"], r["subtype"]): r["stored_step_width"] for r in data["measurements"]}
    instruction_widths = ({("LD", ("M0",)): 1, ("OUT", ("M1",)): 1, ("END", ()): 2}
                          if profile == "Q06UDV" else
                          {("LD", ("M0",)): 2, ("OUT", ("M1",)): 2, ("END", ()): 1})
    values = []
    for record in records:
        if record.get("target_kind") == "statement":
            value = observed.get((record.get("target_statement_body"), record.get("target_statement_type")))
        elif record.get("target_kind") == "instruction" and not record.get("inline_note"):
            value = instruction_widths.get((record.get("target_opcode"), tuple(record.get("target_operands", ()))))
        else:
            value = None
        values.append(value)
    known = all(v is not None for v in values)
    steps = []
    current = 0
    for width in values:
        steps.append(current if known else None)
        if known:
            current += width
    return steps, {"status": "CLOSED_NATIVE_WIDTHS" if known else "UNVERIFIED_TARGET_PROGRAM_STEPS",
                   "profile": profile, "width_authority_sha256": WIDTH_SHA256,
                   "verified_record_width_count": sum(v is not None for v in values),
                   "unverified_record_width_count": sum(v is None for v in values),
                   "total_steps": current if known else None, "actual_memory_allocation": "NOT_CHECKED",
                   "source_steps_changed": False, "size_extrapolation": "NONE",
                   "unknown_steps_serialization": "BLANK_GX_RECOMPUTES_VERIFY_OFFICIAL_EXPORT"}
