#!/usr/bin/env python3
"""GX Works3 FX5U narrow subset to GX Works2 Q import CSV.

Input schema:
  A GX Works3 FX5U Ladder ``.gx3`` archive containing either one dynamically
  linked POU or dynamically linked multi-POU project. Every ``LadderBlocks`` and linked ``StepInfo`` row is first
  represented in a digest-only structural IR and decoded through its linked
  MIL projection. Unknown structure, operand descriptor, instruction, and
  operand count fail closed as ``MINING_REQUIRED``; the file hash is evidence,
  never an input approval gate.

Output schemas:
  * normalized CSV: UTF-8 RFC 4180, header
    ``schema_version,source_project,source_program,sequence,source_step,instruction,operand``.
  * Q import CSV: one file per program, UTF-16LE BOM, tab delimiter, CRLF,
    seven quoted fields, QCPU (Q mode) Q06UDV metadata and GX Works2 column names.
  * device-comment CSV: UTF-16LE BOM, tab delimiter, CRLF, two quoted fields,
    containing CP949 32-byte-safe display comments for Q06UDV-compatible devices.
  * comment-review CSV: UTF-8, preserving full source text for every comment,
    including target-device exclusions, at the GX Works2 CP949 byte boundary.
  * conversion-report JSON: source identity, program inventory, Ladder X/Y spelling
    audit, comment projection, hardware operand inventory, and post-conversion
    validation states.
  * invalid CSV: UTF-8 RFC 4180, one validation error per rejected record.
  * comment-review and invalid TXT: lossless UTF-8/CRLF viewing companions, never import inputs.

Dependencies: Python 3.12+ standard library only.
Run:
  $env:PYTHONPATH='app/src'; python -m converter \
    --input <input.gx3> --project-identifier <GX Works2 project name> \
    --intermediate-output <normalized.csv> --q-output <MAIN.csv> \
    --invalid-output <invalid.csv>

The source archive is read only. All requested outputs must share one directory
and are published by exclusive, no-clobber creation after input hashing and a
complete validation scan. Findings are retained in invalid CSV; Q CSV is emitted
only for a POU with no attributable blocker, except exact record-backed target
holds that use the explicitly review-only official dummy fallback. An
unattributable finding withholds every Q CSV. A failed later publish preserves
earlier partial outputs and reports them as an incomplete set that must not be
imported.
"""

from __future__ import annotations

from converter.site_profiles import (
    EXPECTED_CPU_MODULE_PARAMETERS,
    MULTI_POU_SOURCE_SHA256,
    MULTI_POU_PROGRAMS,
    MULTI_POU_EXPORT_SHA256,
    B2_A1_SOURCE_SHA256,
    B2_A1_PROGRAMS,
    B2_A1_EXPORT_SHA256,
    B2_A1_Q_REPLACEMENTS,
    MULTI_POU_COMPATIBILITY_PROFILES,
    SOURCE_PARAMETER_PROFILES,
    COMMENT_OVERRIDES_BY_SOURCE_SHA256,
)

import argparse
import functools
from collections import Counter
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys
import time
from typing import Callable, Mapping, Sequence
import xml.etree.ElementTree as element_tree
import zipfile
import zlib
from dataclasses import dataclass, replace

from converter.xy_mapping import MappingError, Profile, fixed_bounds, source_span

from converter.source_ir import (
    DecodeCoverage,
    NeutralSourceInstruction,
    SerializedOperation,
    classify_operand,
    coverage_from_rows,
)
from converter.target_layout import LAYOUT_RULE, apply_constant_root_layout
from converter.digit_contract import digit_operand_errors, digit_operand_form
from converter.review_text import (
    FindingContext, companion_text_path, comment_review_text, invalid_review_text,
)


MAX_ENTRIES = 10_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ENTRY_BYTES = 128 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000.0
NORMALIZED_HEADER = [
    "schema_version",
    "source_project",
    "source_program",
    "sequence",
    "source_step",
    "instruction",
    "operand",
]
INVALID_HEADER = ["sequence", "source_step", "instruction", "operand", "error", "finding_code", "mining_tracks", "candidate"]
Q_HEADER = ["Step No.", "Line Statement", "Instruction", "I/O(Device)", "Blank", "PI Statement", "Note"]
GXW3_EXPORT_HEADER = ["Step No.", "Line Statement", "Instruction", "I/O (Device)", "Blank", "P/I Statement", "Note"]
Q_METADATA = ["PLC Information:", "QCPU (Q mode) Q06UDV"]
COMMENT_HEADER = ["Device Name", "Comment"]
COMMENT_REVIEW_HEADER = [
    "Device Name",
    "Q Device Name",
    "Source Comment",
    "GX Works2 Comment",
    "Source CP949 Bytes",
    "GX Works2 CP949 Bytes",
    "Status",
]
PARTIAL_REVIEW_HEADER = [
    "Record Type",
    "Step No.",
    "Line Statement",
    "Instruction",
    "I/O(Device)",
    "Blank",
    "PI Statement",
    "Note",
    "Error",
    "Finding Code",
    "Mining Tracks",
    "Opaque Locator",
]
TARGET_UNSUPPORTED = "TARGET_UNSUPPORTED"
SOURCE_REBUILD_REQUIRED = "SOURCE_REBUILD_REQUIRED"
OBSERVED_BLOCK_ORACLE = "OBSERVED_BLOCK_ORACLE"


DIAGNOSTIC_FALLBACK_UNAVAILABLE = "DIAGNOSTIC_FALLBACK_UNAVAILABLE"
OFFICIAL_DUMMY_FALLBACK = "OFFICIAL_DUMMY_FALLBACK"
DIAGNOSTIC_FALLBACK_PUBLICATION = "REVIEW_ONLY"
DIAGNOSTIC_FALLBACK_RUNTIME_ACTION = "DO_NOT_DOWNLOAD_OR_RUN"
DIAGNOSTIC_FALLBACK_COMPATIBILITY = "A_PLC_COMPATIBILITY_REQUIRED"
MINING_REVIEW_STATUS = "MINING_REVIEW_ONLY"
MINING_REVIEW_SAFETY_GATE = "HARDWARE_MAP_REQUIRED"
MINING_REVIEW_TARGET_ACCEPTANCE = "ROUNDTRIP_REQUIRED"
MINING_REVIEW_RUNTIME_ACTION = "DO_NOT_DOWNLOAD_OR_RUN"
TYPED_MINING_REVIEW_STATUS = "TYPED_MINING_REVIEW_ONLY"
TYPED_MINING_FALLBACK = "SOURCE_MINING_DUMMY_FALLBACK"
TYPED_MINING_REVIEW_PUBLICATION = "MINING_REVIEW_ONLY"
TYPED_MINING_REVIEW_RUNTIME_ACTION = "DO_NOT_DOWNLOAD_OR_RUN"
SOURCE_RECOVERED_REVIEW_ONLY = "SOURCE_RECOVERED_REVIEW_ONLY"
STRUCTURAL_RESULT_DUMMY_FALLBACK = "STRUCTURAL_RESULT_DUMMY_FALLBACK"
MAX_Q_COMMENT_BYTES = 32
MAX_Q_NOTE_BYTES = 32
COMMENT_TARGET_EXCLUDED_STATUS = "TARGET_DEVICE_EXCLUDED"
Q06UDV_REJECTED_INSTRUCTIONS = frozenset({
    "ANS", "DDRVA", "DDSZR", "DPLSV", "HEXA", "INSTR", "IVCK", "IVDR",
    "LIMIT", "RS2", "TRD", "TWR", "ZRST",
})
# ANS is a timed annunciator action. It is expanded at target emission, never
# replaced by a dummy coil. Other rejected application instructions are actions
# whose review-only representation can be closed with ``OUT SM1255``.
Q06UDV_DUMMY_ACTION_INSTRUCTIONS = (Q06UDV_REJECTED_INSTRUCTIONS - {"ANS"}) | {"WTOB"}
# These instructions are permitted only in the exact forms backed by the
# independent Issue #39 phase 6 Q06UDV A/B lifecycle evidence.
Q06UDV_PHASE6_LIFECYCLE_INSTRUCTIONS = frozenset({"WTOB", "BTOW", "EVAL", "DINT"})
Q06UDV_OUTPUT_DESTINATION_INSTRUCTIONS = frozenset({"OUT", "SET", "RST", "PSET", "PRST"})
Q06UDV_STRUCTURAL_FALLBACK_INSTRUCTIONS = frozenset({
    "ANS", "MPS", "MRD", "MPP", "ANB", "ORB", "FOR", "NEXT", "MC", "MCR",
    "CJ", "SCJ", "JMP", "CALL", "RET", "IRET", "SRET", "END", "FEND",
    "GOEND", "BREAK",
})
Q06UDV_UNVERIFIED_LIFECYCLE_INSTRUCTIONS = frozenset({
    "+P", "AND$=", "AND$<>", "LD$=", "LD$<>", "SP.ECPRTCL", "SP.SOCCLOSE",
    "SP.SOCOPEN", "SP.SOCRCV", "SP.SOCSND", "ZRSTP",
    "FLT2INT", "FLT2INTP", "FLT2UINT", "FLT2UINTP",
    "FLT2DINT", "FLT2DINTP", "FLT2UDINT", "FLT2UDINTP",
    "BTOW", "EVAL",
})
Q06UDV_SOURCE_HOLD_ACTION_INSTRUCTIONS = frozenset({"+", "-", "D+", "MOV", "$+"})
Q06UDV_REJECTED_OPERANDS = frozenset({"SD10680.4", "SD10681.4", "SD10692.0", "SM8572"})
Q06UDV_SEMANTIC_HOLD_OPERANDS = frozenset({"SM8000", "SM4095", "SD10130"})
TARGET_EXCLUSION_EXACT_REASON = "Q06UDV exact-known unsupported operand or semantic HOLD"
# Exact FX5 CPU built-in socket status SD words.  Neighbors, indexes, grouped
# forms, literals, destinations, and unprojected rejected operands stay blocked.
_FX5_CPU_BUILTIN_SOCKET_STATUS_SD = {
    "SD10680": "SD1282",
    "SD10681": "SD1284",
    "SD10682": "SD1286",
}
_FX5_CPU_BUILTIN_SOCKET_STATUS_SD_BITS = frozenset("0123456789ABCDEF")
_FX5_CPU_BUILTIN_SOCKET_STATUS_SD_BIT_CONTACTS = frozenset({
    "LD", "LDI", "LDP", "LDF", "AND", "ANI", "ANDP", "ANDF", "OR", "ORI", "ORP", "ORF",
})
_FX5_CPU_BUILTIN_SOCKET_STATUS_SD_WORD_SOURCES = frozenset({"MOV", "MOVP"})
SOURCE_EXACT_SOCKET_OPERAND_COUNTS = {
    "SP.SOCOPEN": (4,),
    "SP.SOCCLOSE": (4,),
    "SP.SOCRCV": (5,),
    "SP.SOCSND": (5,),
}
SP_SOCKET_SOURCE_SIGNATURES = {
    "S_SOCOPEN": (("Ass", "A16", "A16a", "Aba"), "SP.SOCOPEN"),
    "SP_SOCOPEN": (("Ass", "A16", "A16a", "Aba"), "SP.SOCOPEN"),
    "S_SOCCLOSE": (("Ass", "A16", "A16a", "Aba"), "SP.SOCCLOSE"),
    "SP_SOCCLOSE": (("Ass", "A16", "A16a", "Aba"), "SP.SOCCLOSE"),
    "S_SOCRCV": (("Ass", "A16", "A16a", "A16", "Aba"), "SP.SOCRCV"),
    "SP_SOCRCV": (("Ass", "A16", "A16a", "A16", "Aba"), "SP.SOCRCV"),
    "S_SOCSND": (("Ass", "A16", "A16a", "A16", "Aba"), "SP.SOCSND"),
    "SP_SOCSND": (("Ass", "A16", "A16a", "A16", "Aba"), "SP.SOCSND"),
    "S_ECPRTCL": (("Ass", "A16", "A16", "A16a", "Aba"), "SP.ECPRTCL"),
    "SP_ECPRTCL": (("Ass", "A16", "A16", "A16a", "Aba"), "SP.ECPRTCL"),
}
SP_SOCKET_SOURCE_TAGS = {
    "SP.SOCOPEN": frozenset({
        ("String", "u0", '"u0"', "K_1", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "D", "Dots"),
    }),
    "SP.SOCCLOSE": frozenset({
        ("String", "u0", '"u0"', "K_1", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "D", "Dots"),
    }),
    "SP.SOCRCV": frozenset({
        ("String", "u0", '"u0"', "K_1", "D", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "D", "M"),
    }),
    "SP.SOCSND": frozenset({
        ("String", "u0", '"u0"', "K_1", "D", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "D", "M"),
        ("String", "U0", '"U0"', "K_1", "D", "D", "D", "Dots"),
    }),
    "SP.ECPRTCL": frozenset({
        ("String", "u0", '"u0"', "K_1", "K_1", "D", "M"),
    }),
}
MIL_PULSE_COMMAND_VARIANTS = {
    "BMOV": "BMOVP",
    "BMOVP": "BMOVP",
    "DEC": "DECP",
    "DECP": "DECP",
    "DSFR": "DSFRP",
    "DSFRP": "DSFRP",
    "FMOV": "FMOVP",
    "FMOVP": "FMOVP",
    "INC": "INCP",
    "INCP": "INCP",
    "DINCP": "DINCP",
    "MOV": "MOVP",
    "MOVP": "MOVP",
    "VAL": "VALP",
    "VALP": "VALP",
    # P002's official-signature authority distinguishes the A32 rising form
    # from MOVP, and P004 has a GX-authored Q IL golden for DMOVP.  Some MIL
    # rows retain the already-canonical marker, so preserve that exact marker
    # rather than treating it as an unmined pulse alias.
    "DMOVP": "DMOVP",
    "OUT": "PLS",
    "PLS": "PLS",
    # These serialized commands carry a pulse field but use their approved
    # mnemonic directly.  Listing them here is intentional: an unlisted
    # command must never be accepted merely because its base mnemonic exists.
    "FF": "FF",
    "S_SOCOPEN": "S_SOCOPEN",
    "SP_SOCOPEN": "SP_SOCOPEN",
    "S_SOCCLOSE": "S_SOCCLOSE",
    "SP_SOCCLOSE": "SP_SOCCLOSE",
    "S_SOCRCV": "S_SOCRCV",
    "SP_SOCRCV": "SP_SOCRCV",
    "S_SOCSND": "S_SOCSND",
    "SP_SOCSND": "SP_SOCSND",
    "S_ECPRTCL": "S_ECPRTCL",
    "SP_ECPRTCL": "SP_ECPRTCL",
}
SUPPORTED_SEQUENCES = (("LD", "OUT", "END"), ("LDI", "OUT", "END"))
MIL_OPERAND_COUNTS = {
    "": (0,), "*": (3,), "+": (2, 3), "-": (2, 3), "$MOV": (2,), "AND$=": (2,), "AND$<>": (2,), "ANB": (0,), "AND": (1,), "AND<": (2,), "AND<=": (2,), "AND<>": (2,), "AND=": (2,),
    "AND>=": (2,), "AND>": (2,), "ANDD<": (2,), "ANDD<=": (2,), "ANDD<>": (2,), "ANDD=": (2,), "ANDD>=": (2,), "ANDD>": (2,),
    "ANDF": (1,), "ANDFI": (1,), "ANDP": (1,), "ANDPI": (1,), "ANI": (1,), "ANIP": (1,), "BKRST": (2,), "BMOV": (3,), "BMOVP": (3,), "BSFL": (2,),
    "D*": (3,), "D+": (2, 3), "D-": (3,), "D/": (3,), "DATERD": (1,), "DATEWR": (1,), "DEC": (1,), "DECO": (3,), "DECP": (1,), "DINCP": (1,), "DMOV": (2,), "DMOVP": (2,), "DSFR": (1, 2), "DSFRP": (1, 2), "END": (0,), "FF": (1,), "FMOV": (3,), "FMOVP": (3,),
    "INC": (1,), "INCP": (1,), "INV": (0,), "LD": (1,), "LD$=": (2,), "LD$<>": (2,), "LD<": (2,), "LD<=": (2,), "LD<>": (2,), "LD>": (2,),
    "LD=": (2,), "LD>=": (2,), "LDD<": (2,), "LDD<=": (2,), "LDD<>": (2,), "LDD=": (2,), "LDD>=": (2,), "LDD>": (2,), "LDF": (1,), "LDI": (1,), "LDFI": (1,), "LDP": (1,), "LDPI": (1,), "MEP": (0,),
    "MOV": (2,), "MOVP": (2,), "MPP": (0,), "MPS": (0,), "MRD": (0,), "NOP": (0,), "OR": (1,),
    "ORB": (0,), "ORF": (1,), "ORFI": (1,), "ORI": (1,), "OR<": (2,), "OR<=": (2,), "OR<>": (2,), "OR=": (2,), "OR>=": (2,), "OR>": (2,), "ORP": (1,), "ORIP": (1,), "ORD<": (2,), "ORD<=": (2,), "ORD<>": (2,), "ORD=": (2,), "ORD>=": (2,), "ORD>": (2,), "OUT": (1, 2), "OUTH": (2,), "PLS": (1,),
    "RST": (1, 2), "SET": (1,), "TRD": (1,), "TWR": (1,), "ZRST": (2,),
    "ANS": (3,), "DDRVA": (4,), "DDSZR": (4,), "DPLSV": (3,),
    "WTOB": (3,), "LIMIT": (4,), "INSTR": (4,), "/": (3,),
    # Source forms confirmed by GX Works3 official export.  Target acceptance
    # remains a separate validation decision below.
    "WSUM": (3,), "BCD": (2,), "BIN": (2,),
    "SUM": (2,), "LEN": (2,), "FOR": (1,), "NEXT": (0,), "GOEND": (0,),
    "IVDR": (5,), "IVCK": (5,), "RS2": (5,), "HEXA": (3,),
    "VAL": (1, 3), "VALP": (3,), "VAL_U": (3,), "VALP_U": (3,),
    "DVAL": (3,), "DVALP": (3,), "DVAL_U": (3,), "DVALP_U": (3,), "$+": (2, 3),
    "SP.SOCOPEN": (4,), "SP.SOCCLOSE": (4,), "SP.SOCRCV": (5,), "SP.SOCSND": (5,), "SP.ECPRTCL": (5,),
    "ZRSTP": (2,), "+P": (3,),
    "FLT2INT": (2,), "FLT2INTP": (2,), "FLT2UINT": (2,), "FLT2UINTP": (2,),
    "FLT2DINT": (2,), "FLT2DINTP": (2,), "FLT2UDINT": (2,), "FLT2UDINTP": (2,),
    "BTOW": (3,), "EVAL": (2,), "SFL": (2,),
}
MIL_SUPPORTED_INSTRUCTIONS = frozenset(MIL_OPERAND_COUNTS)

LDDB_MINED_COMMANDS = {
    ("ANS", "none", ("A16", "A16", "Abl")): "ANS",
    ("OUTH__16", "none", ("Abl", "A16")): "OUTH",
    ("/", "none", ("A16s", "A16s", "A16sa")): "/",
    ("DIV", "none", ("A16s", "A16s", "A16sa")): "/",
    ("DDIV", "none", ("A32s", "A32s", "A32sa")): "D/",
    ("DMUL", "none", ("A32s", "A32s", "A32sa")): "D*",
    ("ADD", "none", ("A16s", "A16s", "A16s")): "+",
    ("DDRVA", "none", ("A32", "A32", "Aea", "Abl")): "DDRVA",
    ("DDSZR", "none", ("A32", "A32", "Aea", "Abl")): "DDSZR",
    ("DPLSV", "none", ("A32", "Aea", "Abl")): "DPLSV",
    ("WTOB", "none", ("A16", "A16", "A16")): "WTOB",
    ("BTOW", "none", ("A16", "A16", "A16")): "BTOW",
    ("EVAL", "none", ("Ass", "Ar32")): "EVAL",
    ("FLT2DINT", "none", ("Ar32", "A32s")): "FLT2DINT",
    ("LIMIT", "none", ("A16s", "A16s", "A16s", "A16s")): "LIMIT",
    ("INSTR", "none", ("Ass", "Ass", "A16", "A16")): "INSTR",
    ("SP.SOCOPEN", "rising", ("Ass", "A16", "A16a", "Aba")): "SP.SOCOPEN",
    ("SP.SOCCLOSE", "rising", ("Ass", "A16", "A16a", "Aba")): "SP.SOCCLOSE",
    ("SP.SOCRCV", "rising", ("Ass", "A16", "A16a", "A16", "Aba")): "SP.SOCRCV",
    ("SP.SOCSND", "rising", ("Ass", "A16", "A16a", "A16", "Aba")): "SP.SOCSND",
    ("SP.ECPRTCL", "rising", ("Ass", "A16", "A16", "A16a", "Aba")): "SP.ECPRTCL",
    ("ZRSTP", "rising", ("Aea", "Aea")): "ZRSTP",
    ("+P", "rising", ("A16s", "A16s", "A16s")): "+P",
    ("$MOV", "none", ("Ass", "Ass")): "$MOV",
    ("SUB", "none", ("A16s", "A16s", "A16s")): "-",
}
LDDB_MINED_MARKERS = frozenset(key[0] for key in LDDB_MINED_COMMANDS)
LDDB_EXACT_ARITIES = frozenset({
    ("+", "none", ("A16s", "A16s"), 2),
    ("-", "none", ("A16s", "A16s"), 2),
    ("D+", "none", ("A32s", "A32s"), 2),
})
LDDB_RAW_OPERAND_COUNTS = {"+": (3,), "-": (3,), "D+": (3,)}
MIL_EXACT_SOURCE_COMMANDS = {
    ("PLSV", "none", ("A32", "Aea", "Abl"), ("D", "Y", "Y")): "DPLSV",
    ("DSZR", "none", ("A32", "A32", "Aea", "Abl"), ("D", "D", "K_1", "M")): "DDSZR",
    ("DRVA", "none", ("A32", "A32", "Aea", "Abl"), ("D", "D", "K_1", "M")): "DDRVA",
    ("SET", "none", ("A16", "A16", "Abl"), ("T", "D", "F")): "ANS",
    ("SET", "none", ("A16", "A16", "Abl"), ("T", "K_1", "F")): "ANS",
    ("AddOpe", "none", ("A32s", "A32s"), ("K_2", "Z")): "D+",
    ("RST", "rising", ("Aea", "Aea"), ("M", "M")): "ZRSTP",
    ("DivOpe", "none", ("A16s", "A16s", "A16sa"), ("D", "K_1", "D")): "/",
    ("AddOpe", "rising", ("A16s", "A16s", "A16s"), ("D", "D", "D")): "+P",
}
MIL_SCALAR_TAGS = frozenset({"B", "D", "H_1", "K_1", "K_2", "L", "M", "R", "SfcS", "SD", "SM", "ST", "T", "X", "Y", "Z"})
MIL_GROUPED_Y_DEST_PROVENANCE = "mil:grouped-y-destination-mov"
# Complete source-only observation for the MIL Y/Ks MOV serialization.  This
# is intentionally not a generic Y grouped-device rule.
MIL_EXACT_Y_KS_MOV_OPERANDS = frozenset({
    (("k_device", (512, 2)), ("scalar", (100,))),
    (("k_device", (528, 2)), ("scalar", (110,))),
    (("k_device", (544, 2)), ("scalar", (120,))),
})
MIL_EXACT_Y_KS_MOV_PROVENANCE = "mil:exact-y-ks-mov"
LADDER_DATA = re.compile(
    r"^V1:4:1:1:1:1:(?P<contact>a|b):M:c:M:cb\{fg=fg\{dim=2x1:es=\["
    r"e\{s=ce\{op=ct\{op=#:ct=a:as=\[as\{vt=Abl\}\]\}:args=\["
    r"d\{s=#:a=(?P<ld>0|100):vt=nn\}\]\}:pos=0,0\}:"
    r"e\{s=ce\{op=cl\{op=#:ct=a:as=\[as\{vt=Abl\}\]\}:args=\["
    r"d\{s=#:a=(?P<out>1):vt=nn\}\]\}:pos=1,0\}\]\}\}$"
)
END_DATA = "V1:0:end{type=end:dim=1x1}"
MIL_EXACT_NOP_DATA = "V1:1:1:1:ms{el=[ma{k=@BE/NOP:ps=[p{k=NUM:v=#}]}]}"


class ConversionError(ValueError):
    """The input is outside Converter's validated vertical-slice boundary."""

    def __init__(
        self,
        message: str,
        *,
        finding_code: str = "INPUT_INVALID",
        mining_tracks: str = "",
        candidate: str = "",
    ) -> None:
        super().__init__(message)
        self.finding_code = finding_code
        self.mining_tracks = mining_tracks
        self.candidate = candidate


@functools.lru_cache(maxsize=1)
def _validated_upstream_contracts() -> object:
    """Load Converter-local upstream evidence only when a conversion path consumes it."""
    from converter.upstream_contracts import ContractValidationError, validate_vendored_contracts

    try:
        from converter.distribution import PUBLIC_RELEASE
        if PUBLIC_RELEASE:
            from converter.runtime_facts import validate_runtime_facts
            return validate_runtime_facts()
        return validate_vendored_contracts()
    except ContractValidationError as error:
        raise ConversionError(
            "Converter vendored upstream contract validation failed",
            finding_code="INPUT_INVALID",
            mining_tracks="P002|P004",
            candidate=f"contract_error={error}",
        ) from error


@functools.lru_cache(maxsize=1)
def _validated_public_upstream() -> object:
    """Require the approved local public wheel; never fall back to legacy code."""
    from converter.public_upstream import PublicUpstreamValidationError, validate_public_upstream

    try:
        return validate_public_upstream()
    except (PublicUpstreamValidationError, OSError) as error:
        raise ConversionError(
            "Converter published upstream validation failed",
            finding_code="INPUT_INVALID", mining_tracks="P002",
            candidate=f"public_upstream_error={error}",
        ) from error


def _require_source_contract() -> object:
    """Bind the decoder path to the validated Neutral IR profile, not to a mapping."""
    contracts = _validated_upstream_contracts()
    _validated_public_upstream()
    source = contracts.source_contract
    neutral = source.get("neutral_ir")
    if not isinstance(neutral, dict) or neutral.get("schema_name") != "plcman.gx3.neutral-ir":
        raise ConversionError("Converter source contract adapter is incompatible", finding_code="INPUT_INVALID", mining_tracks="P002")
    return contracts


def _require_target_contract() -> tuple[dict[str, object], ...]:
    """Bind encoder gates to FULL target grammar without auto-approving a mapping."""
    records = _validated_upstream_contracts().full_target_records
    if not records:
        raise ConversionError("Converter target contract adapter has no FULL oracle records", finding_code="INPUT_INVALID", mining_tracks="P004")
    return records


def _target_contract_fact(instruction: str) -> dict[str, object] | None:
    """Return the P004 FULL fact consumed by this encoder, when one exists."""
    return next((record for record in _require_target_contract() if record["identity"] == instruction), None)


def _p004_operand_form(value: str) -> str:
    if value in {"SD1255", "SD1282", "SD1284", "SD1286"}:
        return value
    if value.startswith('"') and value.endswith('"'):
        return "STRING"
    if re.fullmatch(r"K4M\d+", value):
        return "K4M"
    if re.fullmatch(r"K2Y[0-7]+", value):
        return "K2Y"
    indexed = re.fullmatch(
        r"(?P<indirect>@?)(?P<group>K\d+)?(?P<family>U\d+\\G|[A-Z]+)(?P<address>[0-9A-Fa-f]+)Z\d+",
        value,
    )
    if indexed is not None:
        family = indexed.group("family")
        address = indexed.group("address")
        group = indexed.group("group")
        if not indexed.group("indirect") and group == "K4" and family == "M" and address.isdecimal():
            return "K4M...Z"
        if indexed.group("indirect") or group:
            return f"INDEXED_MODIFIED_{family}"
        if family == "T" and address.isdecimal():
            return "TnZ"
        if family == "B":
            return "B...Z"
        if family in {"M", "D", "L", "R"} and address.isdecimal():
            return f"{family}...Z"
        if family in {"U1\\G", "U2\\G"} and address.isdecimal():
            return "U...\\G...Z"
        return f"INDEXED_{family}"
    if re.fullmatch(r"K[1-8]F\d+", value):
        return "KnF"
    digit_form = digit_operand_form(value)
    if digit_form is not None:
        return digit_form
    match = re.match(r"([A-Z$]+)", value)
    return match.group(1) if match else "UNKNOWN"


def _validate_target_contract_fact(record: Instruction) -> dict[str, object] | None:
    """Consume P004's observed FULL fact without treating it as exhaustive mapping approval."""
    if digit_operand_errors(record.instruction, (record.operand, *record.continuation_operands)):
        return None
    fact = _target_contract_fact(record.instruction)
    if fact is None:
        return None
    operand_count = (1 if record.operand else 0) + len(record.continuation_operands)
    scope = fact.get("operand_grammar", {}).get("scope")
    if scope != "observed A/B forms only" or not isinstance(fact.get("oracle_ids"), list):
        raise ConversionError("P004 FULL target fact boundary is incompatible", finding_code="INPUT_INVALID", mining_tracks="P004")
    # A matching count is directly covered by P004's oracle.  A different
    # count remains governed by Converter's own mapping/Q gates because the P004
    # contract explicitly declares its grammar as observed, not exhaustive.
    if operand_count not in fact["operand_count"]:
        return None
    operands = tuple(value for value in (record.operand, *record.continuation_operands) if value)
    if record.instruction in SOURCE_EXACT_SOCKET_OPERAND_COUNTS and (
        record.operand != "U0"
        or not _socket_source_has_q_lifecycle(replace(record, operand='"U0"'))
    ):
        return None
    if record.instruction == "SFL" and (
        len(operands) != 2
        or re.fullmatch(r"D[0-9]+", operands[0]) is None
        or re.fullmatch(r"K[0-9]+", operands[1]) is None
        or not 0 <= int(operands[1][1:]) <= 15
    ):
        return None
    if record.instruction in Q06UDV_PHASE6_LIFECYCLE_INSTRUCTIONS:
        # A family prefix alone also matches Dn.bit, indirect and malformed
        # spellings.  The new lifecycle proves plain D words and a K count.
        words = operands[:2]
        if len(words) != 2 or not all(re.fullmatch(r"D\d+", value) for value in words):
            return None
        if record.instruction in {"BTOW", "WTOB"} and (
            len(operands) != 3 or re.fullmatch(r"K\d+", operands[2]) is None
            or not 0 <= int(operands[2][1:]) <= 65535
        ):
            return None
    if record.instruction in {"MOV", "MOVP", "DMOV", "DMOVP"} and len(operands) == 2:
        from converter.bit_devices import parse_digit_designation
        from converter.kn_oracle import SD_PAIRS
        if operands[0] in {"SD1282", "SD1284", "SD1286"} and operands[1].startswith("K"):
            if record.instruction != "MOV" or operands not in SD_PAIRS:
                return None
        elif (operands[0] != "SD1255"
              and not (record.instruction == "MOV" and re.fullmatch(r"U[0-9]+\\G[0-9]+Z[0-9]+", operands[0]) and re.fullmatch(r"K4M[0-9]+", operands[1]))
              and not (record.instruction == "MOV" and re.fullmatch(r"K[1-4]F[0-9]+", operands[0]) and re.fullmatch(r"D[0-9]+", operands[1]))
              and any(re.fullmatch(r"K[1-8](SM|SB|[XYMLFB])[0-9A-F]+", value) for value in operands)):
            # A K literal or indexed group is not the plain grouped operand
            # selected by the oracle above. Never parse it as that group.
            sides = [i for i, value in enumerate(operands)
                     if re.fullmatch(r"K[1-8](SM|SB|[XYMLFB])[0-9A-F]+", value)]
            if len(sides) != 1:
                return None
            side = sides[0]
            group = parse_digit_designation(operands[side])
            if group is None or re.fullmatch(r"D[0-9]+", operands[1-side]) is None:
                return None
            width_groups = 8 if record.instruction.startswith("D") else 4
            if group.digits == width_groups and group.address % 16 not in {0, 1, 5, 9, 13}:
                return None
    observed_forms = fact["operand_grammar"]["observed_forms"]
    actual_form = [_p004_operand_form(value) for value in operands]
    if actual_form not in observed_forms:
        # Legacy K2Y/K4M names describe the same plain grouped families.
        # Keep old oracle spellings while accepting the new family fact only
        # after the width, address and counterpart gates above.
        aliases = {"K2Y": "KnY", "K4M": "KnM"}
        normalized = [aliases.get(value, value) for value in actual_form]
        if ("P004-Q06UDV-ISSUE49-KN" not in fact["oracle_ids"]
                or normalized not in observed_forms):
            return None
    return fact


class ConversionCancelled(RuntimeError):
    """A caller cancelled before any new output set was published."""


class PartialPublishError(ConversionError):
    """A no-clobber publish stopped after creating an incomplete output set."""


@dataclass(frozen=True)
class Instruction:
    sequence: int
    source_step: int
    instruction: str
    operand: str
    line_statement: str = ""
    pi_statement: str = ""
    note: str = ""
    continuation_operands: tuple[str, ...] = ()
    continuation_rows: tuple[tuple[str, ...], ...] = ()
    source_record_id: str = ""
    source_digest: str = ""
    source_locator: str = ""
    # Runtime-only provenance.  It deliberately never reaches CSV/JSON wire
    # formats, but prevents a Q X/Y rendering from being fed through the FX
    # source-spelling identity boundary a second time.
    q_io_projection_applied: bool = False
    # Runtime-only source semantics.  P002 authority binding overwrites the
    # locator/digest/record ID, so source-only target holds retain their
    # already-validated raw meaning in this separate immutable field.
    source_semantic_tags: tuple[str, ...] = ()
    # Target-only Step No. for post-audit layout serialization.  A value here
    # never changes source provenance or source validation identity.
    target_step: int | None = None
    # Source Note type is independent of its text.  A literal leading star is
    # not a substitute for the serialized st=s provenance.
    note_subtype: str = ""
    q_note_projection_applied: bool = False


def _neutral_source_instruction(
    record: Instruction, *, block_id: str = "", operation: SerializedOperation | None = None
) -> NeutralSourceInstruction:
    """Adapt the legacy CSV record after, never before, source meaning decode.

    The adapter does not consult a target profile.  Its operation and operand
    shape are intentionally value-independent so a new source identity with
    the same serialized grammar takes the same direct path.
    """
    operands = tuple(classify_operand(value) for value in (record.operand, *record.continuation_operands) if value)
    family = "statement" if record.line_statement else ("note" if record.note and not record.instruction else "instruction")
    operation = operation or SerializedOperation(family, record.instruction or "LADDER_NOTE", (record.instruction or "LADDER_NOTE",))
    return NeutralSourceInstruction(
        operation, operands, record.source_step, record.sequence, block_id,
        record.note, record.line_statement,
    )


def _coverage_for_records(
    records: tuple[Instruction, ...], opaque_blocks: tuple[OpaqueBlock, ...], *, direct_blocks: int,
    source_operations: tuple[SerializedOperation, ...] = (),
) -> DecodeCoverage:
    if source_operations and len(source_operations) != len(records):
        raise RuntimeError("typed source operation inventory differs from decoded rows")
    return coverage_from_rows(
        tuple(
            _neutral_source_instruction(record, operation=source_operations[index] if source_operations else None)
            for index, record in enumerate(records)
        ),
        direct_blocks=direct_blocks,
        opaque_blocks=len(opaque_blocks),
        opaque_rows=sum(block.row_count for block in opaque_blocks),
    )


@dataclass(frozen=True)
class OpaqueBlock:
    """One linked MIL block withheld from the Q import projection.

    ``row_count`` is the number of StepInfo rows reserved by this block.  It
    keeps the source sequence inventory exact even when its instruction text is
    unmined and therefore cannot be represented as an import row.
    """

    sequence: int
    source_step: int
    row_count: int
    record_id: str
    position: float
    error: str
    finding_code: str
    mining_tracks: str
    candidate: str

    @property
    def locator(self) -> str:
        return (
            f"record_id={self.record_id};pos={self.position};sequence_start={self.sequence};"
            f"row_count={self.row_count};source_step_start={self.source_step};{self.candidate}"
        ).rstrip(";")


@dataclass(frozen=True)
class MilParseResult:
    instructions: tuple[Instruction, ...]
    opaque_blocks: tuple[OpaqueBlock, ...]
    recovery_methods: tuple[tuple[str, int, int], ...] = ()
    direct_blocks: int = 0
    source_operations: tuple[SerializedOperation, ...] = ()

    @property
    def expected_rows(self) -> int:
        return len(self.instructions) + sum(block.row_count for block in self.opaque_blocks)


@dataclass(frozen=True)
class DeviceComment:
    device: str
    text: str
    target_text: str | None = None
    target_status: str | None = None


@dataclass(frozen=True)
class CommentProjection:
    device: str
    source_device: str
    source_text: str
    projected_text: str
    source_bytes: int | None
    projected_bytes: int | None
    status: str

    @property
    def truncated(self) -> bool:
        return "TRUNCATED" in self.status.split("|")


@dataclass(frozen=True)
class LadderBlock:
    """One source LadderBlocks row with raw ladder text represented by a digest."""

    record_id: str
    position: float
    blocktype: int
    data_sha256: str
    rowsize: int | None
    translated: int | None
    conversion_target: int | None


@dataclass(frozen=True)
class StepInfoBlock:
    """One linked StepInfo T_Block row."""

    position: float
    block_id: str
    step_size: int | None
    body_id: int | None


@dataclass(frozen=True)
class StepInfoRow:
    """One linked StepInfo T_Step row."""

    position: float
    element_id_macro_fb: int | None
    block_id: str
    mil_id: int | None
    step_size: int | None


@dataclass(frozen=True)
class StepInfo:
    """Every structural linkage row from the linked POU StepInfo database."""

    blocks: tuple[StepInfoBlock, ...]
    rows: tuple[StepInfoRow, ...]

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(block.block_id for block in self.blocks)

    @property
    def step_block_ids(self) -> tuple[str, ...]:
        return tuple(row.block_id for row in self.rows)


@dataclass(frozen=True)
class ProgramStructure:
    """Lossless-at-row-boundary GX3 ladder and StepInfo structural inventory."""

    blocks: tuple[LadderBlock, ...]
    step_info: StepInfo


@dataclass(frozen=True)
class SourceDiagnostic:
    """Inventory for source objects that were not emitted by this converter."""

    category: str
    finding_code: str
    message: str
    candidate: str
    blocks_program_output: bool = False
    scope: str = "component"


@dataclass(frozen=True)
class ParsedProject:
    source_project: str
    program_name: str
    instructions: tuple[Instruction, ...]
    structure: ProgramStructure
    comments: tuple[DeviceComment, ...] = ()
    findings: tuple[tuple[str, str, str, str, str, str, str, str], ...] = ()
    opaque_blocks: tuple[OpaqueBlock, ...] = ()
    recovery_methods: tuple[tuple[str, int, int], ...] = ()
    source_diagnostics: tuple[SourceDiagnostic, ...] = ()
    coverage: DecodeCoverage = DecodeCoverage()
    source_operations: tuple[SerializedOperation, ...] = ()


@dataclass(frozen=True)
class ParsedProgram:
    name: str
    pou_id: str
    instructions: tuple[Instruction, ...]
    structure: ProgramStructure
    findings: tuple[tuple[str, str, str, str, str, str, str, str], ...] = ()
    direct_decoded: bool = False
    opaque_blocks: tuple[OpaqueBlock, ...] = ()
    recovery_methods: tuple[tuple[str, int, int], ...] = ()
    coverage: DecodeCoverage = DecodeCoverage()
    source_operations: tuple[SerializedOperation, ...] = ()


@dataclass(frozen=True)
class ParsedMultiProject:
    source_project: str
    programs: tuple[ParsedProgram, ...]
    comments: tuple[DeviceComment, ...] = ()
    source_sha256: str = ""
    source_diagnostics: tuple[SourceDiagnostic, ...] = ()

    @property
    def program_name(self) -> str:
        return ", ".join(program.name for program in self.programs)

    @property
    def instructions(self) -> tuple[Instruction, ...]:
        return tuple(record for program in self.programs for record in program.instructions)


def _neutral_ir_semantics(pou: dict[str, object]) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
    """Convert decoded Neutral IR records to a strict, target-independent semantic sequence."""
    records = pou.get("records")
    if not isinstance(records, list):
        raise ConversionError("P002 Neutral IR POU records are invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
    continuations: dict[str, list[str]] = {}
    decoded: list[tuple[int, str, tuple[str, ...]]] = []
    bases: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ConversionError("P002 Neutral IR record is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
        kind = record.get("kind")
        status = record.get("status")
        if kind == "opaque" or status == "unknown":
            continue
        if status != "decoded" or kind not in {"instruction", "continuation", "statement", "note"}:
            raise ConversionError("P002 Neutral IR record enum is unsupported", finding_code="INPUT_INVALID", mining_tracks="P002")
        operands = record.get("operands")
        if not isinstance(operands, list) or any(not isinstance(item, dict) or not isinstance(item.get("raw_token"), str) for item in operands):
            raise ConversionError("P002 Neutral IR operand is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
        rendered = [item["raw_token"] for item in operands]
        if kind == "continuation":
            target = record.get("continues_record_id")
            if not isinstance(target, str) or target not in bases or len(rendered) != 1:
                raise ConversionError("P002 Neutral IR continuation relation is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
            continuations.setdefault(target, []).extend(rendered)
            index = bases[target]
            step, opcode, prior = decoded[index]
            decoded[index] = (step, opcode, (*prior, *rendered))
            continue
        if kind in {"statement", "note"}:
            # Source text has no opcode/operand mapping. Its exact text remains
            # in Neutral IR and must never be guessed into a Q instruction.
            continue
        record_id = record.get("record_id")
        opcode = record.get("opcode")
        step = record.get("step")
        if not isinstance(record_id, str) or not isinstance(opcode, str) or not isinstance(step, int):
            raise ConversionError("P002 Neutral IR instruction is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
        bases[record_id] = len(decoded)
        decoded.append((step, _official_source_opcode(opcode), tuple(rendered)))
    return tuple(decoded)


def _neutral_ir_instructions(
    pou: dict[str, object],
) -> tuple[tuple[Instruction, ...], tuple[SerializedOperation, ...]]:
    """Promote a fully decoded P002 POU without dropping statement or note rows."""
    records = pou.get("records")
    if not isinstance(records, list):
        raise ConversionError("P002 Neutral IR POU records are invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
    decoded: list[Instruction] = []
    operations: list[SerializedOperation] = []
    bases: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "decoded":
            raise ConversionError("P002 Neutral IR promotion is not fully decoded", finding_code="INPUT_INVALID", mining_tracks="P002")
        kind = record.get("kind")
        step = record.get("step")
        operands = record.get("operands")
        if (
            kind not in {"instruction", "continuation", "statement", "note"}
            or not isinstance(step, int)
            or not isinstance(operands, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("raw_token"), str) for item in operands)
        ):
            raise ConversionError("P002 Neutral IR promoted record is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
        rendered = tuple(str(item["raw_token"]) for item in operands)
        if kind == "continuation":
            target = record.get("continues_record_id")
            if not isinstance(target, str) or target not in bases or len(rendered) != 1:
                raise ConversionError("P002 Neutral IR continuation relation is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
            index = bases[target]
            decoded[index] = replace(
                decoded[index],
                continuation_operands=(*decoded[index].continuation_operands, rendered[0]),
            )
            continue
        record_id = record.get("record_id")
        if not isinstance(record_id, str):
            raise ConversionError("P002 Neutral IR promoted record ID is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
        provenance = record.get("provenance")
        digest = provenance.get("source_digest") if isinstance(provenance, dict) else None
        locator = provenance.get("source_locator") if isinstance(provenance, dict) else None
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(locator, str)
            or not locator
        ):
            raise ConversionError(
                "P002 Neutral IR promoted record provenance is invalid",
                finding_code="INPUT_INVALID", mining_tracks="P002",
            )
        if kind == "instruction":
            opcode = record.get("opcode")
            if not isinstance(opcode, str) or not opcode:
                raise ConversionError("P002 Neutral IR promoted opcode is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
            instruction = _official_source_opcode(opcode)
            row = Instruction(
                len(decoded), step, instruction, rendered[0] if rendered else "",
                continuation_operands=rendered[1:],
                source_record_id=record_id,
                source_digest=digest,
                source_locator=locator,
            )
            operation = SerializedOperation("p002_neutral_ir", instruction, (instruction,), variant="decoded")
        else:
            text = record.get("text")
            if not isinstance(text, str):
                raise ConversionError("P002 Neutral IR promoted text is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
            row = (
                Instruction(
                    len(decoded), step, "", "", line_statement=text,
                    source_record_id=record_id, source_digest=digest, source_locator=locator,
                )
                if kind == "statement"
                else Instruction(
                    len(decoded), step, "", "", note=text,
                    source_record_id=record_id, source_digest=digest, source_locator=locator,
                )
            )
            descriptors = (kind.upper(),)
            if kind == "note":
                relation_evidence = provenance.get("relation_evidence")
                if not isinstance(relation_evidence, list) or any(
                    not isinstance(item, str) for item in relation_evidence
                ):
                    raise ConversionError(
                        "P002 Neutral IR note provenance is invalid",
                        finding_code="INPUT_INVALID", mining_tracks="P002",
                    )
                subtype_markers = [
                    item for item in relation_evidence
                    if item.startswith("lddb_inline_note_st=")
                ]
                if len(subtype_markers) != 1 or subtype_markers[0] not in {
                    "lddb_inline_note_st=i", "lddb_inline_note_st=s",
                }:
                    raise ConversionError(
                        "P002 Neutral IR note subtype provenance is invalid",
                        finding_code="INPUT_INVALID", mining_tracks="P002",
                    )
                descriptors = ("NOTE", f"st={subtype_markers[0].rsplit('=', 1)[1]}")
                row = replace(row, note_subtype=descriptors[1][-1])
            operation = SerializedOperation(
                f"p002_neutral_ir_{kind}", kind.upper(), descriptors, variant="decoded"
            )
        bases[record_id] = len(decoded)
        decoded.append(row)
        operations.append(operation)
    return tuple(decoded), tuple(operations)


def _official_source_opcode(opcode: str) -> str:
    """Normalize equivalent decoder spellings for source-contract comparison."""
    return {"MUL": "*", "ANIP": "ANDPI", "LDIF": "LDFI", "ORIP": "ORPI"}.get(opcode, opcode)


def _neutral_ir_diagnostics(value: dict[str, object]) -> tuple[SourceDiagnostic, ...]:
    diagnostics: list[SourceDiagnostic] = []
    finding_groups: list[tuple[str, object]] = [("project", value.get("findings"))]
    for pou in value.get("pous", []):
        if isinstance(pou, dict):
            finding_groups.append(("pou", pou.get("findings")))
    for scope, findings in finding_groups:
        if not isinstance(findings, list):
            raise ConversionError("P002 Neutral IR finding inventory is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("finding_code") not in {
                "MINING_REQUIRED", "SOURCE_GLOBAL_ORDER_NOT_SERIALIZED",
            }:
                raise ConversionError("P002 Neutral IR finding enum is unsupported", finding_code="INPUT_INVALID", mining_tracks="P002")
            digest = finding.get("digest")
            object_kind = finding.get("object_kind")
            locator = finding.get("locator")
            reason = finding.get("reason")
            if (
                not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(object_kind, str)
                or not object_kind
                or not isinstance(locator, str)
                or not locator
                or not isinstance(reason, str)
                or not reason
            ):
                raise ConversionError("P002 Neutral IR finding evidence is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
            upstream_code = str(finding["finding_code"])
            component_only = scope == "project" and object_kind in {"label", "comment"}
            diagnostics.append(SourceDiagnostic(
                object_kind,
                "SOURCE_COMPONENT_NOT_CONVERTED" if component_only else upstream_code,
                reason,
                (
                    f"scope={scope};object_kind={object_kind};"
                    f"source_locator={locator};source_digest={digest}"
                ),
                blocks_program_output=scope == "project" and not component_only,
                scope=scope,
            ))
    return tuple(diagnostics)


def _source_diagnostic_findings(
    diagnostics: tuple[SourceDiagnostic, ...],
) -> list[list[str]]:
    """Promote project-wide unresolved semantics to global publish blockers."""
    return [
        ["", "", "", "", item.message, item.finding_code, "P002", item.candidate]
        for item in diagnostics
        if item.blocks_program_output
    ]


def _p002_typed_mining_finding_row(
    finding: dict[str, object],
    records: list[object],
    program_name: str,
    digest: str,
    locator: str,
    reason: str,
) -> tuple[str, str, str, str, str, str, str, str] | None:
    """Preserve optional P002 typed evidence without inferring missing semantics."""
    evidence = finding.get("typed_fallback")
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise ConversionError("P002 typed mining evidence is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
    record_id = evidence.get("source_record_id")
    fallback_kind = evidence.get("fallback_kind")
    topology_role = evidence.get("topology_role")
    operand_type = evidence.get("operand_type")
    operand_index = evidence.get("operand_index")
    claimed_instruction = evidence.get("instruction")
    claimed_operands = evidence.get("operands")
    if (
        not isinstance(record_id, str)
        or not record_id
        or evidence.get("source_row_verified") is not True
        or evidence.get("alignment") != "exact"
        or fallback_kind not in {"BIT_CONTACT", "BIT_DESTINATION", "WORD_DEVICE"}
        or topology_role not in {"contact", "destination", "word_source"}
        or operand_type not in {"bit", "word"}
        or not isinstance(operand_index, int)
        or operand_index < 0
        or not isinstance(claimed_instruction, str)
        or not claimed_instruction
        or not isinstance(claimed_operands, list)
        or not claimed_operands
        or any(not isinstance(value, str) or not value for value in claimed_operands)
    ):
        raise ConversionError("P002 typed mining evidence is incomplete", finding_code="INPUT_INVALID", mining_tracks="P002")
    matching = [
        record for record in records
        if isinstance(record, dict) and record.get("record_id") == record_id and record.get("status") == "decoded"
    ]
    if len(matching) != 1:
        raise ConversionError("P002 typed mining source row is not unique", finding_code="INPUT_INVALID", mining_tracks="P002")
    record = matching[0]
    sequence = record.get("sequence")
    step = record.get("step")
    opcode = record.get("opcode")
    operands = record.get("operands")
    provenance = record.get("provenance")
    if (
        not isinstance(sequence, int)
        or not isinstance(step, int)
        or not isinstance(opcode, str)
        or not isinstance(operands, list)
        or not isinstance(provenance, dict)
        or provenance.get("source_digest") != digest
        or provenance.get("source_locator") != locator
        or record.get("continues_record_id") is not None
    ):
        raise ConversionError(
            "P002 typed mining source provenance is not exactly bound",
            finding_code="INPUT_INVALID", mining_tracks="P002",
        )
    rendered = [item.get("raw_token") for item in operands if isinstance(item, dict)]
    instruction = _official_source_opcode(opcode)
    if (
        len(rendered) != len(operands)
        or any(not isinstance(value, str) or not value for value in rendered)
        or claimed_instruction != instruction
        or claimed_operands != rendered
        or operand_index >= len(rendered)
    ):
        raise ConversionError("P002 typed mining evidence differs from source row", finding_code="INPUT_INVALID", mining_tracks="P002")
    return (
        str(sequence), str(step), instruction, rendered[0], reason, "MINING_REQUIRED", "P002",
        (
            f"program={program_name};source_record_id={record_id};source_locator={locator};source_digest={digest};"
            f"operands={' '.join(rendered)};source_row_verified=true;alignment=exact;"
            f"typed_mining_fallback={fallback_kind};topology_role={topology_role};"
            f"operand_type={operand_type};operand_index={operand_index}"
        ),
    )


def _neutral_ir_pou_finding_rows(
    pou: dict[str, object], program_name: str
) -> tuple[tuple[str, str, str, str, str, str, str, str], ...]:
    """Preserve each P002 POU gap as an attributable, blocking finding."""
    records = pou.get("records")
    findings = pou.get("findings")
    if not isinstance(records, list) or not isinstance(findings, list):
        raise ConversionError(
            "P002 Neutral IR POU finding inventory is invalid",
            finding_code="INPUT_INVALID", mining_tracks="P002",
        )
    unknown_by_locator: dict[tuple[str, str], list[dict[str, object]]] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "unknown":
            continue
        provenance = record.get("provenance")
        digest = provenance.get("source_digest") if isinstance(provenance, dict) else None
        locator = provenance.get("source_locator") if isinstance(provenance, dict) else None
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(locator, str)
            or not locator
        ):
            raise ConversionError(
                "P002 Neutral IR unknown record provenance is invalid",
                finding_code="INPUT_INVALID", mining_tracks="P002",
            )
        unknown_by_locator.setdefault((digest, locator), []).append(record)

    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    matched: set[tuple[str, str]] = set()
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("finding_code") != "MINING_REQUIRED":
            raise ConversionError(
                "P002 Neutral IR finding enum is unsupported",
                finding_code="INPUT_INVALID", mining_tracks="P002",
            )
        digest = finding.get("digest")
        locator = finding.get("locator")
        reason = finding.get("reason")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(locator, str)
            or not locator
            or not isinstance(reason, str)
            or not reason
        ):
            raise ConversionError(
                "P002 Neutral IR POU finding locator is invalid",
                finding_code="INPUT_INVALID", mining_tracks="P002",
            )
        finding_key = (digest, locator)
        matched_records = unknown_by_locator.get(finding_key, [])
        if finding_key in matched:
            raise ConversionError(
                "P002 Neutral IR POU finding relation is ambiguous",
                finding_code="INPUT_INVALID", mining_tracks="P002",
            )
        typed_row = _p002_typed_mining_finding_row(
            finding, records, program_name, digest, locator, reason,
        )
        if typed_row is not None:
            matched.add(finding_key)
            rows.append(typed_row)
            continue
        if not matched_records:
            matched.add(finding_key)
            rows.append((
                "", "", "", "", reason, "MINING_REQUIRED", "P002",
                f"program={program_name};source_locator={locator};source_digest={digest}",
            ))
            continue
        record_ids = [record.get("record_id") for record in matched_records]
        sequences = [record.get("sequence") for record in matched_records]
        source_steps = [record.get("step") for record in matched_records]
        if any(not isinstance(record_id, str) for record_id in record_ids) or any(
            not isinstance(sequence, int) for sequence in sequences
        ) or any(not isinstance(source_step, int) for source_step in source_steps):
            raise ConversionError(
                "P002 Neutral IR POU finding relation is invalid",
                finding_code="INPUT_INVALID", mining_tracks="P002",
            )
        matched.add(finding_key)
        record_locator = ",".join(sorted(record_ids))
        rows.append((
            str(min(sequences)), str(min(source_steps)), "", "", reason, "MINING_REQUIRED", "P002",
            (
                f"program={program_name};source_record_id={record_locator};"
                f"source_locator={locator};source_digest={digest}"
            ),
        ))
    return tuple(rows)


def _merge_p002_source_findings(
    legacy: tuple[tuple[str, ...], ...],
    upstream: tuple[tuple[str, ...], ...],
    decoded_record_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[str, ...], ...]:
    """Let P002 own a source record without publishing the Converter fallback twice."""
    authoritative_record_ids: set[str] = set(decoded_record_ids)
    for finding in upstream:
        match = re.search(r"(?:^|;)source_record_id=([^;]+)", finding[7])
        if match is not None:
            for record_id in match.group(1).split(","):
                authoritative_record_ids.add(record_id)
                base = re.fullmatch(r"(.+):opaque:\d+", record_id)
                if base is not None:
                    authoritative_record_ids.add(base.group(1))
    retained = []
    for finding in legacy:
        match = re.search(r"(?:^|;)record_id=([^;]+)", finding[7])
        if (
            finding[5] == "MINING_REQUIRED"
            and match is not None
            and match.group(1) in authoritative_record_ids
        ):
            continue
        retained.append(finding)
    return (*retained, *upstream)


def _neutral_ir_decoded_source_record_ids(records: list[object]) -> frozenset[str]:
    """Map decoded row IDs back to the exact UUID owned by the source DB block."""
    owned: set[str] = set()
    uuid_pattern = re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )
    locator_pattern = re.compile(r'(?:LadderBlocks|MIL)\[id="([^"]+)"\]')
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "decoded":
            continue
        provenance = record.get("provenance")
        locator = provenance.get("source_locator") if isinstance(provenance, dict) else None
        match = locator_pattern.search(locator) if isinstance(locator, str) else None
        if match is not None and uuid_pattern.fullmatch(match.group(1)):
            owned.add(match.group(1))
            continue
        record_id = record.get("record_id")
        if isinstance(record_id, str):
            base = record_id.split(":", 1)[0]
            if uuid_pattern.fullmatch(base):
                owned.add(base)
    return frozenset(owned)


def _bind_neutral_ir(
    parsed: ParsedProject | ParsedMultiProject, value: dict[str, object]
) -> ParsedProject | ParsedMultiProject:
    """Bind P002 Neutral IR to Converter's model and reject decoded semantic drift."""
    pous = value.get("pous")
    if not isinstance(pous, list) or any(not isinstance(pou, dict) for pou in pous):
        raise ConversionError("P002 Neutral IR POU inventory is invalid", finding_code="INPUT_INVALID", mining_tracks="P002")
    legacy_programs = parsed.programs if isinstance(parsed, ParsedMultiProject) else (
        ParsedProgram(
            parsed.program_name, "", parsed.instructions, parsed.structure,
            findings=parsed.findings, opaque_blocks=parsed.opaque_blocks,
        ),
    )
    legacy_by_name = {program.name.casefold(): program for program in legacy_programs}
    neutral_names = [pou.get("name", "").casefold() for pou in pous]
    if len(pous) != len(legacy_programs) or set(neutral_names) != set(legacy_by_name):
        raise ConversionError("P002 Neutral IR POU inventory differs from Converter", finding_code="INPUT_INVALID", mining_tracks="P002")
    p002_findings_by_name: dict[str, tuple[tuple[str, str, str, str, str, str, str, str], ...]] = {}
    p002_decoded_record_ids_by_name: dict[str, frozenset[str]] = {}
    p002_promotions_by_name: dict[
        str, tuple[tuple[Instruction, ...], tuple[SerializedOperation, ...], int]
    ] = {}
    p002_provenance_by_name: dict[str, tuple[Instruction, ...]] = {}
    for pou in pous:
        name = pou.get("name")
        if not isinstance(name, str) or name.casefold() not in legacy_by_name:
            raise ConversionError("P002 Neutral IR POU identity differs from Converter", finding_code="INPUT_INVALID", mining_tracks="P002")
        neutral = _neutral_ir_semantics(pou)
        legacy_program = legacy_by_name[name.casefold()]
        legacy = tuple(
            (record.source_step, _official_source_opcode(record.instruction), tuple(value for value in (record.operand, *record.continuation_operands) if value))
            for record in legacy_program.instructions
        )
        legacy_starts = sorted({
            *(record.source_step for record in legacy_program.instructions),
            *(block.source_step for block in legacy_program.opaque_blocks),
        })
        opaque_spans = tuple(
            (
                block,
                next((start for start in legacy_starts if start > block.source_step), None),
            )
            for block in legacy_program.opaque_blocks
        )
        opaque_semantics_used: dict[str, int] = {}
        neutral_records = pou.get("records")
        neutral_row_inventory = (
            sum(record.get("kind") != "continuation" for record in neutral_records if isinstance(record, dict))
            if isinstance(neutral_records, list)
            else -1
        )
        legacy_row_inventory = len(legacy_program.instructions) + sum(block.row_count for block in legacy_program.opaque_blocks)
        pou_findings = pou.get("findings")
        if (
            not isinstance(neutral_records, list)
            or neutral_row_inventory > legacy_row_inventory
            or (
                neutral_row_inventory < legacy_row_inventory
                and (not isinstance(pou_findings, list) or not pou_findings)
            )
        ):
            raise ConversionError(
                "P002 Neutral IR row inventory differs from Converter",
                finding_code="INPUT_INVALID", mining_tracks="P002",
                candidate=f"program_sha256={hashlib.sha256(name.encode('utf-8')).hexdigest()};neutral_rows={neutral_row_inventory};converter_rows={legacy_row_inventory}",
            )
        cursor = 0
        for semantic in neutral:
            try:
                cursor = legacy.index(semantic, cursor) + 1
            except ValueError as error:
                covering = next((
                    block
                    for block, upper in opaque_spans
                    if block.source_step <= semantic[0]
                    and (upper is None or semantic[0] < upper)
                    and opaque_semantics_used.get(block.record_id, 0) < block.row_count
                ), None)
                if covering is not None:
                    # P002 may decode a structure that the legacy Converter parser
                    # still inventories as an attributed opaque range. Keep
                    # that range partial instead of treating the upstream
                    # decoder's additional knowledge as contradictory output.
                    opaque_semantics_used[covering.record_id] = opaque_semantics_used.get(covering.record_id, 0) + 1
                    continue
                raise ConversionError(
                    "P002 Neutral IR decoded semantics differ from Converter",
                    finding_code="INPUT_INVALID", mining_tracks="P002",
                    candidate=f"program_sha256={hashlib.sha256(name.encode('utf-8')).hexdigest()};semantic_sha256={_canonical_sha256(semantic)}",
                ) from error
        p002_findings = _neutral_ir_pou_finding_rows(pou, name)
        decoded_record_ids = _neutral_ir_decoded_source_record_ids(neutral_records)
        p002_findings_by_name[name.casefold()] = p002_findings
        p002_decoded_record_ids_by_name[name.casefold()] = decoded_record_ids
        if not p002_findings and neutral_row_inventory == legacy_row_inventory:
            promoted, _operations = _neutral_ir_instructions(pou)
            if len(promoted) == len(legacy_program.instructions):
                p002_provenance_by_name[name.casefold()] = promoted
        if (
            legacy_program.opaque_blocks
            and not p002_findings
            and neutral_row_inventory == legacy_row_inventory
        ):
            promoted, promoted_operations = _neutral_ir_instructions(pou)
            if len(promoted) != legacy_row_inventory:
                raise ConversionError(
                    "P002 Neutral IR promoted row inventory differs from Converter",
                    finding_code="INPUT_INVALID", mining_tracks="P002",
                )
            p002_promotions_by_name[name.casefold()] = (
                promoted, promoted_operations, len(decoded_record_ids)
            )

    def promote_program(program: ParsedProgram) -> ParsedProgram:
        key = program.name.casefold()
        promotion = p002_promotions_by_name.get(key)
        if promotion is None:
            provenance_records = p002_provenance_by_name.get(key)
            records = program.instructions
            if provenance_records is not None:
                records = tuple(
                    replace(
                        record,
                        source_record_id=provenance.source_record_id,
                        source_digest=provenance.source_digest,
                        source_locator=provenance.source_locator,
                    )
                    for record, provenance in zip(records, provenance_records, strict=True)
                )
            return replace(
                program,
                instructions=records,
                findings=_merge_p002_source_findings(
                    program.findings, p002_findings_by_name[key],
                    p002_decoded_record_ids_by_name[key],
                ),
            )
        records, operations, block_count = promotion
        retained = tuple(finding for finding in program.findings if finding[5] != "MINING_REQUIRED")
        return replace(
            program,
            instructions=records,
            findings=_merge_p002_source_findings(retained, p002_findings_by_name[key]),
            direct_decoded=True,
            opaque_blocks=(),
            recovery_methods=(*program.recovery_methods, ("p002_neutral_ir", block_count, len(records))),
            coverage=_coverage_for_records(
                records, (), direct_blocks=block_count, source_operations=operations,
            ),
            source_operations=operations,
        )

    if isinstance(parsed, ParsedMultiProject):
        parsed = replace(parsed, programs=tuple(promote_program(program) for program in parsed.programs))
    else:
        key = parsed.program_name.casefold()
        promotion = p002_promotions_by_name.get(key)
        if promotion is None:
            provenance_records = p002_provenance_by_name.get(key)
            records = parsed.instructions
            if provenance_records is not None:
                records = tuple(
                    replace(
                        record,
                        source_record_id=provenance.source_record_id,
                        source_digest=provenance.source_digest,
                        source_locator=provenance.source_locator,
                    )
                    for record, provenance in zip(records, provenance_records, strict=True)
                )
            parsed = replace(
                parsed,
                instructions=records,
                findings=_merge_p002_source_findings(
                    parsed.findings, p002_findings_by_name[key],
                    p002_decoded_record_ids_by_name[key],
                ),
            )
        else:
            records, operations, block_count = promotion
            retained = tuple(finding for finding in parsed.findings if finding[5] != "MINING_REQUIRED")
            parsed = replace(
                parsed,
                instructions=records,
                findings=_merge_p002_source_findings(retained, p002_findings_by_name[key]),
                opaque_blocks=(),
                recovery_methods=(*parsed.recovery_methods, ("p002_neutral_ir", block_count, len(records))),
                coverage=_coverage_for_records(
                    records, (), direct_blocks=block_count, source_operations=operations,
                ),
                source_operations=operations,
            )
    diagnostics = _neutral_ir_diagnostics(value)
    return replace(parsed, source_diagnostics=(*parsed.source_diagnostics, *diagnostics))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _handle_final_path(descriptor: int) -> str:
    """Return the kernel-resolved path of an open descriptor."""
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if length == 0 or length >= len(buffer):
            raise OSError("could not resolve open handle path")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(os.path.abspath(value))
    return os.path.normcase(os.path.abspath(os.readlink(f"/proc/self/fd/{descriptor}")))


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return stat_result.st_dev, stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns


def _object_identity(stat_result: os.stat_result) -> tuple[int, int]:
    return stat_result.st_dev, stat_result.st_ino


@dataclass
class InputSnapshot:
    """One no-follow input handle whose exact bytes drive parse and publication."""

    descriptor: int
    path: Path
    body: bytes
    sha256: str
    identity: tuple[int, int, int, int]
    final_path: str

    def verify(self) -> None:
        if _stat_identity(os.fstat(self.descriptor)) != self.identity:
            raise RuntimeError("input handle identity changed during conversion")
        if _handle_final_path(self.descriptor) != self.final_path:
            raise RuntimeError("input handle final path changed during conversion")
        path_stat = os.stat(self.path, follow_symlinks=False)
        if _stat_identity(path_stat) != self.identity or os.path.normcase(os.path.abspath(self.path.resolve())) != self.final_path:
            raise RuntimeError("input path no longer identifies the parsed snapshot")

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


def _open_input_snapshot(path: Path) -> InputSnapshot:
    """Open once, read within the cap, and retain the handle through publish."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        identity = _stat_identity(os.fstat(descriptor))
        final_path = _handle_final_path(descriptor)
        intended = os.path.normcase(os.path.abspath(path.resolve()))
        if final_path != intended:
            raise ConversionError("--input handle final path differs from the validated path")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise ConversionError("--input GX3 archive exceeds byte limit")
            chunks.append(block)
        body = b"".join(chunks)
        snapshot = InputSnapshot(descriptor, path, body, hashlib.sha256(body).hexdigest(), identity, final_path)
        snapshot.verify()
        return snapshot
    except Exception:
        os.close(descriptor)
        raise


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and bool(is_junction()))


def _reject_link_like_ancestors(path: Path, label: str) -> Path:
    current = path.absolute()
    while True:
        if current.exists() and _is_link_like(current):
            raise ConversionError(f"{label} must not traverse a link-like path: {current}")
        if current.parent == current:
            return path.absolute()
        current = current.parent


def _validate_output_paths(input_path: Path, outputs: tuple[Path, ...]) -> tuple[Path, ...]:
    source = _reject_link_like_ancestors(input_path, "--input")
    if not source.is_file() or source.suffix.lower() != ".gx3":
        raise ConversionError("--input must be a regular .gx3 file")
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ConversionError("--input GX3 archive exceeds byte limit")
    resolved_source = source.resolve()
    validated: list[Path] = []
    for output in outputs:
        candidate = _reject_link_like_ancestors(output, "output")
        if candidate.exists() and _is_link_like(candidate):
            raise ConversionError(f"output must not be link-like: {candidate}")
        if candidate.exists() and not candidate.is_file():
            raise ConversionError(f"output must be a regular file path: {candidate}")
        if candidate.resolve() == resolved_source:
            raise ConversionError("an output path must not replace the source .gx3")
        validated.append(candidate)
    if len({path.resolve() for path in validated}) != len(validated):
        raise ConversionError("intermediate, Q, and invalid output paths must be distinct")
    folded_names = [path.name.casefold() for path in validated]
    if len(set(folded_names)) != len(folded_names):
        raise ConversionError("intermediate, Q, and invalid output filenames collide case-insensitively")
    if len({path.parent.resolve() for path in validated}) != 1:
        raise ConversionError("intermediate, Q, and invalid outputs must share one directory")
    if validated[0].parent.resolve() == resolved_source.parent:
        raise ConversionError("output directory must not be the input .gx3 parent directory")
    existing = [path for path in validated if path.exists()]
    if existing:
        raise ConversionError(f"existing output is not overwritten: {existing[0]}")
    return tuple(validated)


def _validate_zip_name(name: str) -> None:
    trimmed = name.rstrip("/")
    posix = PurePosixPath(trimmed)
    windows = PureWindowsPath(trimmed)
    if (
        not trimmed
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or any(part in ("", ".", "..") for part in posix.parts)
        or any(part in ("", ".", "..") for part in windows.parts)
    ):
        raise ConversionError(f"unsafe GX3 archive entry name: {name!r}")


def _safe_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ConversionError(f"GX3 archive entry limit exceeded: {len(infos)} > {MAX_ENTRIES}")
    total_uncompressed = sum(info.file_size for info in infos)
    total_compressed = sum(info.compress_size for info in infos)
    if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
        raise ConversionError("GX3 archive uncompressed byte limit exceeded")
    ratio = float("inf") if total_uncompressed and not total_compressed else total_uncompressed / max(total_compressed, 1)
    if ratio > MAX_COMPRESSION_RATIO:
        raise ConversionError("GX3 archive compression ratio limit exceeded")
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        _validate_zip_name(info.filename)
        if info.file_size > MAX_ENTRY_BYTES:
            raise ConversionError(f"GX3 archive entry byte limit exceeded: {info.filename}")
        if info.filename in entries:
            raise ConversionError(f"duplicate GX3 archive entry: {info.filename}")
        entries[info.filename] = info
    return entries


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    body = archive.read(info)
    if len(body) != info.file_size:
        raise ConversionError(f"GX3 archive entry size mismatch: {info.filename}")
    return body


def _parse_config(body: bytes) -> str:
    root = element_tree.fromstring(body.decode("utf-8-sig"))
    config = root.find("Config")
    approved_identities = {("FX5U", "528"), ("FX5UJ", "529"), ("FX5S", "530")}
    if config is None or (config.get("Unit"), config.get("UnitId")) not in approved_identities:
        raise ConversionError("only approved GX Works3 FX5U/FX5UC/FX5UJ/FX5S Config identities are supported")
    title = config.get("Title", "")
    if not isinstance(title, str):
        raise ConversionError("GX3 project title is invalid")
    return title


def _parse_program(entries: dict[str, zipfile.ZipInfo], archive: zipfile.ZipFile) -> tuple[str, str]:
    link_paths = sorted(path for path in entries if re.fullmatch(r"ConvertData/\d+/PouLinkOrder\.info", path))
    if len(link_paths) != 1:
        raise ConversionError("exactly one linked ProgPou is required")
    link_path = link_paths[0]
    pou_id = _read_entry(archive, entries[link_path]).decode("ascii").strip()
    if re.fullmatch(r"\d+", pou_id) is None:
        raise ConversionError("PouLinkOrder.info must contain a numeric POU identifier")
    required = {f"{pou_id}_StepInfo.db", f"ConvertData/{pou_id}/PouPCode.pcode"}
    if not required.issubset(entries):
        raise ConversionError("linked POU has no matching StepInfo/PouPCode entries")
    program_qpg = link_path.rsplit("/", 1)[0] + "/Program.qpg"
    if program_qpg not in entries:
        raise ConversionError("linked POU has no Program.qpg")
    body = _read_entry(archive, entries[program_qpg])
    names = [
        match.group().decode("utf-16le")
        for match in re.finditer(rb"(?:[ -~]\x00){2,}", body)
        if match.group().decode("utf-16le").strip()
        and match.group().decode("utf-16le") != "@                                "
    ]
    if not names:
        raise _mining_error(
            "linked Program.qpg contains no readable program identifier",
            f"category=program;entry={program_qpg};raw_sha256={hashlib.sha256(body).hexdigest()}",
        )
    # The first non-placeholder UTF-16LE token is the only observed program
    # identity field.  Subsequent tokens can be section labels and never drive
    # conversion semantics.
    return names[0], pou_id


def _linked_pou_ids(entries: dict[str, zipfile.ZipInfo], archive: zipfile.ZipFile) -> tuple[str, ...]:
    """Read every archive-declared POU order without assigning names by position.

    ``PouLinkOrder.info`` is the only observed POU identity order.  Its entries
    must each have the directly linked StepInfo and P-code artifacts before any
    root database is considered.  Program names are resolved separately from
    length-prefixed ``Program.qpg`` records, so a reordered or ambiguous
    project cannot silently inherit a historical source profile.
    """
    # ``entries`` preserves ZipFile.infolist() order, i.e. the source
    # archive's central-directory order.  A root directory number is an
    # identifier, not a cross-group POU ordering authority; sorting it would
    # silently reorder e.g. ConvertData/2 and ConvertData/10.
    link_paths = tuple(path for path in entries if re.fullmatch(r"ConvertData/\d+/PouLinkOrder\.info", path))
    if not link_paths:
        raise _mining_error(
            "GX3 multi-POU link order is ambiguous",
            f"category=pou_link_order;entry_count={len(link_paths)}",
        )
    groups: list[tuple[str, ...]] = []
    for path in link_paths:
        try:
            group = tuple(_read_entry(archive, entries[path]).decode("ascii").splitlines())
        except UnicodeDecodeError as error:
            raise _mining_error("PouLinkOrder.info is not ASCII", "category=pou_link_order") from error
        if not group or any(re.fullmatch(r"\d+", pou_id) is None for pou_id in group) or len(set(group)) != len(group):
            raise _mining_error(
                "PouLinkOrder.info has an invalid POU inventory",
                f"category=pou_link_order;count={len(group)};sha256={_canonical_sha256(group)}",
            )
        groups.append(group)
    pou_ids = tuple(pou_id for group in groups for pou_id in group)
    if len(pou_ids) < 2 or len(set(pou_ids)) != len(pou_ids):
        raise _mining_error(
            "PouLinkOrder.info has an invalid POU inventory",
            f"category=pou_link_order;count={len(pou_ids)};sha256={_canonical_sha256(pou_ids)}",
        )
    missing = [
        f"{pou_id}:{artifact}"
        for pou_id in pou_ids
        for artifact in (f"{pou_id}_StepInfo.db", f"ConvertData/{pou_id}/PouPCode.pcode")
        if artifact not in entries
    ]
    if missing:
        raise _mining_error(
            "linked POU inventory is incomplete",
            f"category=pou_inventory;missing_sha256={_canonical_sha256(missing)}",
        )
    linked_ids = set(pou_ids)
    step_ids = {
        match.group(1)
        for path in entries
        if (match := re.fullmatch(r"(\d+)_StepInfo\.db", path)) is not None
    }
    pcode_ids = {
        match.group(1)
        for path in entries
        if (match := re.fullmatch(r"ConvertData/(\d+)/PouPCode\.pcode", path)) is not None
    }
    # StepInfo participates in the ladder-to-POU relation and therefore must
    # be exact.  P-code is not read by the source decoder; a surplus saved
    # P-code cache cannot select or alter any LDDB/MilDB pairing, while a
    # missing linked P-code remains a hard inventory failure above.
    if step_ids != linked_ids or not linked_ids.issubset(pcode_ids):
        raise _mining_error(
            "linked POU inventory has orphan artifacts",
            "category=pou_inventory;"
            f"linked_sha256={_canonical_sha256(sorted(linked_ids))};"
            f"stepinfo_sha256={_canonical_sha256(sorted(step_ids))};"
            f"pcode_sha256={_canonical_sha256(sorted(pcode_ids))}",
        )
    return pou_ids


def _linked_pou_name_pairs(
    entries: dict[str, zipfile.ZipInfo], archive: zipfile.ZipFile
) -> tuple[tuple[str, str], ...]:
    """Pair each linked POU with names from its own sibling Program.qpg.

    Multiple root POU groups are valid when each group has a complete local
    link order and a same-directory program table.  Pairing each group locally
    prevents a name from one group being assigned by global sort order to a
    different root database group.
    """
    pou_ids = _linked_pou_ids(entries, archive)
    link_paths = tuple(path for path in entries if re.fullmatch(r"ConvertData/\d+/PouLinkOrder\.info", path))
    pairs: list[tuple[str, str]] = []
    for path in link_paths:
        group = tuple(_read_entry(archive, entries[path]).decode("ascii").splitlines())
        program_qpg = path.rsplit("/", 1)[0] + "/Program.qpg"
        if program_qpg not in entries:
            raise _mining_error(
                "linked POU has no Program.qpg",
                f"category=program_name;entry_sha256={hashlib.sha256(program_qpg.encode('utf-8')).hexdigest()}",
            )
        names = _program_qpg_names({program_qpg: entries[program_qpg]}, archive, len(group))
        pairs.extend(zip(group, names, strict=True))
    if tuple(pou_id for pou_id, _name in pairs) != pou_ids:
        raise RuntimeError("linked POU name-pair inventory differs from link order")
    names = [name for _pou_id, name in pairs]
    if len(set(name.casefold() for name in names)) != len(names):
        raise _mining_error(
            "Program.qpg program identifiers are ambiguous",
            f"category=program_name;expected_count={len(names)};candidate_count={len(names)};"
            f"sha256={_canonical_sha256(names)}",
        )
    return tuple(pairs)


def _program_qpg_names(entries: dict[str, zipfile.ZipInfo], archive: zipfile.ZipFile, expected_count: int) -> tuple[str, ...]:
    """Extract exact length-prefixed UTF-16LE program records from Program.qpg.

    Printable-token scanning is deliberately insufficient because Program.qpg
    also carries titles, notes, and device captions.  The observed program
    table stores a two-byte byte-length immediately before a NUL-terminated
    UTF-16LE identifier.  Every candidate must be a valid file-safe POU name,
    and the final inventory must be exact and case-insensitively unique.
    """
    candidates: list[str] = []
    for path in sorted(path for path in entries if re.fullmatch(r"ConvertData/\d+/Program\.qpg", path)):
        body = _read_entry(archive, entries[path])
        for offset in range(2, len(body) - 1):
            byte_length = int.from_bytes(body[offset - 2 : offset], "little")
            if byte_length < 4 or byte_length > 130 or byte_length % 2 or offset + byte_length > len(body):
                continue
            if body[offset + byte_length - 2 : offset + byte_length] != b"\x00\x00":
                continue
            try:
                name = body[offset : offset + byte_length - 2].decode("utf-16le")
            except UnicodeDecodeError:
                continue
            if re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
                candidates.append(name)
    folded = [name.casefold() for name in candidates]
    if (
        len(candidates) != expected_count
        or len(set(candidates)) != len(candidates)
        or len(set(folded)) != len(folded)
    ):
        raise _mining_error(
            "Program.qpg program identifiers are ambiguous",
            f"category=program_name;expected_count={expected_count};candidate_count={len(candidates)};"
            f"sha256={_canonical_sha256(candidates)}",
        )
    return tuple(candidates)


def _dynamic_multi_linked_programs(
    entries: dict[str, zipfile.ZipInfo], archive: zipfile.ZipFile, source_sha256: str
) -> tuple[ParsedProgram, ...]:
    """Decode every multi-POU root database pair from archive structure alone.

    Known-source official CSV remains a regression/compatibility asset only.
    It is never consulted until the dynamic POU order, StepInfo-LDDB relation,
    and paired MilDB relation have independently succeeded.
    """
    pou_name_pairs = _linked_pou_name_pairs(entries, archive)
    pou_ids = tuple(pou_id for pou_id, _name in pou_name_pairs)
    names = tuple(name for _pou_id, name in pou_name_pairs)
    root_lddb = {
        path[:-8]: info
        for path, info in entries.items()
        if re.fullmatch(r"[^/]+_LDDB\.db", path)
    }
    root_mildb = {
        path[:-9]: info
        for path, info in entries.items()
        if re.fullmatch(r"[^/]+_MilDB\.db", path)
    }
    if not root_lddb or len(root_lddb) != len(pou_ids) or len(root_mildb) != len(pou_ids):
        raise _mining_error(
            "multi-POU root database inventory is not a complete paired set",
            f"category=pou_database_inventory;pou_count={len(pou_ids)};lddb_count={len(root_lddb)};"
            f"mildb_count={len(root_mildb)};lddb_sha256={_canonical_sha256(sorted(root_lddb))};"
            f"mildb_sha256={_canonical_sha256(sorted(root_mildb))}",
        )

    linked_structures: list[tuple[str, str, str, ProgramStructure]] = []
    remaining_lddb = dict(root_lddb)
    for name, pou_id in zip(names, pou_ids, strict=True):
        step_body = _read_entry(archive, entries[f"{pou_id}_StepInfo.db"])
        matches: list[tuple[str, ProgramStructure]] = []
        for stem, lddb_info in remaining_lddb.items():
            try:
                structure, _raw_data = _read_ladder_structure(_read_entry(archive, lddb_info), step_body)
            except ConversionError:
                continue
            matches.append((stem, structure))
        if len(matches) != 1:
            raise _mining_error(
                "StepInfo and LDDB do not have a unique structural relation",
                f"category=pou_database_match;program={name};pou_id={pou_id};match_count={len(matches)};"
                f"candidate_sha256={_canonical_sha256([stem for stem, _structure in matches])}",
            )
        stem, structure = matches[0]
        linked_structures.append((name, pou_id, stem, structure))
        del remaining_lddb[stem]
    if remaining_lddb:
        raise _mining_error(
            "multi-POU root LDDB inventory has unlinked entries",
            f"category=pou_database_match;remaining_lddb_sha256={_canonical_sha256(sorted(remaining_lddb))}",
        )

    assignments: list[tuple[str, ProgramStructure, tuple[Instruction, ...], tuple[tuple[str, str, str, str, str, str, str, str], ...], bool, tuple[OpaqueBlock, ...], tuple[tuple[str, int, int], ...], int, tuple[SerializedOperation, ...]]] = []
    remaining_mildb = dict(root_mildb)
    for name, pou_id, stem, structure in linked_structures:
        empty_body = (
            _read_entry(archive, remaining_mildb[stem])
            if stem in remaining_mildb and _mil_row_count(_read_entry(archive, remaining_mildb[stem])) == 0
            else None
        )
        if empty_body is not None:
            # A saved GX3 can legitimately retain its source LDDB and StepInfo
            # while omitting the derived MIL cache.  Decode that POU locally;
            # an unmined graph is isolated below, never treated as a request to
            # mutate/rebuild the customer source in GX Works3.
            direct_result = _parse_lddb_blocks(structure, _read_ladder_structure(
                _read_entry(archive, root_lddb[stem]),
                _read_entry(archive, entries[f"{pou_id}_StepInfo.db"]),
            )[1])
            mil_stem, mil_body = stem, empty_body
        else:
            mil_matches = [
                (mil_stem, mil_body)
                for mil_stem, mil_info in remaining_mildb.items()
                if _mil_relates_to_ladder_blocks(
                    (mil_body := _read_entry(archive, mil_info)),
                    structure,
                )
            ]
            if len(mil_matches) != 1:
                raise _mining_error(
                    "LDDB and MilDB do not have a unique structural relation",
                    f"category=pou_mil_database_match;program={name};pou_id={pou_id};match_count={len(mil_matches)};"
                    f"candidate_sha256={_canonical_sha256([mil_stem for mil_stem, _body in mil_matches])}",
                )
            mil_stem, mil_body = mil_matches[0]
            direct_result = _parse_mil_blocks(mil_body, structure)
        direct_findings = tuple(
            (
                str(block.sequence),
                str(block.source_step),
                "",
                "",
                block.error,
                block.finding_code,
                block.mining_tracks,
                f"program={name};{block.locator}",
            )
            for block in direct_result.opaque_blocks
        )
        # Archive topology and typed grammar decide source support.  Source
        # identity is provenance only; official export is test-only evidence.
        instructions = direct_result.instructions
        findings = direct_findings
        direct_decoded = True
        opaque_blocks = direct_result.opaque_blocks
        recovery_methods = direct_result.recovery_methods
        assignments.append((
            stem, structure, instructions, findings, direct_decoded, opaque_blocks,
            recovery_methods, direct_result.direct_blocks, direct_result.source_operations,
        ))
        del remaining_mildb[mil_stem]
    if remaining_mildb:
        raise _mining_error(
            "multi-POU root MilDB inventory has unlinked entries",
            f"category=pou_database_match;remaining_mildb_sha256={_canonical_sha256(sorted(remaining_mildb))}",
        )
    return tuple(
        ParsedProgram(
            name, pou_id, instructions, structure, findings, direct_decoded, opaque_blocks, recovery_methods,
            _coverage_for_records(
                instructions, opaque_blocks, direct_blocks=direct_blocks,
                source_operations=source_operations,
            ),
            source_operations,
        )
        for name, pou_id, (_stem, structure, instructions, findings, direct_decoded, opaque_blocks, recovery_methods, direct_blocks, source_operations) in zip(names, pou_ids, assignments, strict=True)
    )


def _mining_candidate(record_id: object, data: object, *, blocktype: object | None = None) -> str:
    """Expose a stable locator, never the unmined raw LadderBlocks text."""
    record = record_id if isinstance(record_id, str) else type(record_id).__name__
    raw_hash = hashlib.sha256(repr(data).encode("utf-8")).hexdigest()
    block = "" if blocktype is None else f";blocktype={blocktype}"
    return f"record_id={record}{block};raw_sha256={raw_hash}"


def _mining_error(message: str, candidate: str) -> ConversionError:
    return ConversionError(
        message,
        finding_code="MINING_REQUIRED",
        mining_tracks="P002|P004",
        candidate=candidate,
    )


def _canonical_block_id(value: object) -> str | None:
    """Normalize the two observed GUID spellings without assigning meaning."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.lower().startswith("_guid/"):
        normalized = normalized[6:]
    normalized = normalized.strip("{}").lower()
    return normalized or None


def _sqlite_table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _read_ladder_structure(body: bytes, step_info_body: bytes) -> tuple[ProgramStructure, dict[str, str]]:
    """Read every linked POU block and StepInfo relation before semantic decoding.

    Ladder text is intentionally retained only as SHA-256 digests in the IR.
    This establishes complete source coverage while preventing unmined GX3
    syntax from being silently interpreted as Q-series instructions.
    """
    ladder_connection = sqlite3.connect(":memory:")
    step_connection = sqlite3.connect(":memory:")
    raw_data: dict[str, str] = {}
    try:
        ladder_connection.deserialize(body)
        ladder_connection.execute("PRAGMA query_only = ON")
        if list(ladder_connection.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ConversionError("LDDB.db integrity check failed")
        ladder_tables = {row[0] for row in ladder_connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "LadderBlocks" not in ladder_tables:
            raise ConversionError("LDDB.db has no LadderBlocks table")
        ladder_columns = _sqlite_table_columns(ladder_connection, "LadderBlocks")
        required_ladder_columns = {"id", "pos", "blocktype", "data"}
        if not required_ladder_columns.issubset(ladder_columns):
            raise _mining_error(
                "LDDB.db has an unmined LadderBlocks schema",
                f"category=ladder_schema;missing={','.join(sorted(required_ladder_columns - ladder_columns))}",
            )
        optional_columns = [name for name in ("rowsize", "translated", "ConvTarget") if name in ladder_columns]
        select_columns = ["id", "pos", "blocktype", "data", *optional_columns]
        records = list(ladder_connection.execute(f"SELECT {','.join(select_columns)} FROM LadderBlocks ORDER BY pos, id"))

        blocks: list[LadderBlock] = []
        for record in records:
            record_id, position, blocktype, data, *optional_values = record
            normalized_id = _canonical_block_id(record_id)
            if normalized_id is None or not isinstance(position, (int, float)) or not isinstance(blocktype, int) or not isinstance(data, str):
                raise _mining_error(
                    "LDDB.db contains an unsupported LadderBlocks record type",
                    _mining_candidate(record_id, data, blocktype=blocktype),
                )
            optional = dict(zip(optional_columns, optional_values, strict=True))
            for name, value in optional.items():
                if value is not None and not isinstance(value, int):
                    raise _mining_error(
                        "LDDB.db contains an unsupported LadderBlocks metadata type",
                        _mining_candidate(record_id, data, blocktype=blocktype),
                    )
            if normalized_id in raw_data:
                raise _mining_error(
                    "LDDB.db has duplicate logical LadderBlocks identifiers",
                    f"category=ladder_identifier;record_id={normalized_id}",
                )
            raw_data[normalized_id] = data
            blocks.append(
                LadderBlock(
                    normalized_id,
                    float(position),
                    blocktype,
                    hashlib.sha256(data.encode("utf-8")).hexdigest(),
                    optional.get("rowsize"),
                    optional.get("translated"),
                    optional.get("ConvTarget"),
                )
            )

        step_connection.deserialize(step_info_body)
        step_connection.execute("PRAGMA query_only = ON")
        if list(step_connection.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ConversionError("StepInfo.db integrity check failed")
        step_tables = {row[0] for row in step_connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"T_Block", "T_Step"}.issubset(step_tables):
            raise _mining_error(
                "StepInfo.db has an unmined table structure",
                f"category=stepinfo_schema;tables_sha256={_canonical_sha256(sorted(step_tables))}",
            )
        step_block_columns = _sqlite_table_columns(step_connection, "T_Block")
        step_row_columns = _sqlite_table_columns(step_connection, "T_Step")
        required_step_block_columns = {"Pos", "BlockID"}
        required_step_row_columns = {"Pos", "BlockID", "MilID"}
        if not required_step_block_columns.issubset(step_block_columns) or not required_step_row_columns.issubset(step_row_columns):
            missing = sorted(
                (required_step_block_columns - step_block_columns)
                | (required_step_row_columns - step_row_columns)
            )
            raise _mining_error("StepInfo.db has an unmined linkage schema", f"category=stepinfo_schema;missing={','.join(missing)}")
        block_optionals = [name for name in ("StepSize", "BodyID") if name in step_block_columns]
        row_optionals = [name for name in ("ElementIDMacroFB", "StepSize") if name in step_row_columns]
        step_blocks: list[StepInfoBlock] = []
        for position, block_id, *optional_values in step_connection.execute(
            f"SELECT Pos,BlockID{',' if block_optionals else ''}{','.join(block_optionals)} FROM T_Block ORDER BY Pos, BlockID"
        ):
            normalized_id = _canonical_block_id(block_id)
            optional = dict(zip(block_optionals, optional_values, strict=True))
            if normalized_id is None or not isinstance(position, (int, float)) or any(value is not None and not isinstance(value, int) for value in optional.values()):
                raise _mining_error("StepInfo.db contains an unsupported T_Block row", f"category=stepinfo_block;raw_sha256={_canonical_sha256([position, block_id, *optional_values])}")
            step_blocks.append(StepInfoBlock(float(position), normalized_id, optional.get("StepSize"), optional.get("BodyID")))
        step_rows: list[StepInfoRow] = []
        for position, block_id, mil_id, *optional_values in step_connection.execute(
            f"SELECT Pos,BlockID,MilID{',' if row_optionals else ''}{','.join(row_optionals)} FROM T_Step ORDER BY Pos, BlockID, MilID"
        ):
            normalized_id = _canonical_block_id(block_id)
            optional = dict(zip(row_optionals, optional_values, strict=True))
            if (
                normalized_id is None
                or not isinstance(position, (int, float))
                or (mil_id is not None and not isinstance(mil_id, int))
                or any(value is not None and not isinstance(value, int) for value in optional.values())
            ):
                raise _mining_error("StepInfo.db contains an unsupported T_Step row", f"category=stepinfo_row;raw_sha256={_canonical_sha256([position, block_id, mil_id, *optional_values])}")
            step_rows.append(
                StepInfoRow(float(position), optional.get("ElementIDMacroFB"), normalized_id, mil_id, optional.get("StepSize"))
            )
    finally:
        ladder_connection.close()
        step_connection.close()

    structure = ProgramStructure(tuple(blocks), StepInfo(tuple(step_blocks), tuple(step_rows)))
    ladder_ids = {block.record_id for block in structure.blocks}
    step_ids = set(structure.step_info.block_ids)
    missing_step_blocks = sorted(ladder_ids - step_ids)
    extra_step_blocks = sorted(step_ids - ladder_ids)
    step_rows_without_blocks = sorted(set(structure.step_info.step_block_ids) - ladder_ids)
    duplicate_step_blocks = len(structure.step_info.block_ids) - len(step_ids)
    if missing_step_blocks or extra_step_blocks or step_rows_without_blocks or duplicate_step_blocks:
        raise _mining_error(
            "LadderBlocks and StepInfo linkage is outside the mined structure",
            "category=stepinfo_relation;"
            f"ladder_block_count={len(ladder_ids)};step_block_count={len(step_ids)};"
            f"missing_step_blocks={len(missing_step_blocks)};extra_step_blocks={len(extra_step_blocks)};duplicate_step_blocks={duplicate_step_blocks};"
            f"orphan_step_rows={len(step_rows_without_blocks)};"
            f"ladder_ids_sha256={_canonical_sha256(sorted(ladder_ids))};"
            f"step_ids_sha256={_canonical_sha256(sorted(step_ids))}",
        )
    return structure, raw_data


def _structure_candidate(structure: ProgramStructure) -> str:
    blocktype_counts = Counter(block.blocktype for block in structure.blocks)
    block_summary = [
        (block.record_id, block.position, block.blocktype, block.data_sha256, block.rowsize, block.translated, block.conversion_target)
        for block in structure.blocks
    ]
    step_block_summary = [
        (block.position, block.block_id, block.step_size, block.body_id)
        for block in structure.step_info.blocks
    ]
    step_row_summary = [
        (row.position, row.element_id_macro_fb, row.block_id, row.mil_id, row.step_size)
        for row in structure.step_info.rows
    ]
    return (
        "category=ladder_structure;"
        f"ladder_block_count={len(structure.blocks)};"
        f"blocktype_counts={','.join(f'{kind}:{count}' for kind, count in sorted(blocktype_counts.items()))};"
        f"step_block_count={len(structure.step_info.block_ids)};"
        f"step_row_count={len(structure.step_info.step_block_ids)};"
        f"ladder_sha256={_canonical_sha256(block_summary)};"
        f"step_blocks_sha256={_canonical_sha256(step_block_summary)};"
        f"step_rows_sha256={_canonical_sha256(step_row_summary)}"
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _metadata_candidate(category: str, entry: str, logical_sha256: str, keys: list[tuple[object, ...]]) -> str:
    key_sha256 = _canonical_sha256(keys)
    return f"category={category};entry={entry};logical_sha256={logical_sha256};key_sha256={key_sha256};key_count={len(keys)}"


def _inspect_cpu_module_parameters(
    entries: dict[str, zipfile.ZipInfo],
    archive: zipfile.ZipFile,
    profile: dict[str, tuple[int, str]] = EXPECTED_CPU_MODULE_PARAMETERS,
) -> None:
    unexpected = sorted(
        name
        for name in entries
        if re.fullmatch(r"(?:CPU|UNIT)[^/]*\.PRM|UnitConfig[^/]*\.dat", name) and name not in profile
    )
    if unexpected:
        raise _mining_error(
            "GX3 contains unmined CPU/module parameter entries",
            f"category=cpu_module_parameter;extra_entries_sha256={_canonical_sha256(unexpected)};extra_count={len(unexpected)}",
        )
    for name, (expected_size, expected_sha256) in profile.items():
        info = entries.get(name)
        if info is None:
            raise _mining_error(
                "GX3 is missing a required CPU/module parameter entry",
                f"category=cpu_module_parameter;entry={name};state=missing",
            )
        body = _read_entry(archive, info)
        actual_sha256 = hashlib.sha256(body).hexdigest()
        if info.file_size != expected_size or actual_sha256 != expected_sha256:
            raise _mining_error(
                "GX3 CPU/module parameter entry is outside the validated profile",
                f"category=cpu_module_parameter;entry={name};size={info.file_size};sha256={actual_sha256}",
            )


def _inspect_label_metadata(
    entries: dict[str, zipfile.ZipInfo], archive: zipfile.ZipFile
) -> tuple[SourceDiagnostic, ...]:
    """Inventory labels without making an optional, non-emitted component a read gate."""
    info = entries.get("LabelData.db")
    if info is None:
        return (
            SourceDiagnostic(
                "label", "SOURCE_COMPONENT_NOT_CONVERTED",
                "GX3 has no LabelData.db; ladder conversion continued",
                "category=label;entry=LabelData.db;state=missing",
            ),
        )
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.deserialize(_read_entry(archive, info))
            connection.execute("PRAGMA query_only = ON")
            if list(connection.execute("PRAGMA integrity_check")) != [("ok",)]:
                return (
                    SourceDiagnostic(
                        "label", "SOURCE_COMPONENT_NOT_CONVERTED",
                        "LabelData.db integrity check failed; ladder conversion continued",
                        "category=label;entry=LabelData.db;state=integrity_failed",
                    ),
                )
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"LabelTbl", "ColumnDataTbl"}.issubset(tables):
                return (
                    SourceDiagnostic(
                        "label", "SOURCE_COMPONENT_NOT_CONVERTED",
                        "LabelData.db schema is unsupported; ladder conversion continued",
                        f"category=label;entry=LabelData.db;tables_sha256={_canonical_sha256(sorted(tables))}",
                    ),
                )
            label_types = dict(connection.execute("SELECT LabelID, LabelTypeID FROM LabelTbl"))
            rows = list(
                connection.execute(
                    "SELECT LabelID, RowID, ColumnID, ColumnStrValue, ColumnIntValue "
                    "FROM ColumnDataTbl ORDER BY LabelID, RowID, ColumnID"
                )
            )
        except sqlite3.Error as error:
            return (
                SourceDiagnostic(
                    "label", "SOURCE_COMPONENT_NOT_CONVERTED",
                    "LabelData.db could not be inventoried; ladder conversion continued",
                    f"category=label;entry=LabelData.db;state=sqlite_{type(error).__name__}",
                ),
            )
    finally:
        connection.close()
    logical_sha256 = _canonical_sha256(rows)
    meaningful = [
        (label_id, row_id, column_id)
        for label_id, row_id, column_id, value, _integer in rows
        if label_types.get(label_id) in {5121, 5122}
        and column_id in {2, 15}
        and isinstance(value, str)
        and value != ""
    ]
    if meaningful:
        return (
            SourceDiagnostic(
                "label", "SOURCE_COMPONENT_NOT_CONVERTED",
                "GX3 label metadata is not emitted; ladder conversion continued",
                _metadata_candidate("label", "LabelData.db", logical_sha256, meaningful),
            ),
        )
    return ()


def _format_device_comment_address(
    dev_code: object,
    ext_code: object,
    ext_no: object,
    is_local: object,
    dev_no_high: object,
    dev_no_low: object,
    bit_no: object,
) -> str | None:
    """Project one observed FX5U DEVICE_DATA key to its GX device spelling."""
    values = (dev_code, ext_code, ext_no, is_local, dev_no_high, dev_no_low, bit_no)
    if not all(isinstance(value, int) for value in values):
        raise _mining_error(
            "device-comment database contains an unsupported device key type",
            f"category=device_comment;key_sha256={_canonical_sha256(values)}",
        )
    if is_local != 0 or dev_no_high != 0 or dev_no_low < 0 or bit_no < 0 or bit_no > 16:
        return None
    if dev_code == 35:
        if ext_code != 208 or ext_no <= 0:
            return None
        address = f"U{ext_no}\\G{dev_no_low}"
        return address if bit_no == 0 else f"{address}.{bit_no - 1:X}"
    if ext_code != 0 or ext_no != 0:
        return None
    decimal_prefixes = {
        1: "M", 2: "SM", 3: "L", 32: "D", 33: "SD",
        39: "R", 66: "T", 74: "ST", 101: "S",
    }
    if dev_code in decimal_prefixes:
        address = f"{decimal_prefixes[dev_code]}{dev_no_low}"
    elif dev_code in {16, 17}:
        address = f"{('X' if dev_code == 16 else 'Y')}{dev_no_low:o}"
    elif dev_code == 20:
        number = f"{dev_no_low:X}"
        address = "B" + (f"0{number}" if number[0] in "ABCDEF" else number)
    else:
        return None
    if bit_no == 0:
        return address
    if dev_code not in {32, 33}:
        return None
    return f"{address}.{bit_no - 1:X}"


def _normalize_device_comment_text(address: str, text: object, delete_flag: object) -> str:
    if delete_flag is not None or not isinstance(text, str):
        raise _mining_error(
            "used device comment contains an unsupported state",
            f"category=device_comment;device={address};state_sha256={_canonical_sha256([text, delete_flag])}",
        )
    if any(character in text for character in "\t\x00"):
        raise ConversionError(f"used device comment contains a control delimiter: {address}")
    # GX Works2 discards trailing spaces when the comment database is saved.
    # Normalize that observed behavior before publishing so official re-export
    # remains semantically exact instead of hiding source-only padding.
    return re.sub(r"\r\n?|\n", " ", text).rstrip(" ")


def _read_used_device_comments(
    entries: dict[str, zipfile.ZipInfo],
    archive: zipfile.ZipFile,
    instructions: tuple[Instruction, ...],
) -> tuple[DeviceComment, ...]:
    """Read every English-display device comment from the validated root database."""
    names = sorted(name for name in entries if name.endswith("_DC.db") and "/" not in name)
    if len(names) != 1:
        state = "missing" if not names else f"entry_count={len(names)}"
        raise _mining_error(
            "GX3 requires exactly one validated root device-comment database",
            f"category=device_comment;state={state};entries_sha256={_canonical_sha256(names)}",
        )
    del instructions  # Kept in the signature for compatibility with existing callers/tests.
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(_read_entry(archive, entries[names[0]]))
        connection.execute("PRAGMA query_only = ON")
        if list(connection.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ConversionError("device-comment database integrity check failed")
        device_schema = [row[1] for row in connection.execute("PRAGMA table_info(DEVICE_DATA)")]
        comment_schema = [row[1] for row in connection.execute("PRAGMA table_info(COMMENT_DATA)")]
        if device_schema != ["SEQ", "DevCode", "ExtCode", "ExtNo", "IsLocal", "DevNoHigh", "DevNoLow", "BitNo"]:
            raise _mining_error(
                "device-comment DEVICE_DATA schema is outside the validated profile",
                f"category=device_comment;entry={names[0]};schema_sha256={_canonical_sha256(device_schema)}",
            )
        if comment_schema != ["SEQ", "DeviceSEQ", "CmtNo", "CmtData", "DelFlag"]:
            raise _mining_error(
                "device-comment COMMENT_DATA schema is outside the validated profile",
                f"category=device_comment;entry={names[0]};schema_sha256={_canonical_sha256(comment_schema)}",
            )
        rows = list(
            connection.execute(
                "SELECT d.DevCode, d.ExtCode, d.ExtNo, d.IsLocal, d.DevNoHigh, d.DevNoLow, d.BitNo, "
                "c.CmtData, c.DelFlag FROM DEVICE_DATA d "
                "JOIN COMMENT_DATA c ON c.DeviceSEQ = d.SEQ "
                "WHERE c.CmtNo = 6 ORDER BY d.SEQ, c.SEQ"
            )
        )
    finally:
        connection.close()
    comments: dict[str, DeviceComment] = {}
    for *key, text, delete_flag in rows:
        if key[3] != 0:
            raise _mining_error(
                "program device-comment relation is not decoded",
                f"category=device_comment;scope=program;key_sha256={_canonical_sha256(key)}",
            )
        address = _format_device_comment_address(*key)
        if address is None:
            raise _mining_error(
                "device-comment address is outside the mined FX5 profile",
                f"category=device_comment;key_sha256={_canonical_sha256(key)}",
            )
        target_text = _normalize_device_comment_text(address, text, delete_flag)
        source_text = text
        previous = comments.get(address)
        if previous is not None and previous.text != source_text:
            raise _mining_error(
                "used device has conflicting English comments",
                f"category=device_comment;device={address};comments_sha256={_canonical_sha256([previous.text, source_text])}",
            )
        comments[address] = DeviceComment(
            address,
            source_text,
            target_text if target_text != source_text else None,
            "NORMALIZED" if target_text != source_text else None,
        )
    return tuple(comments.values())


def _read_optional_device_comments(
    entries: dict[str, zipfile.ZipInfo],
    archive: zipfile.ZipFile,
    instructions: tuple[Instruction, ...],
) -> tuple[tuple[DeviceComment, ...], tuple[SourceDiagnostic, ...]]:
    """Isolate an unsupported comment component from otherwise valid Ladder output."""
    try:
        return _read_used_device_comments(entries, archive, instructions), ()
    except (ConversionError, sqlite3.Error) as error:
        candidate = (
            error.candidate
            if isinstance(error, ConversionError) and error.candidate
            else f"category=device_comment;state={type(error).__name__}"
        )
        return (), (
            SourceDiagnostic(
                "device_comment", "SOURCE_COMPONENT_NOT_CONVERTED",
                "GX3 device comments could not be emitted; ladder conversion continued",
                candidate,
            ),
        )


def _apply_comment_overrides(
    source_sha256: str,
    comments: tuple[DeviceComment, ...],
) -> tuple[DeviceComment, ...]:
    overrides = COMMENT_OVERRIDES_BY_SOURCE_SHA256.get(source_sha256, {})
    if not overrides:
        return comments
    by_device = {comment.device: comment.text for comment in comments}
    for device, (expected, replacement) in overrides.items():
        actual = by_device.get(device)
        if actual != expected:
            raise _mining_error(
                "approved device-comment override no longer matches the source text",
                f"category=device_comment_override;device={device};actual_sha256={_canonical_sha256(actual)}",
            )
        try:
            replacement_bytes = len(replacement.encode("cp949"))
        except UnicodeEncodeError as error:
            raise ConversionError(f"approved device-comment override is not CP949 compatible: {device}") from error
        if replacement_bytes > MAX_Q_COMMENT_BYTES or any(character in replacement for character in "\r\n\t\x00"):
            raise ConversionError(f"approved device-comment override is not GX Works2 compatible: {device}")
        by_device[device] = replacement
    return tuple(
        DeviceComment(comment.device, comment.text, by_device[comment.device], "APPROVED_OVERRIDE")
        if comment.device in overrides
        else comment
        for comment in comments
    )


def _inspect_source_profile(input_path: Path) -> str:
    """Record source identity without turning identity into an approval gate."""
    return sha256_file(input_path)


def _profile_asset_directory(source_sha256):
    raise ConversionError("Site profiles are not included in the public edition")


def _read_official_export(source_sha256: str, program_name: str) -> tuple[Instruction, ...]:
    profile = MULTI_POU_COMPATIBILITY_PROFILES[source_sha256]
    export_hashes = profile["exports"]
    path = _profile_asset_directory(source_sha256) / f"{program_name}.csv"
    try:
        body = path.read_bytes()
    except OSError as error:
        raise ConversionError(f"multi-POU compatibility asset is unavailable: {program_name}") from error
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != export_hashes[program_name]:
        raise _mining_error(
            "GX Works3 compatibility export no longer matches its recorded hash",
            f"category=official_export;program={program_name};sha256={actual_sha256}",
        )
    rows = list(csv.reader(io.StringIO(body.decode("utf-16")), delimiter="\t"))
    if len(rows) < 3 or rows[1] != ["Module Type Information:", "FX5CPU FX5U"] or rows[2] != GXW3_EXPORT_HEADER:
        raise _mining_error(
            "GX Works3 compatibility export has an unexpected header",
            f"category=official_export;program={program_name};header_sha256={_canonical_sha256(rows[:3])}",
        )
    records: list[Instruction] = []
    for row in rows[3:]:
        if len(row) != 7:
            raise ConversionError(f"GX Works3 compatibility export row width is invalid: {program_name}")
        step, line_statement, instruction, operand, blank, pi_statement, note = row
        if step:
            if not step.isdecimal() or blank:
                raise ConversionError(f"GX Works3 compatibility export primary row is invalid: {program_name}")
            records.append(
                Instruction(
                    len(records), int(step), instruction, operand,
                    line_statement, pi_statement, note,
                )
            )
        elif records and not any((step, line_statement, instruction, blank, pi_statement)):
            previous = records[-1]
            records[-1] = Instruction(
                previous.sequence, previous.source_step, previous.instruction, previous.operand,
                previous.line_statement, previous.pi_statement, previous.note,
                (*previous.continuation_operands, operand) if operand else previous.continuation_operands,
                (*previous.continuation_rows, tuple(row)),
            )
        else:
            raise ConversionError(f"GX Works3 compatibility export continuation row is invalid: {program_name}")
    return tuple(records)


def _balanced_records(text: str, marker: str) -> list[str]:
    """Return brace-balanced records, rejecting ambiguous serialized MIL text."""
    records: list[str] = []
    offset = 0
    while (start := text.find(marker, offset)) >= 0:
        brace = start + len(marker) - 1
        depth = 0
        end = brace
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth != 0:
            raise _mining_error("MIL contains an unbalanced serialized record", f"category=mil_syntax;raw_sha256={hashlib.sha256(text.encode('utf-8')).hexdigest()}")
        records.append(text[start : end + 1])
        offset = end + 1
    return records


def _mil_operands(record: str) -> list[tuple[str, tuple[int, ...]]]:
    """Extract MIL operand values while retaining their serialized shape."""
    payload_start = record.rfind(":as=[")
    if payload_start < 0:
        return []
    bracket = payload_start + 4
    depth = 0
    end = bracket
    while end < len(record):
        if record[end] == "[":
            depth += 1
        elif record[end] == "]":
            depth -= 1
            if depth == 0:
                break
        end += 1
    if depth != 0:
        raise _mining_error("MIL operand list is unbalanced", f"category=mil_operand;raw_sha256={hashlib.sha256(record.encode('utf-8')).hexdigest()}")
    values = record[bracket + 1 : end]
    top_level: list[str] = []
    cursor = 0
    while cursor < len(values):
        if values[cursor] in ": ":
            cursor += 1
            continue
        if cursor + 1 >= len(values) or values[cursor + 1] != "{":
            raise _mining_error("MIL has an unmined operand token", f"category=mil_operand;raw_sha256={hashlib.sha256(record.encode('utf-8')).hexdigest()}")
        start = cursor
        depth = 0
        while cursor < len(values):
            if values[cursor] == "{":
                depth += 1
            elif values[cursor] == "}":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        if depth:
            raise _mining_error("MIL operand token is unbalanced", f"category=mil_operand;raw_sha256={hashlib.sha256(record.encode('utf-8')).hexdigest()}")
        top_level.append(values[start:cursor])
    parsed: list[tuple[str, tuple[int, ...]]] = []
    for item in top_level:
        numbers = tuple(int(value) for value in re.findall(r"(?:a|v)=(-?\d+)", item))
        if item.startswith("M{"):
            parsed.append(("remote_bit" if "B{b=" in item else "k_device", numbers))
        elif item.startswith("B{"):
            parsed.append(("remote_word", numbers))
        elif item == "c{s=#:v=#:t=#}":
            parsed.append(("string_literal", ()))
        elif item.startswith(("d{", "c{")) and len(numbers) == 1:
            parsed.append(("scalar", numbers))
        else:
            raise _mining_error("MIL has an unmined operand form", f"category=mil_operand;raw_sha256={hashlib.sha256(item.encode('utf-8')).hexdigest()}")
    return parsed


def _mil_format_operand(tags: list[str], value: tuple[str, tuple[int, ...]]) -> tuple[str, int]:
    shape, numbers = value
    if shape == "string_literal":
        if numbers:
            raise _mining_error("MIL string literal type is unmined", "category=mil_operand")
        if (len(tags) >= 3 and tags[0] == "String"
                and re.fullmatch(r"[A-Za-z0-9_\-,$]+", tags[1])
                and tags[2] == f'"{tags[1]}"'):
            return tags[2], 3
        raise _mining_error("MIL string literal type is unmined", "category=mil_operand")
    # ``Zs`` stores the index register as the final numeric field.  Preserve
    # the GX Works3 source operand here; Q-series target acceptance is checked
    # separately and must not be inferred from successful source decoding.
    if shape == "remote_bit" and tags[:3] == ["Us", "G", "Zs"] and len(numbers) == 3:
        return f"U{numbers[0]}\\G{numbers[1]}Z{numbers[2]}", 3
    if shape == "remote_bit":
        if tags[:3] != ["Us", "G", "Dots"] or len(numbers) != 3:
            raise _mining_error("MIL remote bit type is unmined", f"category=mil_operand;tags={','.join(tags[:3])}")
        return f"U{numbers[0]}\\G{numbers[1]}.{numbers[2]:X}", 3
    if shape == "remote_word":
        if tags[:2] != ["Us", "G"] or len(numbers) != 2:
            raise _mining_error("MIL remote word type is unmined", f"category=mil_operand;tags={','.join(tags[:2])}")
        return f"U{numbers[0]}\\G{numbers[1]}", 2
    if shape == "k_device":
        if tags[:2] in (["M", "Zs"], ["D", "Zs"], ["L", "Zs"], ["R", "Zs"], ["T", "Zs"]) and len(numbers) == 2:
            return f"{tags[0]}{numbers[0]}Z{numbers[1]}", 2
        if tags[:2] == ["B", "Zs"] and len(numbers) == 2:
            return f"B{numbers[0]:X}Z{numbers[1]}", 2
        if tags[:3] == ["M", "Zs", "Ks"] and len(numbers) == 3:
            return f"K{numbers[2]}M{numbers[0]}Z{numbers[1]}", 3
        if tags[:2] == ["D", "Dots"] and len(numbers) == 2:
            return f"D{numbers[0]}.{numbers[1]:X}", 2
        if tags[:2] == ["SD", "Dots"] and len(numbers) == 2:
            return f"SD{numbers[0]}.{numbers[1]:X}", 2
        if tags[:2] == ["B", "Ks"] and len(numbers) == 2:
            return f"K{numbers[1]}B{numbers[0]:03X}", 2
        if tags[:2] == ["L", "Ks"] and len(numbers) == 2:
            return f"K{numbers[1]}L{numbers[0]}", 2
        if tags[:2] != ["M", "Ks"] or len(numbers) != 2:
            raise _mining_error("MIL K-device type is unmined", f"category=mil_operand;tags={','.join(tags[:2])}")
        return f"K{numbers[-1]}M{numbers[0]}", 2
    if not tags or tags[0] not in MIL_SCALAR_TAGS or len(numbers) != 1:
        raise _mining_error(
            "MIL scalar operand type is unmined",
            f"category=mil_operand;shape={shape};tags={','.join(tags[:3])};number_count={len(numbers)}",
        )
    prefix = {"K_1": "K", "K_2": "K", "H_1": "H", "SfcS": "S"}.get(tags[0], tags[0])
    if prefix in {"X", "Y"}:
        # Issue #30 official MIL anchors: 1057/1059/1060 -> X421/X423/X424.
        number = f"{numbers[0]:X}"
        if re.fullmatch(r"[0-7]+", number) is None:
            raise _mining_error("MIL X/Y source spelling is invalid", "category=mil_operand")
    else:
        number = f"{numbers[0]:X}" if prefix in {"B", "H"} else str(numbers[0])
    return f"{prefix}{number}", 1


def _mil_format_operand_in_context(
    instruction: str,
    signature: tuple[str, ...],
    all_tags: list[str],
    operand_index: int,
    remaining_tags: list[str],
    value: tuple[str, tuple[int, ...]],
) -> tuple[str, int]:
    """Admit newly observed device spellings only in their exact MIL context."""
    approved_f_contexts = {
        ("LD", ("Abl",), ("F",), 0),
        ("OR", ("Abl",), ("F",), 0),
        ("LDI", ("Abl",), ("F",), 0),
        ("ANI", ("Abl",), ("F",), 0),
        ("RST", ("Abl",), ("F",), 0),
        ("ANS", ("A16", "A16", "Abl"), ("T", "D", "F"), 2),
        ("ANS", ("A16", "A16", "Abl"), ("T", "K_1", "F"), 2),
    }
    # The explicit prefix checks below keep later record descriptors out of the
    # authority key while still making every operand position deterministic.
    f_prefixes = tuple(item for item in approved_f_contexts if all_tags[:len(item[2])] == list(item[2]))
    shape, numbers = value
    if (
        instruction in {"ANI", "ORI"} and signature == ("Abl",)
        and operand_index == 0 and all_tags[:1] == ["DX"] and remaining_tags[:1] == ["DX"]
    ):
        if shape != "scalar" or len(numbers) != 1 or numbers[0] < 0:
            raise _mining_error("MIL exact DX operand is unmined", "category=mil_operand;tag=DX")
        return f"DX{numbers[0]:X}", 1
    if (
        instruction == "MOV" and signature == ("A16", "A16")
        and operand_index == 1 and all_tags[:3] == ["D", "Y", "Ks"]
        and remaining_tags[:2] == ["Y", "Ks"]
        and shape == "k_device" and len(numbers) == 2 and numbers[1] == 2
        and re.fullmatch(r"[0-7]+", f"{numbers[0]:X}")
    ):
        return f"K2Y{numbers[0]:X}", 2
    if (
        instruction == "FOR"
        and signature == ("A16",)
        and operand_index == 0
        and all_tags[:1] == ["K_1"]
    ):
        if shape != "scalar" or len(numbers) != 1 or numbers[0] < 0:
            raise _mining_error("MIL exact FOR operand is unmined", "category=mil_operand;marker=FOR")
        value_tag = str(numbers[0])
        return f"K{value_tag}", 2 if all_tags[1:2] == [value_tag] else 1
    if remaining_tags[:1] == ["F"] and remaining_tags[1:2] not in (["Zs"], ["Ks"]) and any(
        instruction == item[0] and signature == item[1] and operand_index == item[3]
        for item in f_prefixes
    ):
        if shape != "scalar" or len(numbers) != 1 or numbers[0] < 0:
            raise _mining_error("MIL exact F operand shape is unmined", "category=mil_operand;tag=F")
        return f"F{numbers[0]}", 1
    indexed_f_contexts = {
        ("MOV", ("A16", "A16"), ("F", "Ks", "M", "Ks"), 0),
        ("MOV", ("A16", "A16"), ("F", "Ks", "D"), 0),
        ("ANDD=", ("A32s", "A32s"), ("F", "Ks", "K_2"), 0),
        ("AND=", ("A16s", "A16s"), ("F", "Ks", "K_1"), 0),
        ("AND=", ("A16s", "A16s"), ("K_1", "F", "Ks"), 1),
        ("AND<", ("A16s", "A16s"), ("K_1", "F", "Ks"), 1),
        ("AND=", ("A16s", "A16s"), ("K_1", "F", "Ks"), 1),
        ("AND<>", ("A16s", "A16s"), ("K_1", "F", "Ks"), 1),
    }
    if remaining_tags[:2] == ["F", "Ks"] and any(
        instruction == expected_instruction
        and signature == expected_signature
        and all_tags[:len(expected_tags)] == list(expected_tags)
        and operand_index == expected_index
        for expected_instruction, expected_signature, expected_tags, expected_index in indexed_f_contexts
    ):
        if shape != "k_device" or len(numbers) != 2 or numbers[0] < 0 or not 1 <= numbers[1] <= 8:
            raise _mining_error("MIL exact F K-device shape is unmined", "category=mil_operand;tag=F")
        return f"K{numbers[1]}F{numbers[0]}", 2
    if remaining_tags[:2] == ["F", "Zs"] and instruction in {"LD", "OR", "OUT"} and signature == ("Abl",) and operand_index == 0:
        if shape != "k_device" or len(numbers) != 2 or min(numbers) < 0:
            raise _mining_error("MIL exact indexed F shape is unmined", "category=mil_operand;tag=F")
        return f"F{numbers[0]}Z{numbers[1]}", 2
    if (
        remaining_tags[:2] == ["Y", "Ks"]
        and instruction == "MOV"
        and signature == ("A16", "A16")
        and operand_index == 0
        and all_tags == ["Y", "Ks", "D"]
    ):
        if shape != "k_device" or len(numbers) != 2 or min(numbers) < 0:
            raise _mining_error("MIL exact Y K-device shape is unmined", "category=mil_operand;tag=Y")
        return f"K{numbers[1]}Y{numbers[0]:X}", 2
    if (
        remaining_tags[:1] == ["SD"]
        and instruction == "MOV"
        and signature == ("A16", "A16")
        and operand_index in {0, 1}
        and (all_tags[:2] == ["SD", "D"] or all_tags[:3] == ["SD", "M", "Ks"])
    ):
        if shape != "scalar" or len(numbers) != 1 or numbers[0] < 0:
            raise _mining_error("MIL exact SD operand shape is unmined", "category=mil_operand;tag=SD")
        return f"SD{numbers[0]}", 1
    if remaining_tags[:1] == ["W"] and instruction == "$+" and signature == ("Ass", "Ass", "Ass") and operand_index in {0, 1} and all_tags[:3] == ["W", "W", "D"]:
        if shape != "scalar" or len(numbers) != 1 or numbers[0] < 0:
            raise _mining_error("MIL exact W operand shape is unmined", "category=mil_operand;tag=W")
        return f"W{numbers[0]:X}", 1
    return _mil_format_operand(remaining_tags, value)


def _validate_mil_exact_source_context(
    instruction: str,
    signature: tuple[str, ...],
    operand_tags: tuple[str, ...],
    operands: list[tuple[str, tuple[int, ...]]],
    record: str,
) -> None:
    """Keep source-only operand forms bound to their complete MIL context."""
    if operand_tags == ("F", "Zs"):
        operation = {
            "LD": r"lct\{op=#:lt=l:ct=a:as=\[as\{vt=Abl\}\]\}",
            "OR": r"lct\{op=#:lt=o:ct=a:as=\[as\{vt=Abl\}\]\}",
            "OUT": r"cl\{op=#:ct=a:as=\[as\{vt=Abl\}\]\}",
        }.get(instruction)
        if (
            operation is None or signature != ("Abl",)
            or re.fullmatch(
                r"mc\{op=" + operation
                + r":as=\[M\{b=d\{s=#:a=\d+:vt=nn\}:m=d\{s=#:a=\d+:vt=nn\}\}\]\}",
                record,
            ) is None
        ):
            raise _mining_error("MIL exact indexed F context is unmined", "category=mil_operand;tag=F")
    if "DX" in operand_tags:
        logic = {"ANI": "a", "ORI": "o"}.get(instruction)
        if (
            logic is None or signature != ("Abl",) or operand_tags != ("DX",)
            or re.fullmatch(
                r"mc\{op=lct\{op=#:lt=" + logic + r":ct=a:as=\[as\{vt=Abl\}\]\}:"
                r"as=\[d\{s=#:a=\d+:vt=nn\}\]\}", record,
            ) is None
        ):
            raise _mining_error("MIL exact DX context is unmined", "category=mil_operand;tag=DX")
    if operand_tags == ("D", "Y", "Ks") and not (
        instruction == "MOV" and signature == ("A16", "A16")
        and len(operands) == 2 and operands[0][0] == "scalar"
        and operands[1][0] == "k_device" and len(operands[1][1]) == 2
        and operands[1][1][1] == 2
        and re.fullmatch(
            r"mc\{op=cl\{op=#:ct=a:as=\[as\{vt=A16\}:as\{vt=A16\}\]\}:"
            r"as=\[d\{s=#:a=\d+:vt=nn\}:M\{b=d\{s=#:a=\d+:vt=nn\}:m=c\{s=#:v=2\}\}\]\}", record,
        )
    ):
        raise _mining_error("MIL grouped Y destination context is unmined", "category=mil_operand;tag=Y")
    f_contexts = {
        ("LD", ("Abl",), ("F",)),
        ("OR", ("Abl",), ("F",)),
        ("LDI", ("Abl",), ("F",)),
        ("ANI", ("Abl",), ("F",)),
        ("RST", ("Abl",), ("F",)),
        ("ANS", ("A16", "A16", "Abl"), ("T", "D", "F")),
        ("ANS", ("A16", "A16", "Abl"), ("T", "K_1", "F")),
        ("MOV", ("A16", "A16"), ("F", "Ks", "M", "Ks")),
        ("MOV", ("A16", "A16"), ("F", "Ks", "D")),
        ("ANDD=", ("A32s", "A32s"), ("F", "Ks", "K_2")),
        ("AND=", ("A16s", "A16s"), ("F", "Ks", "K_1")),
        ("AND<", ("A16s", "A16s"), ("K_1", "F", "Ks")),
        ("AND=", ("A16s", "A16s"), ("K_1", "F", "Ks")),
        ("AND<>", ("A16s", "A16s"), ("K_1", "F", "Ks")),
        ("LD", ("Abl",), ("F", "Zs")),
        ("OR", ("Abl",), ("F", "Zs")),
        ("OUT", ("Abl",), ("F", "Zs")),
    }
    if "F" in operand_tags and (instruction, signature, operand_tags) not in f_contexts:
        raise _mining_error("MIL exact F descriptor context is unmined", "category=mil_operand;tag=F")
    if (
        (instruction == "MOV" or operand_tags == ("SD", "M", "Ks"))
        and operand_tags[:1] == ("SD",)
        and operands
        and operands[0][0] == "scalar"
        and (instruction, signature, operand_tags) != ("MOV", ("A16", "A16"), ("SD", "D"))
        and not (
            instruction == "MOV" and signature == ("A16", "A16") and operand_tags == ("SD", "M", "Ks")
            and len(operands) == 2 and operands[1][0] == "k_device"
            and len(operands[1][1]) == 2 and operands[1][1][1] == 4
            and re.fullmatch(
                r"mc\{op=cl\{op=#:ct=a:as=\[as\{vt=A16\}:as\{vt=A16\}\]\}:"
                r"as=\[d\{s=#:a=\d+:vt=nn\}:M\{b=d\{s=#:a=\d+:vt=nn\}:m=c\{s=#:v=4\}\}\]\}", record,
            )
        )
    ):
        raise _mining_error("MIL exact SD descriptor context is unmined", "category=mil_operand;tag=SD")
    if (
        instruction == "MOV"
        and signature == ("A16", "A16")
        and operand_tags == ("Y", "Ks", "D")
    ):
        if tuple(operands) not in MIL_EXACT_Y_KS_MOV_OPERANDS:
            raise _mining_error("MIL exact Y K-device tuple is unmined", "category=mil_operand;tag=Y")
        base, width = operands[0][1]
        destination = operands[1][1][0]
        if re.fullmatch(
            rf"mc\{{op=cl\{{op=#:ct=a:as=\[as\{{vt=A16\}}:as\{{vt=A16\}}\]\}}:"
            rf"as=\[M\{{b=d\{{s=#:a={base}:vt=nn\}}:m=c\{{s=#:v={width}\}}\}}:"
            rf"d\{{s=#:a={destination}:vt=nn\}}\]\}}",
            record,
        ) is None:
            raise _mining_error("MIL exact Y K-device serialization is unmined", "category=mil_operand;tag=Y")
    socket_tags = SP_SOCKET_SOURCE_TAGS.get(instruction)
    if socket_tags is not None and operand_tags not in socket_tags:
        raise _mining_error(
            f"MIL {instruction} operand descriptors are unmined",
            "category=mil_operand",
        )
    if socket_tags is not None and any(
        number < 0 for _shape, numbers in operands for number in numbers
    ):
        raise _mining_error(
            f"MIL {instruction} operand value is negative",
            "category=mil_operand",
        )


def _mil_header_tokens(data: str) -> list[str]:
    prefix = data.split(":ms{el=", 1)[0]
    tokens = prefix.split(":")
    first_text = next((index for index, token in enumerate(tokens[1:], start=1) if not token.isdigit()), len(tokens))
    return tokens[first_text:]


def _mil_statement(data: str) -> str | None:
    if ":ms{el=[ma{" not in data:
        return None
    match = re.fullmatch(r"V1:\d+:\d+:\d+:(.*):([si]):ms\{el=\[ma\{.*", data)
    if match is None:
        raise _mining_error("MIL statement record is unmined", f"category=mil_statement;raw_sha256={hashlib.sha256(data.encode('utf-8')).hexdigest()}")
    return ("*" if match.group(2) == "s" else "") + match.group(1)


def _raw_descriptor_is_doubleword_comparison(signature: tuple[str, ...], *, category: str) -> bool:
    """Return the comparison width explicitly serialized by GX3."""
    numeric = tuple(tag for tag in signature if tag.startswith(("A16", "A32")))
    if not numeric:
        return False
    if len(numeric) != 2:
        raise _mining_error("GX3 comparison operand signature is unmined", f"category={category}")
    if any(tag.startswith("A32") for tag in numeric):
        if not all(tag.startswith("A32") for tag in numeric):
            raise _mining_error("GX3 comparison width is mixed", f"category={category}")
        return True
    if all(tag.startswith("A16") for tag in numeric):
        return False
    raise _mining_error("GX3 comparison operand signature is unmined", f"category={category}")


def _canonical_logic_instruction(
    logic: str,
    *,
    inverse: bool,
    comparator: str | None,
    pulse: bool | str,
    signature: tuple[str, ...],
    category: str,
) -> str:
    """Build one IL contact instruction from descriptor-complete GX3 meaning."""
    prefixes = {"l": ("LD", "LDI"), "a": ("AND", "ANI"), "o": ("OR", "ORI")}
    try:
        plain, inverted = prefixes[logic]
    except KeyError as error:
        raise _mining_error("GX3 contact logic descriptor is unmined", f"category={category}") from error
    if comparator is not None:
        if comparator not in {"=", "<>", "<", "<=", ">", ">="}:
            raise _mining_error("GX3 comparison descriptor is unmined", f"category={category}")
        width = "D" if _raw_descriptor_is_doubleword_comparison(signature, category=category) else ""
        return plain + width + comparator
    instruction = inverted if inverse else plain
    if pulse == "f":
        return {"LD": "LDF", "LDI": "LDFI", "AND": "ANDF", "ANI": "ANDFI", "OR": "ORF", "ORI": "ORFI"}[instruction]
    return (
        {"LD": "LDP", "LDI": "LDPI", "AND": "ANDP", "ANI": "ANIP", "OR": "ORP", "ORI": "ORIP"}[instruction]
        if pulse
        else instruction
    )


def _canonical_command_instruction(
    marker: str, *, pulse: bool | str, signature: tuple[str, ...] = (), lddb_serialized: bool = False
) -> str:
    """Build one IL command mnemonic from a MIL or serialized LDDB descriptor."""
    pulse_kind = "none" if pulse is False else pulse
    if marker == "FLT2INT":
        # P002's source opcode signatures share this header across four
        # destination types.  The header alone does not identify the command.
        # This resolves source meaning only; Q acceptance remains independent.
        variants = {
            ("Ar32", "A16s"): "FLT2INT",
            ("Ar32", "A16u"): "FLT2UINT",
            ("Ar32", "A32s"): "FLT2DINT",
            ("Ar32", "A32u"): "FLT2UDINT",
        }
        if signature not in variants or pulse_kind not in {"none", "rising", True}:
            raise _mining_error(
                "GX3 floating conversion signature is unmined",
                f"category=mil_operand;marker={marker};signature={','.join(signature)}",
            )
        return variants[signature] + ("P" if pulse_kind in {"rising", True} else "")
    if lddb_serialized and marker in LDDB_MINED_MARKERS:
        instruction = LDDB_MINED_COMMANDS.get((marker, pulse_kind, signature))
        if instruction is None:
            raise _mining_error(
                "GX3 command signature is unmined",
                f"category=mil_operand;marker={marker};pulse={pulse_kind};signature={','.join(signature)}",
            )
        return instruction
    aliases = {"c": "OUT", "OUT__16": "OUT", "RST__16": "RST", "ME": "MEP", "MUL": "*"}
    if lddb_serialized:
        aliases["ZRST"] = "RST"
    instruction = aliases.get(marker, marker)
    if pulse in {False, "none"}:
        return instruction
    if pulse == "falling":
        raise _mining_error("GX3 command falling-pulse descriptor is unmined", "category=mil_operand")
    if pulse not in {True, "rising"}:
        raise _mining_error("GX3 command pulse descriptor is unmined", "category=mil_operand")
    if instruction == "INC" and any(token.startswith("A32") for token in signature):
        return "DINCP"
    try:
        return MIL_PULSE_COMMAND_VARIANTS[instruction]
    except KeyError as error:
        raise _mining_error(
            "GX3 command pulse descriptor is unmined",
            f"category=mil_operand;marker={marker}",
        ) from error


def _mil_exact_source_command(
    marker: str,
    pulse_kind: str,
    signature: tuple[str, ...],
    descriptors: list[str],
) -> str | None:
    """Resolve only descriptor-complete MIL forms proven by source authority."""
    for (expected_marker, expected_pulse, expected_signature, expected_tags), instruction in MIL_EXACT_SOURCE_COMMANDS.items():
        if (
            marker == expected_marker
            and pulse_kind == expected_pulse
            and signature == expected_signature
            and descriptors[:len(expected_tags)] == list(expected_tags)
        ):
            return instruction
    return None


def _decode_mil_block(
    data: str, step_rows: list[StepInfoRow], sequence_start: int,
    *, source_operations: list[SerializedOperation] | None = None,
) -> list[Instruction]:
    if data == MIL_EXACT_NOP_DATA:
        if len(step_rows) != 1:
            raise _mining_error("MIL NOP StepInfo count is unmined", "category=mil_stepinfo")
        if source_operations is not None:
            source_operations.append(SerializedOperation("mil", "NOP", ("NOP",), variant="NOP"))
        return [Instruction(sequence_start, 0, "NOP", "")]
    statement = _mil_statement(data)
    if statement is not None:
        if len(step_rows) != 1:
            raise _mining_error("MIL statement StepInfo count is unmined", "category=mil_stepinfo")
        if source_operations is not None:
            source_operations.append(SerializedOperation("mil_statement", "STATEMENT", (), variant="statement"))
        return [Instruction(sequence_start, 0, "", "", line_statement=statement)]
    records = _balanced_records(data, "mc{")
    if not records:
        if _mil_header_tokens(data) == ["END"] and len(step_rows) == 1:
            if source_operations is not None:
                source_operations.append(SerializedOperation("mil", "END", ("END",), variant="END"))
            return [Instruction(sequence_start, 0, "END", "")]
        raise _mining_error("MIL has no decodable instruction records", f"category=mil_syntax;raw_sha256={hashlib.sha256(data.encode('utf-8')).hexdigest()}")
    inline_note_count = data.count(":ma{k=@FE/NOTE")
    if len(records) + inline_note_count != len(step_rows):
        raise _mining_error("MIL and StepInfo instruction counts differ", f"category=mil_stepinfo;mil_count={len(records)};step_count={len(step_rows)}")
    tokens = _mil_header_tokens(data)
    cursor = 0
    decoded: list[Instruction] = []
    comparator_suffix = {"EQ": "=", "NE": "<>", "LT": "<", "LE": "<=", "GT": ">", "GE": ">="}
    record_index = 0
    while record_index < len(records):
        # GX Works3 keeps a Note as ``ma{@FE/NOTE...}`` inside the MIL block,
        # while StepInfo gives it its own row and the official CSV emits a
        # blank-step continuation.  The matching header representation is a
        # text token followed by i/s.  Preserve that row explicitly instead of
        # shifting subsequent instruction descriptors.
        while (
            cursor + 1 < len(tokens)
            and tokens[cursor + 1] in {"i", "s"}
            and tokens[cursor] not in MIL_SUPPORTED_INSTRUCTIONS
        ):
            decoded.append(Instruction(sequence_start + len(decoded), 0, "", "", note=tokens[cursor], note_subtype=tokens[cursor + 1]))
            if source_operations is not None:
                source_operations.append(SerializedOperation("mil_note", "NOTE", ("NOTE", f"st={tokens[cursor + 1]}"), variant="note"))
            cursor += 2
        record = records[record_index]
        record_index += 1
        operands = _mil_operands(record)
        signature = tuple(re.findall(r"as\{vt=([^}:]+)\}", record))
        if cursor >= len(tokens):
            raise _mining_error("MIL header has fewer descriptors than records", "category=mil_header")
        descriptor_start = cursor
        marker = tokens[cursor]
        cursor += 1
        match = re.search(r"op=lct\{op=#:lt=([lao]):", record)
        pulse_match = re.search(r"ct=([afp])(?=[:}])", record)
        if pulse_match is None:
            raise _mining_error("MIL contact pulse descriptor is unmined", "category=mil_operand")
        pulse_kind = {"a": "none", "p": "rising", "f": "falling"}[pulse_match.group(1)]
        pulse = "f" if pulse_kind == "falling" else pulse_kind == "rising"
        if match is not None:
            logic = match.group(1)
            if marker in {"A", "B"}:
                instruction = _canonical_logic_instruction(
                    logic,
                    inverse=marker == "B",
                    comparator=None,
                    pulse=pulse,
                    signature=signature,
                    category="mil_operand",
                )
            elif marker in comparator_suffix:
                if pulse_kind != "none":
                    raise _mining_error("MIL comparison pulse descriptor is unmined", "category=mil_operand")
                instruction = _canonical_logic_instruction(
                    logic,
                    inverse=False,
                    comparator=comparator_suffix[marker],
                    pulse=pulse,
                    signature=signature,
                    category="mil_operand",
                )
            else:
                raise _mining_error("MIL logical instruction marker is unmined", f"category=mil_header;marker={marker}")
        elif "op=sct{" in record:
            instruction = {"ME": "MEP"}.get(marker, marker)
        elif "op=cl{" in record:
            if marker == "FOR":
                scalar_value = (
                    operands[0][1][0]
                    if len(operands) == 1
                    and operands[0][0] == "scalar"
                    and len(operands[0][1]) == 1
                    else None
                )
                expanded_for = (
                    scalar_value is not None
                    and tokens[cursor:cursor + 2] == ["K_1", str(scalar_value)]
                    and f":as=[c{{s=#:v={scalar_value}:t=#:si=s}}]" in record
                )
                compact_for = (
                    scalar_value is not None
                    and tokens[cursor:cursor + 1] == ["K_1"]
                    and f":as=[c{{s=#:v={scalar_value}:si=s}}]" in record
                )
                device_for = (
                    scalar_value is not None
                    and scalar_value >= 0
                    and tokens[cursor:cursor + 1] == ["D"]
                    and f":as=[d{{s=#:a={scalar_value}:vt=nn}}]" in record
                )
                exact_for = (
                    pulse_kind == "none"
                    and signature == ("A16",)
                    and (expanded_for or compact_for or device_for)
                )
                if not exact_for:
                    raise _mining_error("MIL exact FOR serialization is unmined", "category=mil_operand;marker=FOR")
            instruction = _mil_exact_source_command(marker, pulse_kind, signature, tokens[cursor:])
            if marker in {"PLSV", "DSZR", "DRVA"}:
                # The MIL marker omits the double-word prefix. Preserve the
                # exact observed width and raw scalar form in the independent
                # source model before comparing it with the pinned reader.
                kinds = ("d", "d", "d") if marker == "PLSV" else ("d", "d", "c", "d")
                operation = "mc{op=cl{op=#:ct=a:as=[" + ":".join(f"as{{vt={vt}}}" for vt in signature) + "]}:as=["
                pattern = re.escape(operation) + ":".join(
                    r"d\{s=#:a=\d+:vt=nn\}" if kind == "d" else r"c\{s=#:v=\d+:si=s\}"
                    for kind in kinds
                ) + r"\]\}"
                if instruction is None or re.fullmatch(pattern, record) is None:
                    raise _mining_error("MIL positioning serialization is unmined", "category=mil_operand")
            if instruction is None:
                if any(
                    marker == expected_marker
                    and pulse_kind == expected_pulse
                    and signature == expected_signature
                    for expected_marker, expected_pulse, expected_signature, _expected_tags in MIL_EXACT_SOURCE_COMMANDS
                ):
                    raise _mining_error(
                        "MIL exact command operand descriptors are unmined",
                        f"category=mil_operand;marker={marker}",
                    )
                instruction = _canonical_command_instruction(marker, pulse=pulse_kind, signature=signature)
            if instruction in SP_SOCKET_SOURCE_SIGNATURES:
                expected_signature, projected_instruction = SP_SOCKET_SOURCE_SIGNATURES[instruction]
                if signature != expected_signature:
                    raise _mining_error(
                        f"MIL {projected_instruction} operand signature is unmined",
                        "category=mil_operand",
                    )
                instruction = projected_instruction
        else:
            raise _mining_error("MIL instruction form is unmined", f"category=mil_syntax;marker={marker}")
        if instruction in {"AddOpe", "SubOpe", "MulOpe", "DivOpe"}:
            arithmetic = {
                ("AddOpe", ("A16s", "A16s", "A16s")): "+",
                ("AddOpe", ("A32s", "A32s", "A32s")): "D+",
                ("AddOpe", ("Ass", "Ass")): "$+",
                ("AddOpe", ("Ass", "Ass", "Ass")): "$+",
                ("SubOpe", ("A16s", "A16s", "A16s")): "-",
                ("SubOpe", ("A32s", "A32s", "A32s")): "D-",
                ("MulOpe", ("A16s", "A16s", "A32s")): "*",
                ("MulOpe", ("A32s", "A32s", "A32sa")): "D*",
                ("DivOpe", ("A32s", "A32s", "A32sa")): "D/",
            }
            try:
                instruction = arithmetic[(instruction, signature)]
            except KeyError as error:
                raise _mining_error("MIL arithmetic operand signature is unmined", "category=mil_operand") from error
        if marker == "VAL" and len(signature) == 3:
            pulse_suffix = "P" if pulse_kind != "none" else ""
            width_prefix = "D" if signature[2].startswith("A32") else ""
            unsigned_suffix = "_U" if signature[2].endswith("u") else ""
            instruction = f"{width_prefix}VAL{pulse_suffix}{unsigned_suffix}"
        if "A32" in record:
            if instruction == "MOV":
                instruction = "DMOV"
            elif instruction == "MOVP":
                instruction = "DMOVP"
        rendered: list[str] = []
        operand_descriptor_start = cursor
        operand_tags = tokens[cursor:]
        for operand_index, operand in enumerate(operands):
            value, consumed = _mil_format_operand_in_context(
                instruction, signature, operand_tags, operand_index, tokens[cursor:], operand,
            )
            rendered.append(value)
            cursor += consumed
        _validate_mil_exact_source_context(
            instruction,
            signature,
            tuple(tokens[operand_descriptor_start:cursor]),
            operands,
            record,
        )
        source_semantic_tags: tuple[str, ...] = ()
        if tuple(tokens[operand_descriptor_start:cursor]) == ("D", "Y", "Ks"):
            source_semantic_tags = (MIL_GROUPED_Y_DEST_PROVENANCE,)
        if (
            instruction == "MOV"
            and signature == ("A16", "A16")
            and tuple(tokens[operand_descriptor_start:cursor]) == ("Y", "Ks", "D")
            and tuple(operands) in MIL_EXACT_Y_KS_MOV_OPERANDS
        ):
            source_semantic_tags = (MIL_EXACT_Y_KS_MOV_PROVENANCE,)
        decoded.append(Instruction(
            sequence_start + len(decoded), 0, instruction, rendered[0] if rendered else "",
            continuation_operands=tuple(rendered[1:]), source_semantic_tags=source_semantic_tags,
        ))
        if source_operations is not None:
            width = "A32" if any(item.startswith("A32") for item in signature) else ("A16" if any(item.startswith("A16") for item in signature) else "")
            signedness = "signed" if any(item.endswith("s") for item in signature) else ""
            source_operations.append(
                SerializedOperation(
                    "mil", marker, tuple(tokens[descriptor_start:cursor]),
                    pulse=pulse_kind != "none", pulse_kind=pulse_kind,
                    inverse=marker == "B", width=width, signedness=signedness, variant=instruction,
                )
            )
    while (
        cursor + 1 < len(tokens)
        and tokens[cursor + 1] in {"i", "s"}
        and tokens[cursor] not in MIL_SUPPORTED_INSTRUCTIONS
    ):
        decoded.append(Instruction(sequence_start + len(decoded), 0, "", "", note=tokens[cursor], note_subtype=tokens[cursor + 1]))
        if source_operations is not None:
            source_operations.append(SerializedOperation("mil_note", "NOTE", ("NOTE", f"st={tokens[cursor + 1]}"), variant="note"))
        cursor += 2
    if len(decoded) != len(step_rows):
        raise _mining_error(
            "MIL and StepInfo annotation counts differ",
            f"category=mil_stepinfo;decoded_count={len(decoded)};step_count={len(step_rows)}",
        )
    if cursor != len(tokens):
        raise _mining_error("MIL header has unused descriptors", f"category=mil_header;unused_sha256={_canonical_sha256(tokens[cursor:])}")
    if source_operations is not None and len(source_operations) != len(decoded):
        raise RuntimeError("MIL typed operation inventory differs from decoded rows")
    return decoded


def _parse_mil_blocks(body: bytes, structure: ProgramStructure) -> MilParseResult:
    """Decode linked MIL blocks while preserving unmined blocks as opaque rows.

    Archive and block identity relations are a global safety boundary.  Once
    they pass, a semantic decoder failure belongs to one MIL block only: later
    linked blocks continue decoding and the withheld StepInfo row range remains
    visible in a separate review artifact.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(body)
        connection.execute("PRAGMA query_only = ON")
        if list(connection.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ConversionError("MilDB.db integrity check failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "MIL" not in tables or not {"id", "pos", "data"}.issubset(_sqlite_table_columns(connection, "MIL")):
            raise _mining_error("MilDB.db has an unmined schema", "category=mil_schema")
        records = list(connection.execute("SELECT id,pos,data FROM MIL ORDER BY pos,id"))
    finally:
        connection.close()
    by_id: dict[str, tuple[float, str]] = {}
    for record_id, position, data in records:
        normalized = _canonical_block_id(record_id)
        if normalized is None or not isinstance(position, (int, float)) or not isinstance(data, str) or normalized in by_id:
            raise _mining_error("MilDB.db contains an unsupported MIL row", _mining_candidate(record_id, data))
        by_id[normalized] = (float(position), data)
    ladder_ids = [block.record_id for block in structure.blocks]
    if set(by_id) != set(ladder_ids):
        raise _mining_error(
            "MIL and LadderBlocks identifiers differ",
            f"category=mil_relation;mil_count={len(by_id)};ladder_count={len(ladder_ids)};"
            f"mil_ids_sha256={_canonical_sha256(sorted(by_id))};ladder_ids_sha256={_canonical_sha256(sorted(ladder_ids))}",
        )
    rows_by_block: dict[str, list[StepInfoRow]] = {}
    for row in structure.step_info.rows:
        rows_by_block.setdefault(row.block_id, []).append(row)
    decoded: list[Instruction] = []
    source_operations: list[SerializedOperation] = []
    opaque_blocks: list[OpaqueBlock] = []
    direct_blocks = 0
    sequence = 0
    step = 0
    for block in structure.blocks:
        mil_position, data = by_id[block.record_id]
        if mil_position != block.position:
            raise _mining_error("MIL and LadderBlocks positions differ", f"category=mil_relation;record_id={block.record_id}")
        block_rows = rows_by_block.get(block.record_id, [])
        try:
            block_operations: list[SerializedOperation] = []
            block_instructions = _decode_mil_block(data, block_rows, sequence, source_operations=block_operations)
            if len(block_instructions) != len(block_rows):
                raise _mining_error(
                    "MIL emitted an incomplete StepInfo block",
                    f"category=mil_stepinfo;record_id={block.record_id}",
                )
            for instruction, step_row in zip(block_instructions, block_rows, strict=True):
                decoded.append(replace(instruction, source_step=step))
                step += step_row.step_size or 0
            source_operations.extend(block_operations)
            direct_blocks += 1
        except ConversionError as error:
            opaque_blocks.append(
                OpaqueBlock(
                    sequence,
                    step,
                    len(block_rows),
                    block.record_id,
                    block.position,
                    str(error),
                    error.finding_code,
                    error.mining_tracks,
                    error.candidate,
                )
            )
            step += sum(row.step_size or 0 for row in block_rows)
        sequence += len(block_rows)
    result = MilParseResult(tuple(decoded), tuple(opaque_blocks), direct_blocks=direct_blocks, source_operations=tuple(source_operations))
    expected_rows = sum(len(rows_by_block.get(block.record_id, [])) for block in structure.blocks)
    if result.expected_rows != expected_rows:
        raise RuntimeError("MIL recovery inventory invariant failed")
    return result


def _mil_row_count(body: bytes) -> int:
    """Return the validated MIL row count used by the source-build gate."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(body)
        connection.execute("PRAGMA query_only = ON")
        if list(connection.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ConversionError("MilDB.db integrity check failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "MIL" not in tables or not {"id", "pos", "data"}.issubset(_sqlite_table_columns(connection, "MIL")):
            raise _mining_error("MilDB.db has an unmined schema", "category=mil_schema")
        return int(connection.execute("SELECT COUNT(*) FROM MIL").fetchone()[0])
    finally:
        connection.close()


def _mil_relates_to_ladder_blocks(body: bytes, structure: ProgramStructure) -> bool:
    """Return whether the root MIL database is the linked projection of this POU.

    Older narrow POC archives contain a root ``*_MilDB.db`` whose records do
    not belong to the selected POU.  It must not be decoded as ladder logic,
    but its mere presence must not make an otherwise valid legacy archive
    unreadable.  A matching relation is deliberately strict; once matched,
    decoding failures remain fail-closed in ``_parse_mil_blocks``.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(body)
        connection.execute("PRAGMA query_only = ON")
        if list(connection.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ConversionError("MilDB.db integrity check failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "MIL" not in tables or not {"id", "pos", "data"}.issubset(_sqlite_table_columns(connection, "MIL")):
            raise _mining_error("MilDB.db has an unmined schema", "category=mil_schema")
        records = list(connection.execute("SELECT id,pos,data FROM MIL ORDER BY pos,id"))
    finally:
        connection.close()
    linked = {block.record_id: block.position for block in structure.blocks}
    observed: dict[str, float] = {}
    for record_id, position, data in records:
        normalized = _canonical_block_id(record_id)
        if normalized is None or not isinstance(position, (int, float)) or not isinstance(data, str) or normalized in observed:
            raise _mining_error("MilDB.db contains an unsupported MIL row", _mining_candidate(record_id, data))
        observed[normalized] = float(position)
    return observed == linked


def _lddb_bracket_payload(text: str, marker: str) -> str:
    """Return one balanced ``marker=[...]`` payload without guessing syntax."""
    start = text.find(marker)
    if start < 0:
        raise _mining_error("LDDB element has no argument payload", "category=lddb_syntax")
    bracket = start + len(marker) - 1
    depth = 0
    for end in range(bracket, len(text)):
        if text[end] == "[":
            depth += 1
        elif text[end] == "]":
            depth -= 1
            if depth == 0:
                return text[bracket + 1 : end]
    raise _mining_error("LDDB argument payload is unbalanced", "category=lddb_syntax")


def _lddb_top_level_values(payload: str) -> list[str]:
    """Split brace-balanced LDDB arguments at the list's top level."""
    values: list[str] = []
    cursor = 0
    while cursor < len(payload):
        if payload[cursor] in ": ":
            cursor += 1
            continue
        start = cursor
        brace = payload.find("{", cursor)
        if brace < cursor:
            raise _mining_error("LDDB argument form is unmined", "category=lddb_operand")
        depth = 0
        for cursor in range(brace, len(payload)):
            if payload[cursor] == "{":
                depth += 1
            elif payload[cursor] == "}":
                depth -= 1
                if depth == 0:
                    values.append(payload[start : cursor + 1])
                    cursor += 1
                    break
        else:
            raise _mining_error("LDDB argument is unbalanced", "category=lddb_operand")
    return values


@dataclass(frozen=True)
class _LddbGridElement:
    """One raw LDDB grid element before descriptor semantics are rendered."""

    x: int
    y: int
    kind: str
    pulse: str
    signature: tuple[str, ...]
    args: list[str]
    has_note: bool
    note_subtype: str = ""


def _lddb_elements(data: str) -> tuple[list[_LddbGridElement], list[tuple[int, int]]]:
    """Read only the observed ``ct``/``cl`` grid representation.

    The textual descriptor stream is used for instruction names and operand
    kinds; grid positions are used solely to produce the observed IL stack
    forms.  Anything outside this small grammar remains opaque.
    """
    if ":cb{fg=fg{" not in data:
        raise _mining_error("LDDB logic block has no observed grid", "category=lddb_syntax")
    elements: list[_LddbGridElement] = []
    for record in _balanced_records(data[data.index(":cb{fg=fg{") :], "e{"):
        positions = list(re.finditer(r":pos=(\d+),(\d+)(?=[:}])", record))
        if not positions:
            raise _mining_error("LDDB grid element position is unmined", "category=lddb_graph")
        position = positions[-1]
        position_suffix = record[position.end() :]
        if position_suffix == "}":
            note_subtype = ""
        else:
            note_marker = re.fullmatch(r":note=c\{t=#:st=([^}:]+)\}\}", position_suffix)
            if note_marker is None:
                raise _mining_error("LDDB inline note placement is unmined", "category=lddb_note")
            note_subtype = note_marker.group(1)
            if note_subtype not in {"i", "s"}:
                raise _mining_error(
                    "LDDB inline note subtype is unmined",
                    f"category=lddb_note;subtype={note_subtype}",
                )
        if record.startswith("e{s=-:"):
            continue
        operation = re.search(r"op=(ct|cl)\{op=#:ct=([afp])(?=[:}])", record)
        if operation is None:
            raise _mining_error("LDDB grid operation is unmined", "category=lddb_graph")
        kind, contact_type = operation.groups()
        if contact_type == "f" and kind != "ct":
            raise _mining_error("LDDB falling command operation is unmined", "category=lddb_graph")
        arguments = (
            _lddb_top_level_values(_lddb_bracket_payload(record, ":args=["))
            if ":args=[" in record
            else []
        )
        has_note = bool(note_subtype)
        if has_note and (
            kind != "cl"
            or ":args=[" not in record
            or ("]:note=c{t=#:st=" + note_subtype + "}}:pos=") not in record
        ):
            raise _mining_error("LDDB inline note placement is unmined", "category=lddb_note")
        elements.append(
            _LddbGridElement(
                int(position.group(1)),
                int(position.group(2)),
                kind,
                contact_type,
                tuple(re.findall(r"as\{vt=([^}:]+)\}", record)),
                arguments,
                has_note,
                note_subtype,
            )
        )
    vertical = [(int(x), int(y)) for x, y in re.findall(r"v\{pos=(\d+),(\d+)\}", data)]
    if not elements:
        raise _mining_error("LDDB logic block has no grid elements", "category=lddb_graph")
    return elements, vertical


def _lddb_descriptor_tokens(data: str) -> list[str]:
    prefix = data.split(":cb{fg=fg{", 1)[0]
    tokens = prefix.split(":")
    if not tokens or tokens[0] != "V1":
        raise _mining_error("LDDB logic header is unmined", "category=lddb_syntax")
    first_descriptor = next((index for index, token in enumerate(tokens[1:], start=1) if not token.isdigit()), len(tokens))
    descriptors = tokens[first_descriptor:]
    if not descriptors:
        raise _mining_error("LDDB logic header has no descriptors", "category=lddb_syntax")
    return descriptors


def _lddb_decimal_argument(value: str) -> int:
    match = re.fullmatch(r"[dc]\{s=#:([av])=(-?\d+):(?:vt=nn|si=(s|u))\}", value)
    if match is None:
        raise _mining_error(
            "LDDB scalar operand is unmined",
            f"category=lddb_operand;raw_sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()}",
        )
    number = int(match.group(2))
    if match.group(3) == "u" and number < 0:
        raise _mining_error("LDDB unsigned scalar operand is negative", "category=lddb_operand")
    return number


def _lddb_xy_spelling(tag: str, number: int) -> str:
    """Recover observed LDDB display digits, not a comment-DB ordinal.

    Issue #30 pairs LDDB a=1143 with official X477. MIL has a separate
    source decoder; comment DB retains its octal-ordinal contract. Q range
    validation follows source recovery and is not part of this helper.
    """
    digits = f"{number:X}"
    if tag not in {"X", "Y"} or re.fullmatch(r"[0-7]+", digits) is None:
        raise _mining_error(
            "LDDB X/Y source spelling is invalid",
            f"category=lddb_operand;tag={tag}",
        )
    return f"{tag}{digits}"


def _lddb_format_operand(tag: str, value: str) -> str:
    """Render the directly observed scalar LDDB operands.

    Indexed/macro composite values intentionally do not enter this path: their
    serialized tag sequence is not one-to-one with a device operand.
    """
    # F/DX devices and the H_2 constant use distinct exact LDDB forms.  Do not
    # admit them through the generic scalar parser: that would erase the
    # source radix or accept an unobserved signedness/type variation.
    if tag in {"F", "DX"}:
        device = re.fullmatch(r"d\{s=#:a=(\d+):vt=nn\}", value)
        if device is None:
            raise _mining_error("LDDB scalar operand is unmined", f"category=lddb_operand;tag={tag}")
        number = int(device.group(1))
        return f"F{number}" if tag == "F" else f"DX{number:X}"
    if tag == "H_2":
        unsigned_hex = re.fullmatch(r"c\{s=#:v=(\d+):si=u\}", value)
        if unsigned_hex is None:
            raise _mining_error("LDDB scalar operand is unmined", "category=lddb_operand;tag=H_2")
        number = int(unsigned_hex.group(1))
        if number > 0xFFFFFFFF:
            raise _mining_error("LDDB H_2 constant exceeds 32 bits", "category=lddb_operand;tag=H_2")
        return f"H{number:X}"
    if tag == "K_1":
        typed_decimal = re.fullmatch(r"c\{s=#:v=(-?\d+):t=#:si=s\}", value)
        if typed_decimal is not None:
            number = int(typed_decimal.group(1))
            if not -0x8000 <= number <= 0x7FFF:
                raise _mining_error("LDDB typed K_1 exceeds A16 signed range", "category=lddb_operand;tag=K_1")
            return f"K{number}"
    number = _lddb_decimal_argument(value)
    if tag in {"K_1", "K_2"}:
        return f"K{number}"
    if tag == "H_1":
        return f"H{number:X}"
    if tag == "B":
        return f"B{number:X}"
    if tag in {"X", "Y"}:
        return _lddb_xy_spelling(tag, number)
    if tag in MIL_SCALAR_TAGS:
        return f"{tag}{number}"
    raise _mining_error("LDDB operand tag is unmined", f"category=lddb_operand;tag={tag}")


def _lddb_format_operand_at(
    descriptors: list[str], cursor: int, value: str, *,
    instruction: str = "", signature: tuple[str, ...] = (), operand_index: int = -1,
    operand_tags: tuple[str, ...] = (),
) -> tuple[str, int]:
    """Consume one scalar or observed two-descriptor composite operand."""
    if cursor >= len(descriptors):
        raise _mining_error("LDDB operand descriptor stream is shorter than its grid", "category=lddb_header")
    tag = descriptors[cursor]
    if value == "c{s=#:v=#:t=#}":
        triple = tuple(descriptors[cursor:cursor + 3])
        bare = triple[1] if len(triple) == 3 else ""
        quoted = triple[2] if len(triple) == 3 else ""
        if (
            len(triple) != 3
            or triple[0] != "String"
            or re.fullmatch(r"[A-Za-z0-9_\-,$]+", bare) is None
            or quoted != f'"{bare}"'
        ):
            raise _mining_error(
                "LDDB String placeholder descriptor is unmined",
                "category=lddb_operand;tag=String",
            )
        return quoted, cursor + 3
    typed_decimal = re.fullmatch(r"c\{s=#:v=(-?\d+):t=#:si=s\}", value)
    if tag == "K_1" and typed_decimal is not None:
        if descriptors[cursor + 1:cursor + 2] != [typed_decimal.group(1)]:
            raise _mining_error("LDDB typed K_1 descriptor differs", "category=lddb_operand;tag=K_1")
        return _lddb_format_operand(tag, value), cursor + 2

    # Remote U#\\G# operands are serialized as a typed two-number base rather
    # than two independent scalar descriptors.  A following ``G`` descriptor
    # is mandatory; accepting the shape without it would silently shift every
    # later operand.
    remote = re.fullmatch(
        r"B\{b=d\{s=#:a=(-?\d+):vt=nn\}:e=d\{s=#:a=(-?\d+):vt=nn\}:vt=(?:i|wd)\}",
        value,
    )
    if remote is not None and tag == "Us" and cursor + 1 < len(descriptors) and descriptors[cursor + 1] == "G":
        unit, address = (int(item) for item in remote.groups())
        return f"U{unit}\\G{address}", cursor + 2

    indexed = re.fullmatch(
        r"M\{b=d\{s=#:a=(-?\d+):vt=nn\}:m=d\{s=#:a=(-?\d+):vt=nn\}\}",
        value,
    )
    if indexed is not None and tag in {"B", "D", "F", "L", "M", "R", "T"} and cursor + 1 < len(descriptors) and descriptors[cursor + 1] == "Zs":
        base, index = (int(item) for item in indexed.groups())
        if tag in {"B", "F"} and (base < 0 or index < 0):
            raise _mining_error("LDDB mined indexed device is negative", f"category=lddb_operand;tag={tag}")
        if tag == "F":
            if (
                instruction not in {"LD", "OUT"}
                or signature != ("Abl",)
                or operand_index != 0
                or operand_tags[:2] != ("F", "Zs")
            ):
                raise _mining_error("LDDB mined indexed F context is unmined", "category=lddb_operand;tag=F")
            return f"F{base}Z{index}", cursor + 2
        rendered_base = f"{base:X}" if tag == "B" else str(base)
        return f"{tag}{rendered_base}Z{index}", cursor + 2
    if (
        indexed is not None
        and tag == "SD"
        and cursor + 1 < len(descriptors)
        and descriptors[cursor + 1] == "Zs"
        and operand_index in {0, 1, 2}
        and (
            (instruction == "DMOV" and signature == ("A32", "A32"))
            or (instruction == "MOV" and signature == ("A16", "A16"))
            or (instruction == "D/" and signature == ("A32s", "A32s", "A32sa"))
        )
    ):
        base, index = (int(item) for item in indexed.groups())
        exact_tags = operand_tags[:4]
        exact_indexed_shape = exact_tags == ("SD", "Zs", "D", "Zs")
        reverse_exact_shape = (
            instruction in {"MOV", "DMOV"}
            and signature in {("A16", "A16"), ("A32", "A32")}
            and operand_index == 1
            and operand_tags == ("D", "Zs", "SD", "Zs")
        )
        division_exact_shape = (
            instruction == "D/"
            and signature == ("A32s", "A32s", "A32sa")
            and operand_index == 2
            and operand_tags == ("D", "Zs", "K_2", "SD", "Zs")
        )
        old_fixed_shape = (
            instruction == "DMOV"
            and signature == ("A32", "A32")
            and (base, index) == (5500, 1)
            and (
                not operand_tags or operand_tags[:3] == ("SD", "Zs", "D")
            )
        )
        if (
            min(base, index) < 0
            or (
                not exact_indexed_shape
                and not reverse_exact_shape
                and not division_exact_shape
                and not old_fixed_shape
            )
        ):
            raise _mining_error("LDDB indexed SD value is unmined", "category=lddb_operand;tag=SD")
        return f"SD{base}Z{index}", cursor + 2

    indexed_k_device = re.fullmatch(
        r"M\{b=M\{b=d\{s=#:a=(-?\d+):vt=nn\}:m=d\{s=#:a=(-?\d+):vt=nn\}\}:m=c\{s=#:v=(-?\d+)\}\}",
        value,
    )
    if indexed_k_device is not None and tag in {"M", "L", "B"} and cursor + 2 < len(descriptors) and descriptors[cursor + 1:cursor + 3] == ["Zs", "Ks"]:
        base, index, width = (int(item) for item in indexed_k_device.groups())
        if not 1 <= width <= 8:
            raise _mining_error("LDDB indexed K-device width is unmined", "category=lddb_operand")
        rendered_base = f"{base:X}" if tag == "B" else str(base)
        return f"K{width}{tag}{rendered_base}Z{index}", cursor + 3

    remote_indexed = re.fullmatch(
        r"M\{b=(B\{b=d\{s=#:a=(-?\d+):vt=nn\}:e=d\{s=#:a=(-?\d+):vt=nn\}:vt=(?:i|wd)\}):m=d\{s=#:a=(-?\d+):vt=nn\}\}",
        value,
    )
    if remote_indexed is not None and tag == "Us" and cursor + 2 < len(descriptors) and descriptors[cursor + 1:cursor + 3] == ["G", "Zs"]:
        _base, unit, address, index = remote_indexed.groups()
        return f"U{int(unit)}\\G{int(address)}Z{int(index)}", cursor + 3

    remote_bit = re.fullmatch(
        r"M\{b=(B\{b=d\{s=#:a=(-?\d+):vt=nn\}:e=d\{s=#:a=(-?\d+):vt=nn\}\}):m=c\{s=#:v=(-?\d+)\}\}",
        value,
    )
    if remote_bit is not None and tag == "Us" and cursor + 2 < len(descriptors) and descriptors[cursor + 1:cursor + 3] == ["G", "Dots"]:
        _base, unit, address, bit = remote_bit.groups()
        if not 0 <= int(bit) <= 15:
            raise _mining_error("LDDB remote bit index is unmined", "category=lddb_operand")
        return f"U{int(unit)}\\G{int(address)}.{int(bit):X}", cursor + 3

    composite = re.fullmatch(
        r"M\{b=d\{s=#:a=(-?\d+):vt=nn\}:m=c\{s=#:v=(-?\d+)\}\}",
        value,
    )
    if composite is not None and cursor + 1 < len(descriptors):
        base, modifier = (int(item) for item in composite.groups())
        suffix = descriptors[cursor + 1]
        if suffix == "Dots" and tag in {"D", "R", "SD"} and 0 <= modifier <= 15:
            return f"{tag}{base}.{modifier:X}", cursor + 2
        if suffix == "Ks" and tag in {"M", "L", "B", "X", "Y"} and 1 <= modifier <= 8:
            if tag in {"X", "Y"}:
                return f"K{modifier}{_lddb_xy_spelling(tag, base)}", cursor + 2
            number = f"{base:X}" if tag == "B" else str(base)
            return f"K{modifier}{tag}{number}", cursor + 2
        f_ks_context = (
            (instruction == "LDD=" and signature == ("A32s", "A32s") and operand_index == 0 and operand_tags[:3] == ("F", "Ks", "K_2"))
            or (instruction == "MOV" and signature == ("A16", "A16") and operand_index == 0 and operand_tags[:4] == ("F", "Ks", "M", "Ks"))
            or (instruction == "MOV" and signature == ("A16", "A16") and operand_index == 0 and operand_tags[:3] == ("F", "Ks", "D"))
            or (instruction == "LD=" and signature == ("A16s", "A16s") and operand_index == 0 and operand_tags[:3] == ("F", "Ks", "K_1"))
            or (instruction in {"LD<", "LD=", "LD<>"} and signature == ("A16s", "A16s") and operand_index == 1 and operand_tags[:3] == ("K_1", "F", "Ks"))
        )
        if suffix == "Ks" and tag == "F" and f_ks_context and 1 <= modifier <= 8 and base >= 0:
            return f"K{modifier}F{base}", cursor + 2
    return _lddb_format_operand(tag, value), cursor + 1


def _observed_lddb_oracle_fingerprint(block: LadderBlock, step_rows: list[StepInfoRow]) -> dict[str, object]:
    return {
        "data_sha256": block.data_sha256,
        "blocktype": block.blocktype,
        "row_count": len(step_rows),
        "row_shape": [[row.mil_id, row.step_size] for row in step_rows],
    }


def _observed_lddb_oracle_key(fingerprint: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(fingerprint, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _observed_lddb_oracle():
    raise _mining_error("Site recovery evidence is unavailable in the public edition", "category=observed_block_oracle")


def _decode_observed_lddb_oracle(block: LadderBlock, step_rows: list[StepInfoRow], sequence_start: int) -> list[Instruction]:
    """Recover one exact previously observed block, never a near match."""
    fingerprint = _observed_lddb_oracle_fingerprint(block, step_rows)
    key = _observed_lddb_oracle_key(fingerprint)
    entry = _observed_lddb_oracle().get(key)
    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        raise _mining_error(
            "LDDB graph has no exact observed-block oracle",
            f"category=observed_block_oracle;key={key}",
        )
    rows = entry.get("instructions")
    deltas = entry.get("step_deltas")
    if not isinstance(rows, list) or not isinstance(deltas, list) or len(rows) != len(step_rows) or deltas != [row.step_size for row in step_rows]:
        raise _mining_error("observed LDDB oracle row contract is invalid", f"category=observed_block_oracle;key={key}")
    decoded: list[Instruction] = []
    for offset, row in enumerate(rows):
        if (
            not isinstance(row, list)
            or len(row) != 6
            or not all(isinstance(value, str) for value in row[:5])
            or not isinstance(row[5], list)
            or not all(isinstance(value, str) for value in row[5])
        ):
            raise _mining_error("observed LDDB oracle instruction contract is invalid", f"category=observed_block_oracle;key={key}")
        decoded.append(Instruction(sequence_start + offset, 0, row[0], row[1], row[2], row[3], row[4], tuple(row[5])))
    return decoded


def _lddb_validate_mined_scalar_context(
    instruction: str,
    signature: tuple[str, ...],
    tag: str,
    operand_index: int,
    following_tag: str = "",
    operand_tags: tuple[str, ...] = (),
) -> None:
    """Bind newly mined scalar tags to their observed instruction positions."""
    if tag == "F":
        approved = (
            (instruction in {"LDI", "ANI", "RST"} and signature == ("Abl",) and operand_index == 0)
            or (instruction == "ANS" and signature == ("A16", "A16", "Abl") and operand_index == 2)
            or (instruction in {"LD", "OUT"} and signature == ("Abl",) and operand_index == 0 and operand_tags == ("F",))
            or (instruction in {"LD", "OUT"} and signature == ("Abl",) and operand_index == 0 and following_tag == "Zs" and operand_tags == ("F", "Zs"))
            or (instruction == "LDD=" and signature == ("A32s", "A32s") and operand_index == 0 and following_tag == "Ks" and operand_tags[:3] == ("F", "Ks", "K_2"))
            or (instruction == "MOV" and signature == ("A16", "A16") and operand_index == 0 and following_tag == "Ks" and operand_tags[:4] == ("F", "Ks", "M", "Ks"))
            or (instruction == "MOV" and signature == ("A16", "A16") and operand_index == 0 and following_tag == "Ks" and operand_tags == ("F", "Ks", "D"))
            or (instruction == "LD=" and signature == ("A16s", "A16s") and operand_index == 0 and following_tag == "Ks" and operand_tags == ("F", "Ks", "K_1"))
            or (instruction in {"LD<", "LD=", "LD<>"} and signature == ("A16s", "A16s") and operand_index == 1 and following_tag == "Ks" and operand_tags[:3] == ("K_1", "F", "Ks"))
        )
    elif tag == "DX":
        approved = instruction == "LDI" and signature == ("Abl",) and operand_index == 0
    elif tag == "H_2":
        approved = instruction == "DMOVP" and signature == ("A32", "A32") and operand_index == 0
    elif tag == "SD" and following_tag == "Zs":
        approved = (
            (
                instruction in {"MOV", "DMOV"}
                and signature in {("A16", "A16"), ("A32", "A32")}
                and (
                    (
                        operand_index == 0
                        and operand_tags in {("SD", "Zs", "D"), ("SD", "Zs", "D", "Zs")}
                    )
                    or (operand_index == 1 and operand_tags == ("D", "Zs", "SD", "Zs"))
                )
            )
            or (
                instruction == "D/"
                and signature == ("A32s", "A32s", "A32sa")
                and operand_index == 2
                and operand_tags == ("D", "Zs", "K_2", "SD", "Zs")
            )
        )
    else:
        return
    if not approved:
        raise _mining_error("LDDB mined scalar context is unmined", f"category=lddb_operand;tag={tag}")


def _lddb_instruction_from_element(
    kind: str,
    pulse: str,
    signature: tuple[str, ...],
    args: list[str],
    descriptors: list[str],
    cursor: int,
    *,
    branch_start: bool,
) -> tuple[str, tuple[str, ...], int, SerializedOperation]:
    """Decode one physical LDDB element against its serialized descriptor."""
    if cursor >= len(descriptors):
        raise _mining_error("LDDB descriptor stream is shorter than its grid", "category=lddb_header")
    descriptor_start = cursor
    marker = descriptors[cursor]
    cursor += 1
    # ``Zs`` can prefix an operation descriptor when its first source operand
    # is indexed.  It is a typed operand modifier, not an instruction marker;
    # keep it paired with the following operand descriptor below.
    if marker == "Zs":
        if cursor >= len(descriptors):
            raise _mining_error("LDDB indexed operation marker is incomplete", "category=lddb_header")
        marker = descriptors[cursor]
        cursor += 1
    if marker in {"INV", "ME"} and not args:
        instruction = {"INV": "INV", "ME": "MEP"}[marker]
    elif kind == "ct":
        if marker in {"a", "b"}:
            instruction = _canonical_logic_instruction(
                "l" if branch_start else "a",
                inverse=marker == "b",
                comparator=None,
                pulse="f" if pulse == "f" else pulse == "p",
                signature=signature,
                category="lddb_operand",
            )
        elif marker in {"=", "<>", "<", "<=", ">", ">="}:
            if pulse != "a":
                raise _mining_error("LDDB comparison pulse descriptor is unmined", "category=lddb_operand")
            instruction = _canonical_logic_instruction(
                "l" if branch_start else "a",
                inverse=False,
                comparator=marker,
                pulse=pulse == "p",
                signature=signature,
                category="lddb_operand",
            )
        else:
            raise _mining_error("LDDB contact descriptor is unmined", f"category=lddb_header;marker={marker}")
    else:
        instruction = _canonical_command_instruction(
            marker,
            pulse={"a": "none", "p": "rising", "f": "falling"}.get(pulse, pulse),
            signature=signature,
            lddb_serialized=True,
        )
        if instruction not in MIL_SUPPORTED_INSTRUCTIONS:
            raise _mining_error("LDDB command descriptor is unmined", f"category=lddb_header;marker={marker}")
        if marker == "RST__16" and len(args) != 1:
            raise _mining_error("LDDB RST__16 serialization is unmined", "category=lddb_operand")
    count = LDDB_RAW_OPERAND_COUNTS.get(marker, MIL_OPERAND_COUNTS.get(instruction))
    exact_arity = (marker, "none" if pulse == "a" else ("rising" if pulse == "p" else pulse), signature, len(args))
    if (count is None or len(args) not in count) and exact_arity not in LDDB_EXACT_ARITIES:
        raise _mining_error(
            "LDDB command argument count is unmined",
            f"category=lddb_operand;instruction={instruction};argument_count={len(args)}",
        )
    operands: list[str] = []
    operand_descriptor_start = cursor
    operand_tags = tuple(descriptors[cursor:])
    validation: list[tuple[str, int, str]] = []
    for operand_index, value in enumerate(args):
        tag = descriptors[cursor] if cursor < len(descriptors) else ""
        following_tag = descriptors[cursor + 1] if cursor + 1 < len(descriptors) else ""
        validation.append((tag, operand_index, following_tag))
        operand, cursor = _lddb_format_operand_at(
            descriptors, cursor, value,
            instruction=instruction, signature=signature, operand_index=operand_index,
            operand_tags=operand_tags,
        )
        operands.append(operand)
    exact_operand_tags = tuple(descriptors[operand_descriptor_start:cursor])
    socket_tags = SP_SOCKET_SOURCE_TAGS.get(instruction)
    if socket_tags is not None and exact_operand_tags not in socket_tags:
        raise _mining_error(
            f"LDDB {instruction} operand descriptors are unmined",
            "category=lddb_operand",
        )
    for tag, operand_index, following_tag in validation:
        _lddb_validate_mined_scalar_context(
            instruction,
            signature,
            tag,
            operand_index,
            following_tag,
            exact_operand_tags,
        )
    width = "A32" if any(item.startswith("A32") for item in signature) else ("A16" if any(item.startswith("A16") for item in signature) else "")
    signedness = "signed" if any(item.endswith("s") for item in signature) else ""
    return (
        instruction,
        tuple(operands),
        cursor,
        SerializedOperation(
            "lddb",
            marker,
            (f"ct={pulse}", *descriptors[descriptor_start:cursor]),
            pulse=pulse in {"p", "f"},
            pulse_kind={"a": "none", "p": "rising", "f": "falling"}[pulse],
            inverse=marker == "b",
            width=width,
            signedness=signedness,
            variant=instruction,
        ),
    )


@dataclass(frozen=True)
class _LddbDecodedElement:
    """One descriptor-complete LDDB cell used by the graph reducer."""

    x: int
    y: int
    kind: str
    instruction: str
    operands: tuple[str, ...]
    order: int
    span: int
    note: str = ""
    operation: SerializedOperation = SerializedOperation("lddb", "", ())
    note_subtype: str = ""


def _lddb_wire_positions(data: str) -> list[tuple[int, int]]:
    """Return the explicit horizontal conductor cells omitted by ``_lddb_elements``."""
    grid = data[data.index(":cb{fg=fg{") :]
    positions: list[tuple[int, int]] = []
    for record in _balanced_records(grid, "e{"):
        if not record.startswith("e{s=-:"):
            continue
        match = re.search(r":pos=(\d+),(\d+)\}$", record)
        if match is None:
            raise _mining_error("LDDB wire position is unmined", "category=lddb_graph")
        positions.append((int(match.group(1)), int(match.group(2))))
    return positions


def _lddb_expression(operator: str, *parts: tuple[object, ...]) -> tuple[object, ...]:
    """Build a flattened associative series/parallel expression."""
    flattened: list[object] = []
    for part in parts:
        if part and part[0] == operator:
            flattened.extend(part[1:])
        else:
            flattened.append(part)
    return (operator, *flattened)


def _lddb_expression_leaves(expression: tuple[object, ...]) -> list[int]:
    if expression[0] == "leaf":
        return [int(expression[1])]
    return [
        leaf
        for part in expression[1:]
        for leaf in _lddb_expression_leaves(part)  # type: ignore[arg-type]
    ]


def _lddb_expression_descriptor_order(expression: tuple[object, ...], elements: list[_LddbDecodedElement]) -> int:
    """Use the first raw descriptor in a reduced branch as its IL order key."""
    leaves = _lddb_expression_leaves(expression)
    if not leaves:
        raise _mining_error("LDDB branch has no descriptor leaves", "category=lddb_graph")
    return min(elements[leaf].order for leaf in leaves)


def _lddb_contact_form(element: _LddbDecodedElement, mode: str) -> tuple[str, tuple[str, ...]]:
    """Render one neutral contact as a load, series, or parallel IL form."""
    if element.instruction in {"INV", "MEP"}:
        if mode != "and":
            raise _mining_error(
                "LDDB inline unary branch position is unmined",
                f"category=lddb_graph;instruction={element.instruction}",
            )
        return element.instruction, ()
    mappings = {
        "LD": {"load": "LD", "and": "AND", "or": "OR"},
        "LDI": {"load": "LDI", "and": "ANI", "or": "ORI"},
        "LDP": {"load": "LDP", "and": "ANDP", "or": "ORP"},
        "LDPI": {"load": "LDPI", "and": "ANIP", "or": "ORIP"},
        "LDF": {"load": "LDF", "and": "ANDF", "or": "ORF"},
        "LDFI": {"load": "LDFI", "and": "ANDFI", "or": "ORFI"},
    }
    base = element.instruction
    if base in mappings:
        return mappings[base][mode], element.operands
    if base.startswith("LD") and base[2:] in {"=", "<>", "<", "<=", ">", ">="}:
        prefix = {"load": "LD", "and": "AND", "or": "OR"}[mode]
        instruction = prefix + base[2:]
        if instruction not in MIL_SUPPORTED_INSTRUCTIONS:
            raise _mining_error(
                "LDDB comparison branch form is unmined",
                f"category=lddb_header;instruction={instruction}",
            )
        return instruction, element.operands
    if base.startswith("LDD") and base[3:] in {"=", "<>", "<", "<=", ">", ">="}:
        prefix = {"load": "LDD", "and": "ANDD", "or": "ORD"}[mode]
        instruction = prefix + base[3:]
        if instruction not in MIL_SUPPORTED_INSTRUCTIONS:
            raise _mining_error(
                "LDDB double-word comparison branch form is unmined",
                f"category=lddb_header;instruction={instruction}",
            )
        return instruction, element.operands
    raise _mining_error("LDDB contact form is unmined", f"category=lddb_header;instruction={base}")


def _lddb_append_expression(
    expression: tuple[object, ...],
    elements: list[_LddbDecodedElement],
    semantic: list[tuple[str, tuple[str, ...], str, SerializedOperation]],
    *,
    mode: str,
) -> None:
    """Compile a reduced series/parallel expression using AND/OR block joins."""
    operator = expression[0]
    if operator == "leaf":
        element = elements[int(expression[1])]
        instruction, operands = _lddb_contact_form(element, mode)
        semantic.append((instruction, operands, "", replace(element.operation, variant=instruction)))
        return
    if mode == "and" and operator != "and":
        _lddb_append_expression(expression, elements, semantic, mode="load")
        semantic.append(("ANB", (), "", SerializedOperation("lddb_graph", "ANB", (), variant="ANB")))
        return
    parts = expression[1:]
    # Parallel contacts are associative: reduction order must not decide the
    # emitted IL order.  Preserve raw descriptor order for every direct OR
    # child, including children exposed by earlier flattening.  Series terms
    # remain left-to-right and are deliberately not reordered.
    if operator == "or":
        parts = tuple(sorted(
            parts,
            key=lambda part: _lddb_expression_descriptor_order(part, elements),  # type: ignore[arg-type]
        ))
    if not parts:
        raise _mining_error("LDDB graph expression is empty", "category=lddb_graph")
    first_mode = mode if operator == "and" and mode == "and" else "load"
    _lddb_append_expression(parts[0], elements, semantic, mode=first_mode)  # type: ignore[arg-type]
    join = "ANB" if operator == "and" else "ORB"
    leaf_mode = "and" if operator == "and" else "or"
    for part in parts[1:]:
        if part[0] == "leaf":  # type: ignore[index]
            _lddb_append_expression(part, elements, semantic, mode=leaf_mode)  # type: ignore[arg-type]
        else:
            _lddb_append_expression(part, elements, semantic, mode="load")  # type: ignore[arg-type]
            semantic.append((join, (), "", SerializedOperation("lddb_graph", join, (), variant=join)))


def _decode_lddb_logic_block(
    data: str, step_rows: list[StepInfoRow], sequence_start: int
) -> tuple[list[Instruction], tuple[SerializedOperation, ...]]:
    """Decode a descriptor-complete reducible LDDB contact/output graph.

    Horizontal wire cells and vertical junctions first become conductor
    components. Contact edges are then reduced only by exact series and
    parallel graph rules. The remaining output tree is emitted with the
    documented Mitsubishi MPS/MRD/MPP stack form. Crossing, cyclic,
    disconnected, or descriptor-incomplete graphs remain opaque.
    """
    elements, vertical = _lddb_elements(data)
    wires = _lddb_wire_positions(data)
    descriptors = _lddb_descriptor_tokens(data)
    dimension = re.search(r":cb\{fg=fg\{dim=(\d+)x(\d+):es=\[", data)
    if dimension is None:
        raise _mining_error("LDDB grid dimension is unmined", "category=lddb_graph")
    width, height = (int(value) for value in dimension.groups())
    if width <= 0 or height <= 0 or any(
        x < 0 or y < 0 or x >= width or y >= height
        for x, y in ((element.x, element.y) for element in elements)
    ):
        raise _mining_error("LDDB grid element is outside its declared dimension", "category=lddb_graph")
    if any(x < 0 or y <= 0 or x > width or y >= height for x, y in vertical):
        raise _mining_error("LDDB vertical relation is outside its declared dimension", "category=lddb_graph")
    if any(x < 0 or y < 0 or x >= width or y >= height for x, y in wires):
        raise _mining_error("LDDB wire is outside its declared dimension", "category=lddb_graph")
    if len({(element.x, element.y) for element in elements}) != len(elements):
        raise _mining_error("LDDB grid has duplicate element positions", "category=lddb_graph")

    decoded_elements: list[_LddbDecodedElement] = []
    cursor = 0
    for order, element in enumerate(elements):
        descriptor_start = cursor
        instruction, operands, cursor, operation = _lddb_instruction_from_element(
            element.kind,
            element.pulse,
            element.signature,
            element.args,
            descriptors,
            cursor,
            branch_start=True,
        )
        marker = descriptors[descriptor_start]
        span = 3 if element.kind == "ct" and marker in {"=", "<>", "<", "<=", ">", ">="} else 1
        if element.x + span > width:
            raise _mining_error("LDDB grid operation exceeds its declared dimension", "category=lddb_graph")
        note = ""
        if element.has_note:
            if cursor + 1 >= len(descriptors) or not descriptors[cursor] or descriptors[cursor] != descriptors[cursor + 1]:
                raise _mining_error("LDDB inline note descriptor pair is unmined", "category=lddb_note")
            note = descriptors[cursor]
            cursor += 2
        decoded_elements.append(
            _LddbDecodedElement(
                element.x,
                element.y,
                element.kind,
                instruction,
                operands,
                order,
                span,
                note,
                operation,
                element.note_subtype,
            )
        )
    if cursor != len(descriptors):
        raise _mining_error("LDDB header has unused descriptors", f"category=lddb_header;unused_sha256={_canonical_sha256(descriptors[cursor:])}")

    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(node: tuple[int, int]) -> tuple[int, int]:
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(left: tuple[int, int], right: tuple[int, int]) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    # Every row begins at the common left rail. GX3 omits empty conductor
    # cells from the primary row, while non-primary branch wires are explicit.
    for y in range(height):
        union((0, 0), (0, y))
    occupied_primary: set[int] = set()
    for element in decoded_elements:
        if element.y == 0:
            occupied_primary.update(range(element.x, element.x + element.span))
    for x in range(width):
        if x not in occupied_primary:
            union((x, 0), (x + 1, 0))
    for x, y in wires:
        union((x, y), (x + 1, y))
    for x, y in vertical:
        union((x, y - 1), (x, y))

    edges: list[list[object]] = []
    commands: dict[tuple[int, int], list[int]] = {}
    for element in decoded_elements:
        left = find((element.x, element.y))
        if element.kind == "ct" or element.instruction in {"INV", "MEP"}:
            right = find((element.x + element.span, element.y))
            if left == right:
                raise _mining_error("LDDB contact is bypassed by its conductor graph", "category=lddb_graph")
            edges.append([left, right, ("leaf", element.order), element.order])
        else:
            commands.setdefault(left, []).append(element.order)

    source = find((0, 0))
    edges = [[find(edge[0]), find(edge[1]), edge[2], edge[3]] for edge in edges]  # type: ignore[arg-type]
    commands = {find(node): orders for node, orders in commands.items()}

    # Exact two-terminal series/parallel reduction. No heuristic path choice
    # is made: an irreducible join remains MINING_REQUIRED.
    while True:
        parallel: dict[tuple[tuple[int, int], tuple[int, int]], list[tuple[int, list[object]]]] = {}
        for index, edge in enumerate(edges):
            parallel.setdefault((edge[0], edge[1]), []).append((index, edge))  # type: ignore[arg-type]
        group = next((items for items in parallel.values() if len(items) > 1), None)
        if group is not None:
            removed = {index for index, _edge in group}
            parts = sorted(
                (edge for _index, edge in group),
                key=lambda edge: _lddb_expression_descriptor_order(edge[2], decoded_elements),  # type: ignore[arg-type]
            )
            edges = [edge for index, edge in enumerate(edges) if index not in removed]
            edges.append([
                parts[0][0], parts[0][1],
                _lddb_expression("or", *(part[2] for part in parts)),
                min(int(part[3]) for part in parts),
            ])
            continue
        incoming = Counter(edge[1] for edge in edges)
        outgoing = Counter(edge[0] for edge in edges)
        series_node = next((
            node for node in set(incoming) | set(outgoing)
            if node != source and node not in commands and incoming[node] == 1 and outgoing[node] == 1
        ), None)
        if series_node is None:
            break
        before = next(edge for edge in edges if edge[1] == series_node)
        after = next(edge for edge in edges if edge[0] == series_node)
        edges.remove(before)
        edges.remove(after)
        edges.append([
            before[0], after[1], _lddb_expression("and", before[2], after[2]),
            min(int(before[3]), int(after[3])),
        ])

    incoming = Counter(edge[1] for edge in edges)
    if any(count > 1 for count in incoming.values()):
        raise _mining_error("LDDB contact graph has an irreducible join", "category=lddb_graph")
    outgoing_edges: dict[tuple[int, int], list[list[object]]] = {}
    for edge in edges:
        outgoing_edges.setdefault(edge[0], []).append(edge)  # type: ignore[arg-type]

    semantic: list[tuple[str, tuple[str, ...], str, SerializedOperation]] = []
    used: set[int] = set()
    active: set[tuple[int, int]] = set()

    def append_output_tree(node: tuple[int, int], *, loaded: bool) -> None:
        if node in active:
            raise _mining_error("LDDB contact graph is cyclic", "category=lddb_graph")
        active.add(node)
        command_orders = sorted(commands.get(node, []))
        branches = sorted(
            outgoing_edges.get(node, []),
            key=lambda edge: _lddb_expression_descriptor_order(edge[2], decoded_elements),  # type: ignore[arg-type]
        )
        if loaded and not command_orders and not branches:
            raise _mining_error("LDDB contact path has no output command", "category=lddb_graph")
        actions: list[tuple[int, str, object]] = [
            (order, "command", order) for order in command_orders
        ] + [
            (
                _lddb_expression_descriptor_order(edge[2], decoded_elements),  # type: ignore[arg-type]
                "branch",
                edge,
            )
            for edge in branches
        ]
        actions.sort(key=lambda action: action[0])

        def append_branch(edge: list[object]) -> None:
            expression = edge[2]
            _lddb_append_expression(
                expression, decoded_elements, semantic, mode="and" if loaded else "load"  # type: ignore[arg-type]
            )
            used.update(_lddb_expression_leaves(expression))  # type: ignore[arg-type]
            append_output_tree(edge[1], loaded=True)  # type: ignore[arg-type]

        cursor = 0
        while cursor < len(actions):
            _order, kind, payload = actions[cursor]
            if kind == "command":
                order = int(payload)
                element = decoded_elements[order]
                semantic.append((element.instruction, element.operands, "", element.operation))
                if element.note:
                    semantic.append(
                        (
                            "",
                            (),
                            element.note,
                            SerializedOperation(
                                "lddb_note",
                                "NOTE",
                                ("NOTE", f"st={element.note_subtype}"),
                                variant="note",
                            ),
                        )
                    )
                used.add(order)
                cursor += 1
                continue
            run_end = cursor
            while run_end < len(actions) and actions[run_end][1] == "branch":
                run_end += 1
            run = [action[2] for action in actions[cursor:run_end]]
            has_later_base_action = loaded and run_end < len(actions)
            if loaded and (len(run) > 1 or has_later_base_action):
                semantic.append(("MPS", (), "", SerializedOperation("lddb_graph", "MPS", (), variant="MPS")))
            for index, edge in enumerate(run):
                if loaded and index > 0:
                    final_unrestored_branch = not has_later_base_action and index + 1 == len(run)
                    instruction = "MPP" if final_unrestored_branch else "MRD"
                    semantic.append((instruction, (), "", SerializedOperation("lddb_graph", instruction, (), variant=instruction)))
                append_branch(edge)  # type: ignore[arg-type]
            if has_later_base_action:
                semantic.append(("MPP", (), "", SerializedOperation("lddb_graph", "MPP", (), variant="MPP")))
            cursor = run_end
        active.remove(node)

    append_output_tree(source, loaded=False)
    if used != set(range(len(decoded_elements))):
        raise _mining_error("LDDB contact graph is disconnected", "category=lddb_graph")
    if len(semantic) != len(step_rows):
        raise _mining_error(
            "LDDB and StepInfo instruction counts differ",
            f"category=lddb_stepinfo;lddb_count={len(semantic)};step_count={len(step_rows)}",
        )
    return [
        Instruction(
            sequence_start + offset,
            0,
            instruction,
            operands[0] if operands else "",
            note=note,
            note_subtype=_operation.descriptors[1][-1] if _operation.descriptors in (("NOTE", "st=i"), ("NOTE", "st=s")) else "",
            continuation_operands=operands[1:],
        )
        for offset, (instruction, operands, note, _operation) in enumerate(semantic)
    ], tuple(operation for _instruction, _operands, _note, operation in semantic)


def _decode_lddb_block(
    block: LadderBlock, data: str, step_rows: list[StepInfoRow], sequence_start: int
) -> tuple[list[Instruction], tuple[SerializedOperation, ...]]:
    if block.blocktype in {1, 2}:
        match = re.fullmatch(r"V1:1:(\d+):(.*):st\{st=c\{t=#:st=([is])\}:dim=0x1\}", data)
        if match is None or int(match.group(1)) != len(match.group(2)):
            raise _mining_error("LDDB statement record is unmined", "category=lddb_statement")
        if len(step_rows) != 1:
            raise _mining_error("LDDB statement StepInfo count is unmined", "category=lddb_stepinfo")
        return (
            [Instruction(sequence_start, 0, "", "", line_statement=("*" if match.group(3) == "s" else "") + match.group(2))],
            (SerializedOperation("lddb_statement", "STATEMENT", (), variant="statement"),),
        )
    if block.blocktype == 5:
        if data != END_DATA or len(step_rows) != 1:
            raise _mining_error("LDDB END record is unmined", "category=lddb_end")
        return [Instruction(sequence_start, 0, "END", "")], (SerializedOperation("lddb", "END", ("END",), variant="END"),)
    if block.blocktype == 3:
        if data != "V1:0:nop{nop=fnn{n=1}:dim=1x1}" or len(step_rows) != 1:
            raise _mining_error("LDDB NOP record is unmined", "category=lddb_nop")
        return [Instruction(sequence_start, 0, "NOP", "")], (SerializedOperation("lddb", "NOP", ("NOP",), variant="NOP"),)
    if block.blocktype == 0:
        return _decode_lddb_logic_block(data, step_rows, sequence_start)
    raise _mining_error("LDDB block type is unmined", f"category=lddb_blocktype;blocktype={block.blocktype}")


def _parse_lddb_blocks(structure: ProgramStructure, raw_data: dict[str, str]) -> MilParseResult:
    """Recover an empty MilDB POU from its linked LDDB and StepInfo source.

    StepInfo linkage and position are checked before per-block recovery.  A
    semantic failure reserves precisely that block's rows as an opaque source
    gap, allowing later POU blocks to remain visible for partial review.
    """
    step_blocks = {block.block_id: block for block in structure.step_info.blocks}
    if len(step_blocks) != len(structure.step_info.blocks):
        raise _mining_error("LDDB has duplicate StepInfo block linkage", "category=lddb_stepinfo")
    rows_by_block: dict[str, list[StepInfoRow]] = {}
    for row in structure.step_info.rows:
        rows_by_block.setdefault(row.block_id, []).append(row)
    decoded: list[Instruction] = []
    source_operations: list[SerializedOperation] = []
    opaque_blocks: list[OpaqueBlock] = []
    recovery_counts: Counter[str] = Counter()
    recovery_rows: Counter[str] = Counter()
    sequence = 0
    source_step = 0
    for block in structure.blocks:
        step_block = step_blocks.get(block.record_id)
        if step_block is None or step_block.position != block.position:
            raise _mining_error("LDDB and StepInfo block positions differ", f"category=lddb_stepinfo;record_id={block.record_id}")
        block_rows = rows_by_block.get(block.record_id, [])
        try:
            # Production source meaning comes solely from the serialized AST
            # and conductor graph.  The observed oracle remains a test asset;
            # it is never loaded, selected, or cross-checked here.
            block_instructions, block_operations = _decode_lddb_block(
                block, raw_data[block.record_id], block_rows, sequence
            )
            method = "STRUCTURAL_LDDB_GRAPH"
            if len(block_instructions) != len(block_rows):
                raise _mining_error("LDDB emitted an incomplete StepInfo block", f"category=lddb_stepinfo;record_id={block.record_id}")
            if len(block_operations) != len(block_instructions):
                raise RuntimeError("LDDB typed operation inventory differs from decoded rows")
            for instruction, step_row in zip(block_instructions, block_rows, strict=True):
                decoded.append(replace(instruction, source_step=source_step))
                source_step += step_row.step_size or 0
            source_operations.extend(block_operations)
            recovery_counts[method] += 1
            recovery_rows[method] += len(block_rows)
        except ConversionError as error:
            opaque_blocks.append(OpaqueBlock(sequence, source_step, len(block_rows), block.record_id, block.position, str(error), error.finding_code, error.mining_tracks, error.candidate))
            source_step += sum(row.step_size or 0 for row in block_rows)
        sequence += len(block_rows)
    result = MilParseResult(
        tuple(decoded), tuple(opaque_blocks),
        tuple((method, recovery_counts[method], recovery_rows[method]) for method in sorted(recovery_counts)),
        sum(recovery_counts.values()),
        tuple(source_operations),
    )
    expected_rows = sum(len(rows_by_block.get(block.record_id, [])) for block in structure.blocks)
    if result.expected_rows != expected_rows:
        raise RuntimeError("LDDB recovery inventory invariant failed")
    return result


def _parse_ladder_blocks_from_structure(structure: ProgramStructure, raw_data: dict[str, str]) -> tuple[Instruction, ...]:
    """Decode only the Converter-approved instruction subset after full structural read."""
    block_ids = {block.record_id for block in structure.blocks}
    step_ids = {row.block_id for row in structure.step_info.rows}
    if step_ids != block_ids:
        raise _mining_error(
            "legacy LDDB and StepInfo block inventory differs",
            f"category=legacy_stepinfo;ladder_ids_sha256={_canonical_sha256(sorted(block_ids))};"
            f"step_ids_sha256={_canonical_sha256(sorted(step_ids))}",
        )
    unsupported_blocktypes = sorted({block.blocktype for block in structure.blocks if block.blocktype not in {0, 5}})
    if unsupported_blocktypes:
        raise _mining_error(
            "GX3 ladder structure contains unmined block types",
            _structure_candidate(structure) + f";unmined_blocktypes={','.join(str(value) for value in unsupported_blocktypes)}",
        )
    instructions: list[Instruction] = []
    unsupported_records: list[LadderBlock] = []
    for block in structure.blocks:
        record_id = block.record_id
        blocktype = block.blocktype
        data = raw_data[record_id]
        if blocktype == 0:
            match = LADDER_DATA.fullmatch(data)
            if match is None:
                unsupported_records.append(block)
                continue
            contact = {"a": "LD", "b": "LDI"}[match.group("contact")]
            instructions.extend(
                (
                    Instruction(len(instructions), len(instructions) * 2, contact, f"M{match.group('ld')}"),
                    Instruction(len(instructions) + 1, (len(instructions) + 1) * 2, "OUT", f"M{match.group('out')}"),
                )
            )
        elif blocktype == 5:
            if data != END_DATA:
                unsupported_records.append(block)
                continue
            instructions.append(Instruction(len(instructions), len(instructions) * 2, "END", ""))
    if unsupported_records:
        raise _mining_error(
            "GX3 ladder structure contains unmined logic records",
            _structure_candidate(structure)
            + f";unmined_record_count={len(unsupported_records)};"
            + f"unmined_record_ids_sha256={_canonical_sha256([block.record_id for block in unsupported_records])}",
        )
    if tuple(item.instruction for item in instructions) not in SUPPORTED_SEQUENCES:
        candidate = "instruction_sequence=" + ",".join(item.instruction for item in instructions)
        raise _mining_error("only LD M0|M100 or LDI M0, OUT M1, END is supported", candidate)
    for item in instructions:
        if item.instruction == "LDI" and item.operand != "M0":
            raise _mining_error("only LDI M0 is supported", f"{item.instruction} {item.operand}")
    step_rows = structure.step_info.rows
    if len(instructions) != len(step_rows):
        raise _mining_error(
            "legacy LDDB decoded rows differ from StepInfo inventory",
            f"category=legacy_stepinfo;decoded_count={len(instructions)};step_count={len(step_rows)}",
        )
    source_step = 0
    partitioned: list[Instruction] = []
    for sequence, (instruction, step_row) in enumerate(zip(instructions, step_rows, strict=True)):
        partitioned.append(replace(instruction, sequence=sequence, source_step=source_step))
        source_step += step_row.step_size or 0
    return tuple(partitioned)


def _parse_ladder_blocks(body: bytes) -> tuple[Instruction, ...]:
    """Compatibility reader for unit tests that only provide an LDDB database."""
    structure, raw_data = _read_ladder_structure(body, _minimal_step_info_for_ladder(body))
    return _parse_ladder_blocks_from_structure(structure, raw_data)


def _minimal_step_info_for_ladder(body: bytes) -> bytes:
    """Build a private, read-only-compatible StepInfo relation for LDDB unit tests.

    Production inputs always supply their linked StepInfo.db.  This helper
    avoids weakening the production linkage gate while keeping narrow parser
    unit tests independent from an unrelated database fixture.
    """
    source = sqlite3.connect(":memory:")
    target = sqlite3.connect(":memory:")
    try:
        source.deserialize(body)
        rows = list(source.execute("SELECT id, pos FROM LadderBlocks ORDER BY pos, id"))
        target.execute("CREATE TABLE T_Block (Pos REAL, BlockID TEXT)")
        target.execute("CREATE TABLE T_Step (Pos REAL, MilID INTEGER, BlockID TEXT)")
        for record_id, position in rows:
            normalized = _canonical_block_id(record_id)
            if normalized is None:
                raise ConversionError("LDDB.db contains an invalid LadderBlocks identifier")
            target.execute("INSERT INTO T_Block VALUES (?, ?)", (position, "{" + normalized.upper() + "}"))
            target.execute("INSERT INTO T_Step VALUES (?, ?, ?)", (position, 0, "{" + normalized.upper() + "}"))
        return target.serialize()
    finally:
        source.close()
        target.close()


def read_gx3(input_path: Path, *, _source_bytes: bytes | None = None) -> ParsedProject | ParsedMultiProject:
    """Read a supported GX3 structure without modifying it."""
    _reject_link_like_ancestors(input_path, "--input")
    if _is_link_like(input_path) or not input_path.is_file() or input_path.suffix.lower() != ".gx3":
        raise ConversionError("--input must be a regular non-link .gx3 file")
    if input_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ConversionError("--input GX3 archive exceeds byte limit")
    source_contracts = _require_source_contract()
    def neutral_ir_for_input() -> dict[str, object]:
        try:
            from converter.upstream_contracts import build_source_neutral_ir

            return build_source_neutral_ir(
                input_path, source_bytes=_source_bytes, contracts=source_contracts,
                public_overlay=_validated_public_upstream(),
            )
        except ConversionError:
            raise
        except Exception as error:
            raise ConversionError(
                "P002 Neutral IR source adapter failed",
                finding_code="INPUT_INVALID", mining_tracks="P002",
                candidate=f"adapter_error={type(error).__name__}",
            ) from error
    try:
        archive_source: Path | io.BytesIO = input_path if _source_bytes is None else io.BytesIO(_source_bytes)
        with zipfile.ZipFile(archive_source) as archive:
            entries = _safe_entries(archive)
            config_entries = [info for name, info in entries.items() if name == "Config.xml"]
            if len(config_entries) != 1:
                raise ConversionError("GX3 must contain exactly one Config.xml")
            source_project = _parse_config(_read_entry(archive, config_entries[0]))
            producer = source_contracts.source_contract["producer"]
            if (
                producer.get("package") != "gx3-fx5-parser-toolkit"
                or producer.get("profile") != "FX5U/FX5UC/FX5UJ/FX5S/Ladder"
                or producer.get("cpu_families") != ["FX5U", "FX5UC", "FX5UJ", "FX5S"]
            ):
                raise ConversionError("Converter source contract producer is incompatible", finding_code="INPUT_INVALID", mining_tracks="P002")
            # The hash is retained for audit/provenance and optional proven
            # source-specific projections. It never decides parser support.
            source_sha256 = (
                _inspect_source_profile(input_path)
                if _source_bytes is None
                else hashlib.sha256(_source_bytes).hexdigest()
            )
            multi_pou_links = [path for path in entries if re.fullmatch(r"ConvertData/\d+/PouLinkOrder\.info", path)]
            if multi_pou_links:
                try:
                    declared_count = sum(
                        len(_read_entry(archive, entries[path]).decode("ascii").splitlines())
                        for path in multi_pou_links
                    )
                except UnicodeDecodeError as error:
                    raise _mining_error("PouLinkOrder.info is not ASCII", "category=pou_link_order") from error
                linked_count = len(_linked_pou_ids(entries, archive)) if declared_count > 1 else 0
            else:
                linked_count = 0
            if linked_count > 1:
                source_diagnostics = _inspect_label_metadata(entries, archive)
                programs = _dynamic_multi_linked_programs(entries, archive, source_sha256)
                instructions = tuple(record for program in programs for record in program.instructions)
                comments, comment_diagnostics = _read_optional_device_comments(
                    entries, archive, instructions
                )
                comments = _apply_comment_overrides(source_sha256, comments)
                source_diagnostics = (*source_diagnostics, *comment_diagnostics)
                return _bind_neutral_ir(ParsedMultiProject(
                    source_project, programs, comments, source_sha256, source_diagnostics
                ), neutral_ir_for_input())
            source_diagnostics = _inspect_label_metadata(entries, archive)
            lddb_entries = [info for name, info in entries.items() if re.fullmatch(r"[^/]+_LDDB\.db", name)]
            if len(lddb_entries) != 1:
                raise ConversionError("GX3 must contain exactly one LDDB.db")
            program_name, pou_id = _parse_program(entries, archive)
            step_info_entry = entries[f"{pou_id}_StepInfo.db"]
            structure, raw_data = _read_ladder_structure(
                _read_entry(archive, lddb_entries[0]),
                _read_entry(archive, step_info_entry),
            )
            mil_entries = [info for name, info in entries.items() if re.fullmatch(r"[^/]+_MilDB\.db", name)]
            findings: tuple[tuple[str, str, str, str, str, str, str, str], ...] = ()
            opaque_blocks: tuple[OpaqueBlock, ...] = ()
            recovery_methods: tuple[tuple[str, int, int], ...] = ()
            source_operations: tuple[SerializedOperation, ...] = ()
            direct_blocks = 0
            if len(mil_entries) == 1:
                mil_body = _read_entry(archive, mil_entries[0])
                if _mil_row_count(mil_body) == 0:
                    mil_result = _parse_lddb_blocks(structure, raw_data)
                    instructions = mil_result.instructions
                    opaque_blocks = mil_result.opaque_blocks
                    recovery_methods = mil_result.recovery_methods
                    source_operations = mil_result.source_operations
                    direct_blocks = mil_result.direct_blocks
                    findings = tuple(
                        (
                            str(block.sequence), str(block.source_step), "", "", block.error,
                            block.finding_code, block.mining_tracks, f"program={program_name};{block.locator}",
                        )
                        for block in opaque_blocks
                    )
                elif _mil_relates_to_ladder_blocks(mil_body, structure):
                    mil_result = _parse_mil_blocks(mil_body, structure)
                    instructions = mil_result.instructions
                    opaque_blocks = mil_result.opaque_blocks
                    direct_blocks = mil_result.direct_blocks
                    source_operations = mil_result.source_operations
                    findings = tuple(
                        (
                            str(block.sequence), str(block.source_step), "", "", block.error,
                            block.finding_code, block.mining_tracks, f"program={program_name};{block.locator}",
                        )
                        for block in opaque_blocks
                    )
                else:
                    # The legacy parser remains intentionally narrow, but CPU
                    # and module parameter bytes are not a source-grammar
                    # approval gate for a ladder conversion.
                    instructions = _parse_ladder_blocks_from_structure(structure, raw_data)
                    direct_blocks = len(structure.blocks)
            elif not mil_entries:
                instructions = _parse_ladder_blocks_from_structure(structure, raw_data)
                direct_blocks = len(structure.blocks)
            else:
                raise _mining_error("GX3 contains multiple root MIL databases", f"category=mil_schema;entry_count={len(mil_entries)}")
            comments, comment_diagnostics = _read_optional_device_comments(
                entries, archive, instructions
            )
            comments = _apply_comment_overrides(source_sha256, comments)
            source_diagnostics = (*source_diagnostics, *comment_diagnostics)
    except ConversionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, OSError, sqlite3.Error, UnicodeError, element_tree.ParseError) as error:
        raise ConversionError(f"--input GX3 archive read failed: {type(error).__name__}") from error
    return _bind_neutral_ir(ParsedProject(
        source_project, program_name, instructions, structure, comments, findings,
        opaque_blocks, recovery_methods, source_diagnostics,
        _coverage_for_records(
            instructions, opaque_blocks, direct_blocks=direct_blocks,
            source_operations=source_operations,
        ),
        source_operations,
    ), neutral_ir_for_input())


def validate_records(records: tuple[Instruction, ...]) -> list[list[str]]:
    """Return every row-level finding for later safe-publication classification."""
    invalid: list[list[str]] = []
    for record in records:
        operand_count = (1 if record.operand else 0) + len(record.continuation_operands)
        if (
            record.instruction in SOURCE_EXACT_SOCKET_OPERAND_COUNTS
            and operand_count not in SOURCE_EXACT_SOCKET_OPERAND_COUNTS[record.instruction]
        ):
            invalid.append([str(record.sequence), str(record.source_step), record.instruction, record.operand, "unapproved source-exact socket operand count", "MINING_REQUIRED", "P002", f"{record.instruction};operand_count={operand_count}"])
        elif record.instruction not in MIL_SUPPORTED_INSTRUCTIONS:
            invalid.append([str(record.sequence), str(record.source_step), record.instruction, record.operand, "unsupported instruction", "MINING_REQUIRED", "P002|P004", f"{record.instruction} {record.operand}".strip()])
        elif (
            record.instruction in MIL_SUPPORTED_INSTRUCTIONS
            and operand_count not in MIL_OPERAND_COUNTS[record.instruction]
        ):
            invalid.append([str(record.sequence), str(record.source_step), record.instruction, record.operand, "unapproved operand count", "MINING_REQUIRED", "P002|P004", f"{record.instruction};operand_count={operand_count}"])
        elif record.instruction == "" and record.operand:
            invalid.append([str(record.sequence), str(record.source_step), record.instruction, record.operand, "statement row is malformed", "INPUT_INVALID", "", ""])
        elif record.instruction == "END" and (record.operand or record.continuation_operands):
            invalid.append([str(record.sequence), str(record.source_step), record.instruction, record.operand, "END must have an empty operand", "MINING_REQUIRED", "P002|P004", f"{record.instruction} {record.operand}".strip()])
    return invalid


def _inline_sts_target_findings(
    program_name: str,
    records: tuple[Instruction, ...],
    operations: tuple[SerializedOperation, ...],
) -> list[list[str]]:
    """Hold peripheral Notes until subtype-preserving Q lifecycle is verified."""
    if not operations:
        return []
    if len(operations) != len(records):
        raise ConversionError(
            "source operation inventory differs from decoded rows",
            finding_code="INPUT_INVALID", mining_tracks="P002",
        )
    findings: list[list[str]] = []
    authority = _validated_upstream_contracts().target_contract.get("note_lifecycle", {})
    for index, (record, operation) in enumerate(zip(records, operations, strict=True)):
        if (
            operation.descriptors in (("NOTE", "st=i"), ("NOTE", "st=s"))
            and record.note_subtype != operation.descriptors[1][-1]
        ):
            findings.append([
                str(record.sequence), str(record.source_step), "", "",
                "Source Note subtype disagrees with its serialized operation provenance",
                "TARGET_UNSUPPORTED", "P004",
                f"program={program_name};note_subtype={record.note_subtype};publication=REVIEW_ONLY;runtime_action=DO_NOT_DOWNLOAD_OR_RUN",
            ])
            continue
        if operation.descriptors == ("NOTE", "st=i") and record.note.startswith("*"):
            findings.append([
                str(record.sequence), str(record.source_step), "", "",
                "In PLC Note begins with the Q CSV peripheral marker; type-preserving escape is unverified",
                "TARGET_UNSUPPORTED", "P004",
                f"program={program_name};note_subtype=i;publication=REVIEW_ONLY;runtime_action=DO_NOT_DOWNLOAD_OR_RUN",
            ])
            continue
        if operation.descriptors != ("NOTE", "st=s"):
            continue
        previous = records[index - 1] if index else None
        if (
            authority.get("support_status") == "FULL"
            and authority.get("subtypes") == ["i", "s"]
            and authority.get("attachment_forms") == [{"instruction": "MOV", "operands": ["K", "D"]}]
            and record.note_subtype == "s"
            and record.note
            and not record.note.startswith("*")
            and not record.instruction and not record.operand
            and previous is not None and previous.instruction == "MOV"
            and re.fullmatch(r"K-?\d+", previous.operand)
            and len(previous.continuation_operands) == 1
            and re.fullmatch(r"D\d+", previous.continuation_operands[0])
        ):
            continue
        findings.append([
            str(record.sequence), str(record.source_step), "", "",
            "Inline Note st=s is In Peripheral; subtype-preserving Q lifecycle is not verified",
            "TARGET_UNSUPPORTED", "P002|P004",
            (
                f"program={program_name};note_subtype=s;"
                f"source_record_id={record.source_record_id};"
                "publication=REVIEW_ONLY;runtime_action=DO_NOT_DOWNLOAD_OR_RUN"
            ),
        ])
    return findings


def _project_fx5_cpu_builtin_socket_status_sd(operand: str) -> str | None:
    """Map only the three approved uppercase FX5 CPU built-in socket status SD scalars.

    Optional ``.0`` through ``.F`` is a single hex bit selector.  Indexed,
    grouped, neighbor, lowercase, multi-digit, and substring forms stay
    unmapped so unprojected rejected operands remain blocked.
    """
    mapped = _FX5_CPU_BUILTIN_SOCKET_STATUS_SD.get(operand)
    if mapped is not None:
        return mapped
    base, separator, bit = operand.partition(".")
    mapped = _FX5_CPU_BUILTIN_SOCKET_STATUS_SD.get(base)
    if mapped is None or separator != "." or bit not in _FX5_CPU_BUILTIN_SOCKET_STATUS_SD_BITS:
        return None
    return f"{mapped}.{bit}"


def _project_fx5_cpu_builtin_socket_status_sd_record(record: Instruction) -> Instruction:
    """Apply the approved socket-status SD map to exact contact and MOV sources."""
    operands = (record.operand, *record.continuation_operands)
    if (
        record.instruction in _FX5_CPU_BUILTIN_SOCKET_STATUS_SD_BIT_CONTACTS
        and len(operands) == 1
        and "." in record.operand
    ):
        mapped = _project_fx5_cpu_builtin_socket_status_sd(record.operand)
        if mapped is not None:
            return replace(record, operand=mapped)
    if (
        record.instruction in _FX5_CPU_BUILTIN_SOCKET_STATUS_SD_WORD_SOURCES
        and len(operands) == 2
        and "." not in record.operand
    ):
        mapped = _project_fx5_cpu_builtin_socket_status_sd(record.operand)
        if mapped is not None:
            candidate = replace(record, operand=mapped)
            # A valid SD address does not approve the whole operand form.
            # Preserve raw source on a target hold so the existing typed word
            # fallback can still represent it, without losing the whole POU.
            if not _validate_q_target_record("", candidate, source_record=record):
                return candidate
    return record


def _project_q_comment_device(device: str) -> str:
    """Project one comment device through X/Y identity, then the approved SD map."""
    _reject_bit_device_word_bit_suffix(device)
    projected_device = project_q06udv_io_device(device)
    mapped = _project_fx5_cpu_builtin_socket_status_sd(projected_device)
    return mapped if mapped is not None else projected_device


def _compatible_comment_device(device: str) -> str:
    """Return the comment device used by Q06UDV compatibility prefilters."""
    try:
        return _project_q_comment_device(device)
    except ConversionError:
        return device


def _socket_source_has_q_lifecycle(record: Instruction) -> bool:
    """Exact FX5 literal-unit migration; source connection is never renumbered."""
    counts = SOURCE_EXACT_SOCKET_OPERAND_COUNTS.get(record.instruction)
    ops = (record.operand, *record.continuation_operands)
    connections = {"K7", "K5"} if record.instruction == "SP.SOCOPEN" else {"K7"}
    if counts is None or len(ops) not in counts or ops[0] not in ('"u0"', '"U0"') or ops[1] not in connections:
        return False
    if record.continuation_rows and (
        len(record.continuation_rows) != len(record.continuation_operands)
        or any(len(row) != 7 or any(row[column] for column in (0, 1, 2, 4)) or row[3] != operand
               for row, operand in zip(record.continuation_rows, record.continuation_operands))
    ):
        return False
    for index, operand in enumerate(ops[2:], 2):
        letter = "M" if index == len(ops) - 1 else "D"
        match = re.fullmatch(letter + r"([0-9]+)", operand)
        if match is None:
            return False
    return True


def project_q_record(source_sha256: str, program_name: str, record: Instruction) -> Instruction:
    """Project source instructions into the verified Q06UDV instruction dialect."""
    full_target_records = _require_target_contract()
    # Keep the source-to-target mapping below unchanged: FULL grammar only
    # supplies a validated target evidence set, never an automatic mapping.
    _ = {item["identity"] for item in full_target_records}
    def finalize(projected: Instruction) -> Instruction:
        if _socket_source_has_q_lifecycle(projected):
            projected = replace(projected, operand="U0")
        return _project_q06udv_io_record(_project_fx5_cpu_builtin_socket_status_sd_record(projected))

    # Explicitly approved migration: FX5 truncation becomes Q rounding.
    # Source IR and provenance stay unchanged; the report discloses the delta.
    # Do this before Note projection so a peripheral Note cannot skip mapping.
    if record.instruction == "FLT2DINT":
        record = replace(record, instruction="DINT")
    if record.note_subtype == "s" and record.note and not record.q_note_projection_applied:
        return finalize(replace(record, note="*" + record.note, q_note_projection_applied=True))
    if record.instruction in SOURCE_EXACT_SOCKET_OPERAND_COUNTS:
        return finalize(record)
    generic = {"TRD": "DATERD", "TWR": "DATEWR", "ANIP": "ANDPI"}.get(record.instruction)
    if generic is not None:
        return finalize(replace(record, instruction=generic))
    operands = (record.operand, *record.continuation_operands)
    if record.instruction == "RST" and len(operands) == 2:
        start, end = operands
        match_start = re.fullmatch(r"([MLD])(\d+)", start)
        match_end = re.fullmatch(r"([MLD])(\d+)", end)
        if match_start and match_end and match_start.group(1) == match_end.group(1):
            first = int(match_start.group(2))
            last = int(match_end.group(2))
            if last >= first:
                count = f"K{last - first + 1}"
                if match_start.group(1) in {"M", "L"}:
                    continuation_rows = record.continuation_rows
                    if continuation_rows:
                        if len(continuation_rows) != 1:
                            raise ConversionError(
                                "ZRST bit-range continuation-row count is invalid",
                                finding_code="MINING_REQUIRED",
                                mining_tracks="P004",
                                candidate=f"program={program_name};operands={start} {end}",
                            )
                        count_row = list(continuation_rows[0])
                        count_row[3] = count
                        continuation_rows = (tuple(count_row),)
                    return finalize(replace(
                        record,
                        instruction="BKRST",
                        operand=start,
                        continuation_operands=(count,),
                        continuation_rows=continuation_rows,
                    ))
                continuation_rows: tuple[tuple[str, ...], ...] = ()
                if record.continuation_rows:
                    if len(record.continuation_rows) != 1:
                        raise ConversionError(
                            "ZRST word-range continuation-row count is invalid",
                            finding_code="MINING_REQUIRED",
                            mining_tracks="P004",
                            candidate=f"program={program_name};operands={start} {end}",
                        )
                    count_row = list(record.continuation_rows[0])
                    count_row[3] = count
                    continuation_rows = (("", "", "", start, "", "", ""), tuple(count_row))
                return finalize(replace(
                    record,
                    instruction="FMOV",
                    operand="K0",
                    continuation_operands=(start, count),
                    continuation_rows=continuation_rows,
                ))
    if source_sha256 != B2_A1_SOURCE_SHA256:
        return finalize(record)
    replacement = B2_A1_Q_REPLACEMENTS.get((program_name, record.instruction, operands))
    if replacement is None:
        return finalize(record)
    instruction, projected_operands = replacement
    continuation_rows = record.continuation_rows
    if continuation_rows:
        if len(continuation_rows) != len(projected_operands) - 1:
            raise ConversionError(
                "approved Q replacement continuation-row count changed",
                finding_code="MINING_REQUIRED",
                mining_tracks="P004",
                candidate=(
                    f"program={program_name};instruction={record.instruction};"
                    f"operands={' '.join(operands)}"
                ),
            )
        projected_rows = []
        for row, operand in zip(continuation_rows, projected_operands[1:], strict=True):
            projected_row = list(row)
            projected_row[3] = operand
            projected_rows.append(tuple(projected_row))
        continuation_rows = tuple(projected_rows)
    return finalize(replace(
        record,
        instruction=instruction,
        operand=projected_operands[0],
        continuation_operands=projected_operands[1:],
        continuation_rows=continuation_rows,
    ))


Q06UDV_MAX_IO_DEVICE_VALUE = fixed_bounds(Profile.Q).x
Q06UDV_IO_MAPPING_MODE = "SOURCE_SPELLING_IDENTITY"
CONVERSION_REPORT_SCHEMA_VERSION = 7
_FX_IO_SOURCE_DEVICE = re.compile(
    r"^(?P<indirect>@?)(?P<group>K[1-8])?(?P<family>[XY])"
    r"(?P<digits>[0-7]+)(?P<index>Z[0-9]+)?$"
)
_Q_IO_TARGET_DEVICE = re.compile(
    r"^(?P<indirect>@?)(?P<group>K[1-8])?(?P<family>[XY])"
    r"(?P<digits>[0-9A-Fa-f]+)(?P<index>Z[0-9]+)?$"
)
_FX_IO_CANDIDATE = re.compile(r"^@?(?:K[0-9]+)?[XY]")


class _QIoDevice(str):
    """Runtime marker for an X/Y operand already rendered for the Q target."""


def _q06udv_io_identity_spelling(match: re.Match[str]) -> str:
    """Production-only X/Y spelling reconstruction from a validated source match."""
    return (
        f"{match.group('indirect')}{match.group('group') or ''}"
        f"{match.group('family')}{match.group('digits')}{match.group('index') or ''}"
    )


def _q06udv_io_target_range_value(digits: str) -> int:
    """Interpret preserved X/Y digits as the Q06UDV hexadecimal ordinal."""
    return int(digits, 16)


def _fx_io_source_syntax_error(operand: str) -> ConversionError:
    return ConversionError(
        f"FX X/Y device has invalid source syntax: {operand}",
        finding_code="DEVICE_RADIX_INVALID",
        mining_tracks="Converter",
        candidate=(
            f"device={operand};source_radix=8;"
            f"mapping_mode={Q06UDV_IO_MAPPING_MODE}"
        ),
    )


def _q_io_range_error(operand: str, value: int) -> ConversionError:
    return ConversionError(
        f"FX X/Y device exceeds the Q06UDV X/Y range: {operand}",
        finding_code="DEVICE_RANGE_INVALID",
        mining_tracks="Converter",
        candidate=(
            f"device={operand};target_hex_ordinal={value};"
            f"target_max={Q06UDV_MAX_IO_DEVICE_VALUE};"
            f"mapping_mode={Q06UDV_IO_MAPPING_MODE}"
        ),
    )


def _check_fx_io_source_span(operand: str, match: re.Match[str]) -> None:
    """Check known literal FX5 X/Y points before source-spelling projection."""
    literal = f"{match.group('group') or ''}{match.group('family')}{match.group('digits')}"
    try:
        source_span(literal, Profile.FX5U)
    except MappingError as error:
        raise _fx_io_source_range_error(operand, detail=str(error)) from error


def _fx_io_source_range_error(operand: str, *, detail: str) -> ConversionError:
    return ConversionError(
        f"FX X/Y source span exceeds the fixed range: {operand}",
        finding_code="DEVICE_RANGE_INVALID", mining_tracks="Converter",
        candidate=(f"device={operand};source_max={fixed_bounds(Profile.FX5U).x};"
                   f"source_radix=8;detail={detail}"),
    )


def expected_q06udv_io_identity_device(operand: str) -> str:
    """Independent expected-target oracle for X/Y source-spelling identity.

    Audit must call this instead of ``project_q06udv_io_device``.  After the
    independent source grammar accepts the token, the
    expected spelling is the exact source operand.  This function must not
    call the production mapper or ``_q06udv_io_identity_spelling``.
    """
    match = _FX_IO_SOURCE_DEVICE.fullmatch(operand)
    if match is None:
        raise _fx_io_source_syntax_error(operand)
    return operand


def project_q06udv_io_device(operand: str) -> str:
    """Preserve one valid FX X/Y source spelling as the Q target spelling.

    Source grammar remains FX octal digits.  Target rendering does not convert
    that ordinal to Q hexadecimal.  Feeding a rendered Q target back through
    this codec is invalid rather than an opportunity for a second projection.
    It does not change physical Q I/O assignment authority.
    """
    if isinstance(operand, _QIoDevice):
        raise ConversionError(
            "Q X/Y device was submitted to the source-spelling identity boundary twice",
            finding_code="DEVICE_RADIX_DOUBLE_PROJECTION",
            mining_tracks="Converter",
            candidate=f"mapping_mode={Q06UDV_IO_MAPPING_MODE};projection_state=Q_TARGET_RENDERED",
        )
    match = _FX_IO_SOURCE_DEVICE.fullmatch(operand)
    if match is None:
        if _FX_IO_CANDIDATE.match(operand):
            raise _fx_io_source_syntax_error(operand)
        return operand
    _check_fx_io_source_span(operand, match)
    return _QIoDevice(_q06udv_io_identity_spelling(match))


def _project_q06udv_io_record(record: Instruction) -> Instruction:
    """Apply X/Y source-spelling identity to every serialized row once."""
    source_surfaces = (
        record.operand,
        *record.continuation_operands,
        *(row[3] for row in record.continuation_rows if len(row) > 3),
    )
    has_source_xy = any(_FX_IO_SOURCE_DEVICE.fullmatch(value) is not None for value in source_surfaces)
    if record.q_io_projection_applied:
        raise ConversionError(
            "Q X/Y record was submitted to the source-spelling identity boundary twice",
            finding_code="DEVICE_RADIX_DOUBLE_PROJECTION",
            mining_tracks="Converter",
            candidate=f"mapping_mode={Q06UDV_IO_MAPPING_MODE};projection_state=Q_TARGET_RENDERED",
        )
    if not has_source_xy:
        return record
    projected_operands = tuple(
        project_q06udv_io_device(operand)
        for operand in (record.operand, *record.continuation_operands)
    )
    continuation_rows: list[tuple[str, ...]] = []
    for row in record.continuation_rows:
        projected_row = list(row)
        if len(projected_row) > 3:
            projected_row[3] = project_q06udv_io_device(projected_row[3])
        continuation_rows.append(tuple(projected_row))
    projected_rows = tuple(continuation_rows)
    return replace(
        record,
        operand=projected_operands[0],
        continuation_operands=projected_operands[1:],
        continuation_rows=projected_rows,
        q_io_projection_applied=True,
    )


def _validate_fx_io_source_record(record: Instruction) -> list[list[str]]:
    """Return fail-closed findings for serialized source device syntax."""
    invalid: list[list[str]] = []
    for operand_position, operand in _device_radix_surface_values(record):
        try:
            _reject_bit_device_word_bit_suffix(operand)
            project_q06udv_io_device(operand)
        except ConversionError as error:
            candidate = ";".join(
                item for item in (
                    f"operand_position={operand_position}",
                    error.candidate,
                ) if item
            )
            invalid.append([
                str(record.sequence), str(record.source_step), record.instruction, operand,
                str(error), error.finding_code, error.mining_tracks, candidate,
            ])
    return invalid


def _device_radix_surface_values(record: Instruction) -> tuple[tuple[str, str], ...]:
    """Return every operand surface covered by the X/Y identity audit.

    A continuation row is an independently serialized Q CSV surface.  Keep it
    distinct from ``continuation_operands`` so a missing or stale extension row
    cannot be hidden by a correctly projected logical continuation operand.
    """
    values: list[tuple[str, str]] = [("primary", record.operand)]
    values.extend((f"continuation:{index}", operand) for index, operand in enumerate(record.continuation_operands))
    values.extend(
        (f"extension:{index}", row[3] if len(row) > 3 else "")
        for index, row in enumerate(record.continuation_rows)
    )
    return tuple(values)


RadixAuditSurfaceIdentity = tuple[str, int, int, str]
_RADIX_AUDIT_ABSENT = "ABSENT"
_RADIX_AUDIT_SUBSTITUTE_SM1255 = "SUBSTITUTE_SM1255"


@dataclass(frozen=True)
class RadixAuditFallbackSurfaceExpectation:
    disposition: str
    fallback_key: tuple[str, str, str, str]
    fallback_kind: str
    source_record: Instruction
    projected_record: Instruction
    source_operand: str
    canonical_target: Instruction


def device_radix_audit(
    source_records_by_program: dict[str, tuple[Instruction, ...]],
    target_records_by_program: dict[str, tuple[Instruction, ...]],
    *,
    verified_nonserialized_fallback_surfaces: Mapping[
        RadixAuditSurfaceIdentity, RadixAuditFallbackSurfaceExpectation,
    ] | None = None,
) -> dict[str, object]:
    """Audit final Q X/Y source-spelling identity by source identity.

    Expected targets come from ``expected_q06udv_io_identity_device``, not from
    the production mapper.  The source identity deliberately includes the POU,
    source step, and physical operand position.  It additionally carries the
    source sequence to fail closed if a malformed source repeats a step.  Only
    direct FX X/Y spellings participate: DX/DY, U<n>\\G<m>, and every other
    device family remain outside this typed identity contract.
    """
    mismatch_samples: list[dict[str, str]] = []
    source_xy_count = 0
    compared_xy_count = 0
    matched_source_xy_count = 0
    changed_source_xy_count = 0
    same_source_xy_count = 0
    failed_or_unresolved_source_xy_count = 0
    mismatch_count = 0
    mismatch_reason_counts: Counter[str] = Counter()
    verified_fallback_excluded_source_xy_count = 0
    verified_fallback_excluded_source_xy_by_position: Counter[str] = Counter()
    consumed_fallback_surface_expectations: set[RadixAuditSurfaceIdentity] = set()

    def mismatch(identity: dict[str, str], expected: str, actual: str, reason: str) -> None:
        nonlocal mismatch_count
        mismatch_count += 1
        mismatch_reason_counts[reason] += 1
        if len(mismatch_samples) < 20:
            mismatch_samples.append({
                **identity,
                "expected_target": expected,
                "actual_target": actual,
                "reason": reason,
            })

    for program_name in sorted(set(source_records_by_program) | set(target_records_by_program)):
        source_records = source_records_by_program.get(program_name, ())
        target_records = target_records_by_program.get(program_name, ())
        targets_by_record_identity: dict[tuple[int, int], list[Instruction]] = {}
        for target_record in target_records:
            targets_by_record_identity.setdefault(
                (target_record.sequence, target_record.source_step), []
            ).append(target_record)

        seen_source_identities: set[tuple[str, int, int, str]] = set()
        for source_record in source_records:
            record_identity = (source_record.sequence, source_record.source_step)
            target_matches = targets_by_record_identity.get(record_identity, [])
            for operand_position, source_operand in _device_radix_surface_values(source_record):
                if _FX_IO_CANDIDATE.match(source_operand) is None:
                    continue
                source_xy_count += 1
                identity = {
                    "program": program_name,
                    "sequence": str(source_record.sequence),
                    "source_step": str(source_record.source_step),
                    "operand_position": operand_position,
                }
                identity_key: RadixAuditSurfaceIdentity = (
                    program_name,
                    source_record.sequence,
                    source_record.source_step,
                    operand_position,
                )
                if identity_key in seen_source_identities:
                    mismatch(identity, "", "", "duplicate_source_identity")
                    failed_or_unresolved_source_xy_count += 1
                    continue
                seen_source_identities.add(identity_key)
                try:
                    expected_target = expected_q06udv_io_identity_device(source_operand)
                except ConversionError:
                    mismatch(identity, "", "", "source_projection_invalid")
                    failed_or_unresolved_source_xy_count += 1
                    continue
                if len(target_matches) != 1:
                    mismatch(
                        identity,
                        expected_target,
                        "",
                        "missing_target_identity" if not target_matches else "duplicate_target_identity",
                    )
                    failed_or_unresolved_source_xy_count += 1
                    continue
                target_values = dict(_device_radix_surface_values(target_matches[0]))
                expectation = (verified_nonserialized_fallback_surfaces or {}).get(identity_key)
                if expectation is not None:
                    consumed_fallback_surface_expectations.add(identity_key)
                    expected_canonical_target = _canonical_official_dummy_fallback_record(
                        expectation.source_record,
                        expectation.projected_record,
                    )
                    if not (
                        expectation.source_record == source_record
                        and expectation.fallback_key == _fallback_record_key(source_record)
                        and expectation.fallback_kind in {"BIT_CONTACT", "BIT_DESTINATION", "ACTION"}
                        and _official_dummy_fallback_kind(source_record) == expectation.fallback_kind
                        and expectation.source_operand == source_operand
                        and expected_canonical_target == expectation.canonical_target
                        and target_matches[0] == expectation.canonical_target
                    ):
                        mismatch(identity, "", "", "verified_fallback_surface_expectation_invalid")
                        failed_or_unresolved_source_xy_count += 1
                        continue
                    disposition = expectation.disposition
                else:
                    disposition = None
                if operand_position not in target_values:
                    if disposition == _RADIX_AUDIT_ABSENT:
                        verified_fallback_excluded_source_xy_count += 1
                        verified_fallback_excluded_source_xy_by_position[operand_position] += 1
                        continue
                    mismatch(identity, expected_target, "", "missing_target_operand_position")
                    failed_or_unresolved_source_xy_count += 1
                    continue
                actual_target = target_values[operand_position]
                if disposition == _RADIX_AUDIT_SUBSTITUTE_SM1255 and actual_target == "SM1255":
                    verified_fallback_excluded_source_xy_count += 1
                    verified_fallback_excluded_source_xy_by_position[operand_position] += 1
                    continue
                if disposition in {_RADIX_AUDIT_ABSENT, _RADIX_AUDIT_SUBSTITUTE_SM1255}:
                    mismatch(identity, "SM1255" if disposition == _RADIX_AUDIT_SUBSTITUTE_SM1255 else "", actual_target, "unexpected_serialized_fallback_surface")
                    failed_or_unresolved_source_xy_count += 1
                    continue
                compared_xy_count += 1
                if actual_target != expected_target:
                    mismatch(identity, expected_target, actual_target, "target_projection_mismatch")
                    failed_or_unresolved_source_xy_count += 1
                else:
                    matched_source_xy_count += 1
                    if source_operand == actual_target:
                        same_source_xy_count += 1
                    else:
                        changed_source_xy_count += 1

        sources_by_record_identity: dict[tuple[int, int], list[Instruction]] = {}
        for source_record in source_records:
            sources_by_record_identity.setdefault(
                (source_record.sequence, source_record.source_step), []
            ).append(source_record)
        for target_record in target_records:
            target_xy_surfaces = tuple(
                (position, value)
                for position, value in _device_radix_surface_values(target_record)
                if _Q_IO_TARGET_DEVICE.fullmatch(value) is not None
            )
            if not target_xy_surfaces:
                continue
            record_identity = (target_record.sequence, target_record.source_step)
            source_matches = sources_by_record_identity.get(record_identity, [])
            if len(source_matches) != 1:
                mismatch(
                    {
                        "program": program_name,
                        "sequence": str(target_record.sequence),
                        "source_step": str(target_record.source_step),
                        "operand_position": "record",
                    },
                    "",
                    "",
                    "target_identity_not_in_source" if not source_matches else "duplicate_source_record_identity",
                )
                continue
            source_xy_positions = {
                position
                for position, value in _device_radix_surface_values(source_matches[0])
                if _FX_IO_CANDIDATE.match(value) is not None
            }
            for operand_position, target_operand in target_xy_surfaces:
                if operand_position not in source_xy_positions:
                    mismatch(
                        {
                            "program": program_name,
                            "sequence": str(target_record.sequence),
                            "source_step": str(target_record.source_step),
                            "operand_position": operand_position,
                        },
                        "",
                        target_operand,
                        "extra_target_operand_position",
                    )

    for identity_key in sorted(
        set((verified_nonserialized_fallback_surfaces or {})) - consumed_fallback_surface_expectations
    ):
        program_name, sequence, source_step, operand_position = identity_key
        mismatch(
            {
                "program": program_name,
                "sequence": str(sequence),
                "source_step": str(source_step),
                "operand_position": operand_position,
            },
            "",
            "",
            "unused_verified_fallback_surface_expectation",
        )

    accounted_xy_count = (
        matched_source_xy_count
        + verified_fallback_excluded_source_xy_count
        + failed_or_unresolved_source_xy_count
    )
    status = "PASS" if source_xy_count == accounted_xy_count and mismatch_count == 0 else "FAIL"
    return {
        "scope": "LADDER_XY_OPERANDS",
        "source_artifact": "normalized.csv",
        "source_role": "SOURCE_PRESERVING",
        "target_role": "Q_TARGET_SOURCE_SPELLING",
        "mapping_mode": Q06UDV_IO_MAPPING_MODE,
        "conversion_applied": False,
        "source_radix": {"X": 8, "Y": 8},
        "target_radix": {"X": 16, "Y": 16},
        "source_xy_count": source_xy_count,
        "compared_xy_count": compared_xy_count,
        "matched_source_xy_count": matched_source_xy_count,
        "changed_source_xy_count": changed_source_xy_count,
        "same_source_xy_count": same_source_xy_count,
        "verified_fallback_excluded_source_xy_count": verified_fallback_excluded_source_xy_count,
        "verified_fallback_excluded_source_xy_by_position": dict(sorted(verified_fallback_excluded_source_xy_by_position.items())),
        "failed_or_unresolved_source_xy_count": failed_or_unresolved_source_xy_count,
        "accounted_xy_count": accounted_xy_count,
        "mismatch_count": mismatch_count,
        "mismatch_reason_counts": dict(sorted(mismatch_reason_counts.items())),
        "status": status,
        "mismatch_sample": mismatch_samples,
    }


def _device_radix_audit_finding(audit: dict[str, object]) -> list[str] | None:
    """Return one global fail-closed finding when final X/Y ordinal drifts."""
    if audit["status"] == "PASS":
        return None
    return [
        "", "", "", "",
        "final Ladder X/Y source-spelling identity audit did not match Q targets",
        "DEVICE_RADIX_AUDIT_FAILED",
        "Converter",
        (
            "scope=project;"
            f"source_xy_count={audit['source_xy_count']};"
            f"compared_xy_count={audit['compared_xy_count']};"
            f"verified_fallback_excluded_source_xy_count={audit['verified_fallback_excluded_source_xy_count']};"
            f"mismatch_count={audit['mismatch_count']}"
        ),
    ]


def _validate_q_target_record(
    program_name: str,
    record: Instruction,
    *,
    source_record: Instruction | None = None,
) -> list[list[str]]:
    """Apply identical Q06UDV target gates to single- and multi-POU records."""
    invalid: list[list[str]] = []
    operands = (record.operand, *record.continuation_operands)
    evidence = source_record or record
    evidence_operands = (evidence.operand, *evidence.continuation_operands)
    operand_count = sum(bool(value) for value in operands)
    from .annunciator import extended_f_form, supported_ans
    if supported_ans(record):
        # Explicit user-approved code rewrite, without timer-unit inspection.
        # This does not assert CPU parameter or runtime equivalence.
        return []
    # Check projected addresses before operand-form or lifecycle gates. GX Works2
    # silently drops out-of-range contacts, which can break the ladder stack.
    out_of_range = [value for value in operands
                    if _q06udv_program_operand_out_of_range(value) and not _q06udv_rejected_operand(value)]
    if out_of_range:
        return [[
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "projected special device exceeds Q06UDV fixed SM/SD0..2047 range",
            "TARGET_UNSUPPORTED", "Converter",
            f"program={program_name};operands={' '.join(evidence_operands)};out_of_range={','.join(out_of_range)}",
        ]]
    digit_errors = digit_operand_errors(record.instruction, operands)
    if digit_errors:
        return [[
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "; ".join(digit_errors), "TARGET_UNSUPPORTED", "P004",
            f"program={program_name};instruction={evidence.instruction};operands={' '.join(evidence_operands)}",
        ]]
    if extended_f_form(record):
        return []
    # The reverse grouped-Y source tuple remains unverified.  The destination
    # tuple now goes through the same exact P004 operand-form gate as Neutral IR.
    source_only_y_tags = {MIL_EXACT_Y_KS_MOV_PROVENANCE}
    if source_only_y_tags.intersection(evidence.source_semantic_tags):
        return [[
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "source-only MIL Y/Ks tuple has no approved P004 Q06UDV lifecycle oracle",
            "TARGET_UNSUPPORTED", "P004",
            (
                f"program={program_name};source_locator={evidence.source_locator};"
                f"source_semantic_tag={','.join(sorted(source_only_y_tags.intersection(evidence.source_semantic_tags)))};"
                f"instruction={evidence.instruction};operands={' '.join(evidence_operands)}"
            ),
        ]]
    target_fact = _validate_target_contract_fact(record)
    kn_transfer = record.instruction in {"MOV", "MOVP", "DMOV", "DMOVP"} and any(
        re.fullmatch(r"K[1-8](SM|SB|[XYMLFB])[0-9A-F]+", value) for value in operands
    )
    # Existing scalar literal-to-M projection is outside the new D/Kn oracle.
    # Keep its prior gate while requiring FULL evidence for the newly added forms.
    if (record.instruction == "MOV" and len(operands) == 2
            and re.fullmatch(r"(?:K[-+]?[0-9]+|H[0-9A-F]+)", operands[0])
            and re.fullmatch(r"K[1-4]M[0-9]+", operands[1])):
        kn_transfer = False
    if (record.instruction == "MOV" and len(operands) == 2
            and re.fullmatch(r"K[1-4]M[0-9]+", operands[0])
            and re.fullmatch(r"M[0-9]+", operands[1])):
        kn_transfer = False
    if (kn_transfer or record.instruction in Q06UDV_PHASE6_LIFECYCLE_INSTRUCTIONS or record.instruction in {"SFL", "SFLP"}) and target_fact is None:
        return [[
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "instruction operand form has no approved P004 Q06UDV FULL lifecycle oracle",
            "TARGET_UNSUPPORTED", "P004",
            f"program={program_name};instruction={evidence.instruction};operands={' '.join(evidence_operands)}",
        ]]
    socket_lifecycle = (
        _socket_source_has_q_lifecycle(evidence)
        and record.instruction == evidence.instruction
        and record.continuation_rows == evidence.continuation_rows
        and record.operand == "U0"
        and record.continuation_operands == evidence.continuation_operands
        and target_fact is not None
    )
    source_form_has_no_lifecycle_oracle = (
        (record.instruction in Q06UDV_UNVERIFIED_LIFECYCLE_INSTRUCTIONS
         and record.instruction not in Q06UDV_PHASE6_LIFECYCLE_INSTRUCTIONS
         and not socket_lifecycle)
        or (record.instruction in {"+", "-", "D+"} and operand_count == 2)
    )
    if source_form_has_no_lifecycle_oracle:
        if operand_count not in MIL_OPERAND_COUNTS[record.instruction]:
            return invalid
        return [[
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "instruction has no approved P004 Q06UDV FULL lifecycle oracle",
            "TARGET_UNSUPPORTED", "P004",
            f"program={program_name};instruction={evidence.instruction};operands={' '.join(evidence_operands)}",
        ]]
    source_exact_operand_hold = target_fact is None and (
        any(_p004_operand_form(operand).startswith("INDEXED_") for operand in operands)
        or any(
            _p004_operand_form(operand) in {
                "TnZ", "B...Z", "M...Z", "D...Z", "L...Z", "R...Z",
                "K4M...Z", "U...\\G...Z",
            }
            for operand in operands
        )
        or any(re.fullmatch(r"K[1-8]F\d+", operand) for operand in operands)
        or (
            record.instruction == "AND"
            and len(operands) == 1
            and re.fullmatch(r"L\d+Z\d+", operands[0]) is not None
        )
        or (
            record.instruction == "$+"
            and len(operands) == 3
            and all(re.fullmatch(r"W[0-9A-Fa-f]+", operand) for operand in operands[:2])
            and re.fullmatch(r"D\d+", operands[2]) is not None
        )
        or (
            record.instruction == "MOV"
            and len(operands) == 2
            and re.fullmatch(r"D\d+", operands[0]) is not None
            and re.fullmatch(r"K2Y[0-9A-Fa-f]+", operands[1]) is not None
        )
        or (
            record.instruction == "MOV"
            and len(operands) == 2
            and re.fullmatch(r"SD\d+", operands[0]) is not None
            and re.fullmatch(r"K4M\d+", operands[1]) is not None
        )
        or (
            record.instruction == "FOR"
            and len(operands) == 1
            and re.fullmatch(r"K\d+", operands[0]) is not None
        )
    )
    if source_exact_operand_hold:
        return [[
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "source-exact operand structure has no approved P004 Q06UDV oracle",
            "TARGET_UNSUPPORTED", "P004",
            f"program={program_name};instruction={evidence.instruction};operands={' '.join(evidence_operands)}",
        ]]
    if record.instruction in Q06UDV_REJECTED_INSTRUCTIONS:
        invalid.append([
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "instruction was rejected by GX Works2 1.635M for Q06UDVCPU",
            "TARGET_UNSUPPORTED", "P004",
            f"program={program_name};instruction={evidence.instruction};operands={' '.join(evidence_operands)}",
        ])
        if record.instruction == "ANS":
            return invalid
    rejected_operands = [operand for operand in operands if _q06udv_rejected_operand(operand)]
    if rejected_operands:
        invalid.append([
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            _q06udv_operand_hold_reason(rejected_operands),
            "TARGET_UNSUPPORTED", "P004",
            (
                f"program={program_name};operands={' '.join(evidence_operands)};"
                f"rejected_operands={','.join(rejected_operands)}"
            ),
        ])
    if target_fact is None and _q06udv_unverified_operand_combination(record, operands):
        invalid.append([
            str(evidence.sequence), str(evidence.source_step), evidence.instruction, evidence.operand,
            "operand combination has no approved P004 Q06UDV oracle",
            "TARGET_UNSUPPORTED", "P004",
            f"program={program_name};instruction={evidence.instruction};operands={' '.join(evidence_operands)}",
        ])
    return invalid


def validate_project(project: ParsedProject | ParsedMultiProject) -> list[list[str]]:
    """Validate a decoded project under its exact-approved semantic profile."""
    if isinstance(project, ParsedMultiProject):
        invalid = _source_diagnostic_findings(project.source_diagnostics)
        for program in project.programs:
            for finding in program.findings:
                candidate = finding[7]
                invalid.append([
                    *finding[:7],
                    candidate if _candidate_program(candidate) == program.name else (
                        f"program={program.name};{candidate}" if candidate else f"program={program.name}"
                    ),
                ])
            # Every direct source row must pass the source instruction and
            # operand contract before target projection.  Restricting this to
            # known mnemonics would let an unknown direct instruction publish
            # without an evidence finding.
            if program.direct_decoded:
                for source_record in program.instructions:
                    for row in validate_records((source_record,)):
                        invalid.append([
                            *row[:7],
                            f"program={program.name};{row[7]}" if row[7] else f"program={program.name}",
                        ])
            for source_record in program.instructions:
                source_io_findings = _validate_fx_io_source_record(source_record)
                if source_io_findings:
                    invalid.extend([
                        [
                            *row[:7],
                            f"program={program.name};{row[7]}" if row[7] else f"program={program.name}",
                        ]
                        for row in source_io_findings
                    ])
                    continue
                record = project_q_record(project.source_sha256, program.name, source_record)
                invalid.extend(_validate_q_target_record(program.name, record, source_record=source_record))
            invalid.extend(validate_ladder_notes(program.instructions, program_name=program.name))
            invalid.extend(_inline_sts_target_findings(
                program.name, program.instructions, program.source_operations,
            ))
        return invalid
    invalid = [*_source_diagnostic_findings(project.source_diagnostics), *project.findings]
    projected_records: list[Instruction] = []
    source_io_invalid_sequences: set[int] = set()
    for source_record in project.instructions:
        source_io_findings = _validate_fx_io_source_record(source_record)
        if source_io_findings:
            invalid.extend(source_io_findings)
            source_io_invalid_sequences.add(source_record.sequence)
            projected_records.append(source_record)
            continue
        projected_records.append(project_q_record("", project.program_name, source_record))
    projected = tuple(projected_records)
    invalid.extend(validate_records(projected))
    for source_record, record in zip(project.instructions, projected, strict=True):
        if source_record.sequence in source_io_invalid_sequences:
            continue
        invalid.extend(_validate_q_target_record(project.program_name, record, source_record=source_record))
    invalid.extend(validate_ladder_notes(projected, program_name=project.program_name))
    invalid.extend(_inline_sts_target_findings(
        project.program_name, project.instructions, project.source_operations,
    ))
    return invalid


def _q06udv_program_operand_out_of_range(operand: str) -> bool:
    """Reject impossible fixed special-device addresses, not dynamic index values.

    Q06UDVCPU manufacturer specification fixes both SM and SD at 2048 points.
    This program gate is separate from the historical exact comment holds.
    """
    match = re.fullmatch(r"@?(?:K([1-8]))?(SM|SD)(\d+)(?:Z\d+)?(?:\.[0-9A-F]+)?", operand)
    if match is None:
        return False
    width = int(match[1]) * 4 if match[1] and match[2] == "SM" else 1
    return int(match[3]) + width - 1 > 2047


def _q06udv_rejected_operand(operand: str) -> bool:
    """Keep exact observed rejections and semantic holds without a numeric limit."""
    return operand in Q06UDV_REJECTED_OPERANDS or operand in Q06UDV_SEMANTIC_HOLD_OPERANDS


def _q06udv_operand_hold_reason(operands: Sequence[str], subject: str = "operand") -> str:
    observed = any(operand in Q06UDV_REJECTED_OPERANDS for operand in operands)
    semantic = any(operand in Q06UDV_SEMANTIC_HOLD_OPERANDS for operand in operands)
    reasons = []
    if observed:
        reasons.append(f"{subject} was rejected by GX Works2 1.635M for Q06UDVCPU")
    if semantic:
        reasons.append(f"{subject} has an exact semantic HOLD without an approved Q equivalent")
    return "; ".join(reasons)


_BIT_DEVICE_WORD_BIT_SUFFIX = re.compile(
    r"^@?(?:K[1-8])?(?:SM|SB|S(?!D)|[XYMLBFV])[0-9A-F]+(?:Z[0-9]+)?\.[0-9A-F]+$",
    re.ASCII | re.IGNORECASE,
)


def _reject_bit_device_word_bit_suffix(operand: str) -> None:
    """A bit device cannot carry the word-device .bit suffix."""
    if _BIT_DEVICE_WORD_BIT_SUFFIX.fullmatch(operand):
        raise ConversionError(
            f"bit device cannot use a word-bit suffix: {operand}",
            finding_code="DEVICE_SYNTAX_INVALID", mining_tracks="Converter",
            candidate=f"device={operand};syntax=BIT_DEVICE_WORD_BIT_SUFFIX",
        )


def _q06udv_unverified_operand_combination(
    record: Instruction, operands: tuple[str, ...]
) -> bool:
    """Keep future source-exact operand families blocked until a Q06UDV oracle exists.

    The formerly blocked Converter families ($+, WSUM, LEN, SUM K4M, D/R double-word
    comparisons, FOR D, and TnZ contacts) are approved by P004 batch-003..005
    A/B import, rebuild, reopen, official re-export, and byte-exact goldens.
    """
    # Direct-access I/O stays in its own device family.  The target mapping is
    # spelling-preserving (DX -> DX and DY -> DY), never DX/DY -> X/Y.
    from .annunciator import plain_f_bit
    if plain_f_bit(record):
        return False
    return any(
        re.fullmatch(r"F\d+(?:Z\d+)?", operand)
        or re.fullmatch(r"B[0-9A-Fa-f]+Z\d+", operand)
        or (record.instruction == "DMOVP" and re.fullmatch(r"H[0-9A-Fa-f]+", operand))
        for operand in operands
    )


def validate_ladder_notes(
    records: tuple[Instruction, ...],
    *,
    program_name: str = "",
) -> list[list[str]]:
    """Reject Ladder Notes that cannot be represented by Korean GX Works2."""
    invalid: list[list[str]] = []
    for record in records:
        note_rows = [("primary", record.note)]
        note_rows.extend(
            (f"continuation[{index}]", row[6])
            for index, row in enumerate(record.continuation_rows)
            if len(row) > 6
        )
        for row_type, note in note_rows:
            try:
                note.encode("cp949")
            except UnicodeEncodeError as error:
                candidate_parts = []
                if program_name:
                    candidate_parts.append(f"program={program_name}")
                candidate_parts.extend((f"row_type={row_type}", f"character_index={error.start}"))
                invalid.append([
                    str(record.sequence), str(record.source_step), record.instruction, record.operand,
                    f"ladder note is not representable in CP949 at character {error.start}",
                    "NOTE_ENCODING_REQUIRED", "", ";".join(candidate_parts),
                ])
    return invalid


def _comment_target_alias_collision_findings(
    projected_by_source: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    """Reject mapped comment targets that would silently merge distinct source devices."""
    by_target: dict[str, list[str]] = {}
    for source_device, projected_device in projected_by_source:
        sources = by_target.setdefault(projected_device, [])
        if source_device not in sources:
            sources.append(source_device)
    invalid: list[list[str]] = []
    for projected_device, sources in by_target.items():
        if len(sources) > 1 and any(source_device != projected_device for source_device in sources):
            invalid.append([
                "", "", "COMMENT", sources[0],
                "device-comment target aliases collide",
                "TARGET_DEVICE_ALIAS_COLLISION",
                "Converter",
                f"target_device={projected_device};source_devices={' '.join(sources)}",
            ])
    return invalid


def _reject_comment_target_alias_collisions(projections: tuple[CommentProjection, ...]) -> None:
    """Fail closed when mapped comment devices would silently merge."""
    findings = _comment_target_alias_collision_findings(
        tuple((projection.source_device, projection.device) for projection in projections)
    )
    if not findings:
        return
    raise ConversionError(
        findings[0][4],
        finding_code=findings[0][5],
        mining_tracks=findings[0][6],
        candidate=findings[0][7],
    )


def validate_comments(comments: tuple[DeviceComment, ...]) -> list[list[str]]:
    """Inventory target-incompatible devices and non-CP949 comment text."""
    invalid: list[list[str]] = []
    projected_by_source: list[tuple[str, str]] = []
    for comment in comments:
        try:
            _reject_bit_device_word_bit_suffix(comment.device)
            projected_device = _project_q_comment_device(comment.device)
        except ConversionError as error:
            invalid.append([
                "", "", "COMMENT", comment.device, str(error),
                error.finding_code, error.mining_tracks, error.candidate,
            ])
            continue
        projected_by_source.append((comment.device, projected_device))
        if _q06udv_rejected_operand(projected_device):
            invalid.append([
                "", "", "COMMENT", comment.device,
                _q06udv_operand_hold_reason((projected_device,), "device"),
                "TARGET_UNSUPPORTED", "P004", f"device={comment.device}",
            ])
            continue
        try:
            comment.text.encode("cp949")
        except UnicodeEncodeError as error:
            invalid.append(
                [
                    "",
                    "",
                    "COMMENT",
                    comment.device,
                    f"device comment is not representable in CP949 at character {error.start}",
                    "COMMENT_ENCODING_REQUIRED",
                    "",
                    f"device={comment.device};character_index={error.start}",
                ]
            )
    invalid.extend(_comment_target_alias_collision_findings(tuple(projected_by_source)))
    return invalid


def validate_comment_encoding(comments: tuple[DeviceComment, ...]) -> list[list[str]]:
    """Return only findings that prevent deterministic COMMENT.csv publication."""
    return [
        row for row in validate_comments(comments)
        if row[5] in {
            "COMMENT_ENCODING_REQUIRED",
            "DEVICE_RADIX_INVALID",
            "DEVICE_RANGE_INVALID",
            "DEVICE_SYNTAX_INVALID",
            "TARGET_DEVICE_ALIAS_COLLISION",
        }
    ]


def project_comment(comment: DeviceComment) -> CommentProjection:
    """Project one comment to GX Works2's observed 32-byte CP949 boundary."""
    projected_device = _project_q_comment_device(comment.device)
    target_text = comment.target_text if comment.target_text is not None else comment.text
    try:
        source_encoded = comment.text.encode("cp949")
        encoded = target_text.encode("cp949")
    except UnicodeEncodeError as error:
        raise ConversionError(
            f"device comment is not representable in CP949: {comment.device}",
            finding_code="COMMENT_ENCODING_REQUIRED",
            candidate=f"device={comment.device};character_index={error.start}",
        ) from error
    if len(encoded) <= MAX_Q_COMMENT_BYTES:
        status = comment.target_status or "EXACT"
        return CommentProjection(
            projected_device, comment.device, comment.text, target_text,
            len(source_encoded), len(encoded), status,
        )
    projected: list[str] = []
    projected_bytes = 0
    for character in target_text:
        width = len(character.encode("cp949"))
        if projected_bytes + width > MAX_Q_COMMENT_BYTES:
            break
        projected.append(character)
        projected_bytes += width
    # Truncation can expose a space that was internal in the normalized input.
    # GX Works2 removes this final ASCII padding on save; retain source_text
    # while making the actual projected bytes match its stored comment.
    projected_text = "".join(projected).rstrip(" ")
    return CommentProjection(
        projected_device,
        comment.device,
        comment.text,
        projected_text,
        len(source_encoded),
        len(projected_text.encode("cp949")),
        "TRUNCATED",
    )


def project_comments(comments: tuple[DeviceComment, ...]) -> tuple[CommentProjection, ...]:
    return tuple(project_comment(comment) for comment in comments)


def _comment_projection_for_review(projection: CommentProjection) -> CommentProjection:
    """Mark Q06UDV-excluded comments without losing their source projection details."""
    if not _q06udv_rejected_operand(projection.device):
        return projection
    return replace(
        projection,
        status=f"{COMMENT_TARGET_EXCLUDED_STATUS}|{projection.status}",
    )


def _comment_review_projection(comment: DeviceComment) -> CommentProjection:
    """Preserve target-excluded source text even when it cannot be encoded for GX Works2."""
    try:
        projection = project_comment(comment)
    except ConversionError:
        projected_device = _compatible_comment_device(comment.device)
        if not _q06udv_rejected_operand(projected_device):
            raise
        return CommentProjection(
            projected_device,
            comment.device,
            comment.text,
            "",
            None,
            None,
            f"{COMMENT_TARGET_EXCLUDED_STATUS}|ENCODING_UNREPRESENTABLE",
        )
    return _comment_projection_for_review(projection)


def _q06udv_compatible_comment_projections(
    projections: tuple[CommentProjection, ...],
) -> tuple[CommentProjection, ...]:
    """Exclude only independently observed exact target-rejected comment devices."""
    return tuple(
        projection
        for projection in projections
        if not _q06udv_rejected_operand(projection.device)
    )


def project_ladder_note(note: str) -> str:
    """Project one Ladder Note to the Korean GX Works2 CP949 32-byte limit."""
    try:
        encoded = note.encode("cp949")
    except UnicodeEncodeError as error:
        raise ConversionError(
            f"ladder note is not representable in CP949 at character {error.start}",
            finding_code="NOTE_ENCODING_REQUIRED",
            candidate=f"character_index={error.start}",
        ) from error
    if len(encoded) <= MAX_Q_NOTE_BYTES:
        return note
    projected: list[str] = []
    projected_bytes = 0
    for character in note:
        width = len(character.encode("cp949"))
        if projected_bytes + width > MAX_Q_NOTE_BYTES:
            break
        projected.append(character)
        projected_bytes += width
    logging.warning(
        "GX Works2 Ladder Note was projected to the CP949 byte limit; source_bytes=%s; projected_bytes=%s",
        len(encoded), projected_bytes,
    )
    return "".join(projected)


def _encode_csv(rows: list[list[str]], *, encoding: str, delimiter: str, quoting: int) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, quoting=quoting, lineterminator="\r\n")
    writer.writerows(rows)
    text = stream.getvalue()
    if encoding == "utf-16le-bom":
        return b"\xff\xfe" + text.encode("utf-16le")
    return text.encode(encoding)


def normalized_csv(project: ParsedProject | ParsedMultiProject) -> bytes:
    rows = [NORMALIZED_HEADER]
    programs = project.programs if isinstance(project, ParsedMultiProject) else (
        ParsedProgram(project.program_name, "", project.instructions, project.structure),
    )
    for program in programs:
        rows.extend(
            [
                "1", project.source_project, program.name, str(record.sequence),
                str(record.source_step), record.instruction, record.operand,
            ]
            for record in program.instructions
        )
    return _encode_csv(rows, encoding="utf-8", delimiter=",", quoting=csv.QUOTE_MINIMAL)


WINDOWS_RESERVED_FILENAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
WINDOWS_FILENAME_FORBIDDEN = frozenset('<>:"/\\|?*')
GXWORKS2_PROGRAM_NAME = re.compile(r"^[A-Za-z0-9_]{1,8}$")


def gxworks2_program_names(program_names: tuple[str, ...]) -> tuple[str, ...]:
    """Return deterministic, unique GX Works2 program names of at most eight ASCII bytes."""
    folded_sources = [name.casefold() for name in program_names]
    if len(set(folded_sources)) != len(folded_sources):
        raise ConversionError("source POU names collide case-insensitively")

    direct_names: dict[str, str] = {}
    for name in program_names:
        if GXWORKS2_PROGRAM_NAME.fullmatch(name) and name.upper() not in WINDOWS_RESERVED_FILENAMES:
            direct_names[name] = name

    used = {name.casefold() for name in direct_names.values()}
    targets: list[str] = []
    fallback_counter = 0
    for source_name in program_names:
        direct = direct_names.get(source_name)
        if direct is not None:
            targets.append(direct)
            continue

        stem = "".join(
            character.upper() if character.isascii() and character.isalnum() else "_"
            for character in source_name
        ).strip("_") or "POU"
        digest = hashlib.sha256(source_name.encode("utf-8")).hexdigest().upper()
        candidates = (
            stem[:4].ljust(4, "_") + digest[offset:offset + 4]
            for offset in range(0, len(digest) - 3, 4)
        )
        target = next((candidate for candidate in candidates if candidate.casefold() not in used), "")
        while not target:
            fallback_counter += 1
            candidate = f"P{fallback_counter:07X}"
            if candidate.casefold() not in used:
                target = candidate
        used.add(target.casefold())
        targets.append(target)
    return tuple(targets)


def q_import_csv_name(program_name: str) -> str:
    """Return a validated GX Works2 `<program name>.csv` filename."""
    if not program_name:
        raise ConversionError("POU name is empty and cannot be used as a Q CSV filename")
    if program_name in {".", ".."}:
        raise ConversionError(f"POU name is not a valid Windows filename: {program_name!r}")
    if program_name.endswith((" ", ".")):
        raise ConversionError(
            f"POU name has a trailing dot or space and cannot be used as a Q CSV filename: {program_name!r}"
        )
    if any(
        ord(character) < 32
        or character in WINDOWS_FILENAME_FORBIDDEN
        or character in ";="
        for character in program_name
    ):
        raise ConversionError(
            "POU name contains an invalid filename or candidate-structure delimiter and cannot be used as a Q CSV filename: "
            f"{program_name!r}"
        )
    if program_name.split(".", 1)[0].upper() in WINDOWS_RESERVED_FILENAMES:
        raise ConversionError(
            f"POU name is a reserved Windows filename and cannot be used as a Q CSV filename: {program_name!r}"
        )
    if not GXWORKS2_PROGRAM_NAME.fullmatch(program_name):
        raise ConversionError(
            "GX Works2 program name must contain only ASCII letters, digits, or underscore and be at most 8 bytes: "
            f"{program_name!r}"
        )
    filename = f"{program_name}.csv"
    if len(filename) > 255:
        raise ConversionError("POU name is too long for a Windows Q CSV filename")
    return filename


def q_paths_for_names(q_output: Path, program_names: tuple[str, ...]) -> tuple[Path, ...]:
    target_names = gxworks2_program_names(program_names)
    filenames = tuple(q_import_csv_name(name) for name in target_names)
    folded = [filename.casefold() for filename in filenames]
    if len(set(folded)) != len(folded):
        raise ConversionError("POU names collide case-insensitively as Q CSV filenames")
    return tuple(q_output.with_name(filename) for filename in filenames)


def q_paths_for_project(project: ParsedProject | ParsedMultiProject, q_output: Path) -> tuple[Path, ...]:
    """Map one parsed project to exact `<POU name>.csv` destinations."""
    names = (
        tuple(program.name for program in project.programs)
        if isinstance(project, ParsedMultiProject)
        else (project.program_name,)
    )
    return q_paths_for_names(q_output, names)


def partial_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject,
    q_output: Path,
) -> tuple[Path, ...]:
    """Plan non-importable per-POU review paths beside the Q CSV paths."""
    q_paths = q_paths_for_project(project, q_output)
    review_paths = tuple(path.with_suffix(".txt") for path in q_paths)
    folded = [str(path.name).casefold() for path in (*q_paths, *review_paths)]
    if len(set(folded)) != len(folded):
        raise ConversionError("POU Q and partial-review output names collide case-insensitively")
    return review_paths


def mining_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject,
    q_output: Path,
) -> tuple[Path, ...]:
    """Plan import-shaped review paths that remain outside normal Q publication."""
    q_paths = q_paths_for_project(project, q_output)
    partial_paths = tuple(path.with_suffix(".txt") for path in q_paths)
    mining_paths = tuple(path.with_suffix(".mining-review.csv") for path in q_paths)
    folded = [str(path.name).casefold() for path in (*q_paths, *partial_paths, *mining_paths)]
    if len(set(folded)) != len(folded):
        raise ConversionError("POU Q, partial-review, and mining-review output names collide case-insensitively")
    return mining_paths


def typed_mining_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject,
    q_output: Path,
) -> tuple[Path, ...]:
    """Plan strict typed-mining review paths apart from relation-only review."""
    q_paths = q_paths_for_project(project, q_output)
    partial_paths = tuple(path.with_suffix(".txt") for path in q_paths)
    mining_paths = tuple(path.with_suffix(".mining-review.csv") for path in q_paths)
    typed_paths = tuple(path.with_suffix(".typed-mining-review.csv") for path in q_paths)
    folded = [str(path.name).casefold() for path in (*q_paths, *partial_paths, *mining_paths, *typed_paths)]
    if len(set(folded)) != len(folded):
        raise ConversionError("POU Q, partial-review, mining-review, and typed-mining-review output names collide case-insensitively")
    return typed_paths


def source_recovered_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject, q_output: Path,
) -> tuple[Path, ...]:
    """Plan source-recovery ledgers which are never normal Q CSV outputs."""
    return tuple(path.with_suffix(".source-recovered-review.csv") for path in q_paths_for_project(project, q_output))


def structural_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject, q_output: Path,
) -> tuple[Path, ...]:
    """Plan non-semantic structural review ledgers separately from Q CSV."""
    return tuple(path.with_suffix(".structural-review.csv") for path in q_paths_for_project(project, q_output))


def import_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject, q_output: Path,
) -> tuple[Path, ...]:
    """Return the one shared no-clobber plan for review-only import CSVs.

    Source recovery and structural review are mutually exclusive per actual POU,
    but both use the same import-shaped artifact name.  Keeping one plan avoids
    presenting the same destination twice to exclusive creation.
    """
    return q_paths_for_project(project, q_output)


def source_recovered_import_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject, q_output: Path,
) -> tuple[Path, ...]:
    return import_review_paths_for_project(project, q_output)


def structural_import_review_paths_for_project(
    project: ParsedProject | ParsedMultiProject, q_output: Path,
) -> tuple[Path, ...]:
    return import_review_paths_for_project(project, q_output)


def planned_review_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    """Return the non-importable partial-review output plan."""
    if project is not None:
        return partial_review_paths_for_project(project, q_output)
    return partial_review_paths_for_project(read_gx3(input_path), q_output)


def planned_mining_review_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    """Return the gated import-shaped mining-review output plan."""
    if project is not None:
        return mining_review_paths_for_project(project, q_output)
    return mining_review_paths_for_project(read_gx3(input_path), q_output)


def planned_typed_mining_review_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    """Return the strict typed-mining review output plan."""
    if project is not None:
        return typed_mining_review_paths_for_project(project, q_output)
    return typed_mining_review_paths_for_project(read_gx3(input_path), q_output)


def planned_source_recovered_review_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    """Return the non-import source-recovery review output plan."""
    if project is not None:
        return source_recovered_review_paths_for_project(project, q_output)
    return source_recovered_review_paths_for_project(read_gx3(input_path), q_output)


def planned_structural_review_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    """Return the non-import structural-review output plan."""
    if project is not None:
        return structural_review_paths_for_project(project, q_output)
    return structural_review_paths_for_project(read_gx3(input_path), q_output)


def planned_import_review_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    return import_review_paths_for_project(project, q_output) if project is not None else import_review_paths_for_project(read_gx3(input_path), q_output)


def planned_source_recovered_import_review_outputs(input_path: Path, q_output: Path, *, project: ParsedProject | ParsedMultiProject | None = None) -> tuple[Path, ...]:
    return planned_import_review_outputs(input_path, q_output, project=project)


def planned_structural_import_review_outputs(input_path: Path, q_output: Path, *, project: ParsedProject | ParsedMultiProject | None = None) -> tuple[Path, ...]:
    return planned_import_review_outputs(input_path, q_output, project=project)


def planned_q_outputs(
    input_path: Path,
    q_output: Path,
    *,
    project: ParsedProject | ParsedMultiProject | None = None,
) -> tuple[Path, ...]:
    """Return the complete no-clobber Q output set from the parsed source structure."""
    if project is not None:
        return q_paths_for_project(project, q_output)
    return q_paths_for_project(read_gx3(input_path), q_output)


def q_import_csv(
    project_identifier: str,
    records: tuple[Instruction, ...],
    *,
    preserve_source_steps: bool = False,
    target_only_steps: bool = False,
) -> bytes:
    if any(record.instruction == "ANS" for record in records):
        from .annunciator import expand
        try:
            records = expand(records)
        except ValueError as error:
            raise ConversionError(str(error), finding_code="ANS_EXPANSION_INVALID", mining_tracks="Converter") from error
        # Source step numbers cannot address a one-to-many target program.
        records = tuple(replace(record, target_step=None) for record in records)
        target_only_steps = True
    target_records = _require_target_contract()
    if any(record["csv_projection"] != {"instruction_column": 2, "operand_column": 3, "column_count": 7} for record in target_records):
        raise ConversionError("P004 target CSV projection is incompatible", finding_code="INPUT_INVALID", mining_tracks="P004")
    if not project_identifier or any(character in project_identifier for character in "\r\n\t\x00"):
        raise ConversionError("--project-identifier must be nonempty and contain no control delimiters")
    rows = [[project_identifier], Q_METADATA, Q_HEADER]
    for record in records:
        _validate_target_contract_fact(record)
        if not record.instruction and not record.operand and not record.line_statement and not record.pi_statement and record.note:
            rows.append(["", "", "", "", "", "", project_ladder_note(record.note)])
            continue
        step = record.target_step if record.target_step is not None else (
            record.source_step if preserve_source_steps else (
                record.sequence if record.source_step == record.sequence * 2 else record.source_step
            )
        )
        projected = [
            str(record.target_step) if target_only_steps and record.target_step is not None else
            "" if target_only_steps else str(step), record.line_statement, record.instruction, record.operand, "",
            record.pi_statement, project_ladder_note(record.note),
        ]
        target_fact = _validate_target_contract_fact(record)
        if target_fact is not None:
            projection = target_fact["csv_projection"]
            if (
                projected[projection["instruction_column"]] != target_fact["display_token"]
                or projected[projection["operand_column"]] != record.operand
                or len(projected) != projection["column_count"]
                or not target_fact["oracle_ids"]
            ):
                raise ConversionError("P004 oracle-bound target projection differs", finding_code="INPUT_INVALID", mining_tracks="P004")
        rows.append(projected)
        if record.continuation_rows:
            for continuation_row in record.continuation_rows:
                projected_row = list(continuation_row)
                projected_row[6] = project_ladder_note(projected_row[6])
                rows.append(projected_row)
        else:
            rows.extend([["", "", "", operand, "", "", ""] for operand in record.continuation_operands])
    return _encode_csv(rows, encoding="utf-16le-bom", delimiter="\t", quoting=csv.QUOTE_ALL)


FallbackKey = tuple[str, str, str, str]


def _fallback_record_key(record: Instruction) -> FallbackKey:
    operands = (record.operand, *record.continuation_operands)
    return str(record.sequence), str(record.source_step), record.instruction, " ".join(operands)


def _candidate_fields(candidate: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in candidate.split(";"):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _finding_fallback_key(row: Sequence[str]) -> FallbackKey:
    fields = _candidate_fields(row[7]) if len(row) > 7 else {}
    return row[0], row[1], row[2], fields.get("operands", row[3])


def _target_unsupported_record_keys(
    program_name: str,
    records: tuple[Instruction, ...],
    findings: list[list[str]],
) -> frozenset[FallbackKey]:
    record_keys = {_fallback_record_key(record) for record in records}
    return frozenset(
        key
        for row in findings
        if len(row) >= 8
        and row[5] == TARGET_UNSUPPORTED
        and _candidate_program(row[7]) == program_name
        and (key := _finding_fallback_key(row)) in record_keys
    )


def _target_unsupported_fallback_keys(
    program_name: str,
    records: tuple[Instruction, ...],
    findings: list[list[str]],
    program_names: tuple[str, ...],
    program: ParsedProgram | None = None,
) -> frozenset[FallbackKey]:
    """Return only fully attributable rows eligible for official dummy fallback.

    A POU is eligible only when *every* local finding is an exact,
    record-backed ``TARGET_UNSUPPORTED`` finding and every affected record has
    one of the review-only transformations below.  Opaque, mining, malformed,
    global, structural, and otherwise ambiguous findings remain blockers.
    """
    valid_names = frozenset(program_names)
    if any(
        len(row) < 8 or _candidate_program(row[7]) not in valid_names
        for row in findings
    ):
        return frozenset()

    record_keys = {_fallback_record_key(record) for record in records}
    local_findings = [
        row for row in findings
        if _candidate_program(row[7]) == program_name
    ]
    if not local_findings:
        return frozenset()

    if any(_official_dummy_fallback_kind(record) == "WORD_COMPARE_OR" for record in records):
        if (
            program is None or not program.direct_decoded or program.opaque_blocks
            or program.coverage.opaque_rows
            or program.coverage.expected_rows != len(records)
            or program.instructions != records
        ):
            return frozenset()

    records_by_key = {_fallback_record_key(record): record for record in records}
    if any(
        row[5] != TARGET_UNSUPPORTED
        or _finding_fallback_key(row) not in record_keys
        or not _official_dummy_fallback_kind(records_by_key[_finding_fallback_key(row)])
        for row in local_findings
    ):
        return frozenset()

    keys: set[FallbackKey] = set()
    for row in local_findings:
        keys.add(_finding_fallback_key(row))
    return frozenset(keys)


def _is_fallback_bit_operand(operand: str) -> bool:
    """Return whether one exact bit device can use the approved review dummy."""
    if re.fullmatch(
        r"@?(?:K[1-8])?(?:[BFMLXY]|SM)\d+(?:Z\d+)?(?:\.[0-9A-Fa-f]+)?",
        operand,
    ) is not None:
        return True
    # A bit-qualified special register is a contact operand, not a word value.
    return (
        re.fullmatch(r"SD\d+(?:Z\d+)?\.[0-9A-Fa-f]+", operand) is not None
        and (_q06udv_rejected_operand(operand) or _q06udv_program_operand_out_of_range(operand))
    )


def _is_fallback_word_operand(operand: str) -> bool:
    """Return whether an exact held special register has a dummy spelling."""
    return (re.fullmatch(r"SD\d+(?:Z\d+)?", operand) is not None
            and (_q06udv_rejected_operand(operand) or _q06udv_program_operand_out_of_range(operand)))


def _official_dummy_fallback_kind(record: Instruction) -> str | None:
    """Classify one exact source record for the approved review-only fallback."""
    operands = (record.operand, *record.continuation_operands)
    instruction = record.instruction
    # Generalize only the verified word OR<> skeleton. Other comparisons,
    # indexed operands and incomplete source shapes remain ineligible.
    if (
        instruction == "OR<>"
        and len(operands) == 2
        and re.fullmatch(r"SD\d+", operands[0]) is not None
        and _is_fallback_word_operand(operands[0])
        and re.fullmatch(r"D\d+", operands[1]) is not None
    ):
        return "WORD_COMPARE_OR"
    if (
        instruction in Q06UDV_STRUCTURAL_FALLBACK_INSTRUCTIONS
        or instruction.startswith(("CJ", "SCJ", "JMP", "CALL", "RET", "BREAK"))
        or re.fullmatch(r"(?:LD|AND|OR)\$?[<>=]+P?", instruction) is not None
    ):
        return None
    if (
        instruction in {"LD", "AND", "OR", "LDI", "ANI", "ORI"}
        and len(operands) == 1
        and _is_fallback_bit_operand(record.operand)
    ):
        return "BIT_CONTACT"
    if instruction in Q06UDV_OUTPUT_DESTINATION_INSTRUCTIONS:
        if len(operands) == 1 and _is_fallback_bit_operand(record.operand):
            return "BIT_DESTINATION"
        return None
    if (
        instruction in Q06UDV_SOURCE_HOLD_ACTION_INSTRUCTIONS
        and any(_is_fallback_word_operand(operand) for operand in operands)
    ):
        # The instruction itself is target-supported; only the unsupported
        # source word device is replaced, preserving the operation and every
        # other operand.  A target-unsupported instruction still takes the
        # action fallback below even when it happens to mention an SD device.
        return "WORD_DEVICE"
    if (
        (
            instruction in Q06UDV_DUMMY_ACTION_INSTRUCTIONS
            or instruction in Q06UDV_UNVERIFIED_LIFECYCLE_INSTRUCTIONS
            or instruction in Q06UDV_SOURCE_HOLD_ACTION_INSTRUCTIONS
        )
        and not record.line_statement
        and not record.pi_statement
    ):
        return "ACTION"
    if (
        instruction in MIL_SUPPORTED_INSTRUCTIONS
        and any(_is_fallback_word_operand(operand) for operand in operands)
    ):
        return "WORD_DEVICE"
    return None


def _replace_fallback_operands(record: Instruction) -> Instruction:
    """Replace each exact unsupported SD word with Mitsubishi's review dummy."""
    operands = (record.operand, *record.continuation_operands)
    replacements = tuple("SD1255" if _is_fallback_word_operand(value) else value for value in operands)
    continuation_rows = []
    for row, operand in zip(record.continuation_rows, replacements[1:], strict=False):
        projected_row = list(row)
        if len(projected_row) > 3:
            projected_row[3] = operand
        continuation_rows.append(tuple(projected_row))
    if len(continuation_rows) != len(record.continuation_rows):
        # A missing or excess continuation row is not a simple value rewrite.
        raise ConversionError(
            "official dummy word fallback has an ambiguous continuation-row shape",
            finding_code="MINING_REQUIRED",
            mining_tracks="Converter",
        )
    return replace(
        record,
        operand=replacements[0],
        continuation_operands=replacements[1:],
        continuation_rows=tuple(continuation_rows),
    )


def project_q_records_with_target_fallback(
    source_records: tuple[Instruction, ...],
    projected_records: tuple[Instruction, ...],
    fallback_keys: frozenset[FallbackKey],
) -> tuple[Instruction, ...]:
    """Project exact target holds to official review-only dummy devices.

    The returned records are import-shaped diagnostics only.  Their original
    source identity stays in invalid.csv, partial-review.csv, and the report;
    callers must retain the review-only and do-not-run markers.
    """
    projected: list[Instruction] = []
    for source_record, projected_record in zip(source_records, projected_records, strict=True):
        if _fallback_record_key(source_record) not in fallback_keys:
            projected.append(projected_record)
            continue
        kind = _official_dummy_fallback_kind(source_record)
        if kind == "WORD_COMPARE_OR":
            projected.append(replace(
                projected_record, instruction="OR", operand="SM1255",
                continuation_operands=(), continuation_rows=(),
            ))
            continue
        if kind == "BIT_CONTACT":
            projected.append(replace(
                projected_record,
                operand="SM1255",
                continuation_operands=(),
                continuation_rows=(),
            ))
            continue
        if kind in {"ACTION", "BIT_DESTINATION"}:
            projected.append(replace(
                projected_record,
                instruction="OUT",
                operand="SM1255",
                continuation_operands=(),
                continuation_rows=(),
            ))
            continue
        if kind == "WORD_DEVICE":
            projected.append(_replace_fallback_operands(projected_record))
            continue
        raise ConversionError(
            "target fallback key is not eligible for an official dummy projection",
            finding_code="MINING_REQUIRED",
            mining_tracks="Converter",
        )
    return tuple(projected)


def _canonical_official_dummy_fallback_record(
    source_record: Instruction,
    projected_record: Instruction,
) -> Instruction | None:
    """Return the one exact serialized shape allowed for a dummy fallback."""
    kind = _official_dummy_fallback_kind(source_record)
    if kind == "WORD_COMPARE_OR":
        return replace(
            projected_record, instruction="OR", operand="SM1255",
            continuation_operands=(), continuation_rows=(),
        )
    if kind == "BIT_CONTACT":
        return replace(
            projected_record,
            operand="SM1255",
            continuation_operands=(),
            continuation_rows=(),
        )
    if kind in {"ACTION", "BIT_DESTINATION"}:
        return replace(
            projected_record,
            instruction="OUT",
            operand="SM1255",
            continuation_operands=(),
            continuation_rows=(),
        )
    if kind == "WORD_DEVICE":
        return _replace_fallback_operands(projected_record)
    return None


def _verified_official_dummy_projection(
    program_name: str,
    source_records: tuple[Instruction, ...],
    projected_records: tuple[Instruction, ...],
    fallback_keys: frozenset[FallbackKey],
) -> tuple[tuple[Instruction, ...], frozenset[FallbackKey]]:
    """Return a fallback projection only when every dummy row passes Q target gates."""
    if not fallback_keys:
        return projected_records, fallback_keys
    fallback_records = project_q_records_with_target_fallback(
        source_records,
        projected_records,
        fallback_keys,
    )
    for source_record, projected_record, fallback_record in zip(
        source_records, projected_records, fallback_records, strict=True,
    ):
        if _fallback_record_key(source_record) not in fallback_keys:
            continue
        expected_fallback = _canonical_official_dummy_fallback_record(source_record, projected_record)
        if (
            expected_fallback is None
            or fallback_record != expected_fallback
            or fallback_record.sequence != source_record.sequence
            or fallback_record.source_step != source_record.source_step
        ):
            return projected_records, frozenset()
        if any(
            finding[5] == TARGET_UNSUPPORTED
            for finding in _validate_q_target_record(
                program_name,
                fallback_record,
                source_record=source_record,
            )
        ):
            return projected_records, frozenset()
    return fallback_records, fallback_keys


def _typed_mining_fallback_kind(record: Instruction, row: Sequence[str]) -> str | None:
    """Return one explicitly proven source-mining dummy kind, never infer it.

    P002/Converter may attach this narrow evidence only after it has retained the
    exact source row, instruction/topology role, and operand type.  A visible
    legacy rendering is not evidence by itself.  The candidate fields are
    deliberately verbose because this output is a review artifact, not a
    normal conversion result.
    """
    if len(row) < 8 or row[5] != "MINING_REQUIRED":
        return None
    fields = _candidate_fields(row[7])
    operands = " ".join((record.operand, *record.continuation_operands))
    if (
        not record.instruction
        or not record.operand
        or not row[2]
        or not row[3]
        or row[0] != str(record.sequence)
        or row[1] != str(record.source_step)
        or row[2] != record.instruction
        or fields.get("operands", row[3]) != operands
        or fields.get("source_row_verified") != "true"
        or fields.get("alignment") != "exact"
    ):
        return None
    kind = fields.get("typed_mining_fallback", "")
    if (
        record.instruction in Q06UDV_STRUCTURAL_FALLBACK_INSTRUCTIONS
        or re.fullmatch(r"(?:LD|AND|OR)\$?[<>=]+P?", record.instruction) is not None
    ):
        return None
    if kind == "BIT_CONTACT":
        return (
            kind
            if fields.get("topology_role") == "contact"
            and fields.get("operand_type") == "bit"
            and record.instruction in {"LD", "AND", "OR", "LDI", "ANI", "ORI"}
            and len((record.operand, *record.continuation_operands)) == 1
            else None
        )
    if kind == "BIT_DESTINATION":
        return (
            kind
            if fields.get("topology_role") == "destination"
            and fields.get("operand_type") == "bit"
            and record.instruction in Q06UDV_OUTPUT_DESTINATION_INSTRUCTIONS
            and len((record.operand, *record.continuation_operands)) == 1
            else None
        )
    if kind == "WORD_DEVICE":
        return (
            kind
            if fields.get("topology_role") == "word_source"
            and fields.get("operand_type") == "word"
            and fields.get("operand_index") == "0"
            and record.instruction in MIL_SUPPORTED_INSTRUCTIONS
            else None
        )
    return None


def _typed_mining_fallback_keys(
    program: ParsedProgram,
    findings: list[list[str]],
    program_names: tuple[str, ...],
) -> dict[FallbackKey, str]:
    """Return typed mining keys only for a wholly attributable, decoded POU."""
    if (
        not program.direct_decoded
        or program.opaque_blocks
        or program.coverage.opaque_rows
        or program.coverage.expected_rows != len(program.instructions)
    ):
        return {}
    valid_names = frozenset(program_names)
    if any(
        len(row) < 8 or _candidate_program(row[7]) not in valid_names
        for row in findings
    ):
        return {}
    records_by_key = {_fallback_record_key(record): record for record in program.instructions}
    local_findings = [row for row in findings if _candidate_program(row[7]) == program.name]
    if not local_findings or any(row[5] != "MINING_REQUIRED" for row in local_findings):
        return {}
    keys: dict[FallbackKey, str] = {}
    for row in local_findings:
        key = _finding_fallback_key(row)
        record = records_by_key.get(key)
        kind = _typed_mining_fallback_kind(record, row) if record is not None else None
        if kind is None or key in keys:
            return {}
        keys[key] = kind
    return keys


def project_q_records_with_typed_mining_fallback(
    source_records: tuple[Instruction, ...],
    projected_records: tuple[Instruction, ...],
    fallback_kinds: Mapping[FallbackKey, str],
) -> tuple[Instruction, ...]:
    """Create an import-shaped typed-mining review artifact, never normal Q CSV."""
    return tuple(
        projected_record if (kind := fallback_kinds.get(_fallback_record_key(source_record))) is None
        else _canonical_typed_mining_fallback_record(projected_record, kind)
        for source_record, projected_record in zip(source_records, projected_records, strict=True)
    )


def _canonical_typed_mining_fallback_record(
    projected_record: Instruction,
    kind: str,
) -> Instruction:
    """Return the one approved serialized shape for a typed-mining placeholder."""
    if kind == "BIT_CONTACT":
        return replace(projected_record, operand="SM1255", continuation_operands=(), continuation_rows=())
    if kind == "BIT_DESTINATION":
        return replace(projected_record, instruction="OUT", operand="SM1255", continuation_operands=(), continuation_rows=())
    if kind == "WORD_DEVICE":
        return replace(projected_record, operand="SD1255")
    raise ConversionError("typed mining fallback kind is invalid", finding_code="MINING_REQUIRED", mining_tracks="P002|Converter")


def _typed_mining_fallback_shape_is_valid(
    projected_record: Instruction,
    fallback_record: Instruction,
    kind: str,
) -> bool:
    """Validate a typed review row from field invariants, independent of its writer."""
    retained_fields_match = (
        fallback_record.sequence == projected_record.sequence
        and fallback_record.source_step == projected_record.source_step
        and fallback_record.line_statement == projected_record.line_statement
        and fallback_record.pi_statement == projected_record.pi_statement
        and fallback_record.note == projected_record.note
    )
    if kind == "BIT_CONTACT":
        return (
            retained_fields_match
            and fallback_record.instruction == projected_record.instruction
            and fallback_record.operand == "SM1255"
            and fallback_record.continuation_operands == ()
            and fallback_record.continuation_rows == ()
        )
    if kind == "BIT_DESTINATION":
        return (
            retained_fields_match
            and fallback_record.instruction == "OUT"
            and fallback_record.operand == "SM1255"
            and fallback_record.continuation_operands == ()
            and fallback_record.continuation_rows == ()
        )
    if kind == "WORD_DEVICE":
        return (
            retained_fields_match
            and fallback_record.instruction == projected_record.instruction
            and fallback_record.operand == "SD1255"
            and fallback_record.continuation_operands == projected_record.continuation_operands
            and fallback_record.continuation_rows == projected_record.continuation_rows
        )
    return False


def _verified_typed_mining_projection(
    program_name: str,
    source_records: tuple[Instruction, ...],
    projected_records: tuple[Instruction, ...],
    fallback_kinds: Mapping[FallbackKey, str],
) -> tuple[tuple[Instruction, ...], dict[FallbackKey, str]]:
    """Require exact projection identity and a fresh Q target validation pass."""
    if not fallback_kinds:
        return projected_records, {}
    fallback_records = project_q_records_with_typed_mining_fallback(
        source_records, projected_records, fallback_kinds,
    )
    for source_record, projected_record, fallback_record in zip(
        source_records, projected_records, fallback_records, strict=True,
    ):
        kind = fallback_kinds.get(_fallback_record_key(source_record))
        if kind is None:
            continue
        if (
            not _typed_mining_fallback_shape_is_valid(projected_record, fallback_record, kind)
            or fallback_record.sequence != source_record.sequence
            or fallback_record.source_step != source_record.source_step
            or any(
                finding[5] == TARGET_UNSUPPORTED
                for finding in _validate_q_target_record(
                    program_name, fallback_record, source_record=source_record,
                )
            )
        ):
            return projected_records, {}
    return fallback_records, dict(fallback_kinds)


def _verified_nonserialized_fallback_surface_expectations(
    program_name: str,
    source_records: tuple[Instruction, ...],
    projected_records: tuple[Instruction, ...],
    fallback_records: tuple[Instruction, ...],
    verified_fallback_keys: frozenset[FallbackKey],
) -> dict[RadixAuditSurfaceIdentity, RadixAuditFallbackSurfaceExpectation]:
    """Return exact X/Y fallback dispositions from a verified dummy row.

    Official dummy fallback is not a target projection of every source operand.
    ACTION and BIT_DESTINATION records are intentionally serialized as the one
    safe ``OUT SM1255`` diagnostic.  The audit must retain that distinction
    without allowing a finding-code or record-key allowlist to hide a malformed
    final record.  WORD_DEVICE preserves operands and never enters this set.
    """
    surfaces: dict[RadixAuditSurfaceIdentity, RadixAuditFallbackSurfaceExpectation] = {}
    for source_record, projected_record, fallback_record in zip(
        source_records, projected_records, fallback_records, strict=True,
    ):
        if _fallback_record_key(source_record) not in verified_fallback_keys:
            continue
        kind = _official_dummy_fallback_kind(source_record)
        expected = _canonical_official_dummy_fallback_record(source_record, projected_record)
        if (
            expected is None
            or fallback_record != expected
            or fallback_record.sequence != source_record.sequence
            or fallback_record.source_step != source_record.source_step
        ):
            continue
        if kind == "WORD_DEVICE":
            continue
        for operand_position, source_operand in _device_radix_surface_values(source_record):
            if _FX_IO_SOURCE_DEVICE.fullmatch(source_operand) is not None:
                identity = (
                    program_name,
                    source_record.sequence,
                    source_record.source_step,
                    operand_position,
                )
                surfaces[identity] = RadixAuditFallbackSurfaceExpectation(
                    _RADIX_AUDIT_SUBSTITUTE_SM1255
                    if operand_position == "primary"
                    else _RADIX_AUDIT_ABSENT,
                    _fallback_record_key(source_record),
                    kind,
                    source_record,
                    projected_record,
                    source_operand,
                    expected,
                )
    return surfaces


def partial_review_csv(
    records: tuple[Instruction, ...],
    opaque_blocks: tuple[OpaqueBlock, ...],
    findings: tuple[tuple[str, ...], ...],
    *,
    fallback_keys: frozenset[FallbackKey] = frozenset(),
) -> bytes:
    """Write decoded Q-shaped rows and withheld block evidence as UTF-8 CSV.

    The header intentionally differs from GX Works2's import schema.  This is
    a human review ledger, never an importable substitute for a complete POU.
    """
    def review_note(note: str) -> str:
        try:
            return project_ladder_note(note)
        except ConversionError:
            # Review CSV is UTF-8 evidence, so retain the original text when
            # it cannot be represented in a GX Works2 CP949 import row.
            return note

    opaque_locators = frozenset(block.locator for block in opaque_blocks)
    record_findings: dict[tuple[str, str], list[tuple[int, tuple[str, ...]]]] = {}
    target_record_findings: dict[FallbackKey, list[tuple[int, tuple[str, ...]]]] = {}
    for index, finding in enumerate(findings):
        if len(finding) < 8:
            continue
        candidate = finding[7]
        locator_candidate = candidate.partition(";")[2] if candidate.startswith("program=") else candidate
        if locator_candidate in opaque_locators:
            continue
        if finding[0] and finding[1] and finding[5] == TARGET_UNSUPPORTED:
            target_record_findings.setdefault(_finding_fallback_key(finding), []).append((index, finding))
        elif finding[0] and finding[1]:
            record_findings.setdefault((finding[0], finding[1]), []).append((index, finding))

    consumed_findings: set[int] = set()
    rows = [PARTIAL_REVIEW_HEADER]
    for record in records:
        matched = [
            *target_record_findings.get(_fallback_record_key(record), []),
            *record_findings.get((str(record.sequence), str(record.source_step)), []),
        ]
        matched_rows = [finding for _index, finding in matched]
        consumed_findings.update(index for index, _finding in matched)
        target_blocked = any(finding[5].startswith("TARGET_") for finding in matched_rows)
        record_type = (
            OFFICIAL_DUMMY_FALLBACK
            if _fallback_record_key(record) in fallback_keys
            else ("TARGET_BLOCKED" if target_blocked else ("VALIDATION_BLOCKED" if matched_rows else "Q_COMPATIBLE"))
        )
        error = " | ".join(dict.fromkeys(finding[4] for finding in matched_rows))
        code = " | ".join(dict.fromkeys(finding[5] for finding in matched_rows))
        tracks = " | ".join(dict.fromkeys(finding[6] for finding in matched_rows))
        locator = " | ".join(dict.fromkeys(finding[7] for finding in matched_rows))
        if _fallback_record_key(record) in fallback_keys:
            locator = (
                f"{locator};diagnostic_fallback={OFFICIAL_DUMMY_FALLBACK};"
                f"publication={DIAGNOSTIC_FALLBACK_PUBLICATION};"
                f"runtime_action={DIAGNOSTIC_FALLBACK_RUNTIME_ACTION};"
                f"compatibility={DIAGNOSTIC_FALLBACK_COMPATIBILITY}"
            )
        if not record.instruction and not record.operand and not record.line_statement and not record.pi_statement and record.note:
            rows.append([record_type, "", "", "", "", "", "", review_note(record.note), error, code, tracks, locator])
            continue
        rows.append([
            record_type, str(record.source_step), record.line_statement, record.instruction,
            record.operand, "", record.pi_statement, review_note(record.note),
            error, code, tracks, locator,
        ])
        continuation_type = f"{record_type}_CONTINUATION"
        if record.continuation_rows:
            for continuation in record.continuation_rows:
                rows.append([
                    continuation_type, *continuation[:6], review_note(continuation[6]), "", "", "", "",
                ])
        else:
            rows.extend(
                [continuation_type, "", "", "", operand, "", "", "", "", "", "", ""]
                for operand in record.continuation_operands
            )
    for block in opaque_blocks:
        rows.append([
            "SOURCE_GAP", str(block.source_step), "", "", "", "", "", "", block.error,
            block.finding_code, block.mining_tracks, block.locator,
        ])
    for index, finding in enumerate(findings):
        if index in consumed_findings:
            continue
        sequence, source_step, instruction, operand, error, code, tracks, candidate = (*finding[:8],)
        locator_candidate = candidate.partition(";")[2] if candidate.startswith("program=") else candidate
        if locator_candidate in opaque_locators:
            continue
        rows.append([
            "TARGET_BLOCKED" if code.startswith("TARGET_") else "VALIDATION_BLOCKED", source_step, "", instruction, operand, "", "", "", error,
            code, tracks, candidate,
        ])
    return _encode_csv(rows, encoding="utf-8", delimiter=",", quoting=csv.QUOTE_MINIMAL)


def partial_review_text(
    program_name: str,
    records: tuple[Instruction, ...],
    opaque_blocks: tuple[OpaqueBlock, ...],
    findings: tuple[tuple[str, ...], ...],
    *,
    fallback_records: Mapping[FallbackKey, Instruction] | None = None,
) -> bytes:
    """Summarize only unconverted source elements; keep full evidence in JSON/CSV."""
    fallback_records = fallback_records or {}

    def one_line(value: str) -> str:
        return value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")

    def operation(record: Instruction) -> str:
        if record.instruction:
            operands = (record.operand, *record.continuation_operands)
            if record.continuation_rows and not record.continuation_operands:
                operands = (record.operand, *(row[3] for row in record.continuation_rows))
            return " ".join(value for value in (record.instruction, *operands) if value)
        return "Note: " + (record.note or record.line_statement or record.pi_statement)

    items: list[list[str]] = []
    covered_opaque: set[str] = set()
    for finding in findings:
        if len(finding) < 8:
            raise ConversionError("Incomplete finding cannot be summarized")
        sequence, step, instruction, operand, reason, code, _tracks, candidate = finding[:8]
        locator = candidate.partition(";")[2] if candidate.startswith("program=") else candidate
        block = next((block for block in opaque_blocks if block.locator == locator), None)
        if block is not None:
            covered_opaque.add(block.locator)
        matches = [record for record in records if str(record.sequence) == sequence and str(record.source_step) == step]
        record = matches[0] if len(matches) == 1 else None
        fields = dict(part.split("=", 1) for part in candidate.split(";") if "=" in part)
        source = operation(record) if record is not None else " ".join(
            value for value in (instruction, fields.get("operands", operand)) if value
        )
        if block is not None:
            source = f"미해독 구간 ({block.row_count}행)"
        if not source:
            source = "프로그램 공통 문제"
        if "In Peripheral" in reason:
            reason = "In Peripheral Note 유형 보존의 Q import·재저장·재export 검증 미완료"
        elif reason.startswith("instruction was rejected by GX Works2"):
            reason = "Q06UDV 대상에서 지원되지 않는 명령"
        elif reason.startswith("operand was rejected by GX Works2"):
            reason = "Q06UDV 대상에서 지원되지 않는 디바이스 또는 피연산자"
        elif "exact semantic HOLD" in reason:
            reason = "원본 특수 디바이스에 대응하는 Q 의미가 승인되지 않아 검토용 치환만 허용"
        lines = [f"Step {step or '?'}: {one_line(source)}", f"사유: {one_line(reason)}", f"코드: {code}"]
        replacement = fallback_records.get(_fallback_record_key(record)) if record is not None else None
        if replacement is not None:
            lines.append(f"검토용 CSV 치환: {one_line(operation(replacement))} (원래 기능 미구현)")
        items.append(lines)
    for block in opaque_blocks:
        if block.locator in covered_opaque:
            continue
        items.append([
            f"Step {block.source_step}: 미해독 구간 ({block.row_count}행)",
            f"사유: {one_line(block.error)}", f"코드: {block.finding_code}",
        ])
    lines = [f"미변환 요소: {one_line(program_name)}", f"총 {len(items)}건", ""]
    if fallback_records:
        lines.extend(["더미 치환이 포함된 검토용 CSV: REVIEW_ONLY / DO_NOT_DOWNLOAD_OR_RUN", ""])
    for index, item in enumerate(items, 1):
        lines.extend([f"{index}. {item[0]}", *(f"   {line}" for line in item[1:]), ""])
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def device_comment_csv(project_identifier: str, comments: tuple[DeviceComment, ...]) -> bytes:
    if not project_identifier or any(character in project_identifier for character in "\r\n\t\x00"):
        raise ConversionError("--project-identifier must be nonempty and contain no control delimiters")
    compatible_comments: list[DeviceComment] = []
    for comment in comments:
        try:
            projected_device = _project_q_comment_device(comment.device)
        except ConversionError:
            # Keep invalid source X/Y in the validation set so publication
            # remains fail-closed with its typed diagnostic.
            compatible_comments.append(comment)
            continue
        if not _q06udv_rejected_operand(projected_device):
            compatible_comments.append(comment)
    compatible = tuple(compatible_comments)
    invalid = validate_comment_encoding(compatible)
    if invalid:
        raise ConversionError(
            f"device comment validation rejected {len(invalid)} row(s): {invalid[0][4]}",
            finding_code=invalid[0][5],
            candidate=invalid[0][7],
        )
    projections = _q06udv_compatible_comment_projections(project_comments(compatible))
    _reject_comment_target_alias_collisions(projections)
    rows = [[project_identifier], COMMENT_HEADER]
    rows.extend([[comment.device, comment.projected_text] for comment in projections])
    return _encode_csv(rows, encoding="utf-16le-bom", delimiter="\t", quoting=csv.QUOTE_ALL)


def _comment_review_rows(comments: tuple[DeviceComment, ...]) -> tuple[tuple[str, ...], ...]:
    projections = tuple(_comment_review_projection(comment) for comment in comments)
    return tuple(
        (
            projection.source_device,
            projection.device,
            projection.source_text,
            projection.projected_text,
            "" if projection.source_bytes is None else str(projection.source_bytes),
            "" if projection.projected_bytes is None else str(projection.projected_bytes),
            projection.status,
        )
        for projection in projections
    )


def _encode_comment_review_rows(rows: tuple[tuple[str, ...], ...]) -> bytes:
    return _encode_csv([COMMENT_REVIEW_HEADER, *rows], encoding="utf-8", delimiter=",", quoting=csv.QUOTE_MINIMAL)


def comment_review_csv(comments: tuple[DeviceComment, ...]) -> bytes:
    return _encode_comment_review_rows(_comment_review_rows(comments))


def _comment_review_payloads(
    destination: Path, comments: tuple[DeviceComment, ...], *, import_csv_state: str,
) -> tuple[tuple[Path, bytes], ...]:
    rows = _comment_review_rows(comments)
    return (
        (destination, _encode_comment_review_rows(rows)),
        (companion_text_path(destination), comment_review_text(
            rows, headers=COMMENT_REVIEW_HEADER, excluded_status=COMMENT_TARGET_EXCLUDED_STATUS,
            import_csv_state=import_csv_state,
        )),
    )


def conversion_report_json(
    input_path: Path,
    source_hash: str,
    project: ParsedProject | ParsedMultiProject,
    q_outputs: tuple[Path, ...],
    *,
    validation_findings: tuple[tuple[str, ...], ...] = (),
    published_q_outputs: tuple[Path, ...] | None = None,
    review_outputs: tuple[Path, ...] | None = None,
    published_review_outputs: tuple[Path, ...] | None = None,
    review_output_hashes: Mapping[str, str] | None = None,
    mining_review_outputs: tuple[Path, ...] | None = None,
    published_mining_review_outputs: tuple[Path, ...] | None = None,
    typed_mining_review_outputs: tuple[Path, ...] | None = None,
    published_typed_mining_review_outputs: tuple[Path, ...] | None = None,
    source_recovered_review_outputs: tuple[Path, ...] | None = None,
    published_source_recovered_review_outputs: tuple[Path, ...] | None = None,
    published_source_recovered_import_review_outputs: tuple[Path, ...] | None = None,
    structural_review_outputs: tuple[Path, ...] | None = None,
    published_structural_review_outputs: tuple[Path, ...] | None = None,
    published_structural_import_review_outputs: tuple[Path, ...] | None = None,
    published_diagnostic_fallback_import_review_outputs: tuple[Path, ...] | None = None,
    review_import_metadata: Mapping[str, Mapping[str, object]] | None = None,
    comment_review_published: bool = False,
    diagnostic_fallback_keys: dict[str, frozenset[FallbackKey]] | None = None,
    diagnostic_fallback_attempted_keys: dict[str, frozenset[FallbackKey]] | None = None,
    typed_mining_fallback_keys: dict[str, Mapping[FallbackKey, str]] | None = None,
    radix_audit: dict[str, object] | None = None,
    target_layout_reports: Mapping[str, Mapping[str, object]] | None = None,
) -> bytes:
    compatible_comments = tuple(
        comment for comment in project.comments
        if not _q06udv_rejected_operand(_compatible_comment_device(comment.device))
    )
    comment_encoding_blocked = any(
        len(row) > 5 and row[5] == "COMMENT_ENCODING_REQUIRED"
        for row in validation_findings
    ) or bool(validate_comment_encoding(compatible_comments))
    projections = () if comment_encoding_blocked else tuple(
        _comment_review_projection(comment) for comment in project.comments
    )
    overflows = [projection for projection in projections if projection.truncated]
    overrides = [projection for projection in projections if projection.status.split("|")[-1] == "APPROVED_OVERRIDE"]
    normalized = [projection for projection in projections if projection.status.split("|")[-1] == "NORMALIZED"]
    exact = [projection for projection in projections if projection.status.split("|")[-1] == "EXACT"]
    target_incompatible = [
        row for row in validate_comments(project.comments)
        if len(row) > 5 and row[5] == "TARGET_UNSUPPORTED"
    ]
    emitted_comment_projections = _q06udv_compatible_comment_projections(projections)
    if radix_audit is None:
        source_records_by_program = (
            {program.name: program.instructions for program in project.programs}
            if isinstance(project, ParsedMultiProject)
            else {project.program_name: project.instructions}
        )
        source_identity = project.source_sha256 if isinstance(project, ParsedMultiProject) else source_hash
        radix_audit = device_radix_audit(
            source_records_by_program,
            {
                name: tuple(project_q_record(source_identity, name, record) for record in records)
                for name, records in source_records_by_program.items()
            },
        )
    preserved_hardware = sorted(
        {
            operand
            for record in project.instructions
            for operand in (record.operand, *record.continuation_operands)
            if re.match(r"^U\d+\\G", operand)
        }
    )
    published = (
        frozenset(published_q_outputs)
        if published_q_outputs is not None
        else (None if not validation_findings else frozenset())
    )
    published_reviews = frozenset(published_review_outputs or ())
    published_mining_reviews = frozenset(published_mining_review_outputs or ())
    published_typed_mining_reviews = frozenset(published_typed_mining_review_outputs or ())
    published_source_recovered_reviews = frozenset(published_source_recovered_review_outputs or ())
    published_source_recovered_import_reviews = frozenset(published_source_recovered_import_review_outputs or ())
    published_structural_reviews = frozenset(published_structural_review_outputs or ())
    published_structural_import_reviews = frozenset(published_structural_import_review_outputs or ())
    published_diagnostic_fallback_import_reviews = frozenset(
        published_diagnostic_fallback_import_review_outputs or ()
    )
    review_import_metadata = review_import_metadata or {}
    has_mining_required = any(
        len(row) > 5 and row[5] == "MINING_REQUIRED" for row in validation_findings
    )
    upstream_typed_evidence = (
        "EXACT_UPSTREAM_TYPED_EVIDENCE_BOUND"
        if published_typed_mining_reviews
        else ("UPSTREAM_TYPED_EVIDENCE_REQUIRED" if has_mining_required else "NOT_REQUIRED")
    )

    def q_output_name(destination: Path) -> str:
        return destination.name if published is None or destination in published else ""

    def stepinfo_expected_rows(structure: ProgramStructure | None, fallback: int) -> int:
        """Keep report coverage anchored to source StepInfo, not emitted CSV rows."""
        return len(structure.step_info.rows) if structure is not None else fallback

    diagnostic_fallback_keys = diagnostic_fallback_keys or {}
    diagnostic_fallback_attempted_keys = diagnostic_fallback_attempted_keys or {}
    typed_mining_fallback_keys = typed_mining_fallback_keys or {}
    target_layout_reports = target_layout_reports or {}

    def finding_inventory(name: str) -> dict[str, int]:
        """Return the complete, program-bound finding-code inventory."""
        counts: Counter[str] = Counter()
        for row in validation_findings:
            candidate_program = _candidate_program(row[7]) if len(row) >= 8 else ""
            belongs_to_single_program = (
                not isinstance(project, ParsedMultiProject)
                and name == project.program_name
                and not candidate_program
            )
            if candidate_program != name and not belongs_to_single_program:
                continue
            finding_code = row[5]
            if finding_code:
                counts[finding_code] += 1
        return dict(sorted(counts.items()))

    def unavailable_fallback_count(name: str) -> int:
        return len({
            _finding_fallback_key(row)
            for row in validation_findings
            if (
                len(row) >= 8
                and row[5] == TARGET_UNSUPPORTED
                and _candidate_program(row[7]) == name
                and _finding_fallback_key(row) not in diagnostic_fallback_keys.get(name, frozenset())
            )
        })

    def program_status(
        name: str,
        destination: Path,
        review_destination: Path,
        mining_review_destination: Path,
        typed_mining_review_destination: Path,
        source_recovered_review_destination: Path,
        structural_review_destination: Path,
    ) -> str:
        if source_recovered_review_destination in published_source_recovered_reviews:
            return SOURCE_RECOVERED_REVIEW_ONLY
        if structural_review_destination in published_structural_reviews:
            return STRUCTURAL_RESULT_DUMMY_FALLBACK
        if typed_mining_review_destination in published_typed_mining_reviews:
            return TYPED_MINING_REVIEW_STATUS
        if mining_review_destination in published_mining_reviews:
            return MINING_REVIEW_STATUS
        if name in diagnostic_fallback_keys:
            return "REVIEW_ONLY_FALLBACK"
        if review_destination in published_reviews:
            return "PARTIAL_REVIEW"
        return "CONVERTED" if published is None or destination in published else "REVIEW_REQUIRED"

    def target_unsupported_details() -> list[dict[str, object]]:
        records_by_program = {
            name: {_fallback_record_key(record): record for record in records}
            for name, records in (
                {program.name: program.instructions for program in project.programs}.items()
                if isinstance(project, ParsedMultiProject)
                else {project.program_name: project.instructions}.items()
            )
        }
        details: list[dict[str, object]] = []
        for row in validation_findings:
            if len(row) < 8 or row[5] != TARGET_UNSUPPORTED:
                continue
            fields = _candidate_fields(row[7])
            program_name = fields.get("program", "")
            key = _finding_fallback_key(row)
            uses_official_dummy = key in diagnostic_fallback_keys.get(program_name, frozenset())
            source_record = records_by_program.get(program_name, {}).get(key)
            record_reason = "ELIGIBLE" if source_record and _official_dummy_fallback_kind(source_record) else "SOURCE_SEMANTICS_UNKNOWN"
            if source_record and record_reason != "ELIGIBLE" and (
                source_record.instruction in Q06UDV_STRUCTURAL_FALLBACK_INSTRUCTIONS
                or re.fullmatch(r"(?:LD|AND|OR)\$?[<>=]+P?", source_record.instruction) is not None
            ):
                record_reason = "STRUCTURAL_COMPARE"
            local_findings = [
                candidate for candidate in validation_findings
                if len(candidate) >= 8 and _candidate_program(candidate[7]) == program_name
            ]
            pou_reason = ""
            if not uses_official_dummy:
                if any(candidate[5] == "MINING_REQUIRED" for candidate in local_findings):
                    pou_reason = "POU_HAS_MINING_REQUIRED"
                elif any(
                    candidate[5] == TARGET_UNSUPPORTED
                    and _finding_fallback_key(candidate) != key
                    and _official_dummy_fallback_kind(
                        records_by_program.get(program_name, {}).get(_finding_fallback_key(candidate), Instruction(-1, -1, "", ""))
                    ) is None
                    for candidate in local_findings
                ):
                    pou_reason = "POU_HAS_OTHER_INELIGIBLE_TARGET"
                elif key in diagnostic_fallback_attempted_keys.get(program_name, frozenset()):
                    pou_reason = "POST_PROJECTION_RECHECK_FAILED"
            details.append({
                "program": program_name,
                "sequence": row[0],
                "source_step": row[1],
                "instruction": row[2],
                "operands": fields.get("operands", row[3]),
                "finding_code": row[5],
                "reason": row[4],
                "source_record_id": fields.get("source_record_id", ""),
                "note_subtype": fields.get("note_subtype", ""),
                "fallback_eligibility": {
                    "record_reason": record_reason,
                    "pou_atomicity_reason": pou_reason,
                },
                "diagnostic_fallback": (
                    {
                        "finding_code": OFFICIAL_DUMMY_FALLBACK,
                        "publication": DIAGNOSTIC_FALLBACK_PUBLICATION,
                        "runtime_action": DIAGNOSTIC_FALLBACK_RUNTIME_ACTION,
                        "compatibility": DIAGNOSTIC_FALLBACK_COMPATIBILITY,
                    }
                    if uses_official_dummy else None
                ),
            })
        return details

    if isinstance(project, ParsedMultiProject):
        programs = [
            {
                "name": program.name,
                "target_name": destination.stem,
                "pou_id": program.pou_id,
                "instruction_rows": len(program.instructions),
                "expected_rows": stepinfo_expected_rows(
                    program.structure,
                    len(program.instructions) + sum(block.row_count for block in program.opaque_blocks),
                ),
                "decoded_rows": len(program.instructions),
                "opaque_rows": sum(block.row_count for block in program.opaque_blocks),
                "q_output": q_output_name(destination),
                "review_output": review_outputs[index].name if review_outputs and review_outputs[index] in published_reviews else "",
                "mining_review_output": (
                    mining_review_outputs[index].name
                    if mining_review_outputs and mining_review_outputs[index] in published_mining_reviews
                    else ""
                ),
                "typed_mining_review_output": (
                    typed_mining_review_outputs[index].name
                    if typed_mining_review_outputs and typed_mining_review_outputs[index] in published_typed_mining_reviews
                    else ""
                ),
                "source_recovered_review_output": (
                    source_recovered_review_outputs[index].name
                    if source_recovered_review_outputs and source_recovered_review_outputs[index] in published_source_recovered_reviews
                    else ""
                ),
                "import_review_output": (
                    destination.name
                    if program.name in review_import_metadata else ""
                ),
                "import_review": review_import_metadata.get(program.name, {}),
                "structural_review_output": (
                    structural_review_outputs[index].name
                    if structural_review_outputs and structural_review_outputs[index] in published_structural_reviews
                    else ""
                ),
                "status": program_status(
                    program.name,
                    destination,
                    review_outputs[index] if review_outputs else destination,
                    mining_review_outputs[index] if mining_review_outputs else destination,
                    typed_mining_review_outputs[index] if typed_mining_review_outputs else destination,
                    source_recovered_review_outputs[index] if source_recovered_review_outputs else destination,
                    structural_review_outputs[index] if structural_review_outputs else destination,
                ),
                "diagnostic_fallback_unavailable_count": unavailable_fallback_count(program.name),
                "official_dummy_fallback_count": len(
                    diagnostic_fallback_keys.get(program.name, frozenset())
                ),
                "typed_mining_dummy_fallback_count": len(typed_mining_fallback_keys.get(program.name, {})),
                "source_recovery": [
                    {"method": method, "blocks": blocks, "rows": rows}
                    for method, blocks, rows in program.recovery_methods
                ],
                "source_coverage": program.coverage.as_report(),
            }
            for index, (destination, program) in enumerate(zip(q_outputs, project.programs, strict=True))
        ]
    else:
        programs = [
            {
                "name": project.program_name,
                "target_name": q_outputs[0].stem,
                "instruction_rows": len(project.instructions),
                "expected_rows": stepinfo_expected_rows(
                    project.structure,
                    len(project.instructions) + sum(block.row_count for block in project.opaque_blocks),
                ),
                "decoded_rows": len(project.instructions),
                "opaque_rows": sum(block.row_count for block in project.opaque_blocks),
                "q_output": q_output_name(q_outputs[0]),
                "review_output": review_outputs[0].name if review_outputs and review_outputs[0] in published_reviews else "",
                "mining_review_output": (
                    mining_review_outputs[0].name
                    if mining_review_outputs and mining_review_outputs[0] in published_mining_reviews
                    else ""
                ),
                "typed_mining_review_output": (
                    typed_mining_review_outputs[0].name
                    if typed_mining_review_outputs and typed_mining_review_outputs[0] in published_typed_mining_reviews
                    else ""
                ),
                "source_recovered_review_output": (
                    source_recovered_review_outputs[0].name
                    if source_recovered_review_outputs and source_recovered_review_outputs[0] in published_source_recovered_reviews
                    else ""
                ),
                "import_review_output": (
                    q_outputs[0].name
                    if project.program_name in review_import_metadata else ""
                ),
                "import_review": review_import_metadata.get(project.program_name, {}),
                "structural_review_output": (
                    structural_review_outputs[0].name
                    if structural_review_outputs and structural_review_outputs[0] in published_structural_reviews
                    else ""
                ),
                "status": program_status(
                    project.program_name,
                    q_outputs[0],
                    review_outputs[0] if review_outputs else q_outputs[0],
                    mining_review_outputs[0] if mining_review_outputs else q_outputs[0],
                    typed_mining_review_outputs[0] if typed_mining_review_outputs else q_outputs[0],
                    source_recovered_review_outputs[0] if source_recovered_review_outputs else q_outputs[0],
                    structural_review_outputs[0] if structural_review_outputs else q_outputs[0],
                ),
                "diagnostic_fallback_unavailable_count": unavailable_fallback_count(project.program_name),
                "official_dummy_fallback_count": len(
                    diagnostic_fallback_keys.get(project.program_name, frozenset())
                ),
                "typed_mining_dummy_fallback_count": len(typed_mining_fallback_keys.get(project.program_name, {})),
                "source_recovery": [
                    {"method": method, "blocks": blocks, "rows": rows}
                    for method, blocks, rows in project.recovery_methods
                ],
                "source_coverage": project.coverage.as_report(),
            }
        ]
    source_program_records = (
        {program.name: program.instructions for program in project.programs}
        if isinstance(project, ParsedMultiProject)
        else {project.program_name: project.instructions}
    )
    for item in programs:
        # This approved migration deliberately changes numeric rounding.
        # Keep it visible without misreporting it as an unconverted element.
        item["instruction_mapping_adjustments"] = [
            {
                "source_step": record.source_step,
                "source_instruction": "FLT2DINT",
                "target_instruction": "DINT",
                "operands": [record.operand, *record.continuation_operands],
                "status": (
                    "APPLIED_TO_Q_CSV"
                    if (item["q_output"] or item["import_review_output"])
                    and _fallback_record_key(record) not in diagnostic_fallback_keys.get(str(item["name"]), frozenset())
                    and _validate_target_contract_fact(replace(record, instruction="DINT")) is not None
                    else "NOT_APPLIED"
                ),
                "policy": "USER_APPROVED_TRUNCATION_TO_ROUNDING",
                "message": "FLT2DINT를 Q DINT로 치환: 소수부 절삭에서 반올림으로 변경",
                "source_numeric_behavior": "TRUNCATE_TOWARD_ZERO",
                "target_numeric_behavior": "ROUND_FIRST_DECIMAL_DIGIT",
                "arithmetic_status_flags_equivalent": False,
            }
            for record in source_program_records[str(item["name"])]
            if record.instruction == "FLT2DINT"
        ]
        item["review_output_format"] = "TEXT_UNCONVERTED_ELEMENTS_V1" if item["review_output"] else ""
        item["review_output_sha256"] = (review_output_hashes or {}).get(str(item["review_output"]), "")
        # Layout diagnostics are target-only post-audit evidence.  They are
        # intentionally separate from source finding_inventory and therefore
        # cannot reclassify a source row or a review fallback.
        item["target_layout"] = dict(target_layout_reports.get(
            str(item["name"]),
            {
                "status": "NOT_APPLICABLE",
                "rule": LAYOUT_RULE,
                "source_ir_changed": False,
                "source_audit_relation": "PRE_LAYOUT_ONE_TO_ONE_AUDIT_ONLY",
            },
        ))
        inventory = finding_inventory(str(item["name"]))
        item["finding_inventory"] = inventory
        item["device_range_invalid_count"] = inventory.get("DEVICE_RANGE_INVALID", 0)

        has_review_ledger = any(
            bool(item.get(field))
            for field in (
                "review_output",
                "mining_review_output",
                "typed_mining_review_output",
                "source_recovered_review_output",
                "structural_review_output",
            )
        )
        if item["q_output"] and item["status"] == "CONVERTED":
            item["publication_class"] = "NORMAL_Q_CSV"
        elif item["import_review_output"]:
            item["publication_class"] = "IMPORT_REVIEW_CSV"
        elif has_review_ledger:
            item["publication_class"] = "PARTIAL_LEDGER_ONLY"
        else:
            item["publication_class"] = "WITHHELD"

        item["q_semantic_class"] = {
            "CONVERTED": "Q_TARGET_VALIDATION_PASSED",
            "REVIEW_ONLY_FALLBACK": "TARGET_UNSUPPORTED_HELD",
            SOURCE_RECOVERED_REVIEW_ONLY: "SOURCE_RECOVERY_REVIEW_HELD",
            STRUCTURAL_RESULT_DUMMY_FALLBACK: "STRUCTURAL_DUMMY_NOT_EQUIVALENT",
            MINING_REVIEW_STATUS: "MINING_REVIEW_HELD",
            TYPED_MINING_REVIEW_STATUS: "MINING_REVIEW_HELD",
            "PARTIAL_REVIEW": "PARTIAL_REVIEW_HELD",
        }.get(str(item["status"]), "UNATTRIBUTED_BLOCK")
    decoded_rows = len(project.instructions)
    opaque_rows = (
        sum(block.row_count for program in project.programs for block in program.opaque_blocks)
        if isinstance(project, ParsedMultiProject)
        else sum(block.row_count for block in project.opaque_blocks)
    )
    expected_rows = sum(int(item["expected_rows"]) for item in programs)
    feature_signatures: Counter[str] = Counter()
    for item in programs:
        for signature in item["source_coverage"]["feature_signatures"]:
            feature_signatures[str(signature["signature"])] += int(signature["rows"])
    target_details = target_unsupported_details()
    official_record_keys = {
        (program_name, *key)
        for program_name, keys in diagnostic_fallback_keys.items()
        for key in keys
    }
    target_record_keys = {
        (_candidate_program(row[7]), *_finding_fallback_key(row))
        for row in validation_findings
        if len(row) >= 8 and row[5] == TARGET_UNSUPPORTED
    }
    official_detail_count = len(official_record_keys)
    unavailable_detail_count = len(target_record_keys - official_record_keys)
    if official_detail_count and unavailable_detail_count:
        fallback_mode = "MIXED_REVIEW_ONLY"
        fallback_finding_code = "OFFICIAL_DUMMY_FALLBACK|DIAGNOSTIC_FALLBACK_UNAVAILABLE"
    elif official_detail_count:
        fallback_mode = "OFFICIAL_DUMMY_REVIEW_ONLY"
        fallback_finding_code = OFFICIAL_DUMMY_FALLBACK
    elif unavailable_detail_count:
        fallback_mode = "FAIL_CLOSED_REVIEW_ONLY"
        fallback_finding_code = DIAGNOSTIC_FALLBACK_UNAVAILABLE
    else:
        fallback_mode = "NOT_REQUIRED"
        fallback_finding_code = ""
    range_review_allowed_findings = {"DEVICE_RANGE_INVALID", "TARGET_UNSUPPORTED", "DEVICE_RADIX_AUDIT_FAILED"}
    range_bearing_programs = [
        item for item in programs if int(item["device_range_invalid_count"]) > 0
    ]
    range_review_programs = [
        item for item in range_bearing_programs
        if item["status"] in {"PARTIAL_REVIEW", "REVIEW_ONLY_FALLBACK"}
        and item["q_output"] == ""
        and item["opaque_rows"] == 0
        and bool(item.get("review_output"))
        and item["finding_inventory"].get("DEVICE_RANGE_INVALID") == item["device_range_invalid_count"]
        and set(item["finding_inventory"]) <= range_review_allowed_findings
    ]
    range_review_finding_count = sum(
        int(item["device_range_invalid_count"]) for item in range_review_programs
    )
    range_review_exact = (
        bool(range_review_programs)
        and len(range_review_programs) == len(range_bearing_programs)
        and radix_audit.get("status") == "FAIL"
        and radix_audit.get("mismatch_count") == range_review_finding_count
        and radix_audit.get("failed_or_unresolved_source_xy_count") == range_review_finding_count
        and radix_audit.get("mismatch_reason_counts") == {
            "source_projection_invalid": range_review_finding_count
        }
    )
    normal_q_pou_count = sum(
        1 for item in programs if item["publication_class"] == "NORMAL_Q_CSV"
    )
    import_review_pou_count = sum(
        1 for item in programs if item["publication_class"] == "IMPORT_REVIEW_CSV"
    )
    ledger_only_pou_count = sum(
        1 for item in programs if item["publication_class"] == "PARTIAL_LEDGER_ONLY"
    )
    unhandled_pou_count = sum(
        1 for item in programs if item["publication_class"] == "WITHHELD"
    )
    artifact_pou_count = len(programs) - unhandled_pou_count
    all_pous_have_artifacts = bool(programs) and unhandled_pou_count == 0
    publication_status = (
        "COMPLETE_NORMAL"
        if bool(programs) and normal_q_pou_count == len(programs)
        else ("COMPLETE_WITH_REVIEW" if all_pous_have_artifacts else "INCOMPLETE")
    )

    report = {
        "schema_version": CONVERSION_REPORT_SCHEMA_VERSION,
        "fixed_xy_bounds": {"source": {"profile": Profile.FX5U.value,
                                         "x_max": fixed_bounds(Profile.FX5U).x,
                                         "y_max": fixed_bounds(Profile.FX5U).y},
                            "target": {"profile": Profile.Q.value,
                                         "x_max": fixed_bounds(Profile.Q).x,
                                         "y_max": fixed_bounds(Profile.Q).y}},
        "non_xy_numeric_capacity": {
            "status": "NOT_CHECKED",
            "scope": "ALL_NON_XY_DEVICES",
            "removed_fixed_ceiling_families": ["SM", "SD", "U\\G", "KnSM"],
            "target_capacity_acceptance": "NOT_GRANTED",
        },
        "source": {
            "path": str(input_path),
            "sha256": source_hash,
            "project": project.source_project,
        },
        "source_diagnostics": [
            {
                "category": diagnostic.category,
                "finding_code": diagnostic.finding_code,
                "message": diagnostic.message,
                "candidate": diagnostic.candidate,
                "scope": diagnostic.scope,
                "blocks_program_output": diagnostic.blocks_program_output,
            }
            for diagnostic in project.source_diagnostics
        ],
        "target": {"cpu": "QCPU (Q mode) Q06UDV"},
        "programs": programs,
        "instruction_validation": {
            "method": "TYPED_SOURCE_IR_DIRECT",
            "instruction_rows": len(project.instructions),
            "expected_rows": expected_rows,
            "decoded_rows": decoded_rows,
            "opaque_rows": opaque_rows,
            "instruction_types": dict(
                sorted(Counter(record.instruction or "LADDER_NOTE" for record in project.instructions).items())
            ),
            "unverified_rows": sum(
                1 for row in validation_findings if len(row) <= 2 or row[2] != "COMMENT"
            ),
            "finding_count": len(validation_findings),
        },
        "device_radix_audit": radix_audit,
        "source_coverage": {
            "direct_parser": {
                "blocks": sum(item["source_coverage"]["direct_parser"]["blocks"] for item in programs),
                "rows": sum(item["source_coverage"]["direct_parser"]["rows"] for item in programs),
            },
            "official_replay": {"blocks": 0, "rows": 0},
            "oracle_recovery": {"blocks": 0, "rows": 0},
            "opaque": {"blocks": sum(item["source_coverage"]["opaque"]["blocks"] for item in programs), "rows": opaque_rows},
            "expected_rows": expected_rows,
            "feature_signatures": [
                {"signature": signature, "rows": rows}
                for signature, rows in sorted(feature_signatures.items())
            ],
        },
        "comments": {
            "total": len(project.comments),
            "import_rows": len(emitted_comment_projections),
            "excluded_target_incompatible_devices": len(target_incompatible),
            "cp949_limit_bytes": MAX_Q_COMMENT_BYTES,
            "truncated": len(overflows),
            "exact": len(exact),
            "approved_override": len(overrides),
            "normalized": len(normalized),
            "comment_review_rows": len(projections) if comment_review_published else 0,
            "full_originals_preserved_in_comment_review_csv": comment_review_published,
            "target_incompatible_devices": len(target_incompatible),
            "target_exclusion_finding_code": TARGET_UNSUPPORTED if target_incompatible else "",
            "target_exclusion_reason": (
                TARGET_EXCLUSION_EXACT_REASON
                if target_incompatible else ""
            ),
            "target_excluded_sample": [row[3] for row in target_incompatible[:20]],
            "projection_status": (
                "REVIEW_REQUIRED"
                if comment_encoding_blocked
                else ("FILTERED_TARGET_INCOMPATIBLE" if target_incompatible else "VALIDATED")
            ),
        },
        "comment_review": {
            "schema_version": 2,
            "header": COMMENT_REVIEW_HEADER,
            "encoding": "UTF-8",
            "delimiter": ",",
            "published": comment_review_published,
        },
        "hardware_specific_operands": {
            "finding_code": "HARDWARE_COMPATIBILITY_UNVERIFIED" if preserved_hardware else "",
            "literal_policy": "PRESERVE_SOURCE_SPELLING",
            "target_compatibility": "UNVERIFIED" if preserved_hardware else "NOT_DETECTED",
            "count": len(preserved_hardware),
            "sample": preserved_hardware[:20],
        },
        "diagnostic_fallback": {
            "mode": fallback_mode,
            "finding_code": fallback_finding_code,
            "publication": DIAGNOSTIC_FALLBACK_PUBLICATION if target_details else "",
            "runtime_action": DIAGNOSTIC_FALLBACK_RUNTIME_ACTION if target_details else "",
            "compatibility": DIAGNOSTIC_FALLBACK_COMPATIBILITY if official_detail_count else "",
            "normal_q_acceptance": "EXCLUDED" if official_detail_count else "",
            "published_import_outputs": sorted(
                path.name for path in published_diagnostic_fallback_import_reviews
            ),
            "bit_device": "SM1255",
            "action_instruction": "OUT SM1255",
            "word_device": "SD1255",
            "official_dummy_fallback_count": official_detail_count,
            "diagnostic_fallback_unavailable_count": unavailable_detail_count,
            "findings": target_details,
        },
        "device_range_review": {
            "mode": (
                "DEVICE_RANGE_INVALID_FAIL_CLOSED_REVIEW_ONLY"
                if range_review_exact else "NOT_REQUIRED"
            ),
            "finding_code": "DEVICE_RANGE_INVALID" if range_review_exact else "",
            "publication": "REVIEW_ONLY" if range_review_exact else "",
            "runtime_action": "DO_NOT_DOWNLOAD_OR_RUN" if range_review_exact else "",
            "program_count": len(range_review_programs) if range_review_exact else 0,
            "finding_count": range_review_finding_count if range_review_exact else 0,
        },
        "mining_review": {
            "status": MINING_REVIEW_STATUS if published_mining_reviews else "NOT_PUBLISHED",
            "published_outputs": sorted(path.name for path in published_mining_reviews),
            "normal_q_acceptance": "EXCLUDED",
            "safety_gate": MINING_REVIEW_SAFETY_GATE,
            "target_acceptance": MINING_REVIEW_TARGET_ACCEPTANCE,
            "runtime_action": MINING_REVIEW_RUNTIME_ACTION,
        },
        "typed_mining_review": {
            "status": TYPED_MINING_REVIEW_STATUS if published_typed_mining_reviews else "NOT_PUBLISHED",
            "upstream_typed_evidence": upstream_typed_evidence,
            "published_outputs": sorted(path.name for path in published_typed_mining_reviews),
            "finding_code": TYPED_MINING_FALLBACK if published_typed_mining_reviews else "",
            "publication": TYPED_MINING_REVIEW_PUBLICATION if published_typed_mining_reviews else "",
            "normal_q_acceptance": "EXCLUDED",
            "runtime_action": TYPED_MINING_REVIEW_RUNTIME_ACTION,
        },
        "source_recovered_review": {
            "status": SOURCE_RECOVERED_REVIEW_ONLY if published_source_recovered_reviews else "NOT_PUBLISHED",
            "published_outputs": sorted(path.name for path in published_source_recovered_reviews),
            "finding_code": SOURCE_RECOVERED_REVIEW_ONLY if published_source_recovered_reviews else "",
            "publication": "REVIEW_ONLY" if published_source_recovered_reviews else "",
            "normal_q_acceptance": "EXCLUDED",
            "runtime_action": "DO_NOT_DOWNLOAD_OR_RUN" if published_source_recovered_reviews else "",
            "published_import_outputs": sorted(path.name for path in published_source_recovered_import_reviews),
        },
        "structural_result_review": {
            "status": STRUCTURAL_RESULT_DUMMY_FALLBACK if published_structural_reviews else "NOT_PUBLISHED",
            "published_outputs": sorted(path.name for path in published_structural_reviews),
            "finding_code": STRUCTURAL_RESULT_DUMMY_FALLBACK if published_structural_reviews else "",
            "publication": "REVIEW_ONLY" if published_structural_reviews else "",
            "normal_q_acceptance": "EXCLUDED",
            "runtime_action": "DO_NOT_DOWNLOAD_OR_RUN" if published_structural_reviews else "",
            "published_import_outputs": sorted(path.name for path in published_structural_import_reviews),
        },
        "publication_summary": {
            "published_q_csv_count": sum(1 for item in programs if item["q_output"]),
            "normal_q_csv_count": sum(
                1 for item in programs
                if item["q_output"] and item["status"] != "REVIEW_ONLY_FALLBACK"
            ),
            "review_only_fallback_q_csv_count": sum(
                1 for item in programs
                if item["q_output"] and item["status"] == "REVIEW_ONLY_FALLBACK"
            ),
            "review_only_fallback_import_csv_count": sum(
                1 for item in programs
                if item["import_review_output"] and item["status"] == "REVIEW_ONLY_FALLBACK"
            ),
            "total_pou_count": len(programs),
            "normal_q_pou_count": normal_q_pou_count,
            "import_review_pou_count": import_review_pou_count,
            "ledger_only_pou_count": ledger_only_pou_count,
            "artifact_pou_count": artifact_pou_count,
            "unhandled_pou_count": unhandled_pou_count,
            "all_pous_have_artifacts": all_pous_have_artifacts,
            "withheld_pou_count": sum(1 for item in programs if not item["q_output"]),
            "withheld_pous": [item["name"] for item in programs if not item["q_output"]],
            "unverified_source_rows": _unverified_source_row_count(programs, validation_findings),
        },
        "status": {
            "publication": publication_status,
            "conversion": (
                "PARTIAL"
                if validation_findings or target_incompatible or project.source_diagnostics
                else "CONVERTED"
            ),
            "rebuild": "REBUILD_REQUIRED",
            "roundtrip": "ROUNDTRIP_REQUIRED",
            "hardware_map": "HARDWARE_MAP_REQUIRED" if preserved_hardware else "NOT_DETECTED",
            "field_test": "FIELD_TEST_REQUIRED",
        },
    }
    return (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def invalid_csv(invalid: list[list[str]]) -> bytes:
    return _encode_csv([INVALID_HEADER, *invalid], encoding="utf-8", delimiter=",", quoting=csv.QUOTE_MINIMAL)


def _invalid_payloads(
    destination: Path, invalid: list[list[str]], *, contexts: tuple[FindingContext, ...] = (),
) -> tuple[tuple[Path, bytes], ...]:
    return (
        (destination, invalid_csv(invalid)),
        (companion_text_path(destination), invalid_review_text(invalid, headers=INVALID_HEADER, contexts=contexts)),
    )


def _invalid_review_contexts(
    invalid: list[list[str]], records_by_program: Mapping[str, tuple[Instruction, ...]],
    fallback_records: Mapping[str, Mapping[FallbackKey, Instruction]],
) -> tuple[FindingContext, ...]:
    """Enrich viewing only; never infer a record from a matching Step alone."""
    def operation(record: Instruction) -> str:
        operands = record.continuation_operands or tuple(row[3] for row in record.continuation_rows)
        return " ".join(value for value in (record.instruction, record.operand, *operands) if value)

    indexes: dict[str, dict[tuple[str, str], list[Instruction]]] = {}
    for name, records in records_by_program.items():
        index: dict[tuple[str, str], list[Instruction]] = {}
        for record in records:
            index.setdefault((str(record.sequence), str(record.source_step)), []).append(record)
        indexes[name] = index
    contexts: list[FindingContext] = []
    for number, row in enumerate(invalid, 1):
        raw = tuple(row)
        if row[2] == "COMMENT":
            contexts.append(FindingContext(number, raw, category="comment"))
            continue
        candidates: dict[str, list[str]] = {}
        for part in row[7].split(";"):
            key, separator, value = part.partition("=")
            if separator:
                candidates.setdefault(key.strip(), []).append(value.strip())
        names = set(candidates.get("program", ()))
        if len(names) != 1 or (name := next(iter(names))) not in records_by_program:
            continue
        # Conflicting duplicate context keys must not silently select one value.
        fields = {key: values[0] for key, values in candidates.items() if len(set(values)) == 1}
        if any(len(set(candidates.get(key, ()))) > 1 for key in (
            "source_record_id", "source_digest", "source_locator", "operands",
        )):
            continue
        matches = []
        for record in indexes[name].get((row[0], row[1]), ()):
            record_id = fields.get("source_record_id", "")
            if record_id:
                if record.source_record_id != record_id:
                    continue
                if fields.get("source_digest") and record.source_digest != fields["source_digest"]:
                    continue
                if fields.get("source_locator") and record.source_locator != fields["source_locator"]:
                    continue
            elif (
                _fallback_record_key(record) != (row[0], row[1], row[2], fields.get("operands", row[3]))
                or len(set(candidates.get("operands", ()))) > 1
                or len(set(candidates.get("source_record_id", ()))) > 1
            ):
                continue
            matches.append(record)
        if len(matches) != 1:
            continue
        record = matches[0]
        detail: dict[str, str] = {
            "source_operation": operation(record), "source_note": record.note,
            "source_line_statement": record.line_statement, "source_pi_statement": record.pi_statement,
        }
        replacement = fallback_records.get(name, {}).get(_fallback_record_key(record))
        if replacement is not None and fields.get("publication") and fields.get("runtime_action"):
            detail.update(
                fallback_operation=operation(replacement), publication=fields["publication"],
                runtime_action=fields["runtime_action"],
            )
        contexts.append(FindingContext(number, raw, category="pou", program=name, **detail))
    return tuple(contexts)


def _candidate_program(candidate: str) -> str:
    for part in candidate.split(";"):
        key, separator, value = part.partition("=")
        if separator and key.strip() == "program":
            return value.strip()
    return ""


def _unverified_source_row_count(
    programs: Sequence[dict[str, object]],
    validation_findings: tuple[tuple[str, ...], ...],
) -> int:
    """Count source-record and opaque-block identities without double-counting."""
    total = 0
    for program in programs:
        name = str(program["name"])
        opaque = int(program["opaque_rows"])
        exact_ids: set[str] = set()
        opaque_members: dict[str, set[str]] = {}
        opaque_claims: dict[str, int] = {}
        bare_claims: dict[str, int] = {}
        for row in validation_findings:
            if len(row) < 8 or _candidate_program(row[7]) != name:
                continue
            if row[5] != "MINING_REQUIRED":
                continue
            fields = _candidate_fields(row[7])
            record_ids = {value for value in fields.get("source_record_id", "").split(",") if value}
            opaque_ids = {value for value in record_ids if ":opaque:" in value}
            exact_ids.update(record_ids - opaque_ids)
            for record_id in opaque_ids:
                base = record_id.rsplit(":opaque:", 1)[0]
                opaque_members.setdefault(f"opaque:{base}", set()).add(record_id)
            if record_ids:
                continue
            identity = f"{fields.get('source_digest', '')}|{fields.get('source_locator', '')}|{row[0]}|{row[1]}"
            for key in ("source_row_count", "row_count", "step_count"):
                value = fields.get(key, "")
                if value.isdecimal() and int(value) > 0:
                    if key == "step_count" or fields.get("opaque_block_id"):
                        opaque_identity = _opaque_block_identity(fields, identity)
                        opaque_claims[opaque_identity] = max(opaque_claims.get(opaque_identity, 0), int(value))
                    else:
                        bare_claims[identity] = max(bare_claims.get(identity, 0), int(value))
                    break
            else:
                if row[0] and row[1]:
                    bare_claims[identity] = 1
        opaque_identities = set(opaque_members) | set(opaque_claims)
        opaque_claim_total = sum(
            max(len(opaque_members.get(identity, set())), opaque_claims.get(identity, 0))
            for identity in opaque_identities
        )
        total += len(exact_ids) + max(opaque, opaque_claim_total) + sum(bare_claims.values())
    return total


def _opaque_block_identity(fields: Mapping[str, str], fallback_identity: str) -> str:
    """Unify P002 opaque record IDs and its exact StepInfo locator spelling."""
    block_id = fields.get("opaque_block_id", "")
    if block_id:
        return f"opaque:{block_id}"
    locator = fields.get("source_locator", "")
    match = re.fullmatch(r'MIL\[id=(?:"([^"]+)"|([^\]]+))\]', locator)
    if match is not None:
        return f"opaque:{match.group(1) or match.group(2)}"
    return f"opaque-claim:{fallback_identity}"


def _is_global_pou_relation_mining_finding(row: list[str]) -> bool:
    """Recognize only the observed P002 project-level empty-MilDB relation gate."""
    if len(row) < 8 or row[5:7] != ["MINING_REQUIRED", "P002"]:
        return False
    fields = _candidate_fields(row[7])
    return (
        not _candidate_program(row[7])
        and fields.get("scope") == "project"
        and fields.get("object_kind") == "topology"
        and fields.get("source_locator") == "*_LDDB.db,*_MilDB.db"
        and bool(re.fullmatch(
            r"pou_count=\d+; paired_database_count=\d+; no unique StepInfo-LDDB-MilDB relation",
            row[4],
        ))
    )


def _is_local_pou_relation_mining_finding(row: list[str], program_name: str) -> bool:
    """Recognize only the per-POU companion finding for the same relation gate."""
    if len(row) < 8 or row[4:7] != ["POU database relation is unresolved", "MINING_REQUIRED", "P002"]:
        return False
    fields = _candidate_fields(row[7])
    return (
        fields.get("program") == program_name
        and fields.get("source_locator") == "POU relations"
        and bool(re.fullmatch(r"[0-9a-f]{64}", fields.get("source_digest", "")))
    )


def _mining_review_eligible(
    program: ParsedProgram,
    invalid: list[list[str]],
) -> bool:
    """Gate import-shaped review output to fully decoded relation-only POUs."""
    if (
        not program.direct_decoded
        or program.opaque_blocks
        or program.coverage.opaque_rows
        or program.coverage.expected_rows != len(program.instructions)
    ):
        return False
    applicable = [
        row for row in invalid
        if not _candidate_program(row[7] if len(row) > 7 else "")
        or _candidate_program(row[7]) == program.name
    ]
    if not applicable:
        return False
    has_global_relation = any(_is_global_pou_relation_mining_finding(row) for row in applicable)
    has_local_relation = any(_is_local_pou_relation_mining_finding(row, program.name) for row in applicable)
    return has_global_relation and has_local_relation and all(
        _is_global_pou_relation_mining_finding(row)
        or _is_local_pou_relation_mining_finding(row, program.name)
        for row in applicable
    )


def _pou_blockers(
    invalid: list[list[str]],
    program_names: tuple[str, ...],
    official_dummy_fallback_keys: dict[str, frozenset[FallbackKey]] | None = None,
) -> tuple[bool, frozenset[str]]:
    """Return global-blocker state and exactly attributable blocked POUs."""
    valid_names = frozenset(program_names)
    official_dummy_fallback_keys = official_dummy_fallback_keys or {}
    blocked: set[str] = set()
    global_finding = False
    for row in invalid:
        if len(row) <= 7:
            global_finding = True
            continue
        name = _candidate_program(row[7])
        if name in valid_names:
            if (
                row[5] == TARGET_UNSUPPORTED
                and _finding_fallback_key(row) in official_dummy_fallback_keys.get(name, frozenset())
            ):
                continue
            blocked.add(name)
        else:
            global_finding = True
    return global_finding, frozenset(blocked)


def _mark_official_dummy_fallback(
    invalid: list[list[str]],
    official_dummy_fallback_keys: dict[str, frozenset[FallbackKey]],
) -> None:
    """Mark exact target rows projected with official review-only dummy devices."""
    for row in invalid:
        if len(row) < 8 or row[5] != TARGET_UNSUPPORTED:
            continue
        program_name = _candidate_program(row[7])
        if _finding_fallback_key(row) not in official_dummy_fallback_keys.get(program_name, frozenset()):
            continue
        row[7] = (
            f"{row[7]};diagnostic_fallback={OFFICIAL_DUMMY_FALLBACK};"
            f"publication={DIAGNOSTIC_FALLBACK_PUBLICATION};"
            f"runtime_action={DIAGNOSTIC_FALLBACK_RUNTIME_ACTION};"
            f"compatibility={DIAGNOSTIC_FALLBACK_COMPATIBILITY}"
        )


def _mark_typed_mining_fallback(
    invalid: list[list[str]],
    typed_mining_fallback_keys: Mapping[str, Mapping[FallbackKey, str]],
) -> None:
    """Mark only emitted typed-mining rows as non-runnable review diagnostics."""
    for row in invalid:
        if len(row) < 8 or row[5] != "MINING_REQUIRED":
            continue
        program_name = _candidate_program(row[7])
        kind = typed_mining_fallback_keys.get(program_name, {}).get(_finding_fallback_key(row))
        if kind is None:
            continue
        row[7] = (
            f"{row[7]};typed_mining_fallback={TYPED_MINING_FALLBACK};"
            f"fallback_kind={kind};publication={TYPED_MINING_REVIEW_PUBLICATION};"
            f"runtime_action={TYPED_MINING_REVIEW_RUNTIME_ACTION}"
        )










def _actual_review_coverage_is_exact(program: ParsedProgram) -> bool:
    """Require complete, non-opaque POU coverage before any special review output."""
    return (
        not program.opaque_blocks
        and program.coverage.opaque_blocks == 0
        and program.coverage.opaque_rows == 0
        and program.coverage.expected_rows == len(program.instructions)
    )


def _actual_source_recovery_candidate(program, source_sha256):
    return any(method[0] == "p002_neutral_ir" for method in program.recovery_methods)


def _actual_source_recovered_review_eligible(program, source_sha256, findings):
    return False


def _actual_source_recovery_allows_normal_q(
    program: ParsedProgram, source_sha256: str, findings: Sequence[Sequence[str]],
) -> bool:
    """Publish a recovery candidate normally only when its exact evidence is eligible."""
    return (
        not _actual_source_recovery_candidate(program, source_sha256)
        or _actual_source_recovered_review_eligible(program, source_sha256, findings)
    )


def _actual_structural_review_projection(program, projected, findings, source_sha256):
    return None


def _log_target_unsupported_findings(
    invalid: list[list[str]],
    diagnostic_fallback_keys: dict[str, frozenset[FallbackKey]],
) -> None:
    """Log every Q target rejection without paths or source archive identity."""
    details: list[tuple[str, str, str, str, str, str, str]] = []
    for row in invalid:
        if row[5] != "TARGET_UNSUPPORTED":
            continue
        candidate_fields: dict[str, str] = {}
        for field in row[7].split(";"):
            key, separator, value = field.partition("=")
            if separator:
                candidate_fields[key] = value
        details.append(
            (
                candidate_fields.get("program", ""),
                row[1],
                row[2],
                candidate_fields.get("operands", row[3]),
                row[5],
                row[4],
                OFFICIAL_DUMMY_FALLBACK if (
                    _finding_fallback_key(row)
                    in diagnostic_fallback_keys.get(candidate_fields.get("program", ""), frozenset())
                ) else "",
            )
        )
    if not details:
        return

    def sort_key(detail: tuple[str, str, str, str, str, str, str]) -> tuple[object, ...]:
        program, source_step, instruction, operands, finding_code, reason, _fallback = detail
        numeric_step = int(source_step) if source_step.isdecimal() else -1
        return program, numeric_step, source_step, instruction, operands, finding_code, reason

    ordered_details = sorted(details, key=sort_key)
    logging.warning("Q target unsupported findings: count=%s", len(ordered_details))
    if any(fallback for *_detail, fallback in details):
        logging.warning(
            "OFFICIAL_DUMMY_FALLBACK: exact record-backed target holds use official "
            "SM1255/SD1255 diagnostics; publication=REVIEW_ONLY "
            "action=DO_NOT_DOWNLOAD_OR_RUN compatibility=A_PLC_COMPATIBILITY_REQUIRED"
        )
    else:
        logging.warning(
            "TARGET_UNSUPPORTED remains blocked: no exact record-backed official dummy "
            "fallback is eligible"
        )
    for program, source_step, instruction, operands, finding_code, reason, fallback in ordered_details:
        logging.warning(
            "Q target unsupported detail: %s",
            json.dumps(
                {
                    "program": program,
                    "source_step": source_step,
                    "instruction": instruction,
                    "operands": operands,
                    "finding_code": finding_code,
                    "reason": reason,
                    "diagnostic_fallback": fallback,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ConversionCancelled("conversion cancelled before output publish")


def _partial_publish_error(published: list[Path], reason: str) -> PartialPublishError:
    return PartialPublishError(
        "incomplete output set, do not import; "
        f"reason={reason}; partial_outputs="
        + ",".join(str(path) for path in published)
    )


def _partial_paths(published: list[Path], destination: Path, destination_created: bool) -> list[Path]:
    return [*published, destination] if destination_created else [*published]


def _write_atomically(
    payloads: tuple[tuple[Path, bytes], ...],
    input_snapshot: InputSnapshot,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Create outputs exclusively; never move, overwrite, or delete any output file."""
    published: list[Path] = []
    for destination, payload in payloads:
        destination_created = False
        actual_leaf_path = ""
        try:
            if cancel_check is not None and cancel_check():
                if published:
                    raise _partial_publish_error(published, "cancelled before output publish")
                raise ConversionCancelled("conversion cancelled before output publish")
            input_snapshot.verify()
            destination.parent.mkdir(parents=True, exist_ok=True)
            _reject_link_like_ancestors(destination.parent, "output parent")
            parent_path = os.path.normcase(os.path.abspath(destination.parent.resolve()))
            parent_identity = _object_identity(os.stat(destination.parent, follow_symlinks=False))
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(destination, flags)
            destination_created = True
            try:
                expected_leaf_path = os.path.normcase(os.path.abspath(destination))
                actual_leaf_path = _handle_final_path(descriptor)
                if actual_leaf_path != expected_leaf_path:
                    raise RuntimeError("output leaf final path differs from the approved destination")
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("output write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
                if _handle_final_path(descriptor) != expected_leaf_path:
                    raise RuntimeError("output leaf final path changed during publication")
                if (
                    os.path.normcase(os.path.abspath(destination.parent.resolve())) != parent_path
                    or _object_identity(os.stat(destination.parent, follow_symlinks=False)) != parent_identity
                ):
                    raise RuntimeError("output parent identity changed during publication")
                input_snapshot.verify()
            finally:
                os.close(descriptor)
        except (ConversionCancelled, PartialPublishError):
            raise
        except Exception as error:
            if published or destination_created:
                partial_paths = _partial_paths(published, destination, destination_created)
                location = (
                    f"; intended_path={destination}; actual_handle_path={actual_leaf_path}"
                    if destination_created else ""
                )
                raise _partial_publish_error(partial_paths, f"{type(error).__name__}: {error}{location}") from error
            if isinstance(error, FileExistsError):
                raise ConversionError(f"existing output is not overwritten: {destination}") from error
            raise
        published.append(destination)
    try:
        input_snapshot.verify()
        if cancel_check is not None and cancel_check():
            raise ConversionCancelled("cancelled after final output publish")
    except Exception as error:
        raise _partial_publish_error(published, str(error)) from error


def _convert_snapshot(
    input_path: Path,
    project_identifier: str,
    intermediate_output: Path,
    q_output: Path,
    invalid_output: Path,
    cancel_check: Callable[[], bool] | None = None,
    *,
    comment_output: Path | None = None,
    comment_review_output: Path | None = None,
    report_output: Path | None = None,
    input_snapshot: InputSnapshot,
) -> tuple[int, int]:
    """Convert one source archive and return processed rows and review findings."""
    requested_outputs = (intermediate_output, invalid_output)
    if comment_output is not None:
        requested_outputs = (*requested_outputs, comment_output)
    if comment_review_output is not None:
        requested_outputs = (*requested_outputs, comment_review_output)
    if report_output is not None:
        requested_outputs = (*requested_outputs, report_output)
    requested_outputs = (*requested_outputs, companion_text_path(invalid_output))
    if comment_review_output is not None:
        requested_outputs = (*requested_outputs, companion_text_path(comment_review_output))
    validated_outputs = _validate_output_paths(input_path, requested_outputs)
    intermediate_output, invalid_output = validated_outputs[:2]
    extra_index = 2
    if comment_output is not None:
        comment_output = validated_outputs[extra_index]
        extra_index += 1
    if comment_review_output is not None:
        comment_review_output = validated_outputs[extra_index]
        extra_index += 1
    if report_output is not None:
        report_output = validated_outputs[extra_index]
    source_hash = input_snapshot.sha256
    try:
        project = read_gx3(input_path, _source_bytes=input_snapshot.body)
    except (ConversionError, OSError, sqlite3.Error, element_tree.ParseError, UnicodeDecodeError) as error:
        finding_code = error.finding_code if isinstance(error, ConversionError) else "INPUT_INVALID"
        mining_tracks = error.mining_tracks if isinstance(error, ConversionError) else ""
        candidate = error.candidate if isinstance(error, ConversionError) else ""
        invalid_row = [["", "", "", "", str(error), finding_code, mining_tracks, candidate]]
        _write_atomically(_invalid_payloads(invalid_output, invalid_row), input_snapshot, cancel_check)
        raise

    for diagnostic in project.source_diagnostics:
        logging.warning(
            "source component was not converted: category=%s finding_code=%s detail=%s",
            diagnostic.category,
            diagnostic.finding_code,
            diagnostic.candidate,
        )

    q_outputs = planned_q_outputs(
        input_path,
        q_output,
        project=project,
    )
    review_outputs = partial_review_paths_for_project(project, q_output)
    mining_review_outputs = mining_review_paths_for_project(project, q_output)
    typed_mining_review_outputs = typed_mining_review_paths_for_project(project, q_output)
    source_recovered_review_outputs = source_recovered_review_paths_for_project(project, q_output)
    structural_review_outputs = structural_review_paths_for_project(project, q_output)
    import_review_outputs = import_review_paths_for_project(project, q_output)
    expanded_outputs = (
        intermediate_output,
        *q_outputs,
        *review_outputs,
        *mining_review_outputs,
        *typed_mining_review_outputs,
        *source_recovered_review_outputs,
        *structural_review_outputs,
        invalid_output,
    )
    if comment_output is not None:
        expanded_outputs = (*expanded_outputs, comment_output)
    if comment_review_output is not None:
        expanded_outputs = (*expanded_outputs, comment_review_output)
    if report_output is not None:
        expanded_outputs = (*expanded_outputs, report_output)
    expanded_outputs = (*expanded_outputs, companion_text_path(invalid_output))
    if comment_review_output is not None:
        expanded_outputs = (*expanded_outputs, companion_text_path(comment_review_output))
    _validate_output_paths(input_path, expanded_outputs)

    try:
        invalid = validate_project(project)
        if comment_output is not None:
            invalid.extend(validate_comment_encoding(project.comments))
    except (ConversionError, OSError, sqlite3.Error, element_tree.ParseError, UnicodeDecodeError) as error:
        finding_code = error.finding_code if isinstance(error, ConversionError) else "INPUT_INVALID"
        mining_tracks = error.mining_tracks if isinstance(error, ConversionError) else ""
        candidate = error.candidate if isinstance(error, ConversionError) else ""
        invalid_row = [["", "", "", "", str(error), finding_code, mining_tracks, candidate]]
        _write_atomically(_invalid_payloads(invalid_output, invalid_row), input_snapshot, cancel_check)
        raise

    _raise_if_cancelled(cancel_check)
    opaque_row_count = (
        sum(block.row_count for program in project.programs for block in program.opaque_blocks)
        if isinstance(project, ParsedMultiProject)
        else sum(block.row_count for block in project.opaque_blocks)
    )
    expected_row_count = len(project.instructions) + opaque_row_count
    logging.info("progress: decoded %s/%s instruction rows; opaque=%s", len(project.instructions), expected_row_count, opaque_row_count)
    preserved_hardware = sorted(
        {
            operand
            for record in project.instructions
            for operand in (record.operand, *record.continuation_operands)
            if re.match(r"^U\d+\\G", operand)
        }
    )
    if preserved_hardware:
        logging.warning(
            "HARDWARE_COMPATIBILITY_UNVERIFIED: U<n>\\G<m> operands were preserved "
            "without target hardware equivalence; count=%s; sample=%s",
            len(preserved_hardware),
            ",".join(preserved_hardware[:5]),
        )
    programs = (
        project.programs
        if isinstance(project, ParsedMultiProject)
        else (ParsedProgram(project.program_name, "", project.instructions, project.structure, project.findings, False, project.opaque_blocks),)
    )
    source_identity = project.source_sha256 if isinstance(project, ParsedMultiProject) else source_hash
    source_records_by_program = {
        program.name: program.instructions
        for program in programs
    }
    projected_records_by_program = {
        program.name: tuple(
            (
                record
                if _validate_fx_io_source_record(record)
                else project_q_record(source_identity, program.name, record)
            )
            for record in program.instructions
        )
        for program in programs
    }
    program_names = tuple(program.name for program in programs)
    diagnostic_fallback_keys = {
        program.name: _target_unsupported_fallback_keys(
            program.name,
            source_records_by_program[program.name],
            invalid,
            program_names,
            program=program,
        )
        for program in programs
    }
    attempted_diagnostic_fallback_keys = dict(diagnostic_fallback_keys)
    fallback_projected_records_by_program: dict[str, tuple[Instruction, ...]] = {}
    verified_fallback_keys: dict[str, frozenset[FallbackKey]] = {}
    for program in programs:
        records, keys = _verified_official_dummy_projection(
            program.name,
            source_records_by_program[program.name],
            projected_records_by_program[program.name],
            diagnostic_fallback_keys[program.name],
        )
        fallback_projected_records_by_program[program.name] = records
        if keys:
            verified_fallback_keys[program.name] = keys
    diagnostic_fallback_keys = verified_fallback_keys
    typed_mining_fallback_keys = {
        program.name: _typed_mining_fallback_keys(program, invalid, program_names)
        for program in programs
    }
    typed_mining_projected_records_by_program: dict[str, tuple[Instruction, ...]] = {}
    verified_typed_mining_fallback_keys: dict[str, dict[FallbackKey, str]] = {}
    for program in programs:
        records, keys = _verified_typed_mining_projection(
            program.name,
            source_records_by_program[program.name],
            projected_records_by_program[program.name],
            typed_mining_fallback_keys[program.name],
        )
        typed_mining_projected_records_by_program[program.name] = records
        if keys:
            verified_typed_mining_fallback_keys[program.name] = keys
    typed_mining_fallback_keys = verified_typed_mining_fallback_keys
    structural_review_records = {
        program.name: value
        for program in programs
        if program.name not in diagnostic_fallback_keys
        if (value := _actual_structural_review_projection(
            program, projected_records_by_program[program.name], invalid, source_hash,
        )) is not None
    }
    verified_nonserialized_fallback_surfaces = {
        identity: expectation
        for program in programs
        for identity, expectation in _verified_nonserialized_fallback_surface_expectations(
            program.name,
            source_records_by_program[program.name],
            projected_records_by_program[program.name],
            fallback_projected_records_by_program[program.name],
            diagnostic_fallback_keys.get(program.name, frozenset()),
        ).items()
    }
    radix_audit = device_radix_audit(
        source_records_by_program,
        fallback_projected_records_by_program,
        verified_nonserialized_fallback_surfaces=verified_nonserialized_fallback_surfaces,
    )
    radix_audit_finding = _device_radix_audit_finding(radix_audit)
    if radix_audit_finding is not None:
        invalid.append(radix_audit_finding)
    source_recovered_programs = {
        program.name for program in programs
        if _actual_source_recovered_review_eligible(program, source_hash, invalid)
    }
    has_global_finding, blocked_programs = _pou_blockers(
        invalid,
        program_names,
        diagnostic_fallback_keys,
    )
    if isinstance(project, ParsedMultiProject):
        blocked_programs = frozenset({
            *blocked_programs,
            *(program.name for program in project.programs if program.opaque_blocks),
        })
    elif project.opaque_blocks:
        has_global_finding = True
    # The source-to-target projection and device-radix audits above remain
    # one-to-one.  Only an otherwise normal Q CSV may run this target-only
    # layout proof; review and fallback artifacts retain their original rows
    # and explicitly report that no layout decision was made.
    normal_q_program_names = frozenset(
        program.name
        for program in programs
        if (
            not has_global_finding
            and program.name not in blocked_programs
            and program.name not in diagnostic_fallback_keys
            and _actual_source_recovery_allows_normal_q(program, source_hash, invalid)
        )
    )
    target_layout_results = {
        program.name: apply_constant_root_layout(fallback_projected_records_by_program[program.name])
        for program in programs
        if program.name in normal_q_program_names
    }
    target_layout_reports = {
        program.name: (
            target_layout_results[program.name].report
            if program.name in target_layout_results
            else {
                "status": "NOT_APPLIED_NON_NORMAL_Q",
                "rule": LAYOUT_RULE,
                "source_ir_changed": False,
                "source_audit_relation": "PRE_LAYOUT_ONE_TO_ONE_AUDIT_ONLY",
                "reason": "NORMAL_Q_CSV_REQUIRED",
            }
        )
        for program in programs
    }
    target_layout_records_by_program = {
        program.name: (
            target_layout_results[program.name].records
            if program.name in target_layout_results
            else fallback_projected_records_by_program[program.name]
        )
        for program in programs
    }
    if isinstance(project, ParsedMultiProject):
        q_payloads = tuple(
            (
                destination,
                q_import_csv(
                    project_identifier,
                    target_layout_records_by_program[program.name],
                    preserve_source_steps=True,
                ),
            )
            for destination, program in zip(q_outputs, project.programs, strict=True)
            if program.name in normal_q_program_names
        )
    else:
        q_payloads = (
            ((
                q_outputs[0],
                q_import_csv(
                    project_identifier,
                    target_layout_records_by_program[project.program_name],
                ),
            ),)
            if project.program_name in normal_q_program_names
            else ()
        )
    if source_recovered_programs - {
        program.name
        for destination, program in zip(q_outputs, programs, strict=True)
        if any(path == destination for path, _payload in q_payloads)
    }:
        raise RuntimeError("accepted source recovery did not publish through the normal Q path")
    diagnostic_fallback_import_review_payloads = tuple(
        (
            destination,
            q_import_csv(
                project_identifier,
                fallback_projected_records_by_program[program.name],
                preserve_source_steps=isinstance(project, ParsedMultiProject),
            ),
        )
        for program, destination in zip(programs, import_review_outputs, strict=True)
        if program.name in diagnostic_fallback_keys
    )
    mining_review_payloads = tuple(
        (
            destination,
            q_import_csv(
                project_identifier,
                projected_records_by_program[program.name],
                preserve_source_steps=isinstance(project, ParsedMultiProject),
            ),
        )
        for program, destination in zip(programs, mining_review_outputs, strict=True)
        if _mining_review_eligible(program, invalid)
    )
    typed_mining_review_payloads = tuple(
        (
            destination,
            q_import_csv(
                project_identifier,
                typed_mining_projected_records_by_program[program.name],
                preserve_source_steps=isinstance(project, ParsedMultiProject),
            ),
        )
        for program, destination in zip(programs, typed_mining_review_outputs, strict=True)
        if program.name in typed_mining_fallback_keys
    )
    source_recovered_review_payloads = ()
    source_recovered_import_review_payloads = ()
    structural_review_payloads = tuple(
        (
            destination,
            partial_review_csv(
                structural_review_records[program.name], (),
                ([
                    "13", "89", "OR", "SM1255", "non-semantic structural compare skeleton",
                    STRUCTURAL_RESULT_DUMMY_FALLBACK, "P002|Converter",
                    f"program={program.name};source_record_id={_ACTUAL_90_BLOCK}:11;"
                    f"source_locator=MIL[id=\"{_ACTUAL_90_BLOCK}\"];source_digest={_ACTUAL_90_DIGEST};"
                    "source_step=89;source_operands=SD10130 D918;post_projection_validation=PASSED;"
                    "publication=REVIEW_ONLY;runtime_action=DO_NOT_DOWNLOAD_OR_RUN",
                ], [
                    "17", "105", "MOV", "SD1255", "exact word-device review dummy",
                    STRUCTURAL_RESULT_DUMMY_FALLBACK, "P002|Converter",
                    f"program={program.name};source_record_id={_ACTUAL_90_BLOCK}:15;"
                    f"source_locator=MIL[id=\"{_ACTUAL_90_BLOCK}\"];source_digest={_ACTUAL_90_DIGEST};"
                    "source_step=105;source_operands=SD10130 D918;post_projection_validation=PASSED;"
                    "publication=REVIEW_ONLY;runtime_action=DO_NOT_DOWNLOAD_OR_RUN",
                ]),
            ),
        )
        for program, destination in zip(programs, structural_review_outputs, strict=True)
        if program.name in structural_review_records
    )
    structural_import_review_payloads = tuple(
        (destination, q_import_csv(project_identifier, structural_review_records[program.name], preserve_source_steps=isinstance(project, ParsedMultiProject)))
        for program, destination in zip(programs, import_review_outputs, strict=True)
        if program.name in structural_review_records
    )
    review_import_metadata: dict[str, dict[str, object]] = {}

    def import_metadata(
        program: ParsedProgram,
        destination: Path,
        records: tuple[Instruction, ...],
        import_payload: bytes,
        ledger_destination: Path,
        ledger_payload: bytes,
    ) -> dict[str, object]:
        provenance_rows = [
            [
                record.sequence, record.source_step, record.source_record_id,
                record.source_digest, record.source_locator,
            ]
            for record in records
        ]
        return {
            "output": destination.name,
            "sha256": hashlib.sha256(import_payload).hexdigest(),
            "logical_source_rows": len(records),
            "csv_data_rows": sum(1 + len(record.continuation_rows or record.continuation_operands) for record in records),
            "encoding": "UTF-16LE-BOM",
            "delimiter": "TAB",
            "quoting": "QUOTE_ALL",
            "runtime_action": "DO_NOT_DOWNLOAD_OR_RUN",
            "source_sha256": source_hash,
            "source_record_identity_sha256": hashlib.sha256(
                json.dumps(provenance_rows, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "ledger_output": ledger_destination.name,
            "ledger_sha256": hashlib.sha256(ledger_payload).hexdigest(),
        }

    for program, destination in zip(programs, import_review_outputs, strict=True):
        if program.name in structural_review_records:
            records = structural_review_records[program.name]
            payload = next(value for path, value in structural_import_review_payloads if path == destination)
            ledger_destination, ledger_payload = next(
                item for item in structural_review_payloads
                if item[0].stem == f"{program.name}.structural-review"
            )
            review_import_metadata[program.name] = import_metadata(
                program, destination, records, payload, ledger_destination, ledger_payload,
            )
    _mark_official_dummy_fallback(invalid, diagnostic_fallback_keys)
    _mark_typed_mining_fallback(invalid, typed_mining_fallback_keys)
    review_payloads: list[tuple[Path, bytes]] = []
    for program, review_destination in zip(programs, review_outputs, strict=True):
        if not invalid or (
            not has_global_finding
            and program.name not in blocked_programs
            and program.name not in diagnostic_fallback_keys
        ):
            continue
        program_findings = tuple(
            tuple(row)
            for row in invalid
            if has_global_finding and not _candidate_program(row[7] if len(row) > 7 else "")
            or _candidate_program(row[7] if len(row) > 7 else "") == program.name
        )
        review_payloads.append(
            (
                review_destination,
                partial_review_text(
                    program.name,
                    source_records_by_program[program.name],
                    program.opaque_blocks,
                    program_findings,
                    fallback_records={
                        _fallback_record_key(source): projected
                        for source, projected in zip(
                            source_records_by_program[program.name],
                            fallback_projected_records_by_program[program.name], strict=True,
                        )
                        if _fallback_record_key(source) in diagnostic_fallback_keys.get(program.name, frozenset())
                    },
                ),
            )
        )
    for index, (program, destination) in enumerate(zip(programs, import_review_outputs, strict=True)):
        if program.name not in diagnostic_fallback_keys:
            continue
        records = fallback_projected_records_by_program[program.name]
        payload = next(
            value for path, value in diagnostic_fallback_import_review_payloads
            if path == destination
        )
        ledger_destination, ledger_payload = next(
            item for item in review_payloads if item[0] == review_outputs[index]
        )
        review_import_metadata[program.name] = import_metadata(
            program, destination, records, payload, ledger_destination, ledger_payload,
        )
    has_comment_finding = any(len(row) > 2 and row[2] == "COMMENT" for row in invalid)
    has_comment_projection_finding = any(
        len(row) > 5
        and len(row) > 2
        and row[2] == "COMMENT"
        and row[5] in {"COMMENT_ENCODING_REQUIRED", "DEVICE_RADIX_INVALID", "DEVICE_RANGE_INVALID",
                       "DEVICE_SYNTAX_INVALID"}
        for row in invalid
    )
    diagnostic_payloads = (
        (intermediate_output, normalized_csv(project)),
        *q_payloads,
        *mining_review_payloads,
        *typed_mining_review_payloads,
        *source_recovered_review_payloads,
        *structural_review_payloads,
        *diagnostic_fallback_import_review_payloads,
        *source_recovered_import_review_payloads,
        *structural_import_review_payloads,
        *review_payloads,
        *(
            ((comment_output, device_comment_csv(project_identifier, project.comments)),)
            if comment_output is not None and not has_comment_finding
            else ()
        ),
        *(
            _comment_review_payloads(
                comment_review_output, project.comments,
                import_csv_state=("NOT_REQUESTED" if comment_output is None else "WITHHELD" if has_comment_finding else "PLANNED"),
            )
            if comment_review_output is not None and not has_comment_projection_finding
            else ()
        ),
        *(
            ((
                report_output,
                conversion_report_json(
                    input_path,
                    source_hash,
                    project,
                    q_outputs,
                    validation_findings=tuple(tuple(row) for row in invalid),
                    published_q_outputs=tuple(path for path, _payload in q_payloads),
                    review_outputs=review_outputs,
                    published_review_outputs=tuple(path for path, _payload in review_payloads),
                    review_output_hashes={path.name: hashlib.sha256(payload).hexdigest() for path, payload in review_payloads},
                    mining_review_outputs=mining_review_outputs,
                    published_mining_review_outputs=tuple(
                        path for path, _payload in mining_review_payloads
                    ),
                    typed_mining_review_outputs=typed_mining_review_outputs,
                    published_typed_mining_review_outputs=tuple(
                        path for path, _payload in typed_mining_review_payloads
                    ),
                    source_recovered_review_outputs=source_recovered_review_outputs,
                    published_source_recovered_review_outputs=tuple(
                        path for path, _payload in source_recovered_review_payloads
                    ),
                    published_source_recovered_import_review_outputs=tuple(
                        path for path, _payload in source_recovered_import_review_payloads
                    ),
                    structural_review_outputs=structural_review_outputs,
                    published_structural_review_outputs=tuple(
                        path for path, _payload in structural_review_payloads
                    ),
                    published_structural_import_review_outputs=tuple(
                        path for path, _payload in structural_import_review_payloads
                    ),
                    published_diagnostic_fallback_import_review_outputs=tuple(
                        path for path, _payload in diagnostic_fallback_import_review_payloads
                    ),
                    review_import_metadata=review_import_metadata,
                    comment_review_published=(
                        comment_review_output is not None and not has_comment_projection_finding
                    ),
                    diagnostic_fallback_keys=diagnostic_fallback_keys,
                    diagnostic_fallback_attempted_keys=attempted_diagnostic_fallback_keys,
                    typed_mining_fallback_keys=typed_mining_fallback_keys,
                    radix_audit=radix_audit,
                    target_layout_reports=target_layout_reports,
                ),
            ),)
            if report_output is not None
            else ()
        ),
    )
    viewing_fallbacks = {
        name: {
            **{
                _fallback_record_key(source): target
                for source, target in zip(records, fallback_projected_records_by_program[name], strict=True)
                if _fallback_record_key(source) in diagnostic_fallback_keys.get(name, frozenset())
            },
            **{
                _fallback_record_key(source): target
                for source, target in zip(records, typed_mining_projected_records_by_program[name], strict=True)
                if _fallback_record_key(source) in typed_mining_fallback_keys.get(name, {})
            },
        }
        for name, records in source_records_by_program.items()
    }
    invalid_payloads = _invalid_payloads(
        invalid_output, invalid,
        contexts=_invalid_review_contexts(invalid, source_records_by_program, viewing_fallbacks),
    )
    payloads = (*invalid_payloads, *diagnostic_payloads) if invalid else (*diagnostic_payloads, *invalid_payloads)
    _write_atomically(payloads, input_snapshot, cancel_check)
    if invalid:
        _log_target_unsupported_findings(invalid, diagnostic_fallback_keys)
    return expected_row_count, len(invalid)


def convert(
    input_path: Path,
    project_identifier: str,
    intermediate_output: Path,
    q_output: Path,
    invalid_output: Path,
    cancel_check: Callable[[], bool] | None = None,
    *,
    comment_output: Path | None = None,
    comment_review_output: Path | None = None,
    report_output: Path | None = None,
    expected_source_sha256: str | None = None,
) -> tuple[int, int]:
    """Convert from one retained input snapshot and close it after publication."""
    _validate_output_paths(input_path, (intermediate_output, invalid_output, companion_text_path(invalid_output)))
    snapshot = _open_input_snapshot(input_path)
    try:
        if expected_source_sha256 is not None and snapshot.sha256 != expected_source_sha256:
            raise ConversionError("source changed since workflow plan")
        return _convert_snapshot(
            input_path,
            project_identifier,
            intermediate_output,
            q_output,
            invalid_output,
            cancel_check,
            comment_output=comment_output,
            comment_review_output=comment_review_output,
            report_output=report_output,
            input_snapshot=snapshot,
        )
    finally:
        snapshot.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Converter fail-closed GX3 FX5U to GX Works2 Q CSV converter")
    parser.add_argument("--input", type=Path, required=True, help="read-only GX Works3 .gx3 input")
    parser.add_argument("--project-identifier", required=True, help="GX Works2 Q import project identifier")
    parser.add_argument(
        "--intermediate-output",
        type=Path,
        required=True,
        help="UTF-8 normalized CSV preserving FX X/Y source spelling; do not import into GX Works2",
    )
    parser.add_argument(
        "--q-output",
        type=Path,
        required=True,
        help="per-program UTF-16LE Q target GX Works2 import CSV that preserves FX X/Y source spelling identity",
    )
    parser.add_argument("--comment-output", type=Path, required=True, help="UTF-16LE GX Works2 device-comment CSV destination")
    parser.add_argument("--comment-review-output", type=Path, required=True, help="UTF-8 comment review CSV destination; a .txt viewing companion is also created")
    parser.add_argument("--report-output", type=Path, required=True, help="UTF-8 conversion status JSON destination")
    parser.add_argument("--invalid-output", type=Path, required=True, help="UTF-8 invalid-row CSV destination; a .txt viewing companion is also created")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    started = time.monotonic()
    processed = 0
    invalid = 0
    try:
        arguments = build_parser().parse_args()
        processed, invalid = convert(
            arguments.input,
            arguments.project_identifier,
            arguments.intermediate_output,
            arguments.q_output,
            arguments.invalid_output,
            comment_output=arguments.comment_output,
            comment_review_output=arguments.comment_review_output,
            report_output=arguments.report_output,
        )
        return 0
    except ConversionCancelled as error:
        logging.warning("conversion cancelled: %s", error)
        return 130
    except (ConversionError, OSError, RuntimeError) as error:
        invalid = max(invalid, 1)
        logging.error("conversion failed: %s", error)
        return 1
    finally:
        logging.info(
            "summary: processed_rows=%s invalid_rows=%s elapsed_seconds=%.3f",
            processed,
            invalid,
            time.monotonic() - started,
        )


if __name__ == "__main__":
    raise SystemExit(main())
