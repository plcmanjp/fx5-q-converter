"""Explicit, source-bound D/T/ZR relocation for offline Q-to-FX5 candidates.

No automatic allocation and no runtime approval. D remains D. Comment holds
preserve source records while keeping them out of the target import candidates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

TOKEN = re.compile(r'(ZR|D|T|R)([0-9]+)(\.[0-9A-F])?((?:ZZ|LZ|Z)[0-9]+)?\Z', re.I)
PAIRS = {('D', 'D'), ('T', 'T'), ('ZR', 'R')}
CEILINGS = {'D': 7999, 'T': 1023, 'R': 32767}
MAX_POINTS = 65536


def device_token(token: str):
    match = TOKEN.fullmatch(token)
    if not match and not token.startswith('"') and re.match(r'(?:@|K[1-8])?(?:ZR|D|T|R)[0-9]', token, re.I):
        raise ValueError('unsupported relocation device syntax: ' + token)
    return match


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate relocation JSON key: ' + key)
        result[key] = value
    return result


@dataclass(frozen=True)
class RelocationRule:
    source_family: str
    source_start: int
    target_family: str
    target_start: int
    count: int


@dataclass(frozen=True)
class DeviceRelocationSettings:
    source_sha256: str
    rules: tuple[RelocationRule, ...]
    held_comments: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', self.source_sha256):
            raise ValueError('relocation requires an exact source SHA-256')
        if type(self.rules) is not tuple or not self.rules or len(self.rules) > 4096:
            raise ValueError('invalid relocation rule count/type')
        if type(self.held_comments) is not tuple or len(self.held_comments) > MAX_POINTS:
            raise ValueError('invalid held comment list')
        source_points, target_points = set(), set()
        total = 0
        for rule in self.rules:
            if (not isinstance(rule, RelocationRule)
                    or not isinstance(rule.source_family, str) or not isinstance(rule.target_family, str)
                    or (rule.source_family, rule.target_family) not in PAIRS):
                raise ValueError('only D-to-D, T-to-T and ZR-to-R rules are supported')
            if any(type(n) is not int for n in (rule.source_start, rule.target_start, rule.count)):
                raise ValueError('relocation addresses/count must be integers')
            total += rule.count
            if (rule.source_start < 0 or rule.target_start < 0 or rule.count < 1 or total > MAX_POINTS
                    or rule.source_start + rule.count > 2**31
                    or rule.target_start + rule.count - 1 > CEILINGS[rule.target_family]):
                raise ValueError('relocation range exceeds supported bounds')
            for k in range(rule.count):
                source, target = (rule.source_family, rule.source_start+k), (rule.target_family, rule.target_start+k)
                if source in source_points or target in target_points:
                    raise ValueError('overlapping relocation source or target ranges')
                source_points.add(source)
                target_points.add(target)
        if any(not isinstance(token, str) for token in self.held_comments):
            raise ValueError('held comments must be device strings')
        if len(set(self.held_comments)) != len(self.held_comments):
            raise ValueError('duplicate held comment')
        for token in self.held_comments:
            m = TOKEN.fullmatch(token) if isinstance(token, str) else None
            if (not m or m[3] or m[4] or token != f'{m[1].upper()}{int(m[2])}'
                    or self.project(token) != token):
                raise ValueError('held comment must be a static, unrelocated source device')

    @classmethod
    def from_json(cls, payload: bytes):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= 1024*1024 or payload.startswith(b'\xef\xbb\xbf'):
            raise ValueError('invalid relocation settings bytes')
        try:
            data = json.loads(payload.decode('utf-8'), object_pairs_hook=unique_pairs)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError('invalid relocation settings JSON') from error
        if not isinstance(data, dict) or set(data) != {'schema_version', 'source_sha256', 'rules', 'held_comments'}:
            raise ValueError('unexpected relocation fields')
        if type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError('unsupported relocation schema')
        if not isinstance(data['rules'], list) or not isinstance(data['held_comments'], list):
            raise ValueError('relocation rules and holds must be arrays')
        keys = {'source_family', 'source_start', 'target_family', 'target_start', 'count'}
        if any(not isinstance(r, dict) or set(r) != keys for r in data['rules']):
            raise ValueError('unexpected relocation rule fields')
        return cls(data['source_sha256'], tuple(RelocationRule(**r) for r in data['rules']), tuple(data['held_comments']))

    def to_json(self):
        data = dict(schema_version=1, source_sha256=self.source_sha256,
                    rules=[vars(r) for r in self.rules], held_comments=list(self.held_comments))
        return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'\n').encode('utf-8')

    @property
    def digest(self):
        return hashlib.sha256(self.to_json()).hexdigest()

    def point(self, family: str, n: int):
        for rule in self.rules:
            if rule.source_family == family and rule.source_start <= n < rule.source_start+rule.count:
                return rule.target_family, rule.target_start+n-rule.source_start
        return family, n

    def project(self, token: str):
        if not (m := device_token(token)):
            return token
        family, n = self.point(m[1].upper(), int(m[2]))
        return f'{family}{n}' + (m[3] or '') + (m[4] or '')

    def preflight(self, document: dict[str, Any], source_sha256: str):
        if source_sha256 != self.source_sha256:
            raise ValueError('relocation source SHA mismatch')
        owners = {}
        unknown_spans, indexed = [], []
        changed = 0
        held = set(self.held_comments)
        actual_comments = {c['device'] for c in document['comments']}
        if held - actual_comments:
            raise ValueError('held comment absent from source')

        def reserve(family, n):
            target = self.point(family, n)
            old = owners.setdefault(target, (family, n))
            if old != (family, n):
                raise ValueError(f'relocation aliases source {family}{n} with {old} at {target}')

        for rule in self.rules:
            for k in range(rule.count):
                reserve(rule.source_family, rule.source_start+k)
        for program in document['programs']:
            for record in program['records']:
                for i, token in enumerate(record['operands']):
                    m = device_token(token)
                    if not m:
                        continue
                    family = m[1].upper()
                    base = f'{family}{int(m[2])}'
                    if token in held or base in held:
                        raise ValueError('program device cannot be held as comment-only: ' + token)
                    width = operand_width(record['opcode'], record['operands'], i, bool(m[3]))
                    if width is None:
                        if self.project(token) != token:
                            raise ValueError('unknown access width for relocated operand: ' + token)
                        unknown_spans.append(dict(pou=program['name'], locator=record['locator'], operand=token))
                        width = 1
                    if m[4]:
                        indexed.append(dict(pou=program['name'], locator=record['locator'], operand=token))
                    first = self.point(family, int(m[2]))
                    for k in range(width):
                        n = int(m[2])+k
                        if f'{family}{n}' in held:
                            raise ValueError('program span intersects held comment')
                        if self.point(family, n) != (first[0], first[1]+k):
                            raise ValueError('relocation splits a continuous instruction access span')
                        reserve(family, n)
                    changed += self.project(token) != token
        for comment in document['comments']:
            if comment['device'] in held:
                continue
            if m := device_token(comment['device']):
                reserve(m[1].upper(), int(m[2]))
        return dict(state='APPLIED_FOR_OFFLINE_TEST', settings_sha256=self.digest,
                    changed_program_operands=changed, held_comment_count=len(held),
                    rules=len(self.rules), occupied_target_points=len(owners),
                    unresolved_access_spans=unknown_spans, dynamic_index_accesses=indexed,
                    semantic_acceptance='NOT_VERIFIED', q_r_zr_bank_and_retention='NOT_VERIFIED',
                    hardware_allocation='NOT_VERIFIED', native_lifecycle='NOT_RUN')


def operand_width(op: str, operands: list[str], i: int, bit: bool = False):
    """Known static word footprints; None is explicitly unverified, never one."""
    if bit:
        return 1
    if op in {'LD', 'LDI', 'LDP', 'LDF', 'LDPI', 'LDFI', 'AND', 'ANI', 'ANDP',
              'ANDF', 'ANDPI', 'ANDFI', 'OR', 'ORI', 'ORP', 'ORF', 'ORPI', 'ORFI'}:
        return 1
    if op in {'DATERD', 'DATERDP', 'DATEWR', 'DATEWRP', 'TRD', 'TRDP', 'TWR', 'TWRP'}:
        return 7
    if op in {'BMOV', 'BMOVP', 'FMOV', 'FMOVP', 'WSUM', 'WSUMP'}:
        if len(operands) != 3:
            raise ValueError('invalid block instruction arity')
        if i == 2 or (op.startswith('FMOV') and i == 0):
            return 1
        if op.startswith('WSUM') and i == 1:
            return 2
        count = re.fullmatch(r'K([0-9]+)', operands[2])
        if not count:
            return None
        n = int(count[1])
        if not 1 <= n <= MAX_POINTS:
            raise ValueError('block span exceeds inspection bound')
        return n
    if op in {'FROM', 'FROMP'} and i == 2 and len(operands) == 4:
        count = re.fullmatch(r'K([0-9]+)', operands[3])
        return int(count[1]) if count and 1 <= int(count[1]) <= MAX_POINTS else None
    if op in {'D*', 'D*P', 'D/', 'D/P'}:
        return 4 if i == len(operands)-1 else 2
    if op in {'*', '*P', '/', '/P'}:
        return 2 if i == len(operands)-1 else 1
    if op in {'DMOV', 'DMOVP', 'D+', 'D+P', 'D-', 'D-P', 'DINC', 'DINCP'} or re.fullmatch(r'(LD|AND|OR)D(=|<>|<=|>=|<|>)', op):
        return 2
    if op in {'MOV', 'MOVP', '+', '+P', '-', '-P', 'INC', 'INCP', 'RST', 'OUT', 'OUTH', 'OUTHS', 'FOR'} or re.fullmatch(r'(LD|AND|OR)(=|<>|<=|>=|<|>)', op):
        return 1
    return None
