"""Pure Kn digit-designated bit-device grammar for P006.

This module parses grouped bit-device operands such as ``K4M0``.  It does not
approve conversion, guess instruction width, or consult hardware limits.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


REPR_MAX_ADDRESS = 0xFFFFFFFF
_ASCII_DIGIT = frozenset("0123456789")
_Q_PROFILE = "Q06UDV"
_FX5_PROFILE = "FX5"
_PROFILES = frozenset({_Q_PROFILE, _FX5_PROFILE})
_Q_FAMILIES = frozenset({"X", "Y", "M", "L", "SM", "F", "B", "SB"})
_FX5_FAMILIES = frozenset({"X", "Y", "M", "L", "SM", "F", "B", "SB", "S"})
_Q_UNSUPPORTED = ("DX", "DY", "FX", "FY", "T", "ST", "C", "TS", "TC", "CS", "CC", "V", "S")
_WORD_FAMILIES = frozenset({"D", "W", "SD", "SW", "R", "ZR", "Z"})
_FAMILY_NAMES = (
    "CC",
    "CS",
    "DX",
    "DY",
    "FX",
    "FY",
    "SB",
    "SD",
    "SM",
    "ST",
    "SW",
    "TC",
    "TS",
    "ZR",
    "B",
    "C",
    "D",
    "F",
    "L",
    "M",
    "R",
    "S",
    "T",
    "V",
    "W",
    "X",
    "Y",
    "Z",
)
_PLAIN_CONSTANT = re.compile(r"K[+-]?\d+\Z")
_MODULE_PREFIX = re.compile(r"[UJ]\d+\\")
_INDEX_SUFFIX = re.compile(r"[ZV]\d+\Z")
_INCOMPLETE_INDEX = re.compile(r"[ZV]\Z")


@dataclass(frozen=True)
class DigitDesignation:
    """One parsed Kn grouped bit-device operand, with source spelling preserved."""

    raw: str
    digits: int
    family: str
    address_text: str
    radix: int
    address: int
    bit_count: int
    last_address: int


def parse_digit_designation(token: str, profile: str = _Q_PROFILE) -> DigitDesignation | None:
    """Parse a Kn grouped bit-device token for ``Q06UDV`` or ``FX5``.

    Plain constants and ungrouped devices return ``None``.  Malformed or
    CPU-inapplicable grouped operands raise ``ValueError``.
    """

    _require_profile(profile)
    if not isinstance(token, str) or token == "":
        return None
    if _PLAIN_CONSTANT.fullmatch(token):
        return None

    working = token.strip().upper()
    indirect = working[:1] in {"@", "*"}
    if indirect:
        working = working[1:]
    module_prefix = _MODULE_PREFIX.match(working)
    if module_prefix is not None:
        working = working[module_prefix.end() :]
    if not working.startswith("K"):
        return None
    body = working[1:]
    if not re.search(r"[A-Za-z\\]", body):
        return None

    if any(ch.isspace() for ch in token):
        raise ValueError("digit designation cannot contain whitespace")
    if token != token.upper():
        raise ValueError("digit designation must use uppercase ASCII spelling")
    if indirect or "@" in token or "*" in token:
        raise ValueError("digit designation cannot use an indirect prefix")
    if module_prefix is not None or "\\" in token:
        raise ValueError("digit designation cannot use a module prefix")
    if "." in token:
        raise ValueError("digit designation cannot use a bit suffix")
    if "[" in token or "]" in token:
        raise ValueError("digit designation cannot use an index")
    if "+" in token or "-" in token:
        raise ValueError("digit designation cannot contain a sign")

    digits, rest = _parse_kn_prefix(body)
    family, address_part = _split_family(rest)
    address_text, index_mark = _split_index(address_part)
    if index_mark is not None:
        raise ValueError("digit designation cannot use an index")
    _require_family(family, profile)
    radix = _family_radix(family, profile)
    address = _parse_address(address_text, radix)
    bit_count = digits * 4
    last_address = address + bit_count - 1
    return DigitDesignation(
        raw=token,
        digits=digits,
        family=family,
        address_text=address_text,
        radix=radix,
        address=address,
        bit_count=bit_count,
        last_address=last_address,
    )


def validate_digit_width(group: DigitDesignation, data_bits: int) -> None:
    """Permit Kn grouping only for explicit 16-bit or 32-bit data width."""

    if data_bits not in (16, 32):
        raise ValueError("digit designation width permits 16 or 32 bit data only")
    if group.bit_count > data_bits:
        raise ValueError(
            f"digit designation bit_count {group.bit_count} exceeds data_bits {data_bits}"
        )


def _require_profile(profile: str) -> None:
    if profile not in _PROFILES:
        raise ValueError(f"unknown digit designation profile {profile!r}")


def _parse_kn_prefix(body: str) -> tuple[int, str]:
    if not body:
        raise ValueError("malformed digit designation: missing Kn digit")
    first = body[0]
    if first.isdigit() and first not in _ASCII_DIGIT:
        raise ValueError("digit designation Kn cannot use Unicode digits")
    index = 0
    while index < len(body) and body[index].isdigit():
        if body[index] not in _ASCII_DIGIT:
            raise ValueError("digit designation Kn cannot use Unicode digits")
        index += 1
    if index == 0:
        raise ValueError("malformed digit designation: Kn digit must be 1..8")
    n_text = body[:index]
    if any(ch not in _ASCII_DIGIT for ch in n_text):
        raise ValueError("digit designation Kn cannot use Unicode digits")
    if len(n_text) != 1:
        raise ValueError("digit designation Kn cannot use a multidigit n")
    digits = int(n_text, 10)
    if digits == 0:
        raise ValueError("digit designation Kn cannot use zero")
    if digits == 9 or digits < 1 or digits > 8:
        raise ValueError("digit designation Kn cannot use nine; n must be 1..8")
    return digits, body[index:]


def _split_family(rest: str) -> tuple[str, str]:
    if rest[:1].isdigit() and rest[:1] not in _ASCII_DIGIT:
        raise ValueError("digit designation address cannot use Unicode digits")
    for name in _FAMILY_NAMES:
        if rest.startswith(name):
            return name, rest[len(name) :]
    raise ValueError("malformed digit designation: unknown device family")


def _split_index(address_part: str) -> tuple[str, str | None]:
    match = _INDEX_SUFFIX.search(address_part)
    if match is not None and match.start() > 0:
        return address_part[: match.start()], match.group()
    if _INCOMPLETE_INDEX.search(address_part) and len(address_part) > 1:
        return address_part[:-1], address_part[-1]
    return address_part, None


def _require_family(family: str, profile: str) -> None:
    allowed = _Q_FAMILIES if profile == _Q_PROFILE else _FX5_FAMILIES
    if family in allowed:
        return
    if family in _WORD_FAMILIES:
        raise ValueError(f"word-device grouping is not allowed: {family}")
    if profile == _Q_PROFILE and family in _Q_UNSUPPORTED:
        raise ValueError(f"unsupported digit-designation family {family} on Q06UDV")
    raise ValueError(f"CPU-inapplicable digit-designation family {family} for {profile}")


def _family_radix(family: str, profile: str) -> int:
    if profile == _Q_PROFILE:
        if family in {"X", "Y", "B", "SB"}:
            return 16
        return 10
    if family in {"X", "Y"}:
        return 8
    if family in {"B", "SB"}:
        return 16
    return 10


def _parse_address(address_text: str, radix: int) -> int:
    if address_text == "":
        raise ValueError("malformed digit designation: missing address")
    if any(ch.isdigit() and ch not in _ASCII_DIGIT for ch in address_text):
        raise ValueError("digit designation address cannot use Unicode digits")
    if radix == 16:
        pattern, label = r"[0-9A-F]+", "hexadecimal"
    elif radix == 8:
        pattern, label = r"[0-7]+", "octal"
    else:
        pattern, label = r"[0-9]+", "decimal"
    if re.fullmatch(pattern, address_text) is None:
        raise ValueError(f"malformed digit designation: address is not {label}")
    return int(address_text, radix)
