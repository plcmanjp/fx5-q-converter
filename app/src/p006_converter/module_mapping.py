"""Explicit module-number projection; buffer meaning is never inferred."""
from __future__ import annotations

import re
from collections import defaultdict

LIMITS = {"FX5U": 0x10, "FX5UC": 0x10, "FX5UJ": 0x8, "FX5S": 0}
_UNIT = re.compile(r"U([0-9A-F]+)\Z", re.I)
_PREFIX = re.compile(r"@?(U[0-9A-F]+)\\G", re.I)
_DEVICE = re.compile(r"(U[0-9A-F]+)(\\G[0-9]+(?:\.[0-9A-F])?(?:(?:ZZ|LZ|Z)[0-9]+)?)\Z", re.I)


def unit_number(unit: str) -> int:
    match = _UNIT.fullmatch(unit) if isinstance(unit, str) else None
    if not match:
        raise ValueError("모듈 번호는 U와 16진수로 입력하세요")
    return int(match[1], 16)


def source_unit(token: str) -> str | None:
    match = _PREFIX.match(token)
    return f"U{unit_number(match[1]):X}" if match else None


def validate_rules(rules: tuple[tuple[str, str], ...]) -> None:
    if not isinstance(rules, tuple) or len(rules) > 256:
        raise ValueError("모듈 매핑은 최대 256개의 불변 규칙이어야 합니다")
    seen = set()
    for rule in rules:
        if not isinstance(rule, tuple) or len(rule) != 2:
            raise ValueError("모듈 매핑에는 원본과 대상 번호가 필요합니다")
        src, dst = map(unit_number, rule)
        if not (0 <= src <= 0x1FF or 0x3E0 <= src <= 0x3FF):
            raise ValueError("Q 원본 모듈 번호 범위를 벗어났습니다")
        if not 1 <= dst <= 0x10:
            raise ValueError("FX5 대상 모듈 번호는 U1부터 U10까지입니다")
        if src in seen:
            raise ValueError(f"원본 모듈 중복: U{src:X}")
        seen.add(src)


class ModuleMapper:
    def __init__(self, cpu: str, rules: tuple[tuple[str, str], ...] = ()):
        if cpu not in LIMITS:
            raise ValueError("지원하지 않는 FX5 CPU입니다")
        validate_rules(rules)
        self.cpu = cpu
        self.rules = {unit_number(src): unit_number(dst) for src, dst in rules}
        if any(dst > LIMITS[cpu] for dst in self.rules.values()):
            raise ValueError(f"{cpu}에서 지원하지 않는 대상 모듈 번호입니다")

    def target_unit(self, unit: str) -> str:
        number = unit_number(unit)
        target = self.rules.get(number, 1 if number == 0 and LIMITS[self.cpu] else number)
        return f"U{target:X}"

    def project(self, token: str) -> str:
        match = _DEVICE.fullmatch(token)
        if not match:
            return token  # Unknown syntax stays visible to the existing blocker.
        target = self.target_unit(match[1])
        return token if unit_number(target) == unit_number(match[1]) else target + match[2]

    def rows(self, units) -> list[dict]:
        default = ModuleMapper(self.cpu)
        return [{"source": unit, "initial": default.target_unit(unit),
                 "target": self.target_unit(unit),
                 "reason": ("사용자 지정" if unit_number(unit) in self.rules else
                            "모듈 접근 미지원" if not LIMITS[self.cpu] else
                            "FX5 최소 번호 적용" if unit_number(unit) == 0 else "원본 번호 유지")}
                for unit in sorted(set(units), key=unit_number)]

    def preflight(self, tokens) -> list[dict]:
        units = {unit for token in tokens if (unit := source_unit(token)) is not None}
        targets = defaultdict(list)
        for row in self.rows(units):
            targets[row["target"]].append(row["source"])
        conflicts = [f"{', '.join(sources)} → {target}" for target, sources in targets.items() if len(sources) > 1]
        if conflicts:
            raise ValueError("모듈 매핑 충돌: " + "; ".join(conflicts) + ". 대상 번호를 구분하세요")
        return self.rows(units)
